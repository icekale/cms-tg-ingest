import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.hdhive_cards import TmdbDetailCache, build_hdhive_unlock_card


class HdhiveCardTests(unittest.TestCase):
    def test_card_contains_cost_time_and_task_and_poster(self):
        subscription = SimpleNamespace(title="测试剧集")
        item = SimpleNamespace(
            episode_key="s01e02",
            title="2160p 资源",
            resource_slug="resource",
            unlock_points_spent=6,
            unlock_points_source="actual",
            unlocked_at=1700000000,
            task_id=42,
        )
        caption, poster = build_hdhive_unlock_card(
            subscription,
            item,
            tmdb_details={"title": "TMDB 标题", "poster_path": "/poster.jpg"},
        )

        self.assertIn("TMDB 标题", caption)
        self.assertIn("6 分（实际）", caption)
        self.assertIn("任务：#42", caption)
        self.assertEqual(poster, "https://image.tmdb.org/t/p/w500/poster.jpg")

    def test_tmdb_cache_fetches_once_within_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = TmdbDetailCache(Path(tmp) / "cache.db")
            calls = []

            def fetch():
                calls.append(True)
                return {"title": "缓存标题"}

            first = cache.get("tv", "123", fetch)
            second = cache.get("tv", "123", fetch)

        self.assertEqual(first, {"title": "缓存标题"})
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
