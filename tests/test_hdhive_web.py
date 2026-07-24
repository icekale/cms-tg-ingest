import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge
from app.clients.hdhive import HdhiveAccount
from app.hdhive_subscription_store import HdhiveSubscriptionStore
from app.task_store import TaskStore
from app.web import WebApp


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

    def list(self, chat_id=None):
        return self.store.list_subscriptions(chat_id)

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


class HdhiveWebTests(unittest.TestCase):
    def make_app(self):
        directory = tempfile.TemporaryDirectory()
        store = HdhiveSubscriptionStore(Path(directory.name) / "tasks.db")
        subscription = store.create_subscription(
            "464100862",
            "hdhive_tv",
            "tv-slug-1",
            "攻壳机动队",
            "255358",
            source_url="https://hdhive.com/tv/tv-slug-1",
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
        ), service, scheduler, subscription, item

    def test_bridge_passes_hdhive_service_and_scheduler_to_web_server(self):
        calls = []
        config = SimpleNamespace(
            web_enabled=True,
            web_token="",
            task_engine_enabled=True,
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
        for text in ("HDHive 订阅", "测试账号", "88", "攻壳机动队", "TMDB：255358", "01:30", "发现 1", "待确认", "攻壳机动队 S01E02"):
            self.assertIn(text, page)

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
        directory, app, service, _scheduler, subscription, item = self.make_app()

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        try:
            with patch("app.web.Thread", ImmediateThread):
                status, headers, _payload = app.handle_request(
                    "POST", f"/hdhive/subscriptions/{subscription.id}/check", {}, b""
                )
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/hdhive")
            self.assertEqual(service.check_calls, [subscription.id])

            status, headers, _payload = app.handle_request(
                "POST", f"/hdhive/item/{item.id}/confirm", {}, b""
            )
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/hdhive")
            self.assertEqual(service.confirm_calls, [item.id])
        finally:
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


if __name__ == "__main__":
    unittest.main()
