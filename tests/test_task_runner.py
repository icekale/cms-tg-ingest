import tempfile
import unittest
from pathlib import Path

from app.clients.p115 import P115RiskControlError
from app.models import TaskStage, TaskStatus
from app.task_runner import StageResult, TaskRunner
from app.task_store import TaskStore


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


class TaskRunnerTests(unittest.TestCase):
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

    def test_run_once_releases_previous_same_worker_claim_before_claiming(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("task-runner", now=1.0)
            self.assertEqual(claimed.claimed_by, "task-runner")
            workflow = FakeWorkflow([StageResult.defer("等待 CMS 整理完成", delay_seconds=15)])
            runner = TaskRunner(store, workflow, worker_id="task-runner", now=lambda: 10.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(workflow.calls, [(task.id, TaskStage.ORGANIZING)])
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.next_run_at, 25.0)

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
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1.0)
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
            self.assertEqual(updated.claimed_by, "")


if __name__ == "__main__":
    unittest.main()
