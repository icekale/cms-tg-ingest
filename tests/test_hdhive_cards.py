import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.hdhive_cards import TmdbDetailCache, build_hdhive_unlock_card


class _TrackingConnection:
    def __init__(self, connection: sqlite3.Connection, *, fail_execute: bool = False):
        self._connection = connection
        self.closed = False
        self.fail_execute = fail_execute

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def execute(self, *args, **kwargs):
        if self.fail_execute:
            raise sqlite3.OperationalError("forced lookup failure")
        return self._connection.execute(*args, **kwargs)

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        self.closed = True
        return self._connection.close()


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

    def test_tmdb_cache_closes_connections_on_success_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cache.db"
            real_connect = sqlite3.connect
            connections = []

            def connect(*args, **kwargs):
                tracked = _TrackingConnection(real_connect(*args, **kwargs), fail_execute=len(connections) >= 3)
                connections.append(tracked)
                return tracked

            with patch("app.sqlite_utils.sqlite3.connect", side_effect=connect):
                cache = TmdbDetailCache(db_path)
                self.assertEqual(cache.get("tv", "123", lambda: {"title": "cached"}), {"title": "cached"})
                with self.assertRaises(sqlite3.OperationalError):
                    cache.get("tv", "456", lambda: {"title": "unused"})

            self.assertEqual(len(connections), 4)
            closed = [connection.closed for connection in connections]
            for connection in connections:
                connection.close()
            self.assertTrue(all(closed))

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
