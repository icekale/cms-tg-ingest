import logging
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
    diagnose_subscription_check,
    episode_keys,
    parse_hdhive_tv_url,
    select_best_resource,
)
from app.task_store import TaskStore


def resource(
    slug,
    *,
    status="valid",
    resolution="1080P",
    points=8,
    episode_key="s01e01",
    owned=False,
    season_number=None,
    episode_number=None,
    remark="",
):
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
        season_number=season_number,
        episode_number=episode_number,
        episode_key=episode_key,
        remark=remark,
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
        return [item for item in self.unlock_items if item.slug in slugs]


class FakeTmdbResolver:
    enabled = True

    def __init__(self, details=None, error=None):
        self.details = details if details is not None else {"ok": False}
        self.error = error
        self.calls = []

    def lookup(self, tmdb_id, media_type, share_name):
        self.calls.append((str(tmdb_id), media_type, share_name))
        if self.error is not None:
            raise self.error
        return dict(self.details)


class FakeEmby:
    enabled = True

    def __init__(self, existing=None, error=None, *, enabled=True):
        self.enabled = enabled
        self.existing = set(existing or ())
        self.error = error
        self.calls = []

    def existing_episode_keys_by_tmdb(self, tmdb_id):
        self.calls.append(str(tmdb_id))
        if self.error is not None:
            raise self.error
        return set(self.existing)


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
    def make_service(self, resources, unlock_items=None, *, tmdb_resolver=None, emby=None):
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
            tmdb_resolver=tmdb_resolver,
            emby=emby,
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

    def test_orphan_enqueued_without_task_is_unlocked_again(self):
        unlock_items = [HdhiveUnlockItem("ghost", True, "https://115cdn.com/s/ghost?password=abcd", "", "", False)]
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("ghost", resolution="1080P", points=0)], unlock_items
        )
        try:
            stored = store.upsert_item(subscription.id, "S01E01-S01E07", "ghost", "valid", 1080, 0)
            store.mark_item_enqueued(stored.id)
            result = service.check(subscription.id)
            current = store.get_item(stored.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(proxy.unlock_calls, [["ghost"]])
        self.assertEqual(intake_calls, [(["https://115cdn.com/s/ghost?password=abcd"], "464100862")])
        self.assertEqual(current.status, "enqueued")
        self.assertGreater(current.unlocked_at or 0, 0)

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

    def test_stale_unlocking_item_requires_confirmation_without_replay(self):
        unlock_items = [HdhiveUnlockItem("stale", True, "https://115cdn.com/s/stale?password=abcd", "", "", False)]
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("stale", resolution="2160P", points=21)], unlock_items
        )
        try:
            item = store.list_items(subscription.id)
            self.assertEqual(item, [])
            stored = store.upsert_item(subscription.id, "s01e01", "stale", "valid", 2160, 8)
            store.mark_item_unlocking(stored.id)
            with store._lock, store._connection() as connection:
                connection.execute(
                    "UPDATE hdhive_subscription_items SET unlock_requested_at = ?, updated_at = ? WHERE id = ?",
                    (1.0, 1.0, stored.id),
                )

            result = service.check(subscription.id)
            current = store.get_item(stored.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.pending_confirmation, 1)
        self.assertEqual(current.status, "pending_confirmation")
        self.assertEqual(current.skip_reason, "unlock_outcome_unknown")
        self.assertEqual(current.last_error, "解锁结果未知，禁止自动重复扣分")
        self.assertEqual(proxy.unlock_calls, [])
        self.assertEqual(intake_calls, [])

    def test_unlock_dispatch_exception_requires_confirmation_without_replay(self):
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("ambiguous", points=8)]
        )

        def ambiguous_unlock(slugs):
            proxy.unlock_calls.append(list(slugs))
            raise RuntimeError("timeout after unlock dispatch")

        proxy.unlock = ambiguous_unlock
        try:
            first = service.check(subscription.id)
            second = service.check(subscription.id)
            item = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(first.pending_confirmation, 1)
        self.assertEqual(second.pending_confirmation, 1)
        self.assertEqual(item.status, "pending_confirmation")
        self.assertEqual(item.skip_reason, "unlock_outcome_unknown")
        self.assertEqual(proxy.unlock_calls, [["ambiguous"]])
        self.assertEqual(intake_calls, [])

    def test_ambiguous_unlock_response_without_valid_url_never_replays(self):
        cases = (
            HdhiveUnlockItem("ambiguous", False, "", "charged without URL", "INVALID_RESULT", False, points_spent=6),
            HdhiveUnlockItem("ambiguous", False, "", "empty response", "EMPTY_RESULT", False),
            HdhiveUnlockItem("ambiguous", True, "", "missing URL", "", False),
            HdhiveUnlockItem("ambiguous", True, "https://example.com/not-a-share", "", "", False),
        )
        for unlock_item in cases:
            with self.subTest(success=unlock_item.success, points=unlock_item.points_spent, url=unlock_item.full_url):
                directory, store, subscription, proxy, service, intake_calls = self.make_service(
                    [resource("ambiguous", points=8)],
                    [unlock_item],
                )
                try:
                    first = service.check(subscription.id)
                    second = service.check(subscription.id)
                    item = store.list_items(subscription.id)[0]
                finally:
                    directory.cleanup()

                self.assertEqual(first.pending_confirmation, 1)
                self.assertEqual(second.pending_confirmation, 1)
                self.assertEqual(item.status, "pending_confirmation")
                self.assertEqual(item.unlock_state, "unknown")
                self.assertEqual(item.skip_reason, "unlock_outcome_unknown")
                self.assertEqual(proxy.unlock_calls, [["ambiguous"]])
                self.assertEqual(intake_calls, [])

    def test_saved_unlock_matches_terminal_sibling_by_canonical_episode_identity(self):
        saved_url = "https://115cdn.com/s/saved-legacy-alternative?password=abcd"
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("saved-legacy-alternative", points=8)]
        )
        try:
            terminal = store.upsert_item(
                subscription.id,
                "s01e01",
                "legacy-terminal-sibling",
                "valid",
                2160,
                1,
            )
            saved = store.upsert_item(
                subscription.id,
                "S01E01",
                "saved-legacy-alternative",
                "valid",
                1080,
                8,
                normalized_episode_key="S01E01",
            )
            store.mark_item_enqueued(terminal.id, 77)
            store.mark_item_unlocked(saved.id, saved_url, 8, "actual", 1700000000)

            result = service.check(subscription.id)
            current = store.get_item(saved.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 0)
        self.assertEqual(current.status, "enqueued")
        self.assertEqual(current.task_id, 77)
        self.assertEqual(proxy.unlock_calls, [])
        self.assertEqual(intake_calls, [])

    def test_suppressed_saved_unlock_is_terminal_for_completion(self):
        saved_url = "https://115cdn.com/s/saved-alternative?password=abcd"
        tmdb = FakeTmdbResolver(
            {
                "ok": True,
                "status": "Ended",
                "seasons": [{"season_number": 1, "episode_count": 1}],
            }
        )
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("saved-alternative", points=8)],
            tmdb_resolver=tmdb,
            emby=FakeEmby({"S09E09"}),
        )
        try:
            terminal = store.upsert_item(
                subscription.id,
                "S01E01",
                "terminal-sibling",
                "valid",
                2160,
                1,
                normalized_episode_key="S01E01",
            )
            saved = store.upsert_item(
                subscription.id,
                "S01E01",
                "saved-alternative",
                "valid",
                1080,
                8,
                normalized_episode_key="S01E01",
            )
            store.mark_item_enqueued(terminal.id, 77)
            store.mark_item_unlocked(saved.id, saved_url, 8, "actual", 1700000000)

            result = service.check(subscription.id)
            current = store.get_item(saved.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 0)
        self.assertEqual(result.subscription_status, "completed")
        self.assertEqual(current.status, "enqueued")
        self.assertEqual(current.task_id, 77)
        self.assertEqual(proxy.unlock_calls, [])
        self.assertEqual(intake_calls, [])

    def test_stale_unlocking_is_reconciled_before_filter_skip(self):
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("stale-filtered", episode_key="s01e01", points=8)]
        )
        try:
            stored = store.upsert_item(subscription.id, "S01E01", "stale-filtered", "valid", 1080, 8)
            store.mark_item_unlocking(stored.id)
            with store._lock, store._connection() as connection:
                connection.execute(
                    "UPDATE hdhive_subscription_items SET unlock_requested_at = ?, updated_at = ? WHERE id = ?",
                    (1.0, 1.0, stored.id),
                )
            store.update_episode_filter(subscription.id, "S02")

            result = service.check(subscription.id)
            item = store.get_item(stored.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.pending_confirmation, 1)
        self.assertEqual(item.status, "pending_confirmation")
        self.assertEqual(item.skip_reason, "unlock_outcome_unknown")
        self.assertEqual(proxy.unlock_calls, [])
        self.assertEqual(intake_calls, [])

    def test_saved_unlock_is_enqueued_before_emby_skip(self):
        full_url = "https://115cdn.com/s/saved-emby?password=abcd"
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [resource("saved-emby", points=8)],
            emby=FakeEmby({"S01E01"}),
        )
        try:
            item = store.upsert_item(subscription.id, "S01E01", "saved-emby", "valid", 1080, 8)
            store.mark_item_unlocked(item.id, full_url, 8, "actual", 1700000000)

            result = service.check(subscription.id)
            saved = store.get_item(item.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(saved.status, "enqueued")
        self.assertEqual(proxy.unlock_calls, [])
        self.assertEqual(intake_calls, [([full_url], "464100862")])

    def test_saved_unlock_wins_when_ranking_changes_after_intake_failure(self):
        saved_url = "https://115cdn.com/s/saved-ranked?password=abcd"
        better_url = "https://115cdn.com/s/better-ranked?password=efgh"
        unlock_items = [HdhiveUnlockItem("saved-ranked", True, saved_url, "", "", False, points_spent=6)]
        directory, store, subscription, proxy, service, _intake_calls = self.make_service(
            [resource("saved-ranked", resolution="1080P", points=8)],
            unlock_items,
        )
        intake_calls = []

        def enqueue(urls, chat_id):
            intake_calls.append((list(urls), str(chat_id)))
            if len(intake_calls) == 1:
                raise RuntimeError("injected intake failure")
            return 42

        service.enqueue_links = enqueue
        try:
            first = service.check(subscription.id)
            proxy.resource_items = [resource("better-ranked", resolution="2160P", points=1)]
            proxy.unlock_items = [
                *unlock_items,
                HdhiveUnlockItem("better-ranked", True, better_url, "", "", False, points_spent=1),
            ]

            second = service.check(subscription.id)
            items = {item.resource_slug: item for item in store.list_items(subscription.id)}
        finally:
            directory.cleanup()

        self.assertEqual(first.failed, 1)
        self.assertEqual(second.enqueued, 1)
        self.assertEqual(proxy.unlock_calls, [["saved-ranked"]])
        self.assertEqual(intake_calls[-1], ([saved_url], "464100862"))
        self.assertEqual(items["saved-ranked"].status, "enqueued")
        self.assertEqual(items["saved-ranked"].task_id, 42)
        self.assertEqual(items["better-ranked"].status, "discovered")

    def test_intake_error_redacts_persisted_url_from_repr_and_logs(self):
        secret_url = "https://115cdn.com/s/url-secret-code?password=hidden"
        unlock_items = [HdhiveUnlockItem("redacted-error", True, secret_url, "", "", False)]
        directory, store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("redacted-error", points=8)],
            unlock_items,
        )

        def reject(urls, _chat_id):
            raise RuntimeError(f"intake rejected {urls[0]}")

        service.enqueue_links = reject
        try:
            service.check(subscription.id)
            saved = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(saved.status, "unlocked")
        self.assertNotIn("url-secret-code", saved.last_error)
        self.assertNotIn("hidden", saved.last_error)
        self.assertNotIn("url-secret-code", repr(saved))
        with self.assertLogs("hdhive-error-test", level=logging.INFO) as captured:
            logging.getLogger("hdhive-error-test").info("saved item: %r", saved)
        self.assertNotIn("url-secret-code", "\n".join(captured.output))

    def test_saved_unlock_resumes_intake_after_restart_without_second_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            hdhive_path = Path(directory) / "hdhive.db"
            task_store = TaskStore(Path(directory) / "tasks.db")
            store = HdhiveSubscriptionStore(hdhive_path)
            subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")
            full_url = "https://115cdn.com/s/restart?password=abcd"
            first_proxy = FakeSubscriptionProxy(
                [resource("restart", resolution="2160P", points=8)],
                [HdhiveUnlockItem("restart", True, full_url, "", "", False, points_spent=6)],
            )
            intake_task_ids = []

            def enqueue_then_crash(urls, chat_id):
                task = task_store.upsert_task("restart", "abcd", urls[0], chat_id=str(chat_id))
                intake_task_ids.append(task.id)
                raise RuntimeError("injected intake failure")

            first_service = HdhiveSubscriptionService(
                proxy=first_proxy,
                store=store,
                enqueue_links=enqueue_then_crash,
                auto_unlock_max_points=20,
            )

            first_result = first_service.check(subscription.id)
            saved_after_crash = store.list_items(subscription.id)[0]
            original_unlocked_at = saved_after_crash.unlocked_at

            self.assertEqual(first_result.failed, 1)
            self.assertEqual(first_proxy.unlock_calls, [["restart"]])
            self.assertEqual(saved_after_crash.status, "unlocked")
            self.assertEqual(saved_after_crash.unlocked_url, full_url)
            self.assertEqual(saved_after_crash.unlock_state, "unlocked")
            self.assertGreater(saved_after_crash.unlock_requested_at or 0, 0)
            self.assertGreater(saved_after_crash.enqueue_started_at or 0, 0)
            self.assertEqual(saved_after_crash.unlock_points_spent, 6)
            self.assertEqual(saved_after_crash.unlock_points_source, "actual")

            reopened = HdhiveSubscriptionStore(hdhive_path)
            restarted_proxy = FakeSubscriptionProxy([resource("restart", resolution="2160P", points=8)])

            def enqueue_after_restart(urls, chat_id):
                task = task_store.upsert_task("restart", "abcd", urls[0], chat_id=str(chat_id))
                intake_task_ids.append(task.id)
                return task.id

            restarted_service = HdhiveSubscriptionService(
                proxy=restarted_proxy,
                store=reopened,
                enqueue_links=enqueue_after_restart,
                auto_unlock_max_points=20,
            )

            restarted_result = restarted_service.check(subscription.id)
            saved = reopened.get_item(saved_after_crash.id)

            self.assertEqual(restarted_result.enqueued, 1)
            self.assertEqual(restarted_proxy.unlock_calls, [])
            self.assertEqual(intake_task_ids[0], intake_task_ids[1])
            self.assertEqual(saved.task_id, intake_task_ids[0])
            self.assertEqual(saved.unlock_points_spent, 6)
            self.assertEqual(saved.unlock_points_source, "actual")
            self.assertEqual(saved.unlocked_at, original_unlocked_at)

    def test_structured_season_episode_wins_over_invalid_episode_key(self):
        unlock_items = [
            HdhiveUnlockItem("numeric", True, "https://115cdn.com/s/numeric?password=abcd", "", "", False)
        ]
        directory, store, subscription, proxy, service, _intake_calls = self.make_service(
            [
                resource(
                    "numeric",
                    episode_key="not-an-episode",
                    season_number=2,
                    episode_number=3,
                )
            ],
            unlock_items,
        )
        try:
            result = service.check(subscription.id)
            item = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(item.normalized_episode_key, "S02E03")
        self.assertEqual(item.status, "enqueued")
        self.assertEqual(proxy.unlock_calls, [["numeric"]])

    def test_resource_remark_range_is_matched_against_all_emby_episodes(self):
        emby = FakeEmby({"S03E01", "S03E02", "S03E03", "S03E04", "S03E05"})
        directory, store, subscription, proxy, service, _intake_calls = self.make_service(
            [resource("bundle", episode_key="", remark="S03E01-E05 4K WEB-DL")],
            emby=emby,
        )
        try:
            result = service.check(subscription.id)
            item = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 0)
        self.assertEqual(result.summary["emby_exists"], 1)
        self.assertEqual(item.status, "emby_exists")
        self.assertEqual(item.normalized_episode_key, "S03E01-S03E05")
        self.assertEqual(proxy.unlock_calls, [])

    def test_resource_remark_supports_chinese_season_ranges_and_updated_through(self):
        cases = {
            "第三季 第1-5集 4K": ("S03E01", "S03E05"),
            "S03更新至E04 4K": ("S03E01", "S03E04"),
            "第三季(2026)【更05集】": ("S03E01", "S03E05"),
            "第三季 更新至第08集": ("S03E01", "S03E08"),
        }

        for remark, expected in cases.items():
            with self.subTest(remark=remark):
                parsed = episode_keys(resource("bundle", episode_key="", remark=remark))
                self.assertEqual((parsed[0].normalized, parsed[-1].normalized), expected)

    def test_resource_remark_without_season_uses_resource_season(self):
        parsed = episode_keys(
            resource("bundle", episode_key="", remark="更新至第20集", season_number=3)
        )
        self.assertEqual((parsed[0].normalized, parsed[-1].normalized), ("S03E01", "S03E20"))

        # Without any season information the note is skipped, not guessed.
        parsed = episode_keys(resource("bundle", episode_key="", remark="更新至第20集"))
        self.assertEqual(parsed, ())

    def test_resource_remark_range_is_not_skipped_when_one_emby_episode_is_missing(self):
        emby = FakeEmby({"S03E01"})
        unlock_items = [
            HdhiveUnlockItem("bundle", True, "https://115cdn.com/s/bundle?password=abcd", "", "", False)
        ]
        directory, store, subscription, _proxy, service, intake_calls = self.make_service(
            [resource("bundle", episode_key="", remark="S03E01-E05 4K WEB-DL")],
            unlock_items,
            emby=emby,
        )
        try:
            result = service.check(subscription.id)
            item = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(item.status, "enqueued")
        self.assertEqual(intake_calls, [(["https://115cdn.com/s/bundle?password=abcd"], "464100862")])

    def test_reparsed_resource_reuses_existing_unparsed_item(self):
        directory, store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("bundle", episode_key="", remark="S03E01-E05 4K WEB-DL")]
        )
        try:
            existing = store.upsert_item(subscription.id, "opaque-resource-key", "bundle", "", 2160, 8, "old")
            store.mark_item_skipped(existing.id, "unparsed", "无法识别季集编号")
            service.check(subscription.id)
            items = store.list_items(subscription.id)
        finally:
            directory.cleanup()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].normalized_episode_key, "S03E01-S03E05")
        self.assertNotEqual(items[0].status, "unparsed")

    def test_smart_check_skips_emby_existing_episode_across_multiple_seasons(self):
        tmdb = FakeTmdbResolver({"ok": True, "status": "Returning Series", "seasons": []})
        emby = FakeEmby({"S01E01"})
        unlock_items = [
            HdhiveUnlockItem("s2e1", True, "https://115cdn.com/s/s2e1?password=abcd", "", "", False)
        ]
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [
                resource("s1e1", episode_key="s1e1"),
                resource("s2e1", episode_key="s2e1"),
            ],
            unlock_items,
            tmdb_resolver=tmdb,
            emby=emby,
        )
        try:
            result = service.check(subscription.id)
            items = {item.normalized_episode_key: item for item in store.list_items(subscription.id)}
        finally:
            directory.cleanup()

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(items["S01E01"].status, "emby_exists")
        self.assertEqual(items["S02E01"].status, "enqueued")
        self.assertEqual(emby.calls, ["255358"])
        self.assertEqual(intake_calls, [(["https://115cdn.com/s/s2e1?password=abcd"], "464100862")])

    def test_smart_check_applies_filter_and_skips_special_season_by_default(self):
        unlock_items = [
            HdhiveUnlockItem("s1e2", True, "https://115cdn.com/s/s1e2?password=abcd", "", "", False)
        ]
        directory, store, subscription, proxy, service, _intake_calls = self.make_service(
            [
                resource("s0e1", episode_key="s0e1"),
                resource("s1e1", episode_key="s1e1"),
                resource("s1e2", episode_key="s1e2"),
            ],
            unlock_items,
        )
        try:
            store.update_episode_filter(subscription.id, "S01E02-S01E03")
            result = service.check(subscription.id)
            items = {item.normalized_episode_key: item for item in store.list_items(subscription.id)}
        finally:
            directory.cleanup()

        self.assertEqual(result.summary["filtered"], 2)
        self.assertEqual(items["S00E01"].status, "filtered")
        self.assertEqual(items["S01E01"].status, "filtered")
        self.assertNotIn(["s0e1"], proxy.unlock_calls)

    def test_ended_series_becomes_completed_after_expected_episodes_are_terminal(self):
        tmdb = FakeTmdbResolver(
            {
                "ok": True,
                "status": "Ended",
                "seasons": [{"season_number": 1, "episode_count": 2}],
            }
        )
        emby = FakeEmby({"S01E01"})
        unlock_items = [
            HdhiveUnlockItem("s1e2", True, "https://115cdn.com/s/s1e2?password=abcd", "", "", False)
        ]
        directory, store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1"), resource("s1e2", episode_key="s1e2")],
            unlock_items,
            tmdb_resolver=tmdb,
            emby=emby,
        )
        try:
            result = service.check(subscription.id)
            current = store.get_subscription(subscription.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.subscription_status, "completed")
        self.assertEqual(current.status, "completed")
        self.assertEqual(result.summary["expected"], 2)
        self.assertEqual(result.summary["tmdb_status"], "Ended")

    def test_emby_failure_blocks_automatic_unlock_and_reports_reason(self):
        emby = FakeEmby(error=RuntimeError("Emby unavailable"))
        unlock_items = [
            HdhiveUnlockItem("s1e1", True, "https://115cdn.com/s/s1e1?password=abcd", "", "", False)
        ]
        directory, _store, subscription, proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1")],
            unlock_items,
            emby=emby,
        )
        try:
            result = service.check(subscription.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 0)
        self.assertTrue(result.summary["emby_skip_unavailable"])
        self.assertEqual(result.subscription_status, "active")
        self.assertEqual(proxy.unlock_calls, [])

    def test_unconfigured_emby_reports_skip_unavailable(self):
        unlock_items = [
            HdhiveUnlockItem("s1e1", True, "https://115cdn.com/s/s1e1?password=abcd", "", "", False)
        ]
        directory, _store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1")],
            unlock_items,
        )
        try:
            result = service.check(subscription.id)
        finally:
            directory.cleanup()

        self.assertTrue(result.summary["emby_skip_unavailable"])

    def test_disabled_emby_reports_skip_unavailable(self):
        emby = FakeEmby(enabled=False)
        unlock_items = [
            HdhiveUnlockItem("s1e1", True, "https://115cdn.com/s/s1e1?password=abcd", "", "", False)
        ]
        directory, _store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1")],
            unlock_items,
            emby=emby,
        )
        try:
            result = service.check(subscription.id)
        finally:
            directory.cleanup()

        self.assertTrue(result.summary["emby_skip_unavailable"])
        self.assertEqual(emby.calls, [])

    def test_empty_emby_episode_result_is_a_successful_lookup(self):
        emby = FakeEmby()
        unlock_items = [
            HdhiveUnlockItem("s1e1", True, "https://115cdn.com/s/s1e1?password=abcd", "", "", False)
        ]
        directory, _store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1")],
            unlock_items,
            emby=emby,
        )
        try:
            result = service.check(subscription.id)
        finally:
            directory.cleanup()

        self.assertFalse(result.summary["emby_skip_unavailable"])
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(emby.calls, ["255358"])

    def test_unknown_tmdb_result_never_marks_subscription_completed(self):
        tmdb = FakeTmdbResolver({"ok": False, "status": ""})
        unlock_items = [
            HdhiveUnlockItem("s1e1", True, "https://115cdn.com/s/s1e1?password=abcd", "", "", False)
        ]
        directory, store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1")],
            unlock_items,
            tmdb_resolver=tmdb,
        )
        try:
            result = service.check(subscription.id)
            current = store.get_subscription(subscription.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.subscription_status, "active")
        self.assertEqual(current.status, "active")
        self.assertEqual(result.summary["expected"], 0)
        self.assertEqual(len(tmdb.calls), 1)

    def test_high_cost_confirmation_blocks_completion(self):
        tmdb = FakeTmdbResolver(
            {
                "ok": True,
                "status": "Canceled",
                "seasons": [{"season_number": 1, "episode_count": 1}],
            }
        )
        directory, _store, subscription, _proxy, service, _intake_calls = self.make_service(
            [resource("expensive", episode_key="s1e1", points=21)],
            tmdb_resolver=tmdb,
        )
        try:
            result = service.check(subscription.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.pending_confirmation, 1)
        self.assertGreaterEqual(result.summary["blocked"], 1)
        self.assertEqual(result.subscription_status, "active")

    def test_unparsed_resource_blocks_completion_and_is_never_unlocked(self):
        tmdb = FakeTmdbResolver(
            {
                "ok": True,
                "status": "Ended",
                "seasons": [{"season_number": 1, "episode_count": 1}],
            }
        )
        unlock_items = [
            HdhiveUnlockItem("s1e1", True, "https://115cdn.com/s/s1e1?password=abcd", "", "", False),
            HdhiveUnlockItem("unknown", True, "https://115cdn.com/s/unknown?password=abcd", "", "", False),
        ]
        directory, store, subscription, proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1"), resource("unknown", episode_key="")],
            unlock_items,
            tmdb_resolver=tmdb,
        )
        try:
            result = service.check(subscription.id)
            items = {item.resource_slug: item for item in store.list_items(subscription.id)}
        finally:
            directory.cleanup()

        self.assertEqual(result.summary["unparsed"], 1)
        self.assertEqual(items["unknown"].status, "unparsed")
        self.assertNotIn(["unknown"], proxy.unlock_calls)
        self.assertEqual(result.subscription_status, "active")

    def test_filter_change_resets_filtered_item_for_later_unlock(self):
        unlock_items = [
            HdhiveUnlockItem("s1e1", True, "https://115cdn.com/s/s1e1?password=abcd", "", "", False),
            HdhiveUnlockItem("s1e2", True, "https://115cdn.com/s/s1e2?password=abcd", "", "", False),
        ]
        directory, store, subscription, proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1"), resource("s1e2", episode_key="s1e2")],
            unlock_items,
        )
        try:
            store.update_episode_filter(subscription.id, "S01E02")
            first = service.check(subscription.id)
            store.update_episode_filter(subscription.id, "")
            second = service.check(subscription.id)
            item = next(item for item in store.list_items(subscription.id) if item.resource_slug == "s1e1")
        finally:
            directory.cleanup()

        self.assertEqual(first.summary["filtered"], 1)
        self.assertEqual(second.enqueued, 1)
        self.assertEqual(item.status, "enqueued")
        self.assertEqual(proxy.unlock_calls, [["s1e2"], ["s1e1"]])

    def test_stale_filtered_item_does_not_complete_ended_series(self):
        tmdb = FakeTmdbResolver(
            {
                "ok": True,
                "status": "Ended",
                "seasons": [{"season_number": 1, "episode_count": 1}],
            }
        )
        emby = FakeEmby({"S09E09"})
        directory, store, subscription, proxy, service, _intake_calls = self.make_service(
            [resource("s1e1", episode_key="s1e1")],
            tmdb_resolver=tmdb,
            emby=emby,
        )
        try:
            store.update_episode_filter(subscription.id, "S01E02")
            service.check(subscription.id)
            proxy.resource_items = []
            store.update_episode_filter(subscription.id, "")
            result = service.check(subscription.id)
            current = store.get_subscription(subscription.id)
        finally:
            directory.cleanup()

        self.assertEqual(result.subscription_status, "active")
        self.assertEqual(current.status, "active")


    def test_overlapping_episode_range_skips_covered_resource(self):
        unlock_items = [
            HdhiveUnlockItem("narrow", True, "https://115cdn.com/s/narrow?password=abcd", "", "", False),
            HdhiveUnlockItem("broad", True, "https://115cdn.com/s/broad?password=abcd", "", "", False),
        ]
        directory, store, subscription, proxy, service, intake_calls = self.make_service(
            [
                resource("narrow", resolution="1080P", points=8, episode_key="s03e01-s03e05"),
                resource("broad", resolution="1080P", points=8, episode_key="s03e01-s03e07"),
            ],
            unlock_items,
        )
        try:
            result = service.check(subscription.id)
            items = {item.resource_slug: item for item in store.list_items(subscription.id)}
        finally:
            directory.cleanup()

        self.assertEqual(result.enqueued, 1)
        self.assertEqual(proxy.unlock_calls, [["broad"]])
        self.assertEqual(items["narrow"].status, "filtered")
        self.assertEqual(items["narrow"].skip_reason, "集数已被覆盖更完整的其他资源包含")
        self.assertEqual(items["broad"].status, "enqueued")
        self.assertEqual(intake_calls, [(["https://115cdn.com/s/broad?password=abcd"], "464100862")])

