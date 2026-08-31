from __future__ import annotations

import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import bridge
from app.clients.hdhive import HdhiveUnlockItem
from app.hdhive_subscription_store import HdhiveSubscriptionStore
from app.hdhive_subscriptions import HdhiveSubscriptionService
from app.models import TaskStage, TaskStatus
from app.task_runner import StageResult, TaskRunner
from app.task_store import TaskStore, operation_scope
from tests.test_bridge_task_engine import (
    FakeCleanupClient,
    FakeCms,
    FakeP115,
    FakeTelegram,
)
from tests.test_hdhive_subscriptions import FakeSubscriptionProxy, resource


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CRASH_BOUNDARIES = ("intent", "start", "result", "stage_commit")
EXPECTED_MUTATIONS = {"intent": 1, "start": 0, "result": 1, "stage_commit": 1}


class InjectedCrash(RuntimeError):
    pass


class RecoverableCreateP115(FakeP115):
    def __init__(self):
        super().__init__()
        self.remote_share = None
        self.settings_calls = []

    def create_share(self, file_id):
        self.created_shares.append(str(file_id))
        self.remote_share = {
            "share_code": "task10-own-share",
            "receive_code": "task10-code",
            "share_url": "https://115.com/s/task10-synthetic-share",
            "create_time": "1000",
        }
        return dict(self.remote_share)

    def find_own_share_by_title(self, title, min_create_time=0):
        del title, min_create_time
        return dict(self.remote_share) if self.remote_share else None

    def ensure_share_settings(self, share_code, receive_code):
        self.settings_calls.append((str(share_code), str(receive_code)))
        return {"share_code": str(share_code), "receive_code": str(receive_code)}


class AbsenceAwareCleanup(FakeCleanupClient):
    def __init__(self):
        super().__init__()
        self.present = {"folder-id"}

    def delete_file(self, file_id):
        super().delete_file(str(file_id))
        self.present.discard(str(file_id))
        return {"state": True}

    def file_exists_in_parent(self, file_id, parent_id):
        del parent_id
        return str(file_id) in self.present


@dataclass
class SelfShareRuntime:
    tasks: TaskStore
    submissions: bridge.SubmissionStore
    workflow: bridge.BridgeSelfShareTaskWorkflow


def open_self_share_runtime(
    root: Path,
    *,
    p115: FakeP115,
    cms: FakeCms,
    cleanup: FakeCleanupClient | None = None,
    now=lambda: time.time(),
) -> SelfShareRuntime:
    tasks = TaskStore(root / "tasks.db")
    submissions = bridge.SubmissionStore(root / "submissions.db")
    config = bridge.SelfShareConfig(
        enabled=True,
        strm_root=root / "share-strm",
        cms_cid="0",
        cms_local_path="/media/share",
        parent_cid_category_map={"movie-parent": "Movies"},
        auto_organize_retry_seconds=30,
    )
    workflow = bridge.BridgeSelfShareTaskWorkflow(
        cms,
        FakeTelegram(),
        "task-10-chat",
        submissions,
        tasks,
        p115,
        config,
        bridge.MoveConfig(source_roots=[], library_roots={}),
        None,
        None,
        None,
        cleanup_client=cleanup,
        receive_cid="pending-cid",
    )
    workflow._now = now
    return SelfShareRuntime(tasks, submissions, workflow)


@contextmanager
def inject_task_operation_crash(store: TaskStore, boundary: str):
    method_name = {
        "intent": "prepare_operation",
        "start": "start_operation",
        "result": "complete_operation",
        "stage_commit": "commit_claimed_result",
    }[boundary]
    original = getattr(store, method_name)
    hits = []

    def crash_once(*args, **kwargs):
        if hits:
            return original(*args, **kwargs)
        hits.append(boundary)
        if boundary in {"intent", "start"}:
            original(*args, **kwargs)
            raise InjectedCrash(f"crash after {boundary}")
        if boundary == "result":
            raise InjectedCrash("crash before result persistence")
        return None

    with patch.object(store, method_name, side_effect=crash_once):
        yield hits


