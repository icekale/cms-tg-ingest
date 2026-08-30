import json
import os
import sqlite3
import socket
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.clients.p115 import P115RiskControlError
from app.models import TaskStage, TaskStatus
from app.task_actions import available_task_actions
from app.task_runner import StageResult, TaskRunner
from app.task_store import TaskStore
from app.web import WebApp


class FakeWorkflow:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run_stage(self, task):
        self.calls.append((task.id, task.current_stage))
        if not self.results:
            raise AssertionError("unexpected stage call")
        return self.results.pop(0)


class InspectingWorkflow(FakeWorkflow):
    def __init__(self, results):
        super().__init__(results)
        self.seen_tasks = []

    def run_stage(self, task):
        self.seen_tasks.append(task)
        return super().run_stage(task)


class CountingP115:
    def __init__(self, request_count=0):
        self.request_count = request_count


class CountingWorkflow:
    def __init__(self, p115, result, increment=0):
        self.p115 = p115
        self.result = result
        self.increment = increment

    def run_stage(self, task):
        self.p115.request_count += self.increment
        return self.result


class RiskCountingWorkflow:
    def __init__(self, p115, increment=0):
        self.p115 = p115
        self.increment = increment

    def run_stage(self, task):
        self.p115.request_count += self.increment
        raise P115RiskControlError("操作过于频繁，请稍后再试")


class ExplodingCountingWorkflow:
    def __init__(self, p115, increment=0):
        self.p115 = p115
        self.increment = increment

    def run_stage(self, task):
        self.p115.request_count += self.increment
        raise RuntimeError("boom")


class TimeAdvancingCountingWorkflow:
    def __init__(self, p115, clock, results):
        self.p115 = p115
        self.clock = clock
        self.results = list(results)

    def run_stage(self, task):
        result, p115_increment, elapsed_seconds = self.results.pop(0)
        self.p115.request_count += p115_increment
        self.clock[0] += elapsed_seconds
        return result


class TerminateBeforeResultStore(TaskStore):
    def __init__(self, db_path, event_status=None):
        super().__init__(db_path)
        self.event_status = event_status
        self.requested = False

    def _request_termination(self, task_id):
        if not self.requested:
            self.requested = True
            self.request_task_termination(task_id, "Web", now=3)

    def complete_claimed_stage(self, task_id, **kwargs):
        self._request_termination(task_id)
        return super().complete_claimed_stage(task_id, **kwargs)

    def record_event(self, task_id, stage, status, message, **kwargs):
        if kwargs.get("expected_claimed_by") and status == self.event_status:
            self._request_termination(task_id)
        return super().record_event(task_id, stage, status, message, **kwargs)


