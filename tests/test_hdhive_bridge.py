import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import bridge
from app.clients.hdhive import HdhiveAccount, HdhiveResource, HdhiveUnlockItem
from app.hdhive import HdhiveSessionStore, HdhiveWorkflow
from app.hdhive_subscriptions import HdhiveSubscriptionService
from app.telegram_ui import hdhive_resource_keyboard


def resource(slug: str, pan_type: str = "115", points: int = 8) -> HdhiveResource:
    return HdhiveResource(
        slug=slug,
        title=slug,
        pan_type=pan_type,
        share_size="1GB",
        video_resolution=("1080P",),
        source=("WEB-DL",),
        subtitle_language=("简中",),
        subtitle_type=("内封",),
        unlock_points=points,
        validate_status="valid",
        validate_message="",
        is_unlocked=False,
    )


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def answer_callback_query(self, callback_id, text="", show_alert=False):
        self.answers.append((callback_id, text, show_alert))


class FakeProxy:
    def __init__(self):
        self.account_value = HdhiveAccount("Kale", 100, 0, False, "vip", False, False)
        self.items = [resource("115-item", "115"), resource("quark-item", "quark")]
        self.unlock_calls = []

    def account(self):
        return self.account_value

    def resources(self, media_type, tmdb_id):
        return self.items

    def unlock(self, slugs):
        self.unlock_calls.append(list(slugs))
        return [
            HdhiveUnlockItem("115-item", True, "https://115cdn.com/s/one?password=1111", "", "", False),
            HdhiveUnlockItem("quark-item", True, "https://pan.quark.cn/s/two", "", "", False),
        ]


class HdhiveBridgeTests(unittest.TestCase):
    def test_runtime_factory_is_disabled_without_config(self):
        config = SimpleNamespace(hdhive_enabled=False)

        self.assertIsNone(bridge.create_hdhive_workflow(config, object()))

    def test_runtime_factory_uses_cms_refresh_and_configured_session_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                hdhive_enabled=True,
                hdhive_proxy_base_url="https://proxy.test",
                hdhive_token_config_path=str(Path(directory) / "token.json"),
                hdhive_search_session_ttl_seconds=321,
                hdhive_auto_unlock_max_points=20,
                http_timeout=7,
            )
            class Cms:
                def get_hdhive_info(self):
                    return {"code": 200}

            cms = Cms()

            workflow = bridge.create_hdhive_workflow(config, cms)

            self.assertIsInstance(workflow, HdhiveWorkflow)
            self.assertEqual(workflow.sessions.ttl_seconds, 321)
            self.assertIs(workflow.proxy.refresh_via_cms.__self__, cms)

    def test_subscription_service_factory_is_disabled_with_hdhive(self):
        config = SimpleNamespace(hdhive_enabled=False)

        self.assertIsNone(bridge.create_hdhive_subscription_service(config, object(), lambda _urls, _chat: None))

    def test_subscription_service_factory_reuses_proxy_and_intake_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            callback = lambda _urls, _chat: None
            config = SimpleNamespace(
                hdhive_enabled=True,
                task_db_path=str(Path(directory) / "tasks.db"),
                hdhive_auto_unlock_max_points=20,
            )
            proxy = object()
            workflow = SimpleNamespace(proxy=proxy)

            service = bridge.create_hdhive_subscription_service(config, workflow, callback)

            self.assertIsInstance(service, HdhiveSubscriptionService)
            self.assertIs(service.proxy, proxy)
            self.assertIs(service.enqueue_links, callback)

    def test_unlock_callback_enqueues_only_successful_115_links(self):
        proxy = FakeProxy()
        workflow = HdhiveWorkflow(
            object(),
            proxy,
            HdhiveSessionStore(),
            auto_unlock_max_points=20,
        )
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        workflow.set_filter(session_id, "all")
        workflow.toggle_selection(session_id, 0)
        workflow.toggle_selection(session_id, 1)
        telegram = FakeTelegram()
        enqueued = []

        handled = bridge.handle_hdhive_callback(
            f"hive:unlock:{session_id}",
            "callback-1",
            "464100862",
            telegram,
            workflow,
            lambda urls, chat_id: enqueued.append((urls, chat_id)),
        )

        self.assertTrue(handled)
        self.assertEqual(proxy.unlock_calls, [["115-item", "quark-item"]])
        self.assertEqual(enqueued, [(["https://115cdn.com/s/one?password=1111"], "464100862")])
        self.assertIn("https://pan.quark.cn/s/two", telegram.messages[-1][1])

    def test_resource_keyboard_exposes_every_pan_type_and_single_unlock(self):
        resources = [resource("115", "115"), resource("quark", "quark"), resource("115-2", "115"), resource("pikpak", "pikpak")]

        keyboard = hdhive_resource_keyboard(
            "session",
            resources,
            [0, 1, 2, 3],
            [],
            ["115", "quark", "pikpak"],
            "115",
        )
        callbacks = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]

        self.assertIn("hive:filter:session:2", callbacks)
        self.assertIn("hive:single:session:3", callbacks)

    def test_invalid_single_unlock_does_not_clear_existing_selection(self):
        proxy = FakeProxy()
        proxy.items = [resource("good", "115"), resource("bad", "115")]
        proxy.items[1] = HdhiveResource(
            **{**proxy.items[1].__dict__, "validate_status": "invalid"}
        )
        workflow = HdhiveWorkflow(object(), proxy, HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        workflow.toggle_selection(session_id, 0)
        telegram = FakeTelegram()

        bridge.handle_hdhive_callback(
            f"hive:single:{session_id}:1",
            "callback-2",
            "464100862",
            telegram,
            workflow,
            None,
        )

        self.assertEqual(workflow.sessions.get(session_id).selected_indexes, [0])
        self.assertTrue(telegram.answers[-1][2])


if __name__ == "__main__":
    unittest.main()
