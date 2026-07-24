import json
import tempfile
import unittest
from pathlib import Path

from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore
from app.web import WebApp
from app.web_api import serialize_task


class WebApiTests(unittest.TestCase):
    def test_task_api_redacts_share_password_and_returns_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task(
                "abc",
                "secret",
                "https://115cdn.com/s/abc?password=secret&foo=bar",
                strm_mode="direct",
            )
            store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "已接收")
            app = WebApp(store)

            status, headers, body = app.handle_request("GET", f"/api/v1/tasks/{task.id}", {}, b"")
            payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(payload["strm_mode"], "direct")
        self.assertIn("password=***", payload["safe_url"])
        self.assertNotIn("secret", payload["safe_url"])
        self.assertEqual(payload["events"][0]["message"], "已接收")

    def test_task_mode_can_change_before_strm_stage_but_not_after_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc", strm_mode="shared")
            app = WebApp(store)

            status, _headers, _body = app.handle_request(
                "POST", f"/api/v1/tasks/{task.id}/strm-mode", {"Content-Type": "application/json"}, b'{"mode":"direct"}'
            )
            changed = store.find_task(task.id)
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.RUNNING, "locked")
            locked_status, _headers, locked_body = app.handle_request(
                "POST", f"/api/v1/tasks/{task.id}/strm-mode", {}, b"mode=shared"
            )

        self.assertEqual(status, 200)
        self.assertEqual(changed.metadata["strm_mode"], "direct")
        self.assertEqual(locked_status, 409)
        self.assertEqual(json.loads(locked_body)["code"], "strm_mode_locked")

    def test_default_mode_api_and_missing_frontend_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store, frontend_dist_path=Path(tmp) / "missing")
            status, _headers, body = app.handle_request(
                "POST", "/api/v1/settings/strm-mode", {"Content-Type": "application/json"}, b'{"mode":"direct"}'
            )
            frontend_status, _headers, frontend_body = app.handle_request("GET", "/app/", {}, b"")
            self.assertEqual(store.get_default_strm_mode(), "direct")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["strm_default_mode"], "direct")
        self.assertEqual(frontend_status, 404)
        self.assertIn(b"Frontend asset not found", frontend_body)

    def test_serialize_task_does_not_expose_secret_metadata(self):
        task = type(
            "Task",
            (),
            {
                "id": 1,
                "title": "x",
                "share_code": "x",
                "source_type": "share",
                "current_stage": TaskStage.RECEIVED,
                "status": TaskStatus.PENDING,
                "strm_mode": "shared",
                "category": "",
                "tmdb_id": "",
                "url": "https://115cdn.com/s/x?password=secret",
                "error_type": "",
                "error_summary": "",
                "retry_count": 0,
                "next_run_at": 0,
                "claimed_by": "",
                "metadata": {"own_share_url": "https://115cdn.com/s/x?password=secret", "source_path": "/safe"},
                "created_at": 0,
                "updated_at": 0,
            },
        )()
        payload = serialize_task(task)
        self.assertNotIn("secret", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["metadata"]["source_path"], "/safe")


if __name__ == "__main__":
    unittest.main()