class TaskRunnerTests(unittest.TestCase):
    def test_result_persistence_failure_releases_claim_for_immediate_retry(self):
        class FailFirstCompletionStore(TaskStore):
            def __init__(self, db_path):
                super().__init__(db_path)
                self.failed = False

            def complete_claimed_stage(self, task_id, **kwargs):
                if not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError("database is temporarily locked")
                return super().complete_claimed_stage(task_id, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            store = FailFirstCompletionStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("persist-retry", "", "https://115cdn.com/s/persist-retry")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            workflow = FakeWorkflow(
                [
                    StageResult.complete("first result"),
                    StageResult.complete("retried result"),
                ]
            )
            runner = TaskRunner(store, workflow, worker_id="worker", now=lambda: 2)

            with self.assertRaises(sqlite3.OperationalError):
                runner.run_once()

            released = store.find_task(task.id)
            self.assertEqual(released.claimed_by, "")
            self.assertTrue(runner.run_once())
            completed = store.find_task(task.id)
            self.assertEqual(completed.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(completed.claimed_by, "")

    def test_result_persistence_keeps_claim_active_for_heartbeat(self):
        class InspectCompletionStore(TaskStore):
            runner = None
            claim_active_during_completion = False

            def complete_claimed_stage(self, task_id, **kwargs):
                self.claim_active_during_completion = self.runner._active_claim is not None
                return super().complete_claimed_stage(task_id, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            store = InspectCompletionStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("persist-heartbeat", "", "https://115cdn.com/s/persist-heartbeat")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.complete("completed")]),
                worker_id="worker",
                now=lambda: 2,
            )
            store.runner = runner

            self.assertTrue(runner.run_once())

            self.assertTrue(store.claim_active_during_completion)
            self.assertIsNone(runner._active_claim)

    def test_failure_event_persistence_error_releases_claim_for_immediate_retry(self):
        class FailFirstFailureEventStore(TaskStore):
            def __init__(self, db_path):
                super().__init__(db_path)
                self.failed = False

            def record_event(self, task_id, stage, status, message, **kwargs):
                if kwargs.get("expected_claimed_by") and status == TaskStatus.FAILED and not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError("database is temporarily locked")
                return super().record_event(task_id, stage, status, message, **kwargs)

        class FailThenCompleteWorkflow:
            def __init__(self):
                self.calls = 0

            def run_stage(self, task):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("stage failed")
                return StageResult.complete("retried result")

        with tempfile.TemporaryDirectory() as tmp:
            store = FailFirstFailureEventStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("failure-event-retry", "", "https://115cdn.com/s/failure-event-retry")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            runner = TaskRunner(store, FailThenCompleteWorkflow(), worker_id="worker", now=lambda: 2)

            with self.assertRaises(sqlite3.OperationalError):
                runner.run_once()

            released = store.find_task(task.id)
            self.assertEqual(released.claimed_by, "")
            self.assertTrue(runner.run_once())
            self.assertEqual(store.find_task(task.id).current_stage, TaskStage.RECOGNIZING)

    def test_termination_after_claim_skips_workflow(self):
        class TerminateAfterClaimStore(TaskStore):
            def claim_next_runnable(self, worker_id, now=None, stale_after_seconds=21600):
                claimed = super().claim_next_runnable(worker_id, now, stale_after_seconds)
                if claimed is not None:
                    self.request_task_termination(claimed.id, "Web", now=2)
                return claimed

        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateAfterClaimStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("before-stage", "", "https://115cdn.com/s/before-stage")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            workflow = FakeWorkflow([])
            runner = TaskRunner(store, workflow, worker_id="worker", now=lambda: 2)

            self.assertTrue(runner.run_once())
            self.assertEqual(workflow.calls, [])
            self.assertEqual(store.find_task(task.id).status, TaskStatus.CANCELLED)

    def test_termination_during_stage_discards_success_result(self):
        class TerminatingWorkflow:
            def __init__(self, store):
                self.store = store

            def run_stage(self, task):
                self.store.request_task_termination(task.id, "Web", now=3)
                return StageResult.complete("must not advance")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("during-stage", "", "https://115cdn.com/s/during-stage")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            runner = TaskRunner(store, TerminatingWorkflow(store), worker_id="worker", now=lambda: 3)

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.current_stage, TaskStage.ORGANIZING)

    def test_termination_during_stage_exception_records_error_as_cancelled(self):
        class TerminatingFailure:
            def __init__(self, store):
                self.store = store

            def run_stage(self, task):
                self.store.request_task_termination(task.id, "Web", now=3)
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cancel-error", "", "https://115cdn.com/s/cancel-error")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            runner = TaskRunner(store, TerminatingFailure(store), worker_id="worker", now=lambda: 3)

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.error_type, "stage_exception")
            self.assertEqual(current.error_summary, "boom")

    def test_termination_during_lock_wait_is_finalized_after_claim_release(self):
        class TerminateDuringLockStore(TaskStore):
            def claim_task_lock(self, task_id, *args, **kwargs):
                self.request_task_termination(task_id, "Web", now=2)
                return super().claim_task_lock(task_id, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateDuringLockStore(Path(tmp) / "tasks.db")
            holder = store.upsert_task("lock-holder", "", "https://115cdn.com/s/lock-holder")
            store.enqueue_task(holder.id, TaskStage.ORGANIZING, next_run_at=1)
            store.claim_next_runnable("holder-worker", now=1)
            waiter = store.upsert_task("lock-waiter", "", "https://115cdn.com/s/lock-waiter")
            store.enqueue_task(waiter.id, TaskStage.ORGANIZING, next_run_at=1)
            workflow = FakeWorkflow([])
            runner = TaskRunner(store, workflow, worker_id="waiter-worker", now=lambda: 2)

            self.assertTrue(runner.run_once())
            current = store.find_task(waiter.id)
            self.assertEqual(workflow.calls, [])
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.claimed_by, "")

    def test_termination_after_final_check_cancels_complete_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateBeforeResultStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("complete-race", "", "https://115cdn.com/s/complete-race")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            runner = TaskRunner(store, FakeWorkflow([StageResult.complete("must not advance")]), worker_id="worker", now=lambda: 3)

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)

            self.assertTrue(store.requested)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(current.claimed_by, "")

    def test_termination_after_final_check_cancels_defer_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateBeforeResultStore(Path(tmp) / "tasks.db", event_status=TaskStatus.RUNNING)
            task = store.upsert_task("defer-race", "", "https://115cdn.com/s/defer-race")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            runner = TaskRunner(store, FakeWorkflow([StageResult.defer("wait", delay_seconds=30)]), worker_id="worker", now=lambda: 3)

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)

            self.assertTrue(store.requested)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.next_run_at, -1)
            self.assertEqual(current.claimed_by, "")

    def test_termination_after_final_check_cancels_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateBeforeResultStore(Path(tmp) / "tasks.db", event_status=TaskStatus.FAILED)
            task = store.upsert_task("failed-race", "", "https://115cdn.com/s/failed-race")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.failed("stage failed", error_type="strm_missing")]),
                worker_id="worker",
                now=lambda: 3,
            )

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)

            self.assertTrue(store.requested)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.error_type, "strm_missing")
            self.assertEqual(current.error_summary, "stage failed")

    def test_termination_after_final_check_cancels_needs_action_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateBeforeResultStore(Path(tmp) / "tasks.db", event_status=TaskStatus.NEEDS_ACTION)
            task = store.upsert_task("needs-action-race", "", "https://115cdn.com/s/needs-action-race")
            store.enqueue_task(task.id, TaskStage.RECOGNIZING, next_run_at=1)
            runner = TaskRunner(store, FakeWorkflow([StageResult.needs_action("choose")]), worker_id="worker", now=lambda: 3)

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)

            self.assertTrue(store.requested)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.error_type, "needs_action")
            self.assertEqual(current.error_summary, "choose")

    def test_termination_after_final_check_cancels_stage_exception(self):
        class ExplodingWorkflow:
            def run_stage(self, _task):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateBeforeResultStore(Path(tmp) / "tasks.db", event_status=TaskStatus.FAILED)
            task = store.upsert_task("exception-race", "", "https://115cdn.com/s/exception-race")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1)
            runner = TaskRunner(store, ExplodingWorkflow(), worker_id="worker", now=lambda: 3)

            with self.assertLogs("app.task_runner", level="ERROR"):
                self.assertTrue(runner.run_once())
            current = store.find_task(task.id)

            self.assertTrue(store.requested)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.error_type, "stage_exception")
            self.assertEqual(current.error_summary, "boom")

    def test_termination_after_final_check_cancels_p115_risk_result(self):
        class RiskWorkflow:
            def run_stage(self, _task):
                raise P115RiskControlError("too fast")

        with tempfile.TemporaryDirectory() as tmp:
            store = TerminateBeforeResultStore(Path(tmp) / "tasks.db", event_status=TaskStatus.NEEDS_ACTION)
            task = store.upsert_task("risk-race", "", "https://115cdn.com/s/risk-race")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            runner = TaskRunner(store, RiskWorkflow(), worker_id="worker", now=lambda: 3, risk_cooldown_seconds=60)

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)
            cooldown = store.get_runtime_state("115:risk_cooldown_until")

            self.assertTrue(store.requested)
            self.assertEqual(current.status, TaskStatus.CANCELLED)
            self.assertEqual(current.error_type, "p115_risk_control")
            self.assertIn("115 风控", current.error_summary)
            self.assertEqual(float(cooldown["value"]), 63.0)

    def test_default_worker_ids_are_unique_and_process_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            first = TaskRunner(store, FakeWorkflow([]))
            second = TaskRunner(store, FakeWorkflow([]))

            prefix = f"{socket.gethostname()}:{os.getpid()}:"
            self.assertTrue(first.worker_id.startswith(prefix))
            self.assertTrue(second.worker_id.startswith(prefix))
            self.assertEqual(len(first.worker_id.removeprefix(prefix)), 12)
            self.assertEqual(len(second.worker_id.removeprefix(prefix)), 12)
            self.assertNotEqual(first.worker_id, second.worker_id)

    def test_heartbeat_does_not_overwrite_concurrent_runner_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_store = TaskStore(Path(tmp) / "tasks.db")
            real_store.set_runtime_state("task_runner", "running", updated_at=1.0)

            class ErrorInterleavingStore:
                def __init__(self, delegate):
                    self.delegate = delegate

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def get_runtime_state(self, key):
                    state = self.delegate.get_runtime_state(key)
                    if key == "task_runner":
                        self.delegate.set_runtime_state("task_runner", "error", updated_at=99.0)
                    return state

                def refresh_runtime_state_timestamp(self, key, updated_at=None):
                    self.delegate.set_runtime_state("task_runner", "error", updated_at=99.0)
                    return self.delegate.refresh_runtime_state_timestamp(key, updated_at=updated_at)

            runner = TaskRunner(ErrorInterleavingStore(real_store), FakeWorkflow([]), now=lambda: 100.0)

            runner._record_heartbeat()

            state = real_store.get_runtime_state("task_runner")
            self.assertEqual(state["value"], "error")
            self.assertEqual(state["updated_at"], 100.0)

    def test_run_forever_survives_store_error_and_reports_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_store = TaskStore(Path(tmp) / "tasks.db")

            class FailOnceClaimStore:
                def __init__(self, delegate):
                    self.delegate = delegate
                    self.calls = 0
                    self.second_claim = threading.Event()

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def claim_next_runnable(self, worker_id, now=None, stale_after_seconds=21600):
                    self.calls += 1
                    if self.calls == 1:
                        raise sqlite3.OperationalError("database is temporarily locked")
                    self.second_claim.set()
                    return None

            store = FailOnceClaimStore(real_store)
            runner = TaskRunner(store, FakeWorkflow([]), interval_seconds=0.01)
            thread = runner.start()
            try:
                self.assertTrue(store.second_claim.wait(timeout=1))
                self.assertTrue(thread.is_alive())
                state = real_store.get_runtime_state("task_runner")
                self.assertEqual(state["value"], "running")
            finally:
                runner.stop()

    def test_heartbeat_refreshes_while_worker_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            runner = TaskRunner(store, FakeWorkflow([]), interval_seconds=60)

            with patch("app.task_runner._HEARTBEAT_INTERVAL_SECONDS", 0.01):
                runner.start()
                try:
                    deadline = time.time() + 1
                    first = None
                    while time.time() < deadline:
                        first = store.get_runtime_state("task_runner")
                        if first is not None:
                            break
                        time.sleep(0.005)
                    self.assertIsNotNone(first)

                    first_updated_at = first["updated_at"]
                    refreshed = None
                    while time.time() < deadline:
                        refreshed = store.get_runtime_state("task_runner")
                        if refreshed["updated_at"] > first_updated_at:
                            break
                        time.sleep(0.005)
                    self.assertGreater(refreshed["updated_at"], first_updated_at)
                finally:
                    runner.stop(join_timeout=1)

    def test_heartbeat_renews_active_claim_without_changing_claim_version(self):
        class BlockingWorkflow:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def run_stage(self, _task):
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("workflow was not released")
                return StageResult.complete("done")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("active-heartbeat", "", "https://115cdn.com/s/active-heartbeat")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            workflow = BlockingWorkflow()
            runner = TaskRunner(store, workflow, interval_seconds=60)

            with patch("app.task_runner._HEARTBEAT_INTERVAL_SECONDS", 0.01):
                runner.start()
                try:
                    self.assertTrue(workflow.entered.wait(timeout=1))
                    claimed = store.find_task(task.id)
                    deadline = time.time() + 1
                    renewed = claimed
                    while time.time() < deadline:
                        renewed = store.find_task(task.id)
                        if renewed.claim_heartbeat_at > claimed.claim_heartbeat_at:
                            break
                        time.sleep(0.005)

                    self.assertGreater(renewed.claim_heartbeat_at, claimed.claim_heartbeat_at)
                    self.assertEqual(renewed.claimed_at, claimed.claimed_at)
                    self.assertEqual(renewed.claim_token, claimed.claim_token)
                    self.assertEqual(renewed.updated_at, claimed.updated_at)
                finally:
                    runner.stop(join_timeout=0)
                    workflow.release.set()
                    runner.stop(join_timeout=1)

    def test_claim_renewal_failure_flags_task_manual_required(self):
        class FailingRenewStore:
            def __init__(self, delegate):
                self.delegate = delegate

            def __getattr__(self, name):
                return getattr(self.delegate, name)

            def renew_claim(self, *args, **kwargs):
                return None  # renewal keeps failing

        class BlockingWorkflow:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def run_stage(self, _task):
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("workflow was not released")
                return StageResult.complete("done")

        with tempfile.TemporaryDirectory() as tmp:
            real_store = TaskStore(Path(tmp) / "tasks.db")
            task = real_store.upsert_task("renew-fail", "", "https://115cdn.com/s/renew-fail")
            real_store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            store = FailingRenewStore(real_store)
            workflow = BlockingWorkflow()
            runner = TaskRunner(store, workflow, interval_seconds=60)

            with patch("app.task_runner._HEARTBEAT_INTERVAL_SECONDS", 0.01):
                runner.start()
                try:
                    self.assertTrue(workflow.entered.wait(timeout=1))
                    deadline = time.time() + 1
                    flagged = False
                    while time.time() < deadline:
                        refreshed = real_store.find_task(task.id)
                        if refreshed.status == TaskStatus.NEEDS_ACTION and not refreshed.claimed_by.strip():
                            flagged = True
                            break
                        time.sleep(0.01)
                    self.assertTrue(flagged, "task was not flagged NEEDS_ACTION after renewal failure")
                    flagged_task = real_store.find_task(task.id)
                    self.assertEqual(
                        str(flagged_task.metadata.get("claim_renewal_failed_at") or ""),
                        str(flagged_task.metadata.get("claim_renewal_failed_at") or ""),
                    )
                    self.assertGreater(float(flagged_task.metadata.get("claim_renewal_failed_at") or 0), 0)
                finally:
                    runner.stop(join_timeout=0)
                    workflow.release.set()
                    runner.stop(join_timeout=1)

    def test_flag_claim_lost_ignores_foreign_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("foreign-claim", "", "https://115cdn.com/s/foreign-claim")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=time.time())
            self.assertIsNotNone(claimed)

            flagged = store.flag_claim_lost(
                task.id,
                "worker-b",
                "wrong-token",
                now=time.time(),
            )

            self.assertIsNone(flagged)
            refreshed = store.find_task(task.id)
            self.assertEqual(refreshed.status, TaskStatus.RUNNING)
            self.assertEqual(refreshed.claimed_by, "worker-a")

    def test_activity_state_tracks_active_claim_and_idle(self):
        class BlockingWorkflow:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def run_stage(self, _task):
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("workflow was not released")
                return StageResult.complete("done")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("activity-state", "", "https://115cdn.com/s/activity-state")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            workflow = BlockingWorkflow()
            runner = TaskRunner(store, workflow, interval_seconds=60)

            with patch("app.task_runner._HEARTBEAT_INTERVAL_SECONDS", 0.01):
                runner.start()
                try:
                    self.assertTrue(workflow.entered.wait(timeout=1))
                    claimed = store.find_task(task.id)
                    self.assertTrue(claimed.claimed_by)

                    # Fresh runner with no claim yet -> idle snapshot.
                    idle_runner = TaskRunner(store, workflow, interval_seconds=60)
                    idle_runner._record_activity(now=100.0)
                    idle_state = store.get_runtime_state("task_runner:activity")
                    self.assertIn("active_task_id", idle_state["value"])
                    self.assertIn('"active_task_id":0', idle_state["value"])

                    # Active claim -> snapshot reflects the running task.
                    runner._record_activity(now=101.0)
                    state = store.get_runtime_state("task_runner:activity")
                    payload = json.loads(state["value"])
                    self.assertEqual(payload["active_task_id"], task.id)
                    self.assertEqual(payload["active_stage"], "organizing")
                    self.assertGreater(payload["active_since"], 0)
                finally:
                    runner.stop(join_timeout=0)
                    workflow.release.set()
                    runner.stop(join_timeout=1)

    def test_heartbeat_retries_claim_renewal_after_transient_store_error(self):
        class TransientRenewStore:
            def __init__(self, delegate):
                self.delegate = delegate
                self.renew_calls = 0
                self.retried = threading.Event()

            def __getattr__(self, name):
                return getattr(self.delegate, name)

            def renew_claim(self, *args, **kwargs):
                self.renew_calls += 1
                if self.renew_calls == 1:
                    raise sqlite3.OperationalError("database is temporarily locked")
                self.retried.set()
                return self.delegate.renew_claim(*args, **kwargs)

        class BlockingWorkflow:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def run_stage(self, _task):
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("workflow was not released")
                return StageResult.complete("done")

        with tempfile.TemporaryDirectory() as tmp:
            real_store = TaskStore(Path(tmp) / "tasks.db")
            task = real_store.upsert_task("retry-heartbeat", "", "https://115cdn.com/s/retry-heartbeat")
            real_store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            store = TransientRenewStore(real_store)
            workflow = BlockingWorkflow()
            runner = TaskRunner(store, workflow, interval_seconds=60)

            with patch("app.task_runner._HEARTBEAT_INTERVAL_SECONDS", 0.01):
                runner.start()
                try:
                    self.assertTrue(workflow.entered.wait(timeout=1))
                    self.assertTrue(store.retried.wait(timeout=1))
                    self.assertGreaterEqual(store.renew_calls, 2)
                finally:
                    runner.stop(join_timeout=0)
                    workflow.release.set()
                    runner.stop(join_timeout=1)

    def test_start_records_task_runner_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            runner = TaskRunner(store, FakeWorkflow([]), interval_seconds=60)

            runner.start()
            time.sleep(0.01)
            runner.stop(join_timeout=1)

            heartbeat = store.get_runtime_state("task_runner")
            self.assertEqual(heartbeat["value"], "stopped")
            self.assertGreater(heartbeat["updated_at"], 0)

    def test_stop_wakes_and_joins_idle_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            runner = TaskRunner(store, FakeWorkflow([]), interval_seconds=60)

            thread = runner.start()
            time.sleep(0.01)
            runner.stop(join_timeout=1)

            self.assertFalse(thread.is_alive())

    def test_run_once_completes_stage_and_enqueues_next_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=1.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.complete("已接收")]), worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)
            events = store.list_events(task.id)

            self.assertEqual(updated.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(events[-2]["stage"], "received")
            self.assertEqual(events[-2]["status"], "succeeded")
            self.assertEqual(events[-1]["stage"], "organizing")
            self.assertEqual(events[-1]["status"], "pending")

    def test_run_once_commits_when_workflow_reupserts_its_active_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("same", "1212", "https://115cdn.com/s/same?password=1212")
            store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=1.0)

            class ReupsertingWorkflow:
                def __init__(self):
                    self.calls = []

                def run_stage(self, claimed_task):
                    self.calls.append(claimed_task.id)
                    store.upsert_task(
                        "same",
                        "1212",
                        "https://115cdn.com/s/same?password=1212",
                        chat_id="464100862",
                    )
                    return StageResult.complete("已接收")

            workflow = ReupsertingWorkflow()
            runner = TaskRunner(store, workflow, worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())

            updated = store.find_task(task.id)
            self.assertEqual(workflow.calls, [task.id])
            self.assertEqual(updated.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(updated.status, TaskStatus.PENDING)

    def test_direct_task_runner_skips_shared_stages_and_stops_at_emby_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("direct", "", "https://115cdn.com/s/direct", strm_mode="direct")
            store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=1.0)
            workflow = FakeWorkflow(
                [
                    StageResult.complete("received"),
                    StageResult.complete("organized"),
                    StageResult.complete("recognized", {"direct_strm": True}),
                    StageResult.complete("strm ready", {"direct_strm": True, "strm_mode_locked": True}),
                    StageResult.complete("moved"),
                    StageResult.complete("emby confirmed"),
                ]
            )
            runner = TaskRunner(store, workflow, worker_id="worker-1", now=lambda: 1.0)

            for _ in range(6):
                self.assertTrue(runner.run_once())

            updated = store.find_task(task.id)
            self.assertEqual(
                [stage for _task_id, stage in workflow.calls],
                [
                    TaskStage.RECEIVED,
                    TaskStage.ORGANIZING,
                    TaskStage.RECOGNIZING,
                    TaskStage.STRM_READY,
                    TaskStage.MOVED,
                    TaskStage.EMBY_CONFIRMED,
                ],
            )
            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(updated.status, TaskStatus.SUCCEEDED)
            self.assertTrue(updated.metadata["direct_strm"])
            self.assertTrue(updated.metadata["strm_mode_locked"])

    def test_run_once_defers_stage_with_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=5.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.defer("等待 CMS 整理", delay_seconds=30)]), worker_id="worker-1", now=lambda: 5.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.next_run_at, 35.0)
            self.assertEqual(updated.claimed_by, "")

    def test_run_once_records_p115_stage_and_total_request_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=5.0)
            p115 = CountingP115(request_count=10)
            runner = TaskRunner(
                store,
                CountingWorkflow(p115, StageResult.defer("等待 CMS 整理", delay_seconds=30), increment=3),
                worker_id="worker-1",
                now=lambda: 5.0,
                p115_client=p115,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.metadata["p115_stage_request_count"], 3)
            self.assertEqual(updated.metadata["p115_total_request_count"], 3)
            self.assertEqual(updated.metadata["p115_request_count_snapshot"], 13)

    def test_run_once_accumulates_p115_request_counts_across_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待 CMS 整理",
                metadata_patch={"p115_total_request_count": 4, "p115_request_count_snapshot": 10},
            )
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=5.0)
            p115 = CountingP115(request_count=10)
            runner = TaskRunner(
                store,
                CountingWorkflow(p115, StageResult.complete("已找到"), increment=2),
                worker_id="worker-1",
                now=lambda: 5.0,
                p115_client=p115,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.metadata["p115_stage_request_count"], 2)
            self.assertEqual(updated.metadata["p115_total_request_count"], 6)
            self.assertEqual(updated.metadata["p115_request_count_snapshot"], 12)

    def test_run_once_accumulates_per_stage_observability_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=5.0)
            p115 = CountingP115(request_count=0)
            current_time = [5.0]
            runner = TaskRunner(
                store,
                TimeAdvancingCountingWorkflow(
                    p115,
                    current_time,
                    [
                        (StageResult.complete("已找到 CMS 整理目录"), 2, 3.0),
                        (StageResult.defer("等待人工分类", delay_seconds=30), 1, 4.0),
                    ],
                ),
                worker_id="worker-1",
                now=lambda: current_time[0],
                p115_client=p115,
            )

            self.assertTrue(runner.run_once())
            current_time[0] = 10.0
            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.metadata["stage_elapsed_seconds_by_stage"]["organizing"], 3.0)
            self.assertEqual(updated.metadata["stage_elapsed_seconds_by_stage"]["recognizing"], 4.0)
            self.assertEqual(updated.metadata["p115_request_counts_by_stage"]["organizing"], 2)
            self.assertEqual(updated.metadata["p115_request_counts_by_stage"]["recognizing"], 1)

    def test_run_once_records_p115_counts_when_risk_control_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            p115 = CountingP115(request_count=4)
            runner = TaskRunner(
                store,
                RiskCountingWorkflow(p115, increment=2),
                worker_id="worker-1",
                now=lambda: 1.0,
                p115_client=p115,
                risk_cooldown_seconds=60,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.error_type, "p115_risk_control")
            self.assertEqual(updated.metadata["p115_stage_request_count"], 2)
            self.assertEqual(updated.metadata["p115_total_request_count"], 2)
            self.assertEqual(updated.metadata["p115_request_count_snapshot"], 6)

    def test_new_runner_restores_p115_risk_cooldown_from_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            first_task = store.upsert_task("first", "", "https://115cdn.com/s/first")
            store.enqueue_task(first_task.id, TaskStage.ORGANIZING, next_run_at=100.0)
            first_runner = TaskRunner(
                store,
                RiskCountingWorkflow(CountingP115(), increment=1),
                worker_id="worker-1",
                now=lambda: 100.0,
                risk_cooldown_seconds=60,
            )

            self.assertTrue(first_runner.run_once())
            cooldown = store.get_runtime_state("115:risk_cooldown_until")
            self.assertIsNotNone(cooldown)
            self.assertEqual(float(cooldown["value"]), 160.0)

            second_task = store.upsert_task("second", "", "https://115cdn.com/s/second")
            store.enqueue_task(second_task.id, TaskStage.ORGANIZING, next_run_at=101.0)
            workflow = FakeWorkflow([StageResult.complete("不应执行")])
            second_runner = TaskRunner(store, workflow, worker_id="worker-2", now=lambda: 101.0)

            self.assertTrue(second_runner.run_once())
            updated = store.find_task(second_task.id)

            self.assertEqual(workflow.calls, [])
            self.assertEqual(updated.next_run_at, 160.0)
            self.assertEqual(updated.error_type, "p115_risk_cooldown")

    def test_run_once_records_p115_counts_when_stage_exception_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            p115 = CountingP115(request_count=4)
            runner = TaskRunner(
                store,
                ExplodingCountingWorkflow(p115, increment=2),
                worker_id="worker-1",
                now=lambda: 1.0,
                p115_client=p115,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.error_type, "stage_exception")
            self.assertEqual(updated.metadata["p115_stage_request_count"], 2)
            self.assertEqual(updated.metadata["p115_total_request_count"], 2)
            self.assertEqual(updated.metadata["p115_request_count_snapshot"], 6)

    def test_run_once_stores_global_lock_metadata_before_workflow_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=5.0)
            workflow = InspectingWorkflow([StageResult.defer("等待 CMS 整理", delay_seconds=30)])
            runner = TaskRunner(store, workflow, worker_id="worker-1", now=lambda: 5.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(workflow.seen_tasks[0].metadata["_lock_key"], "115:global")
            self.assertEqual(updated.metadata["_lock_key"], "115:global")
            self.assertIn("115", updated.metadata["_lock_reason"])
            self.assertFalse(updated.metadata["_lock_waiting"])

    def test_run_once_uses_destination_lock_for_move_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.SUCCEEDED,
                "STRM ready",
                metadata_patch={"dest_path": "/library/movie"},
            )
            store.enqueue_task(task.id, TaskStage.MOVED, next_run_at=5.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.complete("移动完成")]), worker_id="worker-1", now=lambda: 5.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.metadata["_lock_key"], "dest:/library/movie")
            self.assertIn("媒体库", updated.metadata["_lock_reason"])
            self.assertFalse(updated.metadata["_lock_waiting"])

    def test_run_once_waits_when_another_task_holds_same_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            holder = store.upsert_task("holder", "", "https://115cdn.com/s/holder")
            store.enqueue_task(holder.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("worker-1", now=1.0)
            store.record_event(
                claimed.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "资源锁: 115/CMS 全局阶段",
                metadata_patch={"_lock_key": "115:global", "_lock_reason": "115/CMS 全局阶段", "_lock_waiting": False},
                clear_claim=False,
            )
            waiting = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            store.enqueue_task(waiting.id, TaskStage.ORGANIZING, next_run_at=2.0)
            workflow = FakeWorkflow([StageResult.complete("不应执行")])
            runner = TaskRunner(store, workflow, worker_id="worker-2", interval_seconds=7, now=lambda: 2.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(waiting.id)

            self.assertEqual(workflow.calls, [])
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.next_run_at, 9.0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["_lock_key"], "115:global")
            self.assertTrue(updated.metadata["_lock_waiting"])
            self.assertEqual(updated.metadata["_lock_owner_task_id"], holder.id)

    def test_run_once_waits_when_same_lock_task_is_claimed_before_lock_metadata_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            holder = store.upsert_task("holder", "", "https://115cdn.com/s/holder")
            store.enqueue_task(holder.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("worker-1", now=1.0)
            self.assertEqual(claimed.id, holder.id)
            self.assertEqual(claimed.claimed_by, "worker-1")
            waiting = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            store.enqueue_task(waiting.id, TaskStage.ORGANIZING, next_run_at=2.0)
            workflow = FakeWorkflow([StageResult.complete("不应执行")])
            runner = TaskRunner(store, workflow, worker_id="worker-2", interval_seconds=7, now=lambda: 2.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(waiting.id)

            self.assertEqual(workflow.calls, [])
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.next_run_at, 9.0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["_lock_key"], "115:global")
            self.assertTrue(updated.metadata["_lock_waiting"])
            self.assertEqual(updated.metadata["_lock_owner_task_id"], holder.id)

    def test_run_once_does_not_execute_after_claim_changes_during_lock_prepare(self):
        class ReclaimBeforeLockStore(TaskStore):
            def __init__(self, db_path):
                super().__init__(db_path)
                self.reclaimed = None

            def claim_task_lock(self, task_id, *args, **kwargs):
                if self.reclaimed is None:
                    current = self.find_task(task_id)
                    self.clear_worker_claims(current.claimed_by, now=1.0)
                    self.reclaimed = self.claim_next_runnable("worker-2", now=1.0)
                return super().claim_task_lock(task_id, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            store = ReclaimBeforeLockStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("lock-race", "", "https://115cdn.com/s/lock-race")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            workflow = FakeWorkflow([StageResult.complete("must not execute")])
            runner = TaskRunner(store, workflow, worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())
            current = store.find_task(task.id)

            self.assertEqual(workflow.calls, [])
            self.assertEqual(current.claimed_by, "worker-2")
            self.assertEqual(current.claim_token, store.reclaimed.claim_token)
            self.assertNotIn("_lock_key", current.metadata)

    def test_second_runner_does_not_clear_live_claim(self):
        class BlockingWorkflow:
            def __init__(self):
                self.calls = 0
                self.entered = threading.Event()
                self.second_entered = threading.Event()
                self.release = threading.Event()
                self.lock = threading.Lock()

            def run_stage(self, _task):
                with self.lock:
                    self.calls += 1
                    call_number = self.calls
                if call_number == 1:
                    self.entered.set()
                else:
                    self.second_entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("workflow was not released")
                return StageResult.complete("organized")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)
            task = first_store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            first_store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            workflow = BlockingWorkflow()
            first_runner = TaskRunner(first_store, workflow, worker_id="shared-worker", now=lambda: 1.0)
            second_runner = TaskRunner(second_store, workflow, worker_id="shared-worker", now=lambda: 1.0)

            first_thread = threading.Thread(target=first_runner.run_once)
            second_thread = threading.Thread(target=second_runner.run_once)
            first_thread.start()
            self.assertTrue(workflow.entered.wait(timeout=1))
            second_thread.start()
            workflow.second_entered.wait(timeout=0.25)
            workflow.release.set()
            first_thread.join(timeout=1)
            second_thread.join(timeout=1)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(workflow.calls, 1)
            updated = first_store.find_task(task.id)
            self.assertEqual(updated.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(updated.status, TaskStatus.PENDING)

    def test_run_once_discards_result_when_task_was_requeued_to_different_stage(self):
        class RequeueDuringWorkflow(FakeWorkflow):
            def __init__(self, store, task_id):
                super().__init__([StageResult.defer("等待 CMS 整理完成", delay_seconds=15)])
                self.store = store
                self.task_id = task_id

            def run_stage(self, task):
                self.store.reprocess_task(self.task_id, message="用户从头重跑", next_run_at=0)
                return super().run_stage(task)

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            runner = TaskRunner(
                store,
                RequeueDuringWorkflow(store, task.id),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.next_run_at, 0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["force_reprocess"], True)

    def test_run_once_does_not_overwrite_state_changed_during_result_commit(self):
        class RaceStore(TaskStore):
            def __init__(self, db_path):
                super().__init__(db_path)
                self.raced = False

            def complete_claimed_stage(self, task_id, **kwargs):
                if not self.raced:
                    self.raced = True
                    self.reprocess_task(task_id, message="用户从头重跑", next_run_at=0)
                return super().complete_claimed_stage(task_id, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            store = RaceStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.complete("不应覆盖重跑状态")]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.next_run_at, 0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(store.list_events(task.id)[-1]["message"], "用户从头重跑")

    def test_repeated_defer_uses_backoff_without_growing_event_log(self):
        current_time = 1.0

        def now():
            return current_time

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=current_time)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.defer("等待 CMS 整理", delay_seconds=15) for _ in range(10)]),
                worker_id="worker-1",
                now=now,
            )

            for _ in range(4):
                self.assertTrue(runner.run_once())
                current_time = store.find_task(task.id).next_run_at
            self.assertEqual(store.find_task(task.id).next_run_at, 91.0)

            self.assertTrue(runner.run_once())
            fifth = store.find_task(task.id)
            current_time = fifth.next_run_at
            self.assertEqual(fifth.next_run_at, 151.0)

            for _ in range(5):
                self.assertTrue(runner.run_once())
                current_time = store.find_task(task.id).next_run_at
            tenth = store.find_task(task.id)
            events = store.list_events(task.id)

            self.assertEqual(tenth.next_run_at, 571.0)
            self.assertEqual(tenth.metadata["_defer_count"], 10)
            self.assertEqual(len([event for event in events if event["message"] == "等待 CMS 整理"]), 1)

    def test_repeated_five_second_waits_back_off_after_two_fast_checks(self):
        current_time = 1.0

        def now():
            return current_time

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=current_time)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.defer("等待自有分享 STRM 源目录生成", delay_seconds=5) for _ in range(6)]),
                worker_id="worker-1",
                now=now,
            )

            observed_next_runs = []
            for _ in range(6):
                self.assertTrue(runner.run_once())
                current_time = store.find_task(task.id).next_run_at
                observed_next_runs.append(current_time)

            self.assertEqual(observed_next_runs, [6.0, 11.0, 41.0, 71.0, 131.0, 191.0])
            self.assertEqual(store.find_task(task.id).metadata["_defer_count"], 6)

    def test_organizing_defer_over_limit_becomes_needs_action_and_releases_lock(self):
        current_time = 1.0

        def now():
            return current_time

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待 CMS 整理完成",
                metadata_patch={
                    "_defer_stage": TaskStage.ORGANIZING.value,
                    "_defer_message": "等待 CMS 整理完成",
                    "_defer_count": 29,
                    "_lock_key": "115:global",
                    "_lock_reason": "115/CMS 全局阶段",
                    "_lock_waiting": False,
                },
                next_run_at=current_time,
                clear_claim=True,
            )
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.defer("等待 CMS 整理完成", delay_seconds=15)]),
                worker_id="worker-1",
                now=now,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.error_type, "organizing_timeout")
            self.assertIn("CMS 整理", updated.error_summary)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["_lock_key"], "")
            self.assertFalse(updated.metadata["_lock_waiting"])
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.ORGANIZING.value)

    def test_long_repeated_strm_wait_becomes_needs_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM",
                tmdb_id="123456",
                metadata_patch={
                    "_defer_stage": TaskStage.STRM_READY.value,
                    "_defer_message": "等待自有分享 STRM",
                    "_defer_count": 19,
                    "_lock_key": "tmdb:123456",
                    "_lock_waiting": False,
                    "tmdb_id": "123456",
                },
                next_run_at=1.0,
                clear_claim=True,
            )
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.defer("等待自有分享 STRM", delay_seconds=15)]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.error_type, "stage_wait_timeout")
            self.assertIn("等待自有分享 STRM", updated.error_summary)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.STRM_READY.value)

    def test_share_sync_submitted_wait_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.SHARE_SYNC_SUBMITTED,
                TaskStatus.RUNNING,
                "等待上一条 CMS 分享同步完成",
                tmdb_id="123456",
                metadata_patch={
                    "_defer_stage": TaskStage.SHARE_SYNC_SUBMITTED.value,
                    "_defer_message": "等待上一条 CMS 分享同步完成",
                    "_defer_count": 29,
                    "_lock_key": "tmdb:123456",
                    "_lock_waiting": False,
                    "tmdb_id": "123456",
                },
                next_run_at=1.0,
                clear_claim=True,
            )
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.defer("等待上一条 CMS 分享同步完成", delay_seconds=15)]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.error_type, "stage_wait_timeout")
            self.assertIn("等待上一条 CMS 分享同步完成", updated.error_summary)
            self.assertEqual(updated.claimed_by, "")

    def test_quality_repair_wait_does_not_timeout_before_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-repair", "", "https://115cdn.com/s/quality-repair")
            store.record_event(
                task.id,
                TaskStage.EMBY_CONFIRMED,
                TaskStatus.RUNNING,
                "等待自有分享 STRM",
                metadata_patch={
                    "quality_repair_queued": True,
                    "quality_repair_deadline_at": 10_000.0,
                    "_defer_stage": TaskStage.EMBY_CONFIRMED.value,
                    "_defer_message": "等待自有分享 STRM",
                    "_defer_count": 19,
                },
                next_run_at=1.0,
                clear_claim=True,
            )
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.defer("等待自有分享 STRM", delay_seconds=15)]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.metadata["_defer_count"], 20)
            self.assertEqual(updated.error_summary, "")

    def test_quality_repair_markers_are_cleared_after_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-cleaned", "", "https://115cdn.com/s/quality-cleaned")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "等待清理确认",
                metadata_patch={
                    "quality_repair_queued": True,
                    "quality_repair_action": "restore",
                    "quality_repair_reason": "missing_dest",
                    "quality_run_id": "quality-run",
                    "quality_repair_started_at": 1.0,
                    "quality_repair_deadline_at": 100.0,
                },
                next_run_at=1.0,
                clear_claim=True,
            )
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.complete("清理完成")]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.status, TaskStatus.SUCCEEDED)
            for key in (
                "quality_repair_queued",
                "quality_repair_action",
                "quality_repair_reason",
                "quality_run_id",
                "quality_repair_started_at",
                "quality_repair_deadline_at",
            ):
                self.assertNotIn(key, updated.metadata)

    def test_run_once_records_stage_timing_metadata_on_success(self):
        now_value = 13.5

        def now():
            return now_value

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=10.0)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.complete("清理完成")]),
                worker_id="worker-1",
                now=now,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.metadata["stage_started_at"], 13.5)
            self.assertEqual(updated.metadata["stage_finished_at"], 13.5)
            self.assertEqual(updated.metadata["stage_elapsed_seconds"], 0.0)
            self.assertEqual(updated.metadata["stage_wait_seconds"], 3.5)

    def test_complete_stage_clears_stale_defer_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "等待清理确认",
                metadata_patch={
                    "_defer_stage": TaskStage.CLEANED.value,
                    "_defer_message": "等待清理确认",
                    "_defer_count": 6,
                    "source_path": "/mnt/share/movie",
                },
                next_run_at=1.0,
                clear_claim=True,
            )
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.complete("115 转存源已删除，自有分享保留")]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertNotIn("_defer_stage", updated.metadata)
            self.assertNotIn("_defer_message", updated.metadata)
            self.assertNotIn("_defer_count", updated.metadata)
            self.assertEqual(updated.metadata["source_path"], "/mnt/share/movie")

    def test_run_once_records_needs_action_on_current_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.RECOGNIZING, next_run_at=1.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.needs_action("请选择分类")]), worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.error_summary, "请选择分类")
            self.assertEqual(updated.claimed_by, "")

    def test_run_once_makes_organizing_needs_action_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("organizing-conflict", "", "https://115cdn.com/s/organizing-conflict")
            task = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "整理中",
                metadata_patch={
                    "intake_identity": {
                        "root_ids": ["received-root"],
                        "files": [{"id": "file-a", "name": "file-a.mkv"}],
                    }
                },
            )
            operation = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {})
            store.start_operation(task.id, operation.operation_key)
            store.complete_operation(task.id, operation.operation_key, {"received": True})
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.needs_action("接收文件归属存在歧义，已停止自动绑定")]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.metadata["retry_from_stage"], TaskStage.ORGANIZING.value)
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.ORGANIZING.value)
            self.assertIn("resume_organizing", available_task_actions(updated, 3, store=store))

    def test_run_once_records_failure_from_exception(self):
        class ExplodingWorkflow:
            def run_stage(self, task):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1.0)
            runner = TaskRunner(store, ExplodingWorkflow(), worker_id="worker-1", now=lambda: 1.0)

            with self.assertLogs("app.task_runner", level="ERROR") as logs:
                self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.STRM_READY)
            self.assertEqual(updated.status, TaskStatus.FAILED)
            self.assertEqual(updated.error_type, "stage_exception")
            self.assertIn("boom", updated.error_summary)
            self.assertEqual(updated.retry_count, 0)
            self.assertEqual(updated.claimed_by, "")
            self.assertIn("Task stage failed task_id=1 stage=strm_ready", logs.output[0])

    def test_run_once_stops_115_risk_control_without_retrying(self):
        class RiskControlledWorkflow:
            def run_stage(self, task):
                raise P115RiskControlError("操作过于频繁，请稍后再试")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待 CMS 整理完成",
                metadata_patch={
                    "_lock_key": "115:global",
                    "_lock_reason": "115/CMS 全局阶段",
                    "_lock_waiting": False,
                },
                next_run_at=1.0,
                clear_claim=True,
            )
            runner = TaskRunner(store, RiskControlledWorkflow(), worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.error_type, "p115_risk_control")
            self.assertIn("115 风控", updated.error_summary)
            self.assertEqual(updated.retry_count, 0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["_lock_key"], "")
            self.assertFalse(updated.metadata["_lock_waiting"])

    def test_run_once_defers_following_115_tasks_during_risk_cooldown(self):
        class RiskThenUnexpectedWorkflow:
            def __init__(self):
                self.calls = 0

            def run_stage(self, task):
                self.calls += 1
                if self.calls == 1:
                    raise P115RiskControlError("操作过于频繁，请稍后再试")
                raise AssertionError("workflow should not run during 115 cooldown")

        now_value = 1.0

        def now():
            return now_value

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            first = store.upsert_task("first", "", "https://115cdn.com/s/first")
            second = store.upsert_task("second", "", "https://115cdn.com/s/second")
            store.enqueue_task(first.id, TaskStage.ORGANIZING, next_run_at=1.0)
            store.enqueue_task(second.id, TaskStage.ORGANIZING, next_run_at=2.0)
            workflow = RiskThenUnexpectedWorkflow()
            runner = TaskRunner(store, workflow, worker_id="worker-1", now=now, risk_cooldown_seconds=60)

            self.assertTrue(runner.run_once())
            now_value = 2.0
            self.assertTrue(runner.run_once())
            updated = store.find_task(second.id)

            self.assertEqual(workflow.calls, 1)
            self.assertEqual(updated.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.next_run_at, 61.0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["p115_risk_cooldown_until"], 61.0)
            self.assertIn("115 风控冷却", updated.error_summary)

    def test_run_once_records_explicit_failure_and_clears_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM missing",
                error_summary="未找到 STRM",
            )
            app = WebApp(store, web_token="")
            app.handle_request("POST", f"/task/{task.id}/retry", {}, b"")
            self.assertEqual(store.find_task(task.id).retry_count, 0)
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.failed("STRM missing", error_type="strm_missing")]),
                worker_id="worker-1",
                now=lambda: 1.0,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.STRM_READY)
            self.assertEqual(updated.status, TaskStatus.FAILED)
            self.assertEqual(updated.error_type, "strm_missing")
            self.assertEqual(updated.error_summary, "STRM missing")
            self.assertEqual(updated.retry_count, 1)
            self.assertEqual(updated.claimed_by, "")


