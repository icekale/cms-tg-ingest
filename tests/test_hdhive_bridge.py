import tempfile
import threading
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

import bridge
from app.background_jobs import BackgroundJobCoordinator
from app.clients.hdhive import HdhiveProxyError, HdhiveAccount, HdhiveResource, HdhiveUnlockItem
from app.hdhive import HdhiveSessionStore, HdhiveWorkflow
from app.hdhive_subscriptions import HdhiveSubscriptionService
from app.models import TaskStage, TaskStatus
from app.telegram_ui import (
    format_hdhive_candidate_label,
    format_hdhive_subscriptions,
    hdhive_candidate_keyboard,
    hdhive_resource_keyboard,
    truncate_end,
)


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
        self.rich_messages = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def send_rich_message(self, chat_id, document, reply_markup=None):
        self.rich_messages.append((chat_id, document, reply_markup))
        self.messages.append((chat_id, document.to_plain(), reply_markup))

    def answer_callback_query(self, callback_id, text="", show_alert=False):
        self.answers.append((callback_id, text, show_alert))


class FakeSubscriptionService:
    def __init__(self):
        self.created_urls = []
        self.paused = []
        self.filters = []
        self.store = SimpleNamespace(
            list_items=lambda _subscription_id: [],
            get_subscription=lambda _subscription_id: self.subscription,
        )
        self.subscription = SimpleNamespace(
            id=1,
            title="攻壳机动队",
            tmdb_id="255358",
            chat_id="464100862",
            source_url="https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5",
            status="active",
            last_error="",
            episode_filter="",
            last_summary_json="{}",
        )

    def create_from_url(self, chat_id, url):
        self.created_urls.append((str(chat_id), url))
        return self.subscription

    def list(self, _chat_id):
        return [self.subscription]

    def pause(self, subscription_id):
        self.paused.append(subscription_id)
        self.subscription.status = "paused"
        return self.subscription

    def set_episode_filter(self, subscription_id, value):
        self.filters.append((subscription_id, value))
        self.subscription.episode_filter = value
        return self.subscription


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
    def test_subscription_check_callback_acknowledges_before_blocking_work_finishes(self):
        telegram = FakeTelegram()
        service = FakeSubscriptionService()
        started = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        coordinator = BackgroundJobCoordinator()

        def check(_subscription_id):
            started.set()
            release.wait(1)
            return SimpleNamespace(discovered=1, enqueued=1, pending_confirmation=0, failed=0)

        service.check = check

        def invoke():
            bridge.handle_hdhive_subscription_callback(
                "hsub:check:1",
                "callback-check",
                "464100862",
                telegram,
                service,
                None,
                background_jobs=coordinator,
            )
            returned.set()

        callback_thread = threading.Thread(target=invoke)
        callback_thread.start()
        try:
            self.assertTrue(returned.wait(0.5))
            self.assertTrue(started.wait(1))
            self.assertEqual(telegram.answers[-1][1], "已开始检查")
            self.assertFalse(callback_thread.is_alive())
            release.set()
            coordinator.shutdown(wait=True)
            self.assertIn("检查完成：已入队 1 个", telegram.messages[-1][1])
            self.assertIn("发现 1", telegram.messages[-1][1])
        finally:
            release.set()
            coordinator.shutdown(wait=True)
            callback_thread.join(timeout=1)

    def test_subscription_check_ack_failure_does_not_change_business_result(self):
        class AckFailTelegram(FakeTelegram):
            def answer_callback_query(self, callback_id, text="", show_alert=False):
                raise RuntimeError("Cannot reach Telegram")

        telegram = AckFailTelegram()
        service = FakeSubscriptionService()
        service.check = lambda _subscription_id: SimpleNamespace(
            discovered=1,
            enqueued=1,
            pending_confirmation=0,
            failed=0,
        )

        handled = bridge.handle_hdhive_subscription_callback(
            "hsub:check:1",
            "callback-check",
            "464100862",
            telegram,
            service,
            None,
        )

        self.assertTrue(handled)
        self.assertIn("检查完成：已入队 1 个", telegram.messages[-1][1])
        self.assertIn("发现 1", telegram.messages[-1][1])

    def test_completed_subscription_renders_status_filter_and_summary(self):
        subscription = SimpleNamespace(
            id=1,
            title="剧集",
            tmdb_id="1416",
            source_url="",
            status="completed",
            last_error="",
            episode_filter="S01E01-S01E03",
            last_summary_json=json.dumps(
                {"discovered": 3, "enqueued": 1, "filtered": 1, "emby_exists": 1},
                ensure_ascii=False,
            ),
        )

        text = format_hdhive_subscriptions([subscription]).to_plain()

        self.assertIn("已完结", text)
        self.assertIn("S01E01-S01E03", text)
        self.assertIn("发现 3", text)
        self.assertIn("入队 1", text)
        self.assertIn("已入队 1 个", text)
        self.assertIn("details", [block["type"] for block in format_hdhive_subscriptions([subscription]).to_blocks()])

    def test_subscription_list_uses_diagnosis_and_omits_misleading_emby_warning(self):
        subscription = SimpleNamespace(
            id=1,
            title="剧集",
            tmdb_id="1416",
            source_url="",
            status="active",
            last_error="",
            episode_filter="",
            last_summary_json=json.dumps(
                {
                    "discovered": 2,
                    "enqueued": 0,
                    "emby_skip_unavailable": True,
                    "unparsed": 0,
                    "blocked": 2,
                },
                ensure_ascii=False,
            ),
        )
        items = [SimpleNamespace(status="discovered", skip_reason="", last_error="")]

        text = format_hdhive_subscriptions([subscription], items_by_subscription_id={1: items}).to_plain()

        self.assertIn("未入队：Emby 查询失败，已停止自动解锁", text)
        self.assertIn("无法识别 0", text)
        self.assertIn("阻塞 2", text)
        self.assertNotIn("未据此跳过资源", text)

    def test_subscription_filter_callback_prompts_for_input(self):
        telegram = FakeTelegram()
        service = FakeSubscriptionService()

        bridge.handle_callback_query(
            {
                "id": "callback-filter",
                "from": {"id": "464100862"},
                "message": {"chat": {"id": "464100862"}},
                "data": "hsub:filter:1",
            },
            telegram,
            "464100862",
            object(),
            hdhive_subscription_service=service,
        )

        self.assertIn("请发送集数过滤", telegram.messages[-1][1])

    def test_invalid_subscription_filter_input_keeps_pending_state(self):
        telegram = FakeTelegram()
        service = FakeSubscriptionService()
        bridge.handle_callback_query(
            {
                "id": "callback-filter",
                "from": {"id": "464100862"},
                "message": {"chat": {"id": "464100862"}},
                "data": "hsub:filter:1",
            },
            telegram,
            "464100862",
            object(),
            hdhive_subscription_service=service,
        )

        bridge.handle_update(
            {
                "message": {
                    "chat": {"id": "464100862"},
                    "from": {"id": "464100862"},
                    "text": "S01E",
                }
            },
            object(),
            telegram,
            "464100862",
            object(),
            poll_status=False,
            hdhive_subscription_service=service,
        )

        self.assertIn("格式不正确", telegram.messages[-1][1])
        self.assertEqual(service.filters, [])

    def test_clear_subscription_filter_consumes_pending_input(self):
        telegram = FakeTelegram()
        service = FakeSubscriptionService()
        bridge.handle_callback_query(
            {
                "id": "callback-filter",
                "from": {"id": "464100862"},
                "message": {"chat": {"id": "464100862"}},
                "data": "hsub:filter:1",
            },
            telegram,
            "464100862",
            object(),
            hdhive_subscription_service=service,
        )

        bridge.handle_update(
            {
                "message": {
                    "chat": {"id": "464100862"},
                    "from": {"id": "464100862"},
                    "text": "清除",
                }
            },
            object(),
            telegram,
            "464100862",
            object(),
            poll_status=False,
            hdhive_subscription_service=service,
        )

        self.assertEqual(service.filters, [(1, "")])
        self.assertIn("已清除集数过滤", telegram.messages[-1][1])

    def test_chinese_search_command_starts_hdhive_search(self):
        telegram = FakeTelegram()
        workflow = SimpleNamespace(
            sessions=HdhiveSessionStore()
        )

        bridge.handle_update(
            {
                "message": {
                    "chat": {"id": "464100862"},
                    "from": {"id": "464100862"},
                    "text": "/搜索",
                }
            },
            object(),
            telegram,
            "464100862",
            object(),
            poll_status=False,
            hdhive_workflow=workflow,
        )

        self.assertIn("请输入片名或 TMDB ID", telegram.messages[-1][1])
        self.assertTrue(telegram.messages[-1][1].split("本次搜索编号：", 1)[1])

    def test_chinese_subscription_command_requires_a_hdhive_url(self):
        telegram = FakeTelegram()
        service = FakeSubscriptionService()

        bridge.handle_update(
            {
                "message": {
                    "chat": {"id": "464100862"},
                    "from": {"id": "464100862"},
                    "text": "/订阅",
                }
            },
            object(),
            telegram,
            "464100862",
            object(),
            poll_status=False,
            hdhive_subscription_service=service,
        )

        self.assertIn("用法：/订阅", telegram.messages[-1][1])

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
                tg_allowed_chat_id="464100862",
            )
            proxy = object()
            workflow = SimpleNamespace(proxy=proxy)

            service = bridge.create_hdhive_subscription_service(config, workflow, callback)

            self.assertIsInstance(service, HdhiveSubscriptionService)
            self.assertIs(service.proxy, proxy)
            self.assertIs(service.enqueue_links, callback)
            self.assertEqual(service.default_chat_id, "464100862")

    def test_subscription_service_factory_passes_tmdb_and_emby_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                hdhive_enabled=True,
                task_db_path=str(Path(directory) / "tasks.db"),
                hdhive_auto_unlock_max_points=20,
            )
            tmdb = object()
            emby = object()

            service = bridge.create_hdhive_subscription_service(
                config,
                SimpleNamespace(proxy=object()),
                lambda _urls, _chat: None,
                tmdb_resolver=tmdb,
                emby=emby,
            )

            self.assertIs(service.tmdb_resolver, tmdb)
            self.assertIs(service.emby, emby)

    def test_subscription_service_factory_passes_completion_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = lambda *_args: None
            config = SimpleNamespace(
                hdhive_enabled=True,
                task_db_path=str(Path(directory) / "tasks.db"),
                hdhive_auto_unlock_max_points=20,
            )

            service = bridge.create_hdhive_subscription_service(
                config,
                SimpleNamespace(proxy=object()),
                lambda _urls, _chat: None,
                on_subscription_completed=completed,
            )

            self.assertIs(service.on_subscription_completed, completed)

    def test_direct_hdhive_url_creates_subscription_without_normal_intake(self):
        telegram = FakeTelegram()
        service = FakeSubscriptionService()

        bridge.handle_update(
            {
                "message": {
                    "chat": {"id": "464100862"},
                    "from": {"id": "464100862"},
                    "text": "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5",
                }
            },
            object(),
            telegram,
            "464100862",
            object(),
            hdhive_subscription_service=service,
        )

        self.assertEqual(
            service.created_urls,
            [("464100862", "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5")],
        )
        self.assertIn("已订阅：攻壳机动队", telegram.messages[-1][1])

    def test_subscription_pause_callback_updates_service(self):
        telegram = FakeTelegram()
        service = FakeSubscriptionService()

        bridge.handle_callback_query(
            {
                "id": "callback-subscription",
                "from": {"id": "464100862"},
                "message": {"chat": {"id": "464100862"}},
                "data": "hsub:pause:1",
            },
            telegram,
            "464100862",
            object(),
            hdhive_subscription_service=service,
        )

        self.assertEqual(service.paused, [1])
        self.assertIn("订阅已暂停", telegram.messages[-1][1])

    def test_candidate_keyboard_only_offers_subscription_for_tv(self):
        keyboard = hdhive_candidate_keyboard(
            "session",
            [
                {"media_type": "movie", "title": "电影", "year": "2026"},
                {"media_type": "tv", "title": "剧集", "year": "2026"},
            ],
        )
        rows = keyboard["inline_keyboard"]
        self.assertEqual(rows[0][0]["text"], "1. 电影")
        self.assertEqual(rows[1][0]["text"], "2. 剧集")
        self.assertEqual(rows[1][1]["text"], "订阅此剧")
        self.assertEqual(rows[-1][0]["text"], "取消搜索")
        callbacks = [
            button["callback_data"]
            for row in rows
            for button in row
            if "callback_data" in button
        ]
        self.assertNotIn("hive:subscribe:session:0", callbacks)
        self.assertIn("hive:subscribe:session:1", callbacks)
        self.assertNotIn("2026", rows[0][0]["text"])
        self.assertNotIn("[电影]", rows[0][0]["text"])
        self.assertNotIn("[剧集]", rows[1][0]["text"])

    def test_candidate_keyboard_uses_untitled_fallback(self):
        keyboard = hdhive_candidate_keyboard("session", [{"media_type": "movie", "title": "", "year": "2026"}])
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "1. 未命名")

    def test_candidate_keyboard_truncates_long_titles_at_the_end(self):
        title = "龙" * 80
        keyboard = hdhive_candidate_keyboard("session", [{"media_type": "movie", "title": title, "year": "2026"}])
        label = keyboard["inline_keyboard"][0][0]["text"]
        self.assertTrue(label.startswith("1. 龙"))
        self.assertTrue(label.endswith("…"))
        self.assertEqual(len(label), 64)
        self.assertNotIn("2026", label)
        self.assertNotIn("...", label)

    def test_hdhive_proxy_errors_are_redacted_on_unlock_resource_and_search_paths(self):
        secret_url = "https://evil.test/hdhive?share_code=proxy-share&password=proxy-password"

        class ErrorProxy(FakeProxy):
            def resources(self, _media_type, _tmdb_id):
                raise HdhiveProxyError("HDHIVE_RESOURCES_FAILED", f"资源失败 {secret_url} token=proxy-token")

            def unlock(self, _slugs):
                raise HdhiveProxyError("HDHIVE_UNLOCK_FAILED", f"解锁失败 {secret_url} token=unlock-token")

        resource_workflow = HdhiveWorkflow(object(), ErrorProxy(), HdhiveSessionStore())
        session_id = resource_workflow.sessions.begin("464100862", "Example")
        resource_workflow.set_candidates(session_id, [{"media_type": "movie", "tmdb_id": "550", "title": "Example", "year": "1999"}])
        telegram = FakeTelegram()
        bridge.handle_hdhive_callback(
            f"hive:candidate:{session_id}:0", "callback-resource-error", "464100862", telegram, resource_workflow, None
        )
        resource_message = telegram.messages[-1][1]
        resource_alert = telegram.answers[-1][1]
        self.assertIn("HDHive 请求失败", resource_message)
        self.assertEqual(resource_alert, "HDHive 请求失败")
        for secret in (secret_url, "evil.test", "proxy-share", "proxy-password", "proxy-token"):
            self.assertNotIn(secret, resource_message)
            self.assertNotIn(secret, resource_alert)

        unlock_workflow = HdhiveWorkflow(object(), ErrorProxy(), HdhiveSessionStore())
        unlock_session = unlock_workflow.sessions.begin("464100862", "Example")
        unlock_workflow.load_resources = lambda _session_id, _media_type, _tmdb_id: None
        session = unlock_workflow.sessions.get(unlock_session)
        session.resources = [resource("unlock-item")]
        unlock_workflow.toggle_selection(unlock_session, 0)
        telegram = FakeTelegram()
        bridge.execute_hdhive_unlock(unlock_workflow, unlock_session, "464100862", "callback-unlock-error", telegram, None, False)
        unlock_message = telegram.messages[-1][1]
        unlock_alert = telegram.answers[-1][1]
        self.assertIn("HDHive 请求失败", unlock_message)
        self.assertEqual(unlock_alert, "HDHive 请求失败")
        for secret in (secret_url, "evil.test", "proxy-share", "proxy-password", "unlock-token"):
            self.assertNotIn(secret, unlock_message)
            self.assertNotIn(secret, unlock_alert)

        pending_workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
        pending_workflow.sessions.begin("464100862", "pending")

        def fail_search(_query):
            raise HdhiveProxyError("HDHIVE_SEARCH_FAILED", f"搜索失败 {secret_url} token=search-token")

        pending_workflow.search_candidates = fail_search
        telegram = FakeTelegram()
        bridge.handle_update(
            {"message": {"chat": {"id": "464100862"}, "from": {"id": "464100862"}, "text": "搜索词"}},
            object(),
            telegram,
            "464100862",
            object(),
            poll_status=False,
            hdhive_workflow=pending_workflow,
        )
        search_message = telegram.messages[-1][1]
        self.assertIn("HDHive 搜索失败", search_message)
        for secret in (secret_url, "evil.test", "proxy-share", "proxy-password", "search-token"):
            self.assertNotIn(secret, search_message)

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
        self.assertEqual(len(telegram.rich_messages), 1)
        self.assertNotIn("https://pan.quark.cn/s/two", telegram.rich_messages[-1][1].to_plain())
        self.assertIn("非 115 资源：1 个", telegram.rich_messages[-1][1].to_plain())

    def test_hdhive_candidate_callback_sends_rich_resources_with_keyboard(self):
        workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.set_candidates(
            session_id,
            [{"media_type": "movie", "tmdb_id": "550", "title": "搏击俱乐部", "year": "1999"}],
        )
        telegram = FakeTelegram()

        self.assertTrue(
            bridge.handle_hdhive_callback(
                f"hive:candidate:{session_id}:0",
                "callback-resource",
                "464100862",
                telegram,
                workflow,
                None,
            )
        )

        self.assertEqual(len(telegram.rich_messages), 1)
        document = telegram.rich_messages[-1][1]
        self.assertIn("HDHive 资源：搏击俱乐部", document.to_plain())
        callbacks = [
            button["callback_data"]
            for row in telegram.rich_messages[-1][2]["inline_keyboard"]
            for button in row
        ]
        self.assertIn(f"hive:toggle:{session_id}:0", callbacks)
        self.assertIn(f"hive:single:{session_id}:0", callbacks)
        self.assertIn(f"hive:unlock:{session_id}", callbacks)
        self.assertIn(f"hive:cancel:{session_id}", callbacks)

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

    def test_truncate_end_keeps_the_readable_prefix(self):
        self.assertEqual(truncate_end("abcdefghij", 8), "abcdefg…")
        self.assertEqual(truncate_end("短名", 8), "短名")

    def test_truncate_end_fits_a_single_ellipsis_when_limit_is_one(self):
        self.assertEqual(truncate_end("abc", 1), "…")

    def test_candidate_label_uses_full_title_and_chinese_type(self):
        self.assertEqual(
            format_hdhive_candidate_label(
                {"title": "攻壳机动队 SAC_2045", "year": "2020", "media_type": "tv", "tmdb_id": "80986"}
            ),
            "攻壳机动队 SAC_2045 (2020) · 剧集 · TMDB 80986",
        )
        self.assertEqual(
            format_hdhive_candidate_label(
                {"title": "搏击俱乐部", "year": "1999", "media_type": "movie", "tmdb_id": "550"}
            ),
            "搏击俱乐部 (1999) · 电影 · TMDB 550",
        )

    def test_candidate_label_fills_missing_title_and_year(self):
        self.assertEqual(
            format_hdhive_candidate_label({"media_type": "movie", "tmdb_id": "550"}),
            "未命名 (年份未知) · 电影 · TMDB 550",
        )

    def test_search_query_lists_full_titles_and_name_only_buttons(self):
        telegram = FakeTelegram()
        cms = SimpleNamespace(
            search_movie=lambda keyword, page=1, page_size=8: {
                "code": 200,
                "data": {"results": [{"id": 550, "title": "搏击俱乐部", "release_date": "1999-10-15"}]},
            },
            search_tv=lambda keyword, page=1, page_size=8: {
                "code": 200,
                "data": {"results": [{"id": 80986, "name": "攻壳机动队 SAC_2045", "first_air_date": "2020-04-23"}]},
            },
        )
        workflow = HdhiveWorkflow(cms, FakeProxy(), HdhiveSessionStore())
        allowed = "464100862"
        update = {
            "message": {
                "chat": {"id": allowed},
                "from": {"id": allowed},
                "text": "/搜索",
            }
        }
        bridge.handle_update(
            update, object(), telegram, allowed, object(), poll_status=False, hdhive_workflow=workflow
        )
        update["message"]["text"] = "攻壳"
        bridge.handle_update(
            update, object(), telegram, allowed, object(), poll_status=False, hdhive_workflow=workflow
        )
        text = telegram.rich_messages[-1][1].to_plain()
        keyboard = telegram.rich_messages[-1][2]
        self.assertIn("HDHive 候选媒体", text)

        self.assertEqual(len(telegram.rich_messages), 1)
        self.assertIn("1 | 搏击俱乐部 | 电影 | 1999 | 550", text)
        self.assertIn("2 | 攻壳机动队 SAC_2045 | 剧集 | 2020 | 80986", text)
        self.assertNotIn("[电影]", text)
        self.assertNotIn("TMDB:", text)
        labels = [
            row[0]["text"]
            for row in keyboard["inline_keyboard"]
            if row[0]["callback_data"].startswith("hive:candidate:")
        ]
        self.assertEqual(labels, ["1. 搏击俱乐部", "2. 攻壳机动队 SAC_2045"])

    def test_resource_header_uses_selected_candidate_title(self):
        workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "攻壳")
        workflow.set_candidates(
            session_id,
            [
                {
                    "media_type": "tv",
                    "tmdb_id": "80986",
                    "title": "攻壳机动队 SAC_2045",
                    "year": "2020",
                }
            ],
        )
        workflow.load_resources(session_id, "tv", "80986")
        document, _keyboard = bridge.format_hdhive_resources(workflow, session_id)
        self.assertIsInstance(document, bridge.RichDocument)
        text = document.to_plain()
        self.assertTrue(text.startswith("HDHive 资源：攻壳机动队 SAC_2045 (2020) · 剧集 · TMDB 80986"))
        self.assertNotIn("tv / TMDB", text)
        self.assertIn("table", [block["type"] for block in document.to_blocks()])

    def test_resource_formatter_uses_bounded_table_and_invalid_details(self):
        proxy = FakeProxy()
        proxy.items = [resource(f"item-{index}") for index in range(21)]
        proxy.items[-1] = HdhiveResource(**{**proxy.items[-1].__dict__, "validate_status": "invalid", "validate_message": "资源已失效"})
        workflow = HdhiveWorkflow(object(), proxy, HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        document, _keyboard = bridge.format_hdhive_resources(workflow, session_id)
        blocks = document.to_blocks()
        self.assertEqual(blocks[1]["type"], "table")
        self.assertEqual(len(blocks[1]["cells"]), 21)
        self.assertIn("还有 1 条", document.to_plain())
        self.assertIn("details", [block["type"] for block in blocks])
        self.assertIn("资源已失效", document.to_plain())

    def test_resource_formatter_redacts_validation_urls_and_credentials(self):
        proxy = FakeProxy()
        secret_url = "https://115cdn.com/s/secret-share?password=raw-password"
        proxy.items = [HdhiveResource(**{**resource("invalid").__dict__, "validate_status": "invalid", "validate_message": f"失效：{secret_url} token=raw-token"})]
        workflow = HdhiveWorkflow(object(), proxy, HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")

        document, _keyboard = bridge.format_hdhive_resources(workflow, session_id)
        plain = document.to_plain()

        self.assertIn("失效", plain)
        self.assertNotIn(secret_url, plain)
        self.assertNotIn("raw-password", plain)
        self.assertNotIn("raw-token", plain)
        self.assertNotIn("115cdn.com", plain)
        self.assertNotIn(secret_url, repr(document.to_blocks()))

    def test_resource_formatter_omits_table_when_filter_has_no_rows(self):
        workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        workflow.sessions.get(session_id).pan_type = "missing"
        document, _keyboard = bridge.format_hdhive_resources(workflow, session_id)
        self.assertNotIn("table", [block["type"] for block in document.to_blocks()])
        self.assertIn("当前网盘筛选没有资源", document.to_plain())
        self.assertIn("已选择：0 个", document.to_plain())

        workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        document, _keyboard = bridge.format_hdhive_resources(workflow, session_id)
        self.assertIsInstance(document, bridge.RichDocument)
        text = document.to_plain()
        self.assertTrue(text.startswith("HDHive 资源：未命名 (年份未知) · 电影 · TMDB 550"))
        self.assertIn("# | 资源 | 网盘 | 大小 | 分辨率 | 费用 | 状态", text)

    def test_task_snapshot_missing_title_never_exposes_share_code(self):
        task = SimpleNamespace(
            id=42,
            title="",
            share_code="secret-share-code",
            metadata={},
            current_stage=TaskStage.RECEIVED,
            status=TaskStatus.PENDING,
        )
        snapshot = bridge.format_task_snapshot(task)
        self.assertEqual(snapshot, "#42 任务 #42｜收到链接｜pending")
        self.assertNotIn("secret-share-code", snapshot)


if __name__ == "__main__":
    unittest.main()