if __name__ == "__main__":
    unittest.main()

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


class SubscriptionCheckDiagnosisTests(unittest.TestCase):
    def test_emby_failure_with_leftover_discovered_items(self):
        diagnosis = diagnose_subscription_check(
            {"emby_skip_unavailable": True, "enqueued": 0, "discovered": 2},
            [{"status": "discovered", "skip_reason": "", "last_error": ""}],
        )

        self.assertEqual(diagnosis.conclusion, "未入队：Emby 查询失败，已停止自动解锁")
        self.assertEqual(diagnosis.counts["enqueued"], 0)

    def test_all_unparsed(self):
        items = [
            {"status": "unparsed", "skip_reason": "无法识别季集编号", "last_error": ""}
            for _ in range(3)
        ]

        diagnosis = diagnose_subscription_check({"unparsed": 3, "enqueued": 0}, items)

        self.assertEqual(diagnosis.conclusion, "未入队：3 个无法识别季集")
        self.assertEqual(diagnosis.counts["unparsed"], 3)
        self.assertEqual(diagnosis.reasons, ("无法识别季集编号",))

    def test_all_emby_exists(self):
        diagnosis = diagnose_subscription_check(
            {"emby_exists": 1, "enqueued": 0},
            [{"status": "emby_exists", "skip_reason": "Emby 中已存在该集", "last_error": ""}],
        )

        self.assertEqual(diagnosis.conclusion, "无需入队：集数已在 Emby")

    def test_pending_confirmation(self):
        diagnosis = diagnose_subscription_check(
            {"pending_confirmation": 1, "enqueued": 0},
            [{"status": "pending_confirmation", "skip_reason": "积分超过自动解锁阈值或费用未知", "last_error": ""}],
        )

        self.assertEqual(diagnosis.conclusion, "未入队：1 个待确认")
        self.assertIn("积分超过自动解锁阈值或费用未知", diagnosis.reasons)

    def test_enqueued(self):
        diagnosis = diagnose_subscription_check(
            {"enqueued": 1},
            [{"status": "enqueued", "skip_reason": "", "last_error": "", "task_id": 12}],
        )

        self.assertEqual(diagnosis.conclusion, "已入队 1 个")
        self.assertEqual(diagnosis.counts["enqueued"], 1)

    def test_orphan_enqueued_without_task_is_not_terminal(self):
        diagnosis = diagnose_subscription_check(
            {"enqueued": 0, "discovered": 1},
            [{"status": "enqueued", "skip_reason": "", "last_error": "", "task_id": None, "unlocked_url": ""}],
        )

        self.assertEqual(diagnosis.conclusion, "未入队：1 个入队记录无效，没有任务")
        self.assertEqual(diagnosis.counts["enqueued"], 0)
        self.assertIn("入队记录无效，没有任务", diagnosis.reasons)

    def test_empty_summary_without_items(self):
        diagnosis = diagnose_subscription_check({}, [])

        self.assertEqual(diagnosis.conclusion, "尚无检查结果")
        self.assertEqual(diagnosis.reasons, ())