class RuntimeRecoveryTests(unittest.TestCase):
    def _new_task(self, runtime, stage, *, row=None, metadata=None):
        task = runtime.tasks.upsert_task(
            "task10-source",
            "task10-code",
            "https://example.invalid/task10-source",
        )
        if row is not None or metadata:
            task = runtime.tasks.record_event(
                task.id,
                stage,
                TaskStatus.RUNNING,
                "fault injection setup",
                submission_id=int(row["id"]) if row is not None else None,
                metadata_patch=dict(metadata or {}),
            )
        runtime.tasks.enqueue_task(task.id, stage, next_run_at=0)
        return task

    def _run_crash_and_resume(self, runtime, task, stage, boundary, reopen):
        first_runner = TaskRunner(
            runtime.tasks,
            runtime.workflow,
            worker_id=f"task10-first-{boundary}",
            now=runtime.workflow._now,
        )
        with inject_task_operation_crash(runtime.tasks, boundary) as hits:
            with self.assertLogs("app.task_runner", level="WARNING"):
                self.assertTrue(first_runner.run_once())
        self.assertEqual(hits, [boundary])

        restarted = reopen()
        self.assertIsNot(runtime.tasks, restarted.tasks)
        self.assertIsNot(runtime.workflow, restarted.workflow)
        restarted.tasks.enqueue_task(task.id, stage, next_run_at=0)
        second_runner = TaskRunner(
            restarted.tasks,
            restarted.workflow,
            worker_id=f"task10-restarted-{boundary}",
            now=restarted.workflow._now,
        )
        self.assertTrue(second_runner.run_once())
        return restarted

    def _receive_case(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = [time.time()]
            p115 = FakeP115()
            cms = FakeCms()
            runtime = open_self_share_runtime(root, p115=p115, cms=cms, now=lambda: clock[0])
            task = self._new_task(runtime, TaskStage.RECEIVED)
            key = f"{operation_scope(task)}:receive_share:{task.share_code}:pending-cid"

            restarted = self._run_crash_and_resume(
                runtime,
                task,
                TaskStage.RECEIVED,
                boundary,
                lambda: open_self_share_runtime(root, p115=p115, cms=cms, now=lambda: clock[0]),
            )

            operation = restarted.tasks.find_operation(task.id, key)
            expected_status = "started" if boundary == "start" else "succeeded"
            self.assertEqual(operation.status, expected_status)
            self.assertLessEqual(len(p115.received), 1)
            return len(p115.received)

    def _create_share_case(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = [1000.0]
            p115 = RecoverableCreateP115()
            cms = FakeCms()
            runtime = open_self_share_runtime(root, p115=p115, cms=cms, now=lambda: clock[0])
            row = runtime.submissions.upsert_submission(
                bridge.ShareKey("task10-source", "task10-code"),
                "https://example.invalid/task10-source",
                "received",
                title="Task 10 synthetic title",
            )
            row = runtime.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="Task 10 synthetic title",
            ) or row
            task = self._new_task(
                runtime,
                TaskStage.OWN_SHARE_CREATED,
                row=row,
                metadata={"submission_id": row["id"]},
            )
            key = f"{operation_scope(task)}:create_share:folder-id"

            restarted = self._run_crash_and_resume(
                runtime,
                task,
                TaskStage.OWN_SHARE_CREATED,
                boundary,
                lambda: open_self_share_runtime(root, p115=p115, cms=cms, now=lambda: clock[0]),
            )

            operation = restarted.tasks.find_operation(task.id, key)
            expected_status = "started" if boundary == "start" else "succeeded"
            self.assertEqual(operation.status, expected_status)
            self.assertLessEqual(len(p115.created_shares), 1)
            self.assertEqual(len(p115.settings_calls), EXPECTED_MUTATIONS[boundary])
            return len(p115.created_shares)

    def _cms_sync_case(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = [time.time()]
            p115 = FakeP115()
            cms = FakeCms()
            runtime = open_self_share_runtime(root, p115=p115, cms=cms, now=lambda: clock[0])
            row = runtime.submissions.upsert_submission(
                bridge.ShareKey("task10-source", "task10-code"),
                "https://example.invalid/task10-source",
                "received",
                title="Task 10 synthetic title",
            )
            row = runtime.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="Task 10 synthetic title",
                own_share_code="task10-own-share",
                own_share_receive_code="task10-code",
            ) or row
            task = self._new_task(
                runtime,
                TaskStage.SHARE_SYNC_SUBMITTED,
                row=row,
                metadata={"submission_id": row["id"]},
            )
            key = f"{operation_scope(task)}:cms_share_sync:task10-own-share"

            restarted = self._run_crash_and_resume(
                runtime,
                task,
                TaskStage.SHARE_SYNC_SUBMITTED,
                boundary,
                lambda: open_self_share_runtime(root, p115=p115, cms=cms, now=lambda: clock[0]),
            )

            operation = restarted.tasks.find_operation(task.id, key)
            expected_status = "uncertain" if boundary in {"start", "result"} else "succeeded"
            self.assertEqual(operation.status, expected_status)
            self.assertLessEqual(len(cms.share_sync_calls), 1)
            return len(cms.share_sync_calls)

    def _delete_case(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = [time.time()]
            p115 = FakeP115()
            cms = FakeCms()
            cleanup = AbsenceAwareCleanup()
            runtime = open_self_share_runtime(
                root,
                p115=p115,
                cms=cms,
                cleanup=cleanup,
                now=lambda: clock[0],
            )
            runtime.tasks.set_self_share_review_mode_override("off")
            row = runtime.submissions.upsert_submission(
                bridge.ShareKey("task10-source", "task10-code"),
                "https://example.invalid/task10-source",
                "received",
                title="Task 10 synthetic title",
            )
            row = runtime.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="share_sync_submitted",
                own_share_file_id="folder-id",
                own_share_file_name="Task 10 synthetic title",
                own_share_code="task10-own-share",
                own_share_receive_code="task10-code",
                own_share_url="https://115.com/s/task10-synthetic-share",
                share_sync_status="submitted",
            ) or row
            destination = root / "library" / "Task 10 synthetic title"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text(
                "https://115.com/s/task10-own-share_task10-code_/movie.mkv",
                encoding="utf-8",
            )
            row = runtime.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/synthetic/source",
                dest_path=str(destination),
                category_final="Movies",
            ) or row
            row = runtime.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._new_task(
                runtime,
                TaskStage.CLEANED,
                row=row,
                metadata={
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
            )
            key = f"{operation_scope(task)}:delete_source:folder-id"

            restarted = self._run_crash_and_resume(
                runtime,
                task,
                TaskStage.CLEANED,
                boundary,
                lambda: open_self_share_runtime(
                    root,
                    p115=p115,
                    cms=cms,
                    cleanup=cleanup,
                    now=lambda: clock[0],
                ),
            )

            operation = restarted.tasks.find_operation(task.id, key)
            expected_status = "started" if boundary == "start" else "succeeded"
            self.assertEqual(operation.status, expected_status)
            self.assertLessEqual(len(cleanup.deleted), 1)
            return len(cleanup.deleted)

    def _hdhive_unlock_case(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hdhive_path = root / "hdhive.db"
            task_path = root / "tasks.db"
            store = HdhiveSubscriptionStore(hdhive_path)
            subscription = store.create_subscription(
                "task-10-chat",
                "tmdb_tv",
                "255358",
                "Task 10 series",
                "255358",
            )
            full_url = "https://115.com/s/task10-hdhive-synthetic"
            proxy = FakeSubscriptionProxy(
                [resource("task10-hdhive", points=8)],
                [HdhiveUnlockItem("task10-hdhive", True, full_url, "", "", False, points_spent=6)],
            )
            intake_task_ids = []

            def enqueue(task_store, urls, chat_id):
                task = task_store.upsert_task("task10-hdhive", "", urls[0], chat_id=str(chat_id))
                intake_task_ids.append(task.id)
                return task.id

            first_tasks = TaskStore(task_path)
            first_service = HdhiveSubscriptionService(
                proxy=proxy,
                store=store,
                enqueue_links=lambda urls, chat_id: enqueue(first_tasks, urls, chat_id),
                auto_unlock_max_points=20,
            )
            method_name = {
                "intent": "upsert_item",
                "start": "claim_item_unlocking",
                "result": "mark_item_unlocked",
                "stage_commit": "mark_item_enqueued",
            }[boundary]
            original = getattr(store, method_name)
            hits = []

            def crash_once(*args, **kwargs):
                if hits:
                    return original(*args, **kwargs)
                hits.append(boundary)
                if boundary in {"intent", "start"}:
                    original(*args, **kwargs)
                    raise InjectedCrash(f"crash after {boundary}")
                raise InjectedCrash(f"crash before {boundary} persistence")

            clock_patch = (
                patch("app.hdhive_subscription_store.time.time", return_value=1.0)
                if boundary == "start"
                else nullcontext()
            )
            with patch.object(store, method_name, side_effect=crash_once), clock_patch:
                if boundary in {"intent", "start"}:
                    with self.assertRaises(InjectedCrash):
                        first_service.check(subscription.id)
                else:
                    first_service.check(subscription.id)
            self.assertEqual(hits, [boundary])

            reopened_store = HdhiveSubscriptionStore(hdhive_path)
            reopened_tasks = TaskStore(task_path)
            restarted_service = HdhiveSubscriptionService(
                proxy=proxy,
                store=reopened_store,
                enqueue_links=lambda urls, chat_id: enqueue(reopened_tasks, urls, chat_id),
                auto_unlock_max_points=20,
            )
            self.assertIsNot(store, reopened_store)
            self.assertIsNot(first_service, restarted_service)
            restarted_service.check(subscription.id)

            saved = reopened_store.list_items(subscription.id)[0]
            expected_status = "pending_confirmation" if boundary in {"start", "result"} else "enqueued"
            self.assertEqual(saved.status, expected_status)
            self.assertLessEqual(len(proxy.unlock_calls), 1)
            if boundary == "stage_commit":
                self.assertEqual(len(intake_task_ids), 2)
                self.assertEqual(len(set(intake_task_ids)), 1)
            return len(proxy.unlock_calls)

    def test_fault_injection_matrix_never_repeats_irreversible_mutation(self):
        cases = (
            ("receive", self._receive_case),
            ("create_share", self._create_share_case),
            ("cms_sync", self._cms_sync_case),
            ("delete", self._delete_case),
            ("hdhive_unlock", self._hdhive_unlock_case),
        )
        for operation, run_case in cases:
            for boundary in CRASH_BOUNDARIES:
                with self.subTest(operation=operation, boundary=boundary):
                    mutations = run_case(boundary)
                    self.assertEqual(mutations, EXPECTED_MUTATIONS[boundary])

    def test_overlapping_runners_execute_one_claimed_task_once(self):
        class BlockingWorkflow:
            def __init__(self):
                self.calls = 0
                self.entered = threading.Event()
                self.release = threading.Event()
                self.lock = threading.Lock()

            def run_stage(self, _task):
                with self.lock:
                    self.calls += 1
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise AssertionError("blocking workflow was not released")
                return StageResult.complete("organized")

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)
            task = first_store.upsert_task("task10-overlap", "", "https://example.invalid/task10-overlap")
            first_store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            workflow = BlockingWorkflow()
            first_runner = TaskRunner(first_store, workflow, worker_id="task10-runner-a", now=lambda: 1.0)
            second_runner = TaskRunner(second_store, workflow, worker_id="task10-runner-b", now=lambda: 1.0)
            second_done = threading.Event()
            second_result = []

            first_thread = threading.Thread(target=first_runner.run_once, name="task10-runner-a")

            def run_second():
                try:
                    second_result.append(second_runner.run_once())
                finally:
                    second_done.set()

            second_thread = threading.Thread(target=run_second, name="task10-runner-b")
            first_thread.start()
            try:
                self.assertTrue(workflow.entered.wait(timeout=1))
                second_thread.start()
                self.assertTrue(second_done.wait(timeout=1))
                self.assertEqual(second_result, [False])
                self.assertEqual(workflow.calls, 1)
            finally:
                workflow.release.set()
                first_thread.join(timeout=1)
                if second_thread.ident is not None:
                    second_thread.join(timeout=1)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(workflow.calls, 1)
            recovered = first_store.find_task(task.id)
            self.assertEqual(recovered.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(recovered.status, TaskStatus.PENDING)


class ReleaseGateConfigurationTests(unittest.TestCase):
    def test_ci_keeps_warning_frontend_and_docker_gates_explicit(self):
        content = CI_WORKFLOW.read_text(encoding="utf-8")
        commands = (
            "python -W error::ResourceWarning -m unittest discover -s tests -v",
            "npm ci --prefix frontend",
            "npm test --prefix frontend",
            "npm run build --prefix frontend",
            "docker build -t cms-tg-ingest:test .",
        )
        positions = []
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(command, content)
                positions.append(content.index(command))
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
