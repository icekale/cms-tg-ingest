import unittest
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.clients.hdhive import HdhiveResource, HdhiveUnlockItem
from app.config import Config
from app.hdhive_subscription_store import HdhiveSubscriptionStore
from app.hdhive_subscriptions import (
    HdhiveSubscriptionService,
    HdhiveSubscriptionScheduler,
    HdhiveUrlError,
    parse_hdhive_tv_url,
    select_best_resource,
)


def resource(slug, *, status="valid", resolution="1080P", points=8, episode_key="s01e01", owned=False):
    return HdhiveResource(
        slug=slug,
        title=f"Title {slug}",
        pan_type="115",
        share_size="10GB",
        video_resolution=(resolution,),
        source=("WEB-DL",),
        subtitle_language=("简中",),
        subtitle_type=("内封",),
        unlock_points=points,
        validate_status=status,
        validate_message="",
        is_unlocked=owned,
        episode_key=episode_key,
    )


class FakeSubscriptionProxy:
    def __init__(self, resources, unlock_items=None):
        self.resource_items = list(resources)
        self.unlock_items = unlock_items or []
        self.unlock_calls = []

    def resources(self, media_type, tmdb_id):
        return list(self.resource_items)

    def unlock(self, slugs):
        self.unlock_calls.append(list(slugs))
        return list(self.unlock_items)


class HdhiveSubscriptionUrlTests(unittest.TestCase):
    def test_parse_hdhive_tv_url_accepts_hdhive_tv_pages(self):
        parsed = parse_hdhive_tv_url(
            "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5"
        )

        self.assertEqual(parsed.slug, "542a1c1fe6ac4a5aab152369079596b5")
        self.assertEqual(parsed.url, "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5")

    def test_parse_hdhive_tv_url_rejects_other_hosts_and_paths(self):
        for value in (
            "https://evil.example/tv/542a1c1fe6ac4a5aab152369079596b5",
            "https://hdhive.com/movie/542a1c1fe6ac4a5aab152369079596b5",
            "https://hdhive.com/tv/short",
            "https://hdhive.com/tv/not-a-valid-slug!",
        ):
            with self.subTest(value=value):
                with self.assertRaises(HdhiveUrlError):
                    parse_hdhive_tv_url(value)

    def test_subscription_schedule_defaults_and_env_overrides(self):
        required = {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms.test",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "password",
        }
        with patch.dict("os.environ", required, clear=True):
            defaults = Config.from_env()
        self.assertTrue(defaults.hdhive_subscription_auto_enabled)
        self.assertEqual(defaults.hdhive_subscription_time, "01:30")
        self.assertEqual(defaults.hdhive_subscription_timezone, "Asia/Shanghai")

        with patch.dict(
            "os.environ",
            {**required, "HDHIVE_SUBSCRIPTION_TIME": "03:15", "HDHIVE_SUBSCRIPTION_TIMEZONE": "UTC"},
            clear=True,
        ):
            overridden = Config.from_env()
        self.assertEqual(overridden.hdhive_subscription_time, "03:15")
        self.assertEqual(overridden.hdhive_subscription_timezone, "UTC")


