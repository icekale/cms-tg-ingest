import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.models import TaskStage, TaskStatus
from app.quality import QualityIssue
from app.hdhive_subscription_store import HdhiveSubscriptionStore
from app.quality_rules import QualityRuleMatch
from app.series_rules import parse_episode_filter
from app.task_store import TaskStore
from app.config import Config
from app.quality_automation import QualityAutomation
from app.web import WebApp
from app.web_api import _safe_error, _safe_url, serialize_health, serialize_task


class _ManualActionRuleEngine:
    def evaluate(self, task, issues, *, config=None):
        return QualityRuleMatch(
            rule_id="missing_destination",
            priority=60,
            risk_level="medium",
            reason="destination directory is missing",
            issue_codes=("missing_dest",),
            manual_actions=("view", "snooze", "ignore", "resume"),
            evidence=(str(task.metadata.get("dest_path") or ""),),
        )


class WebApiTests(unittest.TestCase):
    def _quality_service(self, tmp, store):
        root = Path(tmp) / "library"
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=False,
        )
        return QualityAutomation(store, config, allowed_roots=[root]), root

    def test_quality_api_exposes_rule_state_and_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            destination = root / "direct"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
            task = store.upsert_task("quality-api", "", "https://115cdn.com/s/quality-api")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                title="质量 API 任务",
                metadata_patch={
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                },
            )
            app = WebApp(store, quality_automation=quality)

            status, _headers, body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            payload = json.loads(body)
            item = payload["items"][0]

            self.assertEqual(status, 200)
            self.assertEqual(item["code"], "direct_strm")
            self.assertEqual(item["task_id"], task.id)
            self.assertEqual(item["title"], "质量 API 任务")
            self.assertEqual(item["rule_id"], "strm_mode_mismatch")
            self.assertIn("execute", item["available_actions"])
            self.assertNotIn("snooze", item["available_actions"])
            self.assertIn("strm_mode_mismatch", payload["rule_counts"])
            self.assertIn("automation", payload)

    def test_quality_action_api_validates_rule_and_returns_conflict_without_external_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            destination = root / "direct"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
            task = store.upsert_task("quality-action", "", "https://115cdn.com/s/quality-action")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                },
            )
            app = WebApp(store, quality_automation=quality)
            _, _, quality_body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            item = json.loads(quality_body)["items"][0]
            request = json.dumps(
                {
                    "task_id": task.id,
                    "rule_id": item["rule_id"],
                    "rule_version": item["rule_version"],
                    "action": "execute",
                    "actor": "tester",
                }
            ).encode()

            first_status, _, first_body = app.handle_request(
                "POST", "/api/v1/quality/action/execute", {"Content-Type": "application/json"}, request
            )
            second_status, _, second_body = app.handle_request(
                "POST", "/api/v1/quality/action/execute", {"Content-Type": "application/json"}, request
            )
            bad_status, _, bad_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/execute",
                {"Content-Type": "application/json"},
                json.dumps({**json.loads(request), "rule_id": "wrong"}).encode(),
            )

            self.assertEqual(first_status, 200)
            self.assertEqual(json.loads(first_body)["action"], "execute")
            self.assertEqual(second_status, 409)
            self.assertEqual(json.loads(second_body)["error"], "quality_action_conflict")
            self.assertEqual(bad_status, 409)
            self.assertEqual(json.loads(bad_body)["error"], "quality_rule_mismatch")

    def test_missing_quality_destination_exposes_reprocess_not_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            task = store.upsert_task("quality-missing", "", "https://115cdn.com/s/quality-missing")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={"dest_path": str(root / "missing"), "own_share_code": "own"},
            )
            app = WebApp(store, quality_automation=quality)
            _, _, body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            item = json.loads(body)["items"][0]

            self.assertIn("reprocess", item["available_actions"])
            self.assertNotIn("restore", item["available_actions"])

    def test_quality_api_orphan_issue_is_view_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            with patch(
                "app.web_api.scan_task_quality",
                return_value=[QualityIssue("missing_dest", "missing", "/private/path", 999, "孤立问题")],
            ):
                payload = __import__("app.web_api", fromlist=["api_quality"]).api_quality(store)

            item = payload["items"][0]
            self.assertEqual(item["manual_status"], "manual_required")
            self.assertEqual(item["available_actions"], ["view"])
            self.assertEqual(payload["manual_count"], 1)

    def test_quality_action_api_supports_ignore_and_resume_and_rejects_invalid_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            task = store.upsert_task("quality-ignore", "", "https://115cdn.com/s/quality-ignore")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={"dest_path": str(root / "missing"), "own_share_code": "own"},
            )
            quality.rule_engine = _ManualActionRuleEngine()
            app = WebApp(store, quality_automation=quality)
            _, _, body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            item = json.loads(body)["items"][0]
            base = {
                "task_id": task.id,
                "rule_id": item["rule_id"],
                "rule_version": item["rule_version"],
            }
            ignored_status, _, ignored_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/ignore",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "ignore"}).encode(),
            )
            resumed_status, _, resumed_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/resume",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "resume"}).encode(),
            )
            invalid_status, _, invalid_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/ignore",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "execute"}).encode(),
            )

            self.assertEqual(ignored_status, 200)
            self.assertEqual(json.loads(ignored_body)["status"], "ignored")
            self.assertEqual(resumed_status, 200)
            self.assertEqual(json.loads(resumed_body)["status"], "resumed")
            self.assertEqual(invalid_status, 409)
            self.assertEqual(json.loads(invalid_body)["error"], "quality_action_not_allowed")
    def test_health_api_exposes_runner_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state("task_runner", "error", updated_at=100.0)

            payload = serialize_health(store, enabled=True, now=100.0)

        self.assertEqual(payload["runner_state"], "error")

    def test_hdhive_sensitive_url_and_error_variants_are_redacted(self):
        for key in (
            "share_password",
            "share_pwd",
            "refresh_token",
            "hdhive_token",
            "api_key",
            "auth_token",
            "bearer_token",
            "session_token",
            "csrf_token",
            "p115_cookie",
        ):
            with self.subTest(key=key):
                self.assertNotIn("variant-secret", _safe_url(f"https://hdhive.test/tv/x?{key}=variant-secret"))
                self.assertNotIn("variant-secret", _safe_error(f"{key}=variant-secret"))
        self.assertNotIn("user-secret", _safe_url("https://user:user-secret@hdhive.test/tv/x"))

    def test_health_api_reports_last_database_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state(
                "backup_last_result",
                json.dumps({"status": "partial", "files": ["/data/backups/tasks.db"], "error": "missing source"}),
            )

            payload = serialize_health(store, enabled=True, now=100.0)

        self.assertEqual(payload["backup"]["status"], "partial")
        self.assertEqual(payload["backup"]["error"], "missing source")

    def test_hdhive_filter_api_returns_safe_subscription_summary_and_skip_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            subscription_store = HdhiveSubscriptionStore(Path(tmp) / "hdhive.db")
            subscription = subscription_store.create_subscription(
                "464100862",
                "tmdb_tv",
                "1416",
                "剧集",
                "1416",
                source_url="https://hdhive.com/tv/secret-page",
            )
            item = subscription_store.upsert_item(subscription.id, "S01E01", "resource-1", "valid", 1080, 8)
            subscription_store.mark_item_skipped(item.id, "filtered", "不在过滤范围")
            subscription_store.record_check(subscription.id, "password=subscription-secret token=subscription-token")
            error_item = subscription_store.upsert_item(subscription.id, "S01E02", "resource-2", "valid", 1080, 8)
            subscription_store.mark_item_failed(error_item.id, "password=item-secret token=item-token")

            class Service:
                store = subscription_store

                def list(self):
                    return self.store.list_subscriptions()

                def set_episode_filter(self, subscription_id, value):
                    parse_episode_filter(value)
                    return self.store.update_episode_filter(subscription_id, value)

            app = WebApp(task_store, hdhive_service=Service())
            status, _headers, body = app.handle_request(
                "POST",
                f"/api/v1/hdhive/subscriptions/{subscription.id}/episode-filter",
                {"Content-Type": "application/json"},
                b'{"episode_filter":"S02"}',
            )
            payload = json.loads(body)

        self.assertEqual(status, 200)
        row = payload["subscription"]
        self.assertEqual(row["episode_filter"], "S02")
        self.assertIn("last_summary_json", row)
        self.assertFalse(row["completed"])
        self.assertEqual(row["items"][0]["skip_reason"], "不在过滤范围")
        self.assertNotIn("source_url", row)
        self.assertNotIn("subscription-secret", json.dumps(row, ensure_ascii=False))
        self.assertNotIn("item-secret", json.dumps(row, ensure_ascii=False))
        self.assertIn("password=***", row["last_error"])

    def test_hdhive_filter_api_rejects_invalid_filter_without_changing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            subscription_store = HdhiveSubscriptionStore(Path(tmp) / "hdhive.db")
            subscription = subscription_store.create_subscription("464100862", "tmdb_tv", "1416", "剧集", "1416")

            class Service:
                store = subscription_store

                def list(self):
                    return self.store.list_subscriptions()

                def set_episode_filter(self, subscription_id, value):
                    parse_episode_filter(value)
                    return self.store.update_episode_filter(subscription_id, value)

            app = WebApp(task_store, hdhive_service=Service())
            status, _headers, body = app.handle_request(
                "POST",
                f"/api/v1/hdhive/subscriptions/{subscription.id}/episode-filter",
                {"Content-Type": "application/json"},
                b'{"episode_filter":"S01E"}',
            )
            current = subscription_store.get_subscription(subscription.id)

        self.assertEqual(status, 400)
        self.assertEqual(current.episode_filter, "")
        self.assertIn("episode", json.loads(body)["error"])
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

    def test_settings_api_exposes_three_strm_modes_and_program_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store)

            status, _headers, body = app.handle_request("GET", "/api/v1/settings", {}, b"")
            payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["app_name"], "cms-tg-ingest")
        self.assertRegex(payload["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(
            [item["value"] for item in payload["strm_modes"]],
            ["shared", "direct", "source_shared"],
        )

    def test_own_share_receive_code_api_masks_reads_and_supports_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            config = SimpleNamespace(
                cms_state_db_path=Path(tmp) / "missing-cms.db",
                own_share_receive_code="env7",
            )
            app = WebApp(store, self_share_config=config)

            initial_status, _headers, initial_body = app.handle_request("GET", "/api/v1/overview", {}, b"")
            set_status, _headers, set_body = app.handle_request(
                "POST",
                "/api/v1/settings/own-share-receive-code",
                {"Content-Type": "application/json"},
                b'{"receive_code":"web9"}',
            )
            clear_status, _headers, clear_body = app.handle_request(
                "POST",
                "/api/v1/settings/own-share-receive-code",
                {"Content-Type": "application/json"},
                b'{"clear":true}',
            )

        initial = json.loads(initial_body)["own_share_receive_code"]
        updated = json.loads(set_body)["own_share_receive_code"]
        cleared = json.loads(clear_body)["own_share_receive_code"]
        self.assertEqual(initial_status, 200)
        self.assertEqual(initial, {"configured": True, "masked": "****", "source": "env"})
        self.assertEqual(set_status, 200)
        self.assertEqual(updated, {"configured": True, "masked": "****", "source": "web"})
        self.assertEqual(clear_status, 200)
        self.assertEqual(cleared, initial)
        for body in (initial_body, set_body, clear_body):
            self.assertNotIn("env7", body.decode("utf-8"))
            self.assertNotIn("web9", body.decode("utf-8"))

    def test_own_share_receive_code_api_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"))

            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/settings/own-share-receive-code",
                {"Content-Type": "application/json"},
                b'{"receive_code":"12-12"}',
            )

        self.assertEqual(status, 400)
        self.assertIn("字母和数字", json.loads(body)["error"])

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
