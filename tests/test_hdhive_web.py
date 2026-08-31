import json
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import bridge
from app.clients.hdhive import HdhiveAccount
from app.hdhive_subscription_store import HdhiveSubscriptionStore
from app.hdhive_subscriptions import parse_hdhive_tv_url
from app.background_jobs import BackgroundJobCoordinator
from app.series_rules import parse_episode_filter
from app.task_store import TaskStore
from app.web import WebApp, start_web_server


class FakeHdhiveScheduler:
    def __init__(self):
        self.settings_calls = []
        self.run_now_calls = 0

    def status_snapshot(self):
        return {
            "enabled": True,
            "time": "01:30",
            "timezone": "Asia/Shanghai",
            "status": "idle",
            "next_run_at": "2026-07-25T01:30:00+08:00",
            "last_summary": {"enqueued": 2},
        }

    def update_settings(self, **kwargs):
        if kwargs["run_time"] == "25:00":
            raise ValueError("HDHIVE_SUBSCRIPTION_TIME must be a valid time")
        self.settings_calls.append(kwargs)
        return {"enabled": kwargs["enabled"], "time": kwargs["run_time"], "timezone": kwargs["timezone_name"]}

    def run_now(self):
        self.run_now_calls += 1
        return SimpleNamespace(status="succeeded")


class FakeHdhiveService:
    def __init__(self, store):
        self.store = store
        self.proxy = SimpleNamespace(
            account=lambda: HdhiveAccount("测试账号", 88, 3, False, "VIP", False, False)
        )
        self.check_calls = []
        self.confirm_calls = []
        self.filter_calls = []
        self.create_calls = []
        self.default_chat_id = "464100862"

    def list(self, chat_id=None):
        return self.store.list_subscriptions(chat_id)

    def create_from_url(self, chat_id, url):
        parsed = parse_hdhive_tv_url(url)
        self.create_calls.append(("url", str(chat_id), parsed.url))
        return self.store.create_subscription(
            str(chat_id),
            "hdhive_tv",
            parsed.slug,
            parsed.slug,
            "255358",
            source_url=parsed.url,
        )

    def create_from_tmdb(self, chat_id, tmdb_id, title):
        tmdb_id = str(tmdb_id or "").strip()
        if not tmdb_id.isdigit():
            raise ValueError("TMDB 剧集 ID 无效")
        self.create_calls.append(("tmdb", str(chat_id), tmdb_id, str(title or "")))
        return self.store.create_subscription(str(chat_id), "tmdb_tv", tmdb_id, title or tmdb_id, tmdb_id)

    def pause(self, subscription_id):
        return self.store.set_status(subscription_id, "paused")

    def resume(self, subscription_id):
        return self.store.set_status(subscription_id, "active")

    def delete(self, subscription_id):
        return self.store.set_status(subscription_id, "deleted")

    def check(self, subscription_id):
        self.check_calls.append(subscription_id)
        return SimpleNamespace(enqueued=1, discovered=2, pending_confirmation=0, failed=0)

    def confirm_item(self, item_id):
        self.confirm_calls.append(item_id)
        return SimpleNamespace(enqueued=1, pending_confirmation=0, failed=0)

    def set_episode_filter(self, subscription_id, value):
        parse_episode_filter(value)
        self.filter_calls.append((subscription_id, value))
        return self.store.update_episode_filter(subscription_id, value)


