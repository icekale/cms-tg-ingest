import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def test_initializes_tasks_and_events_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path)

            with sqlite3.connect(db_path) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

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