if __name__ == "__main__":
    unittest.main()


class ClaimRecoveryTests(unittest.TestCase):
    def test_restarted_runner_reclaims_stale_claim_before_old_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("hang", "", "https://115cdn.com/s/hang")
            store.enqueue_task(task.id, next_run_at=0)
            # Worker A claims the task and then crashes without renewing its heartbeat.
            claimed = store.claim_next_runnable("worker-a", now=100.0)
            self.assertIsNotNone(claimed)

            workflow = FakeWorkflow([StageResult.complete("done")])
            runner = TaskRunner(
                store,
                workflow,
                worker_id="worker-b",
                now=lambda: 500.0,
                claim_stale_after_seconds=300,
            )
            self.assertTrue(runner.run_once())
            self.assertEqual([call[0] for call in workflow.calls], [task.id])

    def test_active_claim_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("active", "", "https://115cdn.com/s/active")
            store.enqueue_task(task.id, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=100.0)
            self.assertIsNotNone(claimed)
            store.renew_claim(claimed.id, "worker-a", claimed.claim_token, now=460.0)

            workflow = FakeWorkflow([StageResult.complete("done")])
            runner = TaskRunner(
                store,
                workflow,
                worker_id="worker-b",
                now=lambda: 500.0,
                claim_stale_after_seconds=300,
            )
            self.assertFalse(runner.run_once())
            self.assertEqual(workflow.calls, [])
