import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from app.models import TaskStage, TaskStatus
from app.task_health import build_task_health, format_task_health, format_taskstore_health
from app.task_store import TaskStore


class TaskHealthTests(unittest.TestCase):
    def test_health_blank_title_uses_task_number_and_hides_share_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("secret-share", "", "https://115cdn.com/s/secret-share?password=health-password")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "waiting password=health-error",
                next_run_at=200.0,
            )
            report = format_task_health(build_task_health(store, enabled=True, now=100.0), now=100.0)
            self.assertIn(f"#{task.id} 任务 #{task.id}", report)
            self.assertNotIn("secret-share", report)
            self.assertNotIn("health-password", report)
            self.assertNotIn("health-error", report)

    def test_health_wait_detail_hides_uppercase_share_code_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            blocked = "health" + "code"
            task = store.upsert_task(blocked, "", "https://115cdn.com/s/health")
            task = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "waiting",
                title=blocked.upper(),
                metadata_patch={"_defer_message": f"等待 {blocked.upper()}", "_lock_waiting": True, "_lock_reason": blocked.upper()},
                next_run_at=200.0,
            )

            report = format_task_health(build_task_health(store, enabled=True, now=100.0), now=100.0)

            self.assertNotIn(blocked.upper(), report)
            self.assertIn(f"最近锁等待: #{task.id} 任务 #{task.id}", report)

    def test_health_bridge_document_hides_received_title_when_it_is_share_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("secret-share", "", "https://115cdn.com/s/secret-share")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "waiting",
                metadata_patch={"received_title": "secret-share"},
                next_run_at=200.0,
            )
            report = format_task_health(build_task_health(store, enabled=True, now=100.0), now=100.0)
            move_config = bridge.MoveConfig(source_roots=[Path(tmp)], library_roots={"测试": Path(tmp)}, stable_seconds=0)
            plain = bridge.format_health(move_config, True, True, task_health=report).to_plain()
            self.assertIn(f"任务 #{task.id}", plain)
            self.assertNotIn("secret-share", plain)

    def test_health_redacts_conflict_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            move_config = bridge.MoveConfig(
                source_roots=[Path(tmp)],
                library_roots={"测试": Path(tmp)},
                stable_seconds=0,
                conflict_policy="https://evil.test/policy?token=policy-token",
            )
            plain = bridge.format_health(move_config, True, True).to_plain()
            self.assertNotIn("evil.test", plain)
            self.assertNotIn("policy-token", plain)

    def test_health_fallback_title_and_bridge_details_redact_persisted_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            lock = store.upsert_task(
                "secret-share",
                "",
                "https://evil.test/share?password=share-password",
            )
            store.record_event(
                lock.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "lock",
                metadata_patch={
                    "_lock_waiting": True,
                    "_lock_reason": "lock https://evil.test/lock?token=lock-token",
                },
            )
            problem = store.upsert_task(
                "problem-share",
                "",
                "https://evil.test/problem?password=problem-password",
            )
            store.record_event(
                problem.id,
                TaskStage.FAILED,
                TaskStatus.FAILED,
                "failed",
                error_summary="failed https://evil.test/error?receive_code=error-code token=error-token",
            )
            summary = build_task_health(store, enabled=True, now=100.0)
            report = format_task_health(summary, now=100.0)
            move_config = bridge.MoveConfig(source_roots=[Path(tmp)], library_roots={"测试": Path(tmp)}, stable_seconds=0)
            document = bridge.format_health(move_config, True, True, task_health=report)
            plain = document.to_plain()

            self.assertIn(f"最近锁等待: #{lock.id} 任务 #{lock.id}", report)
            for secret in ("share-password", "lock-token", "error-code", "error-token", "evil.test"):
                self.assertNotIn(secret, plain)

    def test_health_redacts_short_numeric_code_in_error_but_keeps_task_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("share", "1234", "https://115cdn.com/s/share")
            store.record_event(task.id, TaskStage.FAILED, TaskStatus.FAILED, "error 1234", error_summary="error 1234")
            report = format_task_health(build_task_health(store, enabled=True, now=100.0), now=100.0)
            self.assertIn(f"任务 #{task.id}", report)
            self.assertNotIn("error 1234", report)

    def test_health_reports_fresh_task_runner_error_as_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state("task_runner", "error", updated_at=100.0)

            summary = build_task_health(store, enabled=True, now=100.0)
            report = format_task_health(summary, now=100.0)

            self.assertEqual(summary.runner_state, "error")
            self.assertFalse(summary.runner_heartbeat_stale)
            self.assertIn("TaskRunner心跳: error", report)

    def test_health_reports_stale_task_runner_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state("task_runner", "running", updated_at=1.0)

            summary = build_task_health(store, enabled=True, now=100.0)
            report = format_task_health(summary, now=100.0)

            self.assertEqual(summary.runner_heartbeat_at, 1.0)
            self.assertTrue(summary.runner_heartbeat_stale)
            self.assertIn("TaskRunner心跳: stale", report)

    def test_health_reports_fresh_runner_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state(
                "task_runner:activity",
                json.dumps(
                    {
                        "active_task_id": 7,
                        "active_stage": "organizing",
                        "active_since": 90.0,
                        "last_claim_attempt_at": 95.0,
                    }
                ),
                updated_at=99.0,
            )

            summary = build_task_health(store, enabled=True, now=100.0)
            report = format_task_health(summary, now=100.0)

            self.assertTrue(summary.runner_active)
            self.assertEqual(summary.runner_active_task_id, 7)
            self.assertEqual(summary.runner_active_stage, "organizing")
            self.assertIn("Runner当前: 处理任务 #7 (organizing", report)

    def test_health_reports_stale_runner_activity_as_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state(
                "task_runner:activity",
                json.dumps(
                    {
                        "active_task_id": 7,
                        "active_stage": "organizing",
                        "active_since": 1.0,
                        "last_claim_attempt_at": 2.0,
                    }
                ),
                updated_at=1.0,
            )

            summary = build_task_health(store, enabled=True, now=100.0)
            report = format_task_health(summary, now=100.0)

            self.assertFalse(summary.runner_active)
            self.assertEqual(summary.runner_active_task_id, 7)
            self.assertIn("Runner当前: idle", report)

    def test_health_handles_corrupt_runner_activity_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state("task_runner:activity", "{not json", updated_at=99.0)

            summary = build_task_health(store, enabled=True, now=100.0)

            self.assertFalse(summary.runner_active)
            self.assertEqual(summary.runner_active_task_id, 0)

    def test_health_reports_last_database_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state(
                "backup_last_result",
                json.dumps({"status": "succeeded", "files": ["/data/backups/tasks.db"]}),
            )

            report = format_taskstore_health(store, enabled=True, now=100.0)

        self.assertIn("数据库备份: succeeded", report)

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
