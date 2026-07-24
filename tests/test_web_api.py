import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_frontend_history_route_falls_back_to_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<div id='app'>ok</div>", encoding="utf-8")
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"), frontend_dist_path=dist)
            status, _headers, body = app.handle_request("GET", "/app/tasks", {}, b"")

        self.assertEqual(status, 200)
        self.assertIn(b"id='app'", body)

    def test_root_redirects_to_vue_frontend_and_legacy_overview_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"))

            root_status, root_headers, root_body = app.handle_request("GET", "/", {}, b"")
            legacy_status, _legacy_headers, legacy_body = app.handle_request("GET", "/legacy", {}, b"")

        self.assertEqual(root_status, 302)
        self.assertEqual(root_headers["Location"], "/app/")
        self.assertEqual(root_body, b"")
        self.assertEqual(legacy_status, 200)
        self.assertIn("运行概览".encode("utf-8"), legacy_body)

    def test_task_action_api_reuses_existing_transition_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "失败", error_summary="重试我")
            app = WebApp(store)

            status, _headers, body = app.handle_request("POST", f"/api/v1/tasks/{task.id}/actions/retry", {}, b"")
            missing_status, _headers, missing_body = app.handle_request("POST", "/api/v1/tasks/999/actions/retry", {}, b"")

            succeeded = store.find_task(task.id)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "pending")
        self.assertEqual(succeeded.status, TaskStatus.PENDING)
        self.assertEqual(missing_status, 404)
        self.assertEqual(json.loads(missing_body)["error"], "task_not_found")

    def test_history_and_quality_action_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            completed = store.upsert_task("completed", "", "https://115cdn.com/s/completed")
            store.record_event(completed.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "完成")
            quality = Mock()
            quality.status_snapshot.return_value = {"enabled": True, "time": "02:50"}
            quality.update_settings.return_value = {"enabled": True, "time": "03:10"}
            app = WebApp(store, quality_automation=quality)

            clear_status, _headers, clear_body = app.handle_request("POST", "/api/v1/history/clear", {}, b"")
            settings_status, _headers, settings_body = app.handle_request(
                "POST",
                "/api/v1/quality/settings",
                {"Content-Type": "application/json"},
                b'{"enabled":true,"time":"03:10","timezone":"Asia/Shanghai","max_tasks":10,"check_limit":2}',
            )
            with patch("app.web.Thread") as thread_cls:
                run_status, _headers, run_body = app.handle_request("POST", "/api/v1/quality/run", {}, b"")

        self.assertEqual(clear_status, 200)
        self.assertEqual(json.loads(clear_body)["cleared"], 1)
        self.assertEqual(settings_status, 200)
        quality.update_settings.assert_called_once_with(
            enabled=True,
            run_time="03:10",
            timezone_name="Asia/Shanghai",
            max_tasks=10,
            check_limit=2,
        )
        self.assertEqual(run_status, 202)
        self.assertTrue(json.loads(run_body)["started"])
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once()

    def test_hdhive_action_api_delegates_to_existing_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            service = Mock()
            scheduler = Mock()
            scheduler.settings.return_value = {"enabled": True, "time": "01:30", "timezone": "Asia/Shanghai"}
            scheduler.update_settings.return_value = {"enabled": False, "time": "02:00", "timezone": "Asia/Shanghai"}
            app = WebApp(store, hdhive_service=service, hdhive_scheduler=scheduler)

            pause_status, _headers, _body = app.handle_request("POST", "/api/v1/hdhive/subscriptions/7/pause", {}, b"")
            confirm_status, _headers, _body = app.handle_request("POST", "/api/v1/hdhive/items/8/confirm", {}, b"")
            settings_status, _headers, settings_body = app.handle_request(
                "POST",
                "/api/v1/hdhive/settings",
                {"Content-Type": "application/json"},
                b'{"enabled":false,"time":"02:00","timezone":"Asia/Shanghai"}',
            )
            with patch("app.web.Thread") as thread_cls:
                run_status, _headers, run_body = app.handle_request("POST", "/api/v1/hdhive/run", {}, b"")

        self.assertEqual(pause_status, 200)
        service.pause.assert_called_once_with(7)
        self.assertEqual(confirm_status, 202)
        service.confirm_item.assert_called_once_with(8)
        self.assertEqual(settings_status, 200)
        scheduler.update_settings.assert_called_once_with(enabled=False, run_time="02:00", timezone_name="Asia/Shanghai")
        self.assertEqual(json.loads(settings_body)["settings"]["time"], "02:00")
        self.assertEqual(run_status, 202)
        self.assertTrue(json.loads(run_body)["started"])
        thread_cls.return_value.start.assert_called_once()

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
