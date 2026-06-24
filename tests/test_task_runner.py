import tempfile
import unittest
from pathlib import Path

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
            self.assertEqual(store.find_task(task.id).next_run_at, 61.0)

            self.assertTrue(runner.run_once())
            fifth = store.find_task(task.id)
            current_time = fifth.next_run_at
            self.assertEqual(fifth.next_run_at, 91.0)

            for _ in range(5):
                self.assertTrue(runner.run_once())
                current_time = store.find_task(task.id).next_run_at
            tenth = store.find_task(task.id)
            events = store.list_events(task.id)

            self.assertEqual(tenth.next_run_at, 361.0)
            self.assertEqual(tenth.metadata["_defer_count"], 10)
            self.assertEqual(len([event for event in events if event["message"] == "等待 CMS 整理"]), 1)

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