class HdhiveSubscriptionServiceTests(unittest.TestCase):
    def make_service(self, resources, unlock_items=None):
        directory = tempfile.TemporaryDirectory()
        store = HdhiveSubscriptionStore(Path(directory.name) / "tasks.db")
        subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")
        proxy = FakeSubscriptionProxy(resources, unlock_items=unlock_items)
        intake_calls = []
        service = HdhiveSubscriptionService(
            proxy=proxy,
            store=store,
            enqueue_links=lambda urls, chat_id: intake_calls.append((list(urls), str(chat_id))),
            auto_unlock_max_points=20,
        )
        return directory, store, subscription, proxy, service, intake_calls

    def test_select_best_resource_uses_validity_then_resolution_then_cost(self):
        selected = select_best_resource(
            [
                resource("invalid-2160", status="invalid", resolution="2160P", points=0),
                resource("unknown-2160", status="", resolution="2160P", points=1),
                resource("valid-1080-expensive", resolution="1080P", points=20),
                resource("valid-720-cheap", resolution="720P", points=1),
            ]
        )

        self.assertEqual(selected.slug, "valid-1080-expensive")

    def test_low_cost_resource_enters_existing_intake_once(self):
        unlock_items = [HdhiveUnlockItem("best", True, "https://115cdn.com/s/new?password=abcd", "", "", False)]
        directory, _store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("best", resolution="2160P", points=20)], unlock_items
        )
        try:
            result = service.check(subscription.id)
            repeated = service.check(subscription.id)
            item = _store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(repeated.enqueued, 0)
        self.assertEqual(proxy.unlock_calls, [["best"]])
        self.assertEqual(intake_calls, [(["https://115cdn.com/s/new?password=abcd"], "464100862")])
        self.assertEqual(item.unlock_points_spent, 20)
        self.assertEqual(item.unlock_points_source, "estimated")
        self.assertGreater(item.unlocked_at or 0, 0)

    def test_high_cost_resource_waits_for_confirmation(self):
        directory, store, subscription, proxy, service, intake_calls = self.make_service([resource("high", points=21)])
        try:
            result = service.check(subscription.id)
            item = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(result.pending_confirmation, 1)
        self.assertEqual(item.status, "pending_confirmation")
        self.assertEqual(proxy.unlock_calls, [])

    def test_unknown_cost_waits_for_confirmation(self):
        item = resource("unknown-cost", points=None)
        directory, store, subscription, proxy, service, _intake_calls = self.make_service([item])
        try:
            result = service.check(subscription.id)
            stored = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(result.pending_confirmation, 1)
        self.assertEqual(stored.status, "pending_confirmation")
        self.assertEqual(proxy.unlock_calls, [])

    def test_stale_unlocking_item_is_retried(self):
        unlock_items = [HdhiveUnlockItem("stale", True, "https://115cdn.com/s/stale?password=abcd", "", "", False)]
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("stale", resolution="2160P", points=8)], unlock_items
        )
        try:
            item = store.list_items(subscription.id)
            self.assertEqual(item, [])
            stored = store.upsert_item(subscription.id, "s01e01", "stale", "valid", 2160, 8)
            store.mark_item_unlocking(stored.id)
            with store._lock, store._connection() as connection:
                connection.execute(
                    "UPDATE hdhive_subscription_items SET updated_at = ? WHERE id = ?",
                    (1.0, stored.id),
                )

            result = service.check(subscription.id)
            current = store.get_item(stored.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(current.status, "enqueued")
        self.assertEqual(proxy.unlock_calls, [["stale"]])
        self.assertEqual(intake_calls, [(["https://115cdn.com/s/stale?password=abcd"], "464100862")])


class HdhiveSubscriptionSchedulerTests(unittest.TestCase):
    def test_scheduler_enqueues_one_best_episode_and_keeps_high_cost_episode_pending(self):
        directory = tempfile.TemporaryDirectory()
        try:
            store = HdhiveSubscriptionStore(Path(directory.name) / "tasks.db")
            subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")
            proxy = FakeSubscriptionProxy(
                [
                    resource("ep1-4k", resolution="2160P", points=8, episode_key="s01e01"),
                    resource("ep1-1080", resolution="1080P", points=1, episode_key="s01e01"),
                    resource("ep2-high", resolution="1080P", points=21, episode_key="s01e02"),
                ],
                [HdhiveUnlockItem("ep1-4k", True, "https://115cdn.com/s/episode1?password=1111", "", "", False)],
            )
            intake_calls = []
            service = HdhiveSubscriptionService(
                proxy=proxy,
                store=store,
                enqueue_links=lambda urls, chat_id: intake_calls.append((urls, chat_id)),
                auto_unlock_max_points=20,
            )
            scheduler = HdhiveSubscriptionScheduler(service, store, enabled=True)

            run = scheduler.run_now()
            items = {item.resource_slug: item for item in store.list_items(subscription.id)}

            self.assertEqual(run.summary["enqueued"], 1)
            self.assertEqual(run.summary["pending_confirmation"], 1)
            self.assertEqual(proxy.unlock_calls, [["ep1-4k"]])
            self.assertEqual(intake_calls, [(["https://115cdn.com/s/episode1?password=1111"], "464100862")])
            self.assertEqual(items["ep1-4k"].status, "enqueued")
            self.assertEqual(items["ep1-1080"].status, "discovered")
            self.assertEqual(items["ep2-high"].status, "pending_confirmation")
        finally:
            directory.cleanup()

    def test_next_run_defaults_to_0130_shanghai(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            scheduler = HdhiveSubscriptionScheduler(
                service=object(),
                store=store,
                enabled=True,
                run_time="01:30",
                timezone_name="Asia/Shanghai",
            )
            now = datetime(2026, 7, 25, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

            next_run = scheduler.next_run_at(now)

            self.assertEqual(next_run.hour, 1)
            self.assertEqual(next_run.minute, 30)
            self.assertEqual(next_run.tzinfo, ZoneInfo("Asia/Shanghai"))

    def test_daily_lease_allows_one_run_per_local_date(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            scheduler = HdhiveSubscriptionScheduler(
                service=object(),
                store=store,
                enabled=True,
                run_time="01:30",
                timezone_name="Asia/Shanghai",
            )
            at_run_time = datetime(2026, 7, 25, 1, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

            first = scheduler.run_if_due(at_run_time)
            second = scheduler.run_if_due(at_run_time + timedelta(minutes=1))

            self.assertIsNotNone(first)
            self.assertIsNone(second)

    def test_manual_scheduler_runs_are_serialized_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")
            entered = threading.Event()
            release = threading.Event()

            class BlockingService:
                def check(self, subscription_id):
                    self.last_subscription_id = subscription_id
                    entered.set()
                    release.wait(timeout=2)
                    return type("Result", (), {"discovered": 0, "enqueued": 0, "pending_confirmation": 0, "failed": 0})()

            service = BlockingService()
            first_scheduler = HdhiveSubscriptionScheduler(service, store, enabled=True)
            second_scheduler = HdhiveSubscriptionScheduler(service, store, enabled=True)
            results = []

            first = threading.Thread(target=lambda: results.append(first_scheduler.run_now()))
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second = threading.Thread(target=lambda: results.append(second_scheduler.run_now()))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)

            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(sum(result is None for result in results), 1)

    def test_status_snapshot_reads_summary_from_completed_in_memory_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            scheduler = HdhiveSubscriptionScheduler(
                service=object(),
                store=store,
                enabled=True,
            )

            run = scheduler.run_now()

            snapshot = scheduler.status_snapshot()

            self.assertEqual(snapshot["last_run_id"], run.run_id)
            self.assertEqual(snapshot["last_summary"], run.summary)


if __name__ == "__main__":
    unittest.main()
