import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.cms_cloud_index import CmsCloudDataIndex


class _TrackingConnection:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self.closed = False

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def execute(self, *args, **kwargs):
        return self._connection.execute(*args, **kwargs)

    def close(self):
        self.closed = True
        return self._connection.close()


class CmsCloudDataIndexTests(unittest.TestCase):
    def _db(self, root: str) -> Path:
        path = Path(root) / "cms-online.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_data (
                    fid TEXT PRIMARY KEY,
                    pid TEXT,
                    name TEXT,
                    pick_code TEXT,
                    is_dir INTEGER NOT NULL,
                    f_modify_time INTEGER
                )
                """
            )
            conn.executemany(
                "INSERT INTO cloud_data (fid, pid, name, pick_code, is_dir, f_modify_time) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("episode", "season", "权力的游戏前传：龙族 (2022) - S03E03.mkv", "episodepick", 0, 0),
                    ("season", "series", "Season 03", "", 1, 0),
                    ("series", "tv-root", "Q-权力的游戏前传：龙族-2022-[tmdb=94997]", "", 1, 0),
                    ("tv-root", "0", "TV", "", 1, 0),
                ],
            )
            conn.commit()
        return path

    def test_sqlite_reads_close_connections_on_success_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            broken_path = Path(tmp) / "broken.db"
            broken_path.touch()
            real_connect = sqlite3.connect
            connections = []

            def connect(*args, **kwargs):
                tracked = _TrackingConnection(real_connect(*args, **kwargs))
                connections.append(tracked)
                return tracked

            with patch("app.cms_cloud_index.sqlite3.connect", side_effect=connect):
                self.assertFalse(CmsCloudDataIndex(db_path).has_file_id("missing"))
                self.assertFalse(CmsCloudDataIndex(broken_path).has_file_id("missing"))

            self.assertEqual(len(connections), 2)
            closed = [connection.closed for connection in connections]
            for connection in connections:
                connection.close()
            self.assertTrue(all(closed))

    def test_resolves_media_root_from_direct_strm_pickcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            source = Path(tmp) / "library" / "Q-权力的游戏前传：龙族-2022-[tmdb=94997]"
            source.mkdir(parents=True)
            (source / "episode.strm").write_text("http://cms/d/episodepick.mkv?/episode.mkv", encoding="utf-8")

            folder = CmsCloudDataIndex(db_path).folder_for_direct_strm(source, "94997")

            self.assertEqual(
                folder,
                {
                    "file_id": "series",
                    "file_name": "Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
                    "parent_id": "tv-root",
                    "direct_file_id": "episode",
                    "direct_file_name": "权力的游戏前传：龙族 (2022) - S03E03.mkv",
                    "direct_parent_id": "season",
                    "direct_relative_path": "episode.strm",
                },
            )

    def test_resolves_renamed_cloud_output_from_recent_cms_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE cloud_data SET name = ?, f_modify_time = ? WHERE fid = ?",
                    ("权力的游戏前传：龙族 (2022) - S03E05 - 第 5 集 - 2160p.mkv", 1045, "episode"),
                )
                conn.commit()
            folder = CmsCloudDataIndex(db_path).folder_for_cloud_output_name(
                "House.of.the.Dragon.S03E05.2022.2160p.mkv",
                started_at=1000,
            )

            self.assertEqual(folder["file_id"], "series")
            self.assertEqual(folder["direct_file_id"], "episode")

    def test_rejects_media_root_with_wrong_tmdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            source = Path(tmp) / "library" / "Q-权力的游戏前传：龙族-2022-[tmdb=94997]"
            source.mkdir(parents=True)
            (source / "episode.strm").write_text("http://cms/d/episodepick.mkv?/episode.mkv", encoding="utf-8")

            folder = CmsCloudDataIndex(db_path).folder_for_direct_strm(source, "99999")

            self.assertIsNone(folder)

    def test_reports_whether_file_id_is_still_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            index = CmsCloudDataIndex(db_path)

            self.assertTrue(index.has_file_id("series"))
            self.assertFalse(index.has_file_id("missing"))

    def test_resolves_media_root_from_cloud_output_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "UPDATE cloud_data SET name = ?, pick_code = ? WHERE fid = ?",
                    ("House.of.the.Dragon.S03E05.mkv", "dragon-pick", "episode"),
                )
                conn.commit()
            folder = CmsCloudDataIndex(db_path).folder_for_cloud_output_name(
                "House.of.the.Dragon.S03E05.mkv"
            )

            self.assertEqual(
                folder,
                {
                    "file_id": "series",
                    "file_name": "Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
                    "parent_id": "tv-root",
                    "direct_file_id": "episode",
                    "direct_file_name": "House.of.the.Dragon.S03E05.mkv",
                    "direct_parent_id": "season",
                },
            )

    def test_prefers_the_most_recent_direct_strm_in_an_existing_series_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "INSERT INTO cloud_data (fid, pid, name, pick_code, is_dir) VALUES (?, ?, ?, ?, ?)",
                    ("new-episode", "season", "权力的游戏前传：龙族 (2022) - S03E03.mkv", "newpick", 0),
                )
                conn.commit()
            source = Path(tmp) / "library" / "Q-权力的游戏前传：龙族-2022-[tmdb=94997]"
            source.mkdir(parents=True)
            old_path = source / "S03E02.strm"
            old_path.write_text("http://cms/d/episodepick.mkv?/episode.mkv", encoding="utf-8")
            new_path = source / "S03E03.strm"
            new_path.write_text("http://cms/d/newpick.mkv?/episode.mkv", encoding="utf-8")
            old_time = time.time() - 60
            old_path.touch()
            new_path.touch()
            os.utime(old_path, (old_time, old_time))

            folder = CmsCloudDataIndex(db_path).folder_for_direct_strm(source, "94997")

            self.assertEqual(folder["direct_file_id"], "new-episode")
            self.assertEqual(folder["direct_relative_path"], "S03E03.strm")
