import tempfile
import unittest
from pathlib import Path

from app.hdhive_subscription_store import HdhiveSubscriptionStore


class HdhiveSubscriptionStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
