import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app.hdhive_subscription_store import HdhiveSubscriptionStore


class HdhiveSubscriptionStoreTests(unittest.TestCase):
    def test_pre_feature_database_migrates_without_losing_existing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE hdhive_subscriptions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        source_value TEXT NOT NULL,
                        source_url TEXT NOT NULL DEFAULT '',
                        title TEXT NOT NULL DEFAULT '',
                        tmdb_id TEXT NOT NULL,
                        media_type TEXT NOT NULL DEFAULT 'tv',
                        pan_type TEXT NOT NULL DEFAULT '115',
                        status TEXT NOT NULL DEFAULT 'active',
                        last_checked_at REAL NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(chat_id, source_type, source_value)
                    );
                    CREATE TABLE hdhive_subscription_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subscription_id INTEGER NOT NULL,
                        episode_key TEXT NOT NULL,
                        resource_slug TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        validate_status TEXT NOT NULL DEFAULT '',
                        resolution_score INTEGER NOT NULL DEFAULT 0,
                        unlock_points INTEGER,
                        status TEXT NOT NULL DEFAULT 'discovered',
                        task_id INTEGER,
                        last_error TEXT NOT NULL DEFAULT '',
                        unlock_points_spent INTEGER,
                        unlock_points_source TEXT NOT NULL DEFAULT '',
                        unlocked_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(subscription_id, episode_key, resource_slug),
                        FOREIGN KEY(subscription_id) REFERENCES hdhive_subscriptions(id)
                    );
                    INSERT INTO hdhive_subscriptions
                        (chat_id, source_type, source_value, title, tmdb_id, created_at, updated_at)
                    VALUES ('chat', 'hdhive_tv', 'legacy', '旧订阅', '1416', 1, 1);
                    INSERT INTO hdhive_subscription_items
                        (subscription_id, episode_key, resource_slug, title, validate_status,
                         resolution_score, unlock_points, task_id, unlock_points_spent,
                         unlock_points_source, unlocked_at, created_at, updated_at)
                    VALUES (1, 's01e01', 'legacy-resource', '旧资源', 'valid', 1080, 3,
                            NULL, 2, 'legacy', 100, 1, 1);
                    """
                )

            store = HdhiveSubscriptionStore(path)
            subscription = store.get_subscription(1)
            item = store.get_item(1)

            self.assertEqual(subscription.title, "旧订阅")
            self.assertEqual(subscription.episode_filter, "")
            self.assertEqual(subscription.last_summary_json, "{}")
            self.assertEqual(item.title, "旧资源")
            self.assertEqual(item.unlock_points_spent, 2)
            self.assertEqual(item.unlock_points_source, "legacy")
            self.assertEqual(item.unlocked_at, 100)
            self.assertEqual(item.normalized_episode_key, "")
            self.assertEqual(item.skip_reason, "")

            claimed = store.claim_item_unlocking(item.id, now=200)
            self.assertEqual(claimed.status, "unlocking")
            self.assertEqual(claimed.unlock_points_spent, 2)
            self.assertEqual(claimed.unlock_points_source, "legacy")
            self.assertEqual(claimed.unlocked_at, 100)

    def test_filter_summary_skip_state_and_completed_status_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.db"
            store = HdhiveSubscriptionStore(path)
            subscription = store.create_subscription("1", "tmdb_tv", "1416", "剧集", "1416")

            updated = store.update_episode_filter(subscription.id, "S01E01-S01E03")
            store.record_check(subscription.id, summary={"emby_exists": 2, "title": "剧集"})
            saved = store.get_subscription(subscription.id)

            self.assertEqual(updated.episode_filter, "S01E01-S01E03")
            self.assertEqual(saved.episode_filter, "S01E01-S01E03")
            self.assertEqual(json.loads(saved.last_summary_json), {"emby_exists": 2, "title": "剧集"})
            self.assertNotIn(": ", saved.last_summary_json)

            item = store.upsert_item(
                subscription.id,
                "S01E02",
                "resource",
                "valid",
                1080,
                8,
                normalized_episode_key="S01E02",
            )
            self.assertEqual(item.normalized_episode_key, "S01E02")
            skipped = store.mark_item_skipped(item.id, "emby_exists", "Emby 已存在")
            self.assertEqual(skipped.status, "emby_exists")
            self.assertEqual(skipped.skip_reason, "Emby 已存在")

            unchanged = store.reset_item_for_check(item.id, "filtered")
            self.assertEqual(unchanged.status, "emby_exists")
            reset = store.reset_item_for_check(item.id, "emby_exists")
            self.assertEqual(reset.status, "discovered")
            self.assertEqual(reset.skip_reason, "")

            self.assertEqual(store.set_status(subscription.id, "completed").status, "completed")
            reopened = HdhiveSubscriptionStore(path)
            self.assertEqual(reopened.get_subscription(subscription.id).status, "completed")

    def test_skip_statuses_are_restricted_and_each_can_be_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            subscription = store.create_subscription("1", "tmdb_tv", "1416", "剧集", "1416")

            for index, status in enumerate(("filtered", "emby_exists", "unparsed")):
                item = store.upsert_item(subscription.id, f"S01E0{index + 1}", f"resource-{index}", "valid", 1080, None)
                skipped = store.mark_item_skipped(item.id, status, f"reason-{index}")
                self.assertEqual(skipped.status, status)
                self.assertEqual(store.reset_item_for_check(item.id, status).status, "discovered")

            item = store.upsert_item(subscription.id, "S01E04", "resource", "valid", 1080, None)
            with self.assertRaises(ValueError):
                store.mark_item_skipped(item.id, "failed", "not a skip status")

            with self.assertRaises(ValueError):
                store.reset_item_for_check(item.id, "failed")

    def test_same_chat_and_source_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")

            first = store.create_subscription("464100862", "hdhive_tv", "slug-1", "剧集", "255358")
            second = store.create_subscription("464100862", "hdhive_tv", "slug-1", "剧集", "255358")

            self.assertEqual(first.id, second.id)
            self.assertEqual(len(store.list_subscriptions("464100862")), 1)

    def test_item_state_and_task_id_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.db"
            store = HdhiveSubscriptionStore(path)
            subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")

            item = store.upsert_item(subscription.id, "s01e01", "resource-1", "valid", 1080, 8)
            second = store.upsert_item(subscription.id, "s01e01", "resource-2", "valid", 2160, 20)
            store.mark_item_pending(second.id, "需要确认")
            store.mark_item_enqueued(item.id, 42)

            reopened = HdhiveSubscriptionStore(path)
            self.assertEqual(reopened.get_item(item.id).status, "enqueued")
            self.assertEqual(reopened.get_item(item.id).task_id, 42)
            self.assertEqual(reopened.get_item(second.id).status, "pending_confirmation")
            self.assertEqual(len(reopened.list_items(subscription.id)), 2)

    def test_unlock_cost_and_time_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hdhive.db"
            store = HdhiveSubscriptionStore(path)
            subscription = store.create_subscription("1", "hdhive_tv", "slug", "剧集", "123")
            item = store.upsert_item(subscription.id, "s01e01", "resource", "valid", 2160, 8, "资源")
            store.mark_item_enqueued(
                item.id,
                42,
                unlock_points_spent=7,
                unlock_points_source="actual",
                unlocked_at=1700000000,
            )
            reopened = HdhiveSubscriptionStore(path)
            saved = reopened.get_item(item.id)

        self.assertEqual(saved.unlock_points_spent, 7)
        self.assertEqual(saved.unlock_points_source, "actual")
        self.assertEqual(saved.unlocked_at, 1700000000)

    def test_subscription_status_actions_and_deleted_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")

            self.assertEqual(store.set_status(subscription.id, "paused").status, "paused")
            self.assertEqual(store.set_status(subscription.id, "active").status, "active")
            self.assertEqual(store.set_status(subscription.id, "deleted").status, "deleted")
            self.assertEqual(store.list_subscriptions("464100862"), [])
            self.assertEqual(len(store.list_subscriptions("464100862", include_deleted=True)), 1)

    def test_daily_run_lease_is_global_and_one_per_date(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")

            self.assertTrue(store.claim_daily_run("2026-07-25", "run-1", 100.0))
            self.assertFalse(store.claim_daily_run("2026-07-25", "run-2", 101.0))
            self.assertTrue(store.claim_daily_run("2026-07-26", "run-3", 200.0))

    def test_item_unlock_claim_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")
            item = store.upsert_item(subscription.id, "s01e01", "resource-1", "valid", 2160, 8)
            barrier = threading.Barrier(2)
            results = []

            def claim():
                barrier.wait()
                results.append(store.claim_item_unlocking(item.id, now=100.0))

            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(store.get_item(item.id).status, "unlocking")

    def test_stale_unlock_claim_can_be_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HdhiveSubscriptionStore(Path(directory) / "tasks.db")
            subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")
            item = store.upsert_item(subscription.id, "s01e01", "resource-1", "valid", 2160, 8)
            store.mark_item_unlocking(item.id)
            with store._lock, store._connection() as connection:
                connection.execute(
                    "UPDATE hdhive_subscription_items SET updated_at = ? WHERE id = ?",
                    (1.0, item.id),
                )

            claimed = store.claim_item_unlocking(item.id, now=7200.0, stale_after_seconds=3600)

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, "unlocking")


if __name__ == "__main__":
    unittest.main()
