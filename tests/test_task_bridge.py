import tempfile
import unittest
from pathlib import Path

from app.models import TaskStage, TaskStatus
from app.task_bridge import ensure_task_for_link, record_failure, record_submission_event, sync_task_from_submission
from app.task_store import TaskStore


class TaskBridgeTests(unittest.TestCase):
    def test_ensure_task_for_link_creates_received_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            task = ensure_task_for_link(store, "abc", "1234", "https://115cdn.com/s/abc?password=1234")
            events = store.list_events(task.id)

            self.assertEqual(task.share_code, "abc")
            self.assertEqual(task.receive_code, "1234")
            self.assertEqual(task.current_stage, TaskStage.RECEIVED)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["message"], "收到链接")

    def test_record_submission_event_updates_metadata_and_skips_exact_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            row = {
                "share_code": "abc",
                "receive_code": "",
                "url": "https://115cdn.com/s/abc",
                "title": "示例电影",
                "category_final": "欧美电影",
            }

            first = record_submission_event(store, row, TaskStage.CMS_SUBMITTED, TaskStatus.RUNNING, "已提交 CMS")
            second = record_submission_event(store, row, TaskStage.CMS_SUBMITTED, TaskStatus.RUNNING, "已提交 CMS")
            events = store.list_events(first.id)

            self.assertEqual(first.id, second.id)
            self.assertEqual(second.current_stage, TaskStage.CMS_SUBMITTED)
            self.assertEqual(second.status, TaskStatus.RUNNING)
            self.assertEqual(second.title, "示例电影")
            self.assertEqual(second.category, "欧美电影")
            self.assertEqual([event["message"] for event in events], ["已提交 CMS"])

    def test_sync_task_from_submission_maps_completed_fields_to_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            row = {
                "share_code": "abc",
                "receive_code": "",
                "url": "https://115cdn.com/s/abc",
                "title": "示例电影",
                "status": "done",
                "move_status": "moved",
                "emby_status": "confirmed",
                "cleanup_status": "deleted",
                "dest_path": "/library/示例电影",
                "emby_title": "示例电影",
                "emby_parent": "电影库",
            }

            task = sync_task_from_submission(store, row, message="同步完成状态")

            self.assertEqual(task.current_stage, TaskStage.CLEANED)
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertIn("同步完成状态", [event["message"] for event in store.list_events(task.id)])

    def test_record_failure_works_with_link_key_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = record_failure(
                store,
                {"share_code": "abc", "receive_code": "", "url": "https://115cdn.com/s/abc"},
                TaskStage.CMS_SUBMITTED,
                "CMS 提交失败",
                error_type="cms_submit_failed",
                error_detail="HTTP 500",
            )

            self.assertEqual(task.current_stage, TaskStage.CMS_SUBMITTED)
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error_type, "cms_submit_failed")
            self.assertEqual(task.error_summary, "CMS 提交失败")
            self.assertEqual(store.list_events(task.id)[0]["error_detail"], "HTTP 500")

    def test_none_task_store_is_noop(self):
        self.assertIsNone(ensure_task_for_link(None, "abc", "", "https://115cdn.com/s/abc"))
        self.assertIsNone(record_submission_event(None, {}, TaskStage.RECEIVED, TaskStatus.PENDING, "noop"))
        self.assertIsNone(record_failure(None, {}, TaskStage.FAILED, "noop"))
        self.assertIsNone(sync_task_from_submission(None, {}))


if __name__ == "__main__":
    unittest.main()
