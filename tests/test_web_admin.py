import tempfile
import unittest
from pathlib import Path

from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore
from app.web import WebApp, render_task_detail, render_task_list


class WebAdminTests(unittest.TestCase):
    def test_render_task_list_contains_task_stage_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM missing",
                title="示例电影",
                error_type="strm_missing",
                error_summary="未找到 STRM",
            )

            html = render_task_list(store)

            self.assertIn("示例电影", html)
            self.assertIn("STRM 生成", html)
            self.assertIn("未找到 STRM", html)
            self.assertIn(f"/task/{task.id}", html)

    def test_render_task_detail_contains_event_timeline_and_retry_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.CMS_SUBMITTED, TaskStatus.SUCCEEDED, "CMS submitted")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", error_summary="未找到 STRM")

            html = render_task_detail(store, task.id)

            self.assertIn("CMS submitted", html)
            self.assertIn("STRM missing", html)
            self.assertIn(f'action="/task/{task.id}/retry"', html)
            self.assertIn("重试当前阶段", html)

    def test_retry_endpoint_records_retry_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", error_summary="未找到 STRM")
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", f"/task/{task.id}/retry", {}, b"")
            updated = store.find_task(task.id)
            events = store.list_events(task.id)

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.retry_count, 1)
            self.assertTrue(any(event["message"] == "手动触发重试" for event in events))
            self.assertEqual(body, b"")

    def test_web_token_blocks_requests_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store, web_token="secret")

            status, headers, body = app.handle_request("GET", "/", {}, b"")

            self.assertEqual(status, 403)
            self.assertIn(b"Forbidden", body)


if __name__ == "__main__":
    unittest.main()