class HdhiveWebTests(unittest.TestCase):
    def test_stop_web_server_shuts_down_only_internally_owned_coordinator(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            owned_server = start_web_server(store, "127.0.0.1", 0)
            owned_coordinator = getattr(owned_server, "_cms_background_jobs", None)
            try:
                self.assertIsNotNone(owned_coordinator)
                bridge.stop_web_server(owned_server)
                self.assertEqual(owned_coordinator.submit("quality:run", lambda: None).outcome, "closed")
            finally:
                bridge.stop_web_server(owned_server)

            injected_coordinator = BackgroundJobCoordinator()
            injected_server = start_web_server(store, "127.0.0.1", 0, background_jobs=injected_coordinator)
            try:
                bridge.stop_web_server(injected_server)
                self.assertEqual(injected_coordinator.submit("quality:run", lambda: None).outcome, "accepted")
            finally:
                injected_coordinator.shutdown(wait=True)
                bridge.stop_web_server(injected_server)

    def test_maybe_start_web_server_keeps_legacy_positional_starter_slot(self):
        calls = []
        config = SimpleNamespace(
            web_enabled=True,
            web_token="secret",
            task_max_retries=3,
            web_host="127.0.0.1",
            web_port=0,
            frontend_dist_path="/tmp/frontend",
            self_share_receive_cid="",
            self_share_own_share_password="",
            cms_state_db_path="/tmp/cms.db",
        )

        def legacy_starter(*args, **kwargs):
            calls.append((args, kwargs))
            return "legacy-server"

        result = bridge.maybe_start_web_server(config, object(), None, None, None, None, None, legacy_starter)

        self.assertEqual(result, "legacy-server")
        self.assertEqual(len(calls), 1)

    def test_maybe_start_web_server_skips_positional_only_log_hub_for_starter(self):
        config = SimpleNamespace(
            web_enabled=True,
            web_token="secret",
            task_max_retries=3,
            web_host="127.0.0.1",
            web_port=0,
            frontend_dist_path="/tmp/frontend",
            self_share_receive_cid="",
            self_share_own_share_password="",
            cms_state_db_path="/tmp/cms.db",
        )

        def positional_only_starter(
            task_store,
            host,
            port,
            log_hub=None,
            /,
            *,
            web_token,
            task_engine_enabled,
            max_retries,
            frontend_dist_path,
            self_share_config,
        ):
            return log_hub

        result = bridge.maybe_start_web_server(
            config,
            object(),
            starter=positional_only_starter,
            log_hub=object(),
        )

        self.assertIsNone(result)

    def test_maybe_start_web_server_skips_positional_only_log_hub_even_with_var_keywords(self):
        config = SimpleNamespace(
            web_enabled=True,
            web_token="secret",
            task_max_retries=3,
            web_host="127.0.0.1",
            web_port=0,
            frontend_dist_path="/tmp/frontend",
            self_share_receive_cid="",
            self_share_own_share_password="",
            cms_state_db_path="/tmp/cms.db",
        )

        def positional_only_starter(task_store, host, port, log_hub=None, /, **kwargs):
            return log_hub, kwargs.get("log_hub")

        result = bridge.maybe_start_web_server(
            config,
            object(),
            starter=positional_only_starter,
            log_hub=object(),
        )

        self.assertEqual(result, (None, None))

    def make_app(self, *, background_jobs=None):
        directory = tempfile.TemporaryDirectory()
        store = HdhiveSubscriptionStore(Path(directory.name) / "tasks.db")
        subscription = store.create_subscription(
            "464100862",
            "hdhive_tv",
            "tv-slug-1",
            "攻壳机动队",
            "255358",
            source_url="https://hdhive.com/tv/tv-slug-1?password=legacy-secret",
        )
        item = store.upsert_item(
            subscription.id,
            "s01e02",
            "resource-1",
            "valid",
            2160,
            21,
            title="攻壳机动队 S01E02",
        )
        store.mark_item_pending(item.id, "积分超过自动解锁阈值或费用未知")
        scheduler = FakeHdhiveScheduler()
        service = FakeHdhiveService(store)
        return directory, WebApp(
            store,
            web_token="",
            hdhive_service=service,
            hdhive_scheduler=scheduler,
            background_jobs=background_jobs,
        ), service, scheduler, subscription, item

    def test_bridge_passes_hdhive_service_and_scheduler_to_web_server(self):
        calls = []
        config = SimpleNamespace(
            web_enabled=True,
            web_token="secret",
            web_host="127.0.0.1",
            web_port=8787,
        )
        service = object()
        scheduler = object()

        def starter(*args, **kwargs):
            calls.append((args, kwargs))
            return "server"

        result = bridge.maybe_start_web_server(
            config,
            object(),
            hdhive_service=service,
            hdhive_scheduler=scheduler,
            starter=starter,
        )

        self.assertEqual(result, "server")
        self.assertIs(calls[0][1]["hdhive_service"], service)
        self.assertIs(calls[0][1]["hdhive_scheduler"], scheduler)

    def test_hdhive_page_shows_account_subscriptions_schedule_and_pending_items(self):
        directory, app, _service, _scheduler, _subscription, _item = self.make_app()
        try:
            status, _headers, payload = app.handle_request("GET", "/hdhive", {}, b"")
        finally:
            directory.cleanup()

        page = payload.decode("utf-8")
        self.assertEqual(status, 200)
        for text in ("HDHive 订阅", "添加订阅", "测试账号", "88", "攻壳机动队", "TMDB：255358", "01:30", "发现 1", "待确认", "攻壳机动队 S01E02"):
            self.assertIn(text, page)
        self.assertNotIn("legacy-secret", page)
        self.assertIn("password=***", page)

    def test_hdhive_page_returns_clear_disabled_response_without_service(self):
        with tempfile.TemporaryDirectory() as directory:
            app = WebApp(TaskStore(Path(directory) / "tasks.db"), web_token="")
            status, _headers, payload = app.handle_request("GET", "/hdhive", {}, b"")

        self.assertEqual(status, 409)
        self.assertIn("HDHive 功能未启用", payload.decode("utf-8"))

    def test_hdhive_subscription_actions_update_service(self):
        directory, app, _service, _scheduler, subscription, _item = self.make_app()
        try:
            for action, expected_status in (("pause", "paused"), ("resume", "active"), ("delete", "deleted")):
                status, headers, _payload = app.handle_request(
                    "POST", f"/hdhive/subscriptions/{subscription.id}/{action}", {}, b""
                )
                self.assertEqual(status, 303)
                self.assertEqual(headers["Location"], "/hdhive")
                self.assertEqual(app.hdhive_service.store.get_subscription(subscription.id).status, expected_status)
        finally:
            directory.cleanup()

    def test_hdhive_check_runs_in_background_and_confirm_delegates(self):
        coordinator = BackgroundJobCoordinator()
        directory, app, service, _scheduler, subscription, item = self.make_app(background_jobs=coordinator)

        try:
            status, headers, _payload = app.handle_request(
                "POST", f"/hdhive/subscriptions/{subscription.id}/check", {}, b""
            )
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/hdhive")

            status, headers, _payload = app.handle_request(
                "POST", f"/hdhive/item/{item.id}/confirm", {}, b""
            )
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/hdhive")
            coordinator.shutdown(wait=True)
            self.assertEqual(service.check_calls, [subscription.id])
            self.assertEqual(service.confirm_calls, [item.id])
        finally:
            coordinator.shutdown(wait=True)
            directory.cleanup()

    def test_api_manual_jobs_deduplicate_duplicate_clicks(self):
        coordinator = BackgroundJobCoordinator()
        directory, app, service, scheduler, subscription, item = self.make_app(background_jobs=coordinator)
        subscription_id = 7
        started = threading.Event()
        release = threading.Event()

        def check(subscription_id):
            service.check_calls.append(subscription_id)
            started.set()
            release.wait(1)

        service.check = check
        try:
            first_status, _headers, first_body = app.handle_request(
                "POST", f"/api/v1/hdhive/subscriptions/{subscription_id}/check", {}, b""
            )
            self.assertTrue(started.wait(1))
            duplicate_responses = [
                app.handle_request("POST", f"/api/v1/hdhive/subscriptions/{subscription_id}/check", {}, b"")
                for _ in range(19)
            ]
            self.assertEqual(first_status, 202)
            self.assertEqual(__import__("json").loads(first_body)["job"]["outcome"], "accepted")
            self.assertTrue(all(status == 409 for status, _headers, _body in duplicate_responses))
            self.assertTrue(all(__import__("json").loads(body)["job"]["outcome"] == "already_running" for _status, _headers, body in duplicate_responses))
            self.assertEqual(service.check_calls, [7])

            scheduler.run_now = lambda: (started.clear(), started.set(), release.wait(1))
            release.clear()
            app.handle_request("POST", "/api/v1/hdhive/run", {}, b"")
            self.assertTrue(started.wait(1))
            runs = [app.handle_request("POST", "/api/v1/hdhive/run", {}, b"") for _ in range(19)]
            self.assertTrue(all(status == 409 for status, _headers, _body in runs))

            release.set()
            coordinator.shutdown(wait=True)
            coordinator = BackgroundJobCoordinator()
            app.background_jobs = coordinator
            started.clear()
            release.clear()
            service.confirm_item = lambda item_id: (service.confirm_calls.append(item_id), started.set(), release.wait(1))
            app.handle_request("POST", f"/api/v1/hdhive/items/{item.id}/confirm", {}, b"")
            self.assertTrue(started.wait(1))
            confirmations = [
                app.handle_request("POST", f"/api/v1/hdhive/items/{item.id}/confirm", {}, b"") for _ in range(19)
            ]
            self.assertTrue(all(status == 409 for status, _headers, _body in confirmations))
            self.assertEqual(service.confirm_calls, [item.id])
        finally:
            release.set()
            coordinator.shutdown(wait=True)
            directory.cleanup()

    def test_hdhive_settings_route_updates_scheduler(self):
        directory, app, _service, scheduler, _subscription, _item = self.make_app()
        try:
            status, headers, _payload = app.handle_request(
                "POST",
                "/hdhive/settings",
                {},
                b"enabled=false&time=03%3A15&timezone=UTC",
            )
        finally:
            directory.cleanup()

        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/hdhive")
        self.assertEqual(
            scheduler.settings_calls,
            [{"enabled": False, "run_time": "03:15", "timezone_name": "UTC"}],
        )

    def test_hdhive_settings_route_rejects_invalid_time(self):
        directory, app, _service, _scheduler, _subscription, _item = self.make_app()
        try:
            status, _headers, payload = app.handle_request(
                "POST",
                "/hdhive/settings",
                {},
                b"enabled=true&time=25%3A00&timezone=Asia%2FShanghai",
            )
        finally:
            directory.cleanup()

        self.assertEqual(status, 400)
        self.assertIn("valid time", payload.decode("utf-8"))

    def test_hdhive_legacy_filter_route_updates_filter(self):
        directory, app, service, _scheduler, subscription, _item = self.make_app()
        try:
            status, headers, _payload = app.handle_request(
                "POST",
                f"/hdhive/subscriptions/{subscription.id}/episode-filter",
                {},
                b"episode_filter=S02",
            )
        finally:
            directory.cleanup()

        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/hdhive")
        self.assertEqual(service.filter_calls, [(subscription.id, "S02")])

    def test_hdhive_legacy_filter_route_rejects_invalid_filter_without_changing_value(self):
        directory, app, service, _scheduler, subscription, _item = self.make_app()
        service.store.update_episode_filter(subscription.id, "S02")
        try:
            status, _headers, payload = app.handle_request(
                "POST",
                f"/hdhive/subscriptions/{subscription.id}/episode-filter",
                {},
                b"episode_filter=S01E",
            )
            current = service.store.get_subscription(subscription.id)
        finally:
            directory.cleanup()

        self.assertEqual(status, 400)
        self.assertIn("episode", payload.decode("utf-8"))
        self.assertEqual(current.episode_filter, "S02")

    def test_api_creates_subscription_from_hdhive_url(self):
        directory, app, service, _scheduler, _subscription, _item = self.make_app()
        try:
            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps({"url": "https://hdhive.com/tv/newshow01"}).encode("utf-8"),
            )
            payload = json.loads(body)
        finally:
            directory.cleanup()

        self.assertEqual(status, 200)
        self.assertEqual(payload["subscription"]["tmdb_id"], "255358")
        self.assertEqual(payload["subscription"]["title"], "newshow01")
        self.assertNotIn("source_url", payload["subscription"])
        self.assertEqual(
            service.create_calls,
            [("url", "464100862", "https://hdhive.com/tv/newshow01")],
        )

    def test_api_creates_subscription_from_tmdb_id(self):
        directory, app, service, _scheduler, _subscription, _item = self.make_app()
        try:
            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps({"tmdb_id": "1416", "title": "Grey's Anatomy"}).encode("utf-8"),
            )
            payload = json.loads(body)
        finally:
            directory.cleanup()

        self.assertEqual(status, 200)
        self.assertEqual(payload["subscription"]["tmdb_id"], "1416")
        self.assertEqual(payload["subscription"]["title"], "Grey's Anatomy")
        self.assertEqual(service.create_calls, [("tmdb", "464100862", "1416", "Grey's Anatomy")])

    def test_api_prefers_url_when_both_url_and_tmdb_are_provided(self):
        directory, app, service, _scheduler, _subscription, _item = self.make_app()
        try:
            status, _headers, _body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps(
                    {
                        "url": "https://hdhive.com/tv/preferurl",
                        "tmdb_id": "1416",
                        "title": "Ignored",
                    }
                ).encode("utf-8"),
            )
        finally:
            directory.cleanup()

        self.assertEqual(status, 200)
        self.assertEqual(service.create_calls[0][0], "url")

    def test_api_rejects_invalid_create_payloads(self):
        directory, app, service, _scheduler, _subscription, _item = self.make_app()
        try:
            empty_status, _headers, empty_body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                b"{}",
            )
            url_status, _headers, url_body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps({"url": "https://evil.example/tv/abc"}).encode("utf-8"),
            )
            tmdb_status, _headers, tmdb_body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps({"tmdb_id": "abc"}).encode("utf-8"),
            )
        finally:
            directory.cleanup()

        self.assertEqual(empty_status, 400)
        self.assertIn("HDHive 剧集链接", json.loads(empty_body)["error"])
        self.assertEqual(url_status, 400)
        self.assertIn("HDHive", json.loads(url_body)["error"])
        self.assertEqual(tmdb_status, 400)
        self.assertIn("TMDB", json.loads(tmdb_body)["error"])
        self.assertEqual(service.create_calls, [])

    def test_api_create_uses_existing_subscription_chat_id_when_default_missing(self):
        directory, app, service, _scheduler, _subscription, _item = self.make_app()
        service.default_chat_id = ""
        try:
            status, _headers, _body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps({"tmdb_id": "1396", "title": "Breaking Bad"}).encode("utf-8"),
            )
        finally:
            directory.cleanup()

        self.assertEqual(status, 200)
        self.assertEqual(service.create_calls, [("tmdb", "464100862", "1396", "Breaking Bad")])

    def test_api_create_rejects_missing_owner_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HdhiveSubscriptionStore(Path(tmp) / "tasks.db")
            service = FakeHdhiveService(store)
            service.default_chat_id = ""
            app = WebApp(TaskStore(Path(tmp) / "other.db"), web_token="", hdhive_service=service)
            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps({"tmdb_id": "1396", "title": "Breaking Bad"}).encode("utf-8"),
            )

        self.assertEqual(status, 400)
        self.assertIn("归属", json.loads(body)["error"])
        self.assertEqual(service.create_calls, [])

    def test_api_create_returns_unavailable_without_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"), web_token="")
            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/hdhive/subscriptions",
                {"Content-Type": "application/json"},
                json.dumps({"tmdb_id": "1416"}).encode("utf-8"),
            )

        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "hdhive_unavailable")

    def test_legacy_create_form_creates_subscription_and_redirects(self):
        directory, app, service, _scheduler, _subscription, _item = self.make_app()
        try:
            status, headers, _payload = app.handle_request(
                "POST",
                "/hdhive/subscriptions",
                {"Content-Type": "application/x-www-form-urlencoded"},
                b"url=https%3A%2F%2Fhdhive.com%2Ftv%2Flegacyslug",
            )
        finally:
            directory.cleanup()

        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/hdhive")
        self.assertEqual(
            service.create_calls,
            [("url", "464100862", "https://hdhive.com/tv/legacyslug")],
        )


if __name__ == "__main__":
    unittest.main()
