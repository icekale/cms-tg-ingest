import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def test_initializes_tasks_and_events_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path)

            conn = sqlite3.connect(db_path)
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()

            self.assertIn("tasks", tables)
            self.assertIn("task_events", tables)
            self.assertIs(store.db_path, db_path)

    def test_upsert_task_is_idempotent_by_share_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            first = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            second = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")

            self.assertEqual(first.id, second.id)
            self.assertEqual(second.current_stage, TaskStage.RECEIVED)
            self.assertEqual(second.status, TaskStatus.PENDING)

    def test_find_task_by_share_key_returns_only_matching_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            expected = store.upsert_task("series", "pass", "https://115cdn.com/s/series?password=pass")
            store.upsert_task("series", "other", "https://115cdn.com/s/series?password=other")

            found = store.find_task_by_share_key("series", "pass")

            self.assertEqual(found.id, expected.id)
            self.assertIsNone(store.find_task_by_share_key("missing", "pass"))

    def test_record_stage_event_updates_current_task_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            updated = store.record_event(
                task.id,
                TaskStage.CMS_SUBMITTED,
                TaskStatus.SUCCEEDED,
                "CMS submitted",
                title="示例电影",
                tmdb_id="12345",
                category="欧美电影",
            )
            events = store.list_events(task.id)

            self.assertEqual(updated.current_stage, TaskStage.CMS_SUBMITTED)
            self.assertEqual(updated.status, TaskStatus.SUCCEEDED)
            self.assertEqual(updated.title, "示例电影")
            self.assertEqual(updated.tmdb_id, "12345")
            self.assertEqual(updated.category, "欧美电影")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["message"], "CMS submitted")

    def test_repeated_running_event_updates_task_without_growing_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待 CMS 整理完成",
                metadata_patch={"first": "yes"},
                next_run_at=10.0,
                clear_claim=True,
            )
            updated = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待 CMS 整理完成",
                metadata_patch={"second": "yes"},
                next_run_at=25.0,
                clear_claim=True,
            )
            events = store.list_events(task.id)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["message"], "等待 CMS 整理完成")
            self.assertEqual(updated.next_run_at, 25.0)
            self.assertEqual(updated.metadata["first"], "yes")
            self.assertEqual(updated.metadata["second"], "yes")

    def test_compare_and_set_transition_records_initial_and_target_events_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            updated = store.compare_and_set_transition(
                task.id,
                TaskStage.RECEIVED,
                {TaskStatus.PENDING},
                require_unclaimed=True,
                target_stage=TaskStage.EMBY_CONFIRMED,
                target_status=TaskStatus.PENDING,
                initial_event_message="initial transition",
                target_event_message="queued transition",
                next_run_at=0,
                clear_errors=True,
                clear_claim=True,
            )
            events = store.list_events(task.id)

            self.assertIsNotNone(updated)
            self.assertEqual(
                [(event["stage"], event["status"], event["message"]) for event in events],
                [
                    (TaskStage.RECEIVED.value, TaskStatus.PENDING.value, "initial transition"),
                    (TaskStage.EMBY_CONFIRMED.value, TaskStatus.PENDING.value, "queued transition"),
                ],
            )
            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.next_run_at, 0)

    def test_compare_and_set_transition_can_override_initial_event_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

            store.compare_and_set_transition(
                task.id,
                TaskStage.CLEANED,
                {TaskStatus.SUCCEEDED},
                require_unclaimed=True,
                target_stage=TaskStage.EMBY_CONFIRMED,
                target_status=TaskStatus.PENDING,
                initial_event_message="restore requested",
                initial_event_stage=TaskStage.EMBY_CONFIRMED,
                target_event_message="restore queued",
                next_run_at=0,
            )

            self.assertEqual(
                [(event["stage"], event["status"], event["message"]) for event in store.list_events(task.id)[-2:]],
                [
                    (TaskStage.EMBY_CONFIRMED.value, TaskStatus.PENDING.value, "restore requested"),
                    (TaskStage.EMBY_CONFIRMED.value, TaskStatus.PENDING.value, "restore queued"),
                ],
            )

    def test_record_failure_stores_error_and_retry_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            failed = store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM not found",
                error_type="strm_missing",
                error_summary="未找到 STRM 文件夹",
                error_detail="checked /mnt/user/Unraid/strm/share",
                increment_retry=True,
            )

            self.assertEqual(failed.current_stage, TaskStage.STRM_READY)
            self.assertEqual(failed.status, TaskStatus.FAILED)
            self.assertEqual(failed.error_type, "strm_missing")
            self.assertEqual(failed.error_summary, "未找到 STRM 文件夹")
            self.assertEqual(failed.retry_count, 1)

    def test_list_recent_tasks_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            one = store.upsert_task("one", "", "https://115cdn.com/s/one")
            two = store.upsert_task("two", "", "https://115cdn.com/s/two")

            recent = store.list_recent_tasks(limit=2)

            self.assertEqual([task.id for task in recent], [two.id, one.id])

    def test_list_open_tasks_excludes_succeeded_and_returns_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.record_event(running.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "running")
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            succeeded = store.upsert_task("succeeded", "", "https://115cdn.com/s/succeeded")
            store.record_event(succeeded.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            manual = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "choose")

            open_tasks = store.list_open_tasks()

            self.assertEqual([task.id for task in open_tasks], [manual.id, failed.id, running.id, pending.id])
            self.assertEqual(
                [task.status for task in open_tasks],
                [TaskStatus.NEEDS_ACTION, TaskStatus.FAILED, TaskStatus.RUNNING, TaskStatus.PENDING],
            )

    def test_list_open_tasks_searches_status_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            statements = []
            original_connect = store._connect

            def traced_connect():
                conn = original_connect()
                conn.set_trace_callback(statements.append)
                return conn

            with patch.object(store, "_connect", side_effect=traced_connect):
                store.list_open_tasks()

            select_sql = next(
                statement.strip()
                for statement in statements
                if statement.lstrip().startswith("SELECT * FROM tasks")
            )
            conn = sqlite3.connect(store.db_path)
            try:
                plan = [str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {select_sql}")]
            finally:
                conn.close()
            normalized_plan = "\n".join(plan).upper()

            self.assertIn("STATUS IN", select_sql.upper())
            self.assertIn("SEARCH TASKS", normalized_plan)
            self.assertIn("IDX_TASKS_NEXT_RUN", normalized_plan)
            self.assertIn("STATUS", normalized_plan)
            self.assertNotIn("SCAN TASKS USING INDEX IDX_TASKS_UPDATED_AT", normalized_plan)

    def test_queue_summary_counts_statuses_and_lock_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            store.enqueue_task(pending.id, TaskStage.RECEIVED, next_run_at=0)
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.enqueue_task(running.id, TaskStage.ORGANIZING, next_run_at=0)
            store.claim_next_runnable("worker", now=0)
            waiting = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            store.record_event(
                waiting.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待资源锁",
                metadata_patch={"_lock_key": "115:global", "_lock_reason": "115/CMS 全局阶段", "_lock_waiting": True},
            )
            manual = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "请选择分类")
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing")

            summary = store.queue_summary(limit=10)

            self.assertEqual(summary.recent_count, 5)
            self.assertEqual(summary.pending_count, 1)
            self.assertEqual(summary.running_count, 2)
            self.assertEqual(summary.needs_action_count, 1)
            self.assertEqual(summary.failed_count, 1)
            self.assertEqual(summary.lock_wait_count, 1)
            self.assertEqual(summary.latest_lock_wait.id, waiting.id)

    def test_task_store_persists_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")

            updated = store.record_event(
                task.id,
                TaskStage.RECEIVED,
                TaskStatus.RUNNING,
                "已接收",
                submission_id=7,
                metadata_patch={"own_share_file_id": "fid-1", "emby_parent": "电影"},
            )

            self.assertEqual(updated.chat_id, "464100862")
            self.assertEqual(updated.submission_id, 7)
            self.assertEqual(updated.metadata["own_share_file_id"], "fid-1")
            self.assertEqual(updated.metadata["emby_parent"], "电影")

    def test_enqueue_and_claim_next_runnable_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, message="等待整理", next_run_at=1.0)

            early = store.claim_next_runnable("worker-1", now=0.5)
            claimed = store.claim_next_runnable("worker-1", now=1.0)
            second = store.claim_next_runnable("worker-2", now=1.0)

            self.assertIsNone(early)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(claimed.status, TaskStatus.RUNNING)
            self.assertEqual(claimed.claimed_by, "worker-1")
            self.assertIsNone(second)

    def test_failed_task_is_not_claimed_until_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "失败", error_summary="未找到 STRM")

            self.assertIsNone(store.claim_next_runnable("worker-1", now=10.0))

            store.enqueue_task(task.id, TaskStage.STRM_READY, message="手动重试", next_run_at=10.0)
            claimed = store.claim_next_runnable("worker-1", now=10.0)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.current_stage, TaskStage.STRM_READY)

    def test_pending_cleaned_task_is_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.CLEANED, message="等待清理", next_run_at=1.0)

            claimed = store.claim_next_runnable("worker-1", now=1.0)

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.current_stage, TaskStage.CLEANED)
            self.assertEqual(claimed.status, TaskStatus.RUNNING)

    def test_succeeded_cleaned_task_is_not_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "清理完成",
                next_run_at=1.0,
            )

            self.assertIsNone(store.claim_next_runnable("worker-1", now=1.0))

    def test_cross_instance_claim_does_not_double_claim_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)
            task = first_store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            first_store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)

            first_claim = first_store.claim_next_runnable("worker-1", now=1.0)
            second_claim = second_store.claim_next_runnable("worker-2", now=1.0)

            self.assertIsNotNone(first_claim)
            self.assertIsNone(second_claim)

    def test_default_stale_claim_timeout_is_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)

            first_claim = store.claim_next_runnable("worker-1", now=1.0)
            second_claim = store.claim_next_runnable("worker-2", now=1000.0)

            self.assertIsNotNone(first_claim)
            self.assertIsNone(second_claim)

    def test_record_event_preserves_claim_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("worker-1", now=1.0)

            updated = store.record_event(claimed.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "处理中")

            self.assertEqual(updated.claimed_by, "worker-1")
            self.assertEqual(updated.claimed_at, 1.0)

    def test_record_event_clear_claim_false_preserves_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("worker-1", now=1.0)

            updated = store.record_event(
                claimed.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "处理中",
                clear_claim=False,
            )

            self.assertEqual(updated.claimed_by, "worker-1")
            self.assertEqual(updated.claimed_at, 1.0)

    def test_clear_worker_claims_releases_previous_process_running_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("task-runner", now=1.0)
            self.assertEqual(claimed.claimed_by, "task-runner")

            released = store.clear_worker_claims("task-runner", now=10.0)
            updated = store.find_task(task.id)

            self.assertEqual(released, 1)
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.claimed_at, 0)
            self.assertEqual(updated.next_run_at, 10.0)

    def test_metadata_merge_preserves_existing_keys_and_ignores_none_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "收到", metadata_patch={"keep": "yes"})

            updated = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "整理",
                metadata_patch={"new": "value", "keep": None},
            )

            self.assertEqual(updated.metadata["keep"], "yes")
            self.assertEqual(updated.metadata["new"], "value")

    def test_cross_instance_metadata_patches_preserve_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)
            task = first_store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            first_store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "收到", metadata_patch={"first": "1"})
            updated = second_store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "整理", metadata_patch={"second": "2"})

            self.assertEqual(updated.metadata["first"], "1")
            self.assertEqual(updated.metadata["second"], "2")

    def test_legacy_schema_migrates_runtime_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        share_code TEXT NOT NULL,
                        receive_code TEXT NOT NULL DEFAULT '',
                        url TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        tmdb_id TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT '',
                        current_stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_type TEXT NOT NULL DEFAULT '',
                        error_summary TEXT NOT NULL DEFAULT '',
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(share_code, receive_code)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE task_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT NOT NULL DEFAULT '',
                        error_type TEXT NOT NULL DEFAULT '',
                        error_detail TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(id)
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO tasks (share_code, receive_code, url, current_stage, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("legacy", "", "https://115cdn.com/s/legacy", TaskStage.RECEIVED.value, TaskStatus.PENDING.value, 1.0, 1.0),
                )
                conn.commit()
            finally:
                conn.close()

            store = TaskStore(db_path)
            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            finally:
                conn.close()
            legacy_claim = store.claim_next_runnable("worker-1", now=10.0)
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc", chat_id="464100862")
            updated = store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "收到", submission_id=7)

            self.assertTrue({"chat_id", "submission_id", "next_run_at", "claimed_by", "claimed_at", "metadata_json"} <= columns)
            self.assertIsNone(legacy_claim)
            self.assertEqual(updated.chat_id, "464100862")
            self.assertEqual(updated.submission_id, 7)
