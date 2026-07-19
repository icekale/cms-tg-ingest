import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import TaskStage, TaskStatus
from app.task_health import build_task_health, format_task_health, format_taskstore_health
from app.task_store import TaskStore


class TaskHealthTests(unittest.TestCase):
    def test_health_uses_all_open_tasks_beyond_recent_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("old-pending", "", "https://115cdn.com/s/old-pending")
            store.record_event(pending.id, TaskStage.CMS_SUBMITTED, TaskStatus.PENDING, "orphaned")
            waiting = store.upsert_task("old-waiting", "", "https://115cdn.com/s/old-waiting")
            store.record_event(
                waiting.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "waiting",
                metadata_patch={
                    "_lock_waiting": True,
                    "_lock_reason": "global lock",
                    "p115_risk_cooldown_until": 500.0,
                },
                next_run_at=200.0,
            )
            failed = store.upsert_task("old-failed", "", "https://115cdn.com/s/old-failed")
            store.record_event(failed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            manual = store.upsert_task("old-manual", "", "https://115cdn.com/s/old-manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "manual")
            for index in range(100):
                task = store.upsert_task(f"done-{index}", "", f"https://115cdn.com/s/done-{index}")
                store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

            with patch.object(store, "list_open_tasks", side_effect=AssertionError("health must use aggregation")):
                summary = build_task_health(store, enabled=True, limit=100, now=100.0)

            self.assertEqual(summary.recent_count, 100)
            self.assertEqual(summary.pending_count, 1)
            self.assertEqual(summary.running_count, 1)
            self.assertEqual(summary.needs_action_count, 1)
            self.assertEqual(summary.problem_count, 3)
            self.assertEqual(summary.lock_wait_count, 1)
            self.assertEqual(summary.latest_problem.id, manual.id)
            self.assertEqual(summary.latest_lock_wait.id, waiting.id)
            self.assertEqual(summary.p115_cooldown_until, 500.0)
            self.assertEqual(len(summary.wait_details), 2)

    def test_open_health_aggregate_counts_all_open_rows_and_json_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            store.record_event(pending.id, TaskStage.ORGANIZING, TaskStatus.PENDING, "pending", next_run_at=10.0)
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.record_event(
                running.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "running",
                metadata_patch={"_lock_waiting": True, "p115_risk_cooldown_until": 500.0},
                next_run_at=20.0,
            )
            legacy = store.upsert_task("legacy", "", "https://115cdn.com/s/legacy")
            store.record_event(legacy.id, TaskStage.MOVED, TaskStatus.PENDING, "legacy", next_run_at=-1.0)
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            manual = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "manual")

            aggregate = store.aggregate_open_task_health(limit=1)

            self.assertEqual(aggregate.pending_count, 2)
            self.assertEqual(aggregate.running_count, 1)
            self.assertEqual(aggregate.needs_action_count, 1)
            self.assertEqual(aggregate.failed_count, 1)
            self.assertEqual(aggregate.unscheduled_count, 1)
            self.assertEqual(aggregate.problem_count, 3)
            self.assertEqual(aggregate.lock_wait_count, 1)
            self.assertEqual(aggregate.p115_cooldown_until, 500.0)
            self.assertEqual(aggregate.wait_tasks[0].id, legacy.id)
            self.assertEqual(aggregate.latest_problem.id, manual.id)
            self.assertEqual(aggregate.latest_lock_wait.id, running.id)

    def test_health_materializes_only_bounded_detail_rows(self):
        class TrackingTaskStore(TaskStore):
            def __init__(self, db_path):
                self.snapshot_ids = []
                super().__init__(db_path)

            def _snapshot(self, row):
                self.snapshot_ids.append(int(row["id"]))
                return super()._snapshot(row)

        with tempfile.TemporaryDirectory() as tmp:
            store = TrackingTaskStore(Path(tmp) / "tasks.db")
            for index in range(20):
                task = store.upsert_task(f"waiting-{index}", "", f"https://115cdn.com/s/waiting-{index}")
                store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.PENDING, "waiting", next_run_at=200.0)
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            lock_wait = store.upsert_task("lock-wait", "", "https://115cdn.com/s/lock-wait")
            store.record_event(
                lock_wait.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "lock wait",
                metadata_patch={"_lock_waiting": True},
                next_run_at=200.0,
            )
            store.snapshot_ids.clear()

            build_task_health(store, enabled=True, limit=1, now=100.0)

            self.assertLessEqual(len(store.snapshot_ids), 8)
            self.assertLess(len(store.snapshot_ids), 22)

    def test_health_limits_all_open_wait_details_in_newest_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            waiting_ids = []
            for index in range(6):
                task = store.upsert_task(f"waiting-{index}", "", f"https://115cdn.com/s/waiting-{index}")
                task = store.record_event(
                    task.id,
                    TaskStage.ORGANIZING,
                    TaskStatus.PENDING,
                    f"waiting {index}",
                    title=f"Waiting {index}",
                    next_run_at=200.0,
                )
                waiting_ids.append(task.id)
            for index in range(100):
                task = store.upsert_task(f"done-{index}", "", f"https://115cdn.com/s/done-{index}")
                store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

            summary = build_task_health(store, enabled=True, limit=100, now=100.0)

            self.assertEqual(summary.pending_count, 6)
            self.assertEqual(summary.wait_overflow_count, 1)
            self.assertEqual(len(summary.wait_details), 5)
            self.assertIn(f"#{waiting_ids[-1]} Waiting 5", summary.wait_details[0])
            self.assertNotIn(f"#{waiting_ids[0]} Waiting 0", "\n".join(summary.wait_details))

    def test_health_reads_recent_count_from_the_same_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            for index in range(3):
                task = store.upsert_task(f"recent-{index}", "", f"https://115cdn.com/s/recent-{index}")
                store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

            with patch.object(store, "list_recent_tasks", side_effect=AssertionError("health must use one aggregate read")):
                summary = build_task_health(store, enabled=True, limit=2, now=100.0)

            self.assertEqual(summary.recent_count, 2)

    def test_health_formatters_share_explicit_clock_at_cooldown_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cooldown", "", "https://115cdn.com/s/cooldown")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "cooldown",
                metadata_patch={"p115_risk_cooldown_until": 100.0},
                next_run_at=100.0,
            )

            summary = build_task_health(store, enabled=True, now=100.0)
            report = format_task_health(summary, now=100.0)
            store_report = format_taskstore_health(store, enabled=True, now=100.0)

            self.assertEqual(summary.p115_cooldown_until, 0.0)
            self.assertIn("115风控冷却: inactive", report)
            self.assertIn("115风控冷却: inactive", store_report)
            self.assertNotIn("115风控冷却: ACTIVE", report)
            self.assertNotIn("115风控冷却: ACTIVE", store_report)


if __name__ == "__main__":
    unittest.main()
