import tempfile
import unittest
from pathlib import Path

import bridge
from app.models import TaskStage, TaskStatus
from app.task_bridge import ensure_task_for_link
from app.task_store import TaskStore


class TaskStoreWorkflowEventTests(unittest.TestCase):
    def make_row(self):
        return {
            "id": 1,
            "share_code": "abc",
            "receive_code": "",
            "url": "https://115cdn.com/s/abc",
            "title": "示例电影",
            "status": "submitted",
        }

    def test_record_cms_status_event_maps_to_organized(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row = self.make_row()
            ensure_task_for_link(task_store, "abc", "", row["url"])

            bridge.sync_cms_status_task_event(task_store, row, status="done", title="示例电影")
            task = task_store.list_recent_tasks(limit=1)[0]

            self.assertEqual(task.current_stage, TaskStage.ORGANIZED)
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(task.title, "示例电影")

    def test_record_self_share_events_from_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row = self.make_row()
            row.update({"own_share_code": "own", "share_sync_status": "submitted", "own_share_file_name": "示例电影"})

            bridge.sync_self_share_task_events(task_store, row)
            task = task_store.list_recent_tasks(limit=1)[0]
            stages = [event["stage"] for event in task_store.list_events(task.id)]

            self.assertEqual(task.current_stage, TaskStage.SHARE_SYNC_SUBMITTED)
            self.assertIn("own_share_created", stages)
            self.assertIn("share_sync_submitted", stages)

    def test_record_move_event_handles_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row = self.make_row()
            moved = dict(row, move_status="moved", dest_path="/library/示例电影", category_final="欧美电影")
            failed = dict(row, move_status="error", move_error="目标目录不在媒体库白名单内")

            bridge.sync_move_task_event(task_store, moved)
            bridge.sync_move_task_event(task_store, failed)
            task = task_store.list_recent_tasks(limit=1)[0]
            events = task_store.list_events(task.id)

            self.assertEqual(events[-2]["stage"], "moved")
            self.assertEqual(events[-2]["status"], "succeeded")
            self.assertEqual(events[-1]["stage"], "moved")
            self.assertEqual(events[-1]["status"], "failed")
            self.assertEqual(task.error_summary, "目标目录不在媒体库白名单内")

    def test_record_emby_and_cleanup_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row = self.make_row()
            emby = dict(row, emby_status="confirmed", emby_title="示例电影", emby_parent="电影库")
            cleaned = dict(emby, cleanup_status="deleted")

            bridge.sync_emby_task_event(task_store, emby)
            bridge.sync_cleanup_task_event(task_store, cleaned)
            task = task_store.list_recent_tasks(limit=1)[0]
            stages = [event["stage"] for event in task_store.list_events(task.id)]

            self.assertEqual(task.current_stage, TaskStage.CLEANED)
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertEqual(stages[-2:], ["emby_confirmed", "cleaned"])


if __name__ == "__main__":
    unittest.main()
