import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.cms_cloud_index import CmsCloudDataIndex


class CmsCloudDataIndexTests(unittest.TestCase):
    def _db(self, root: str) -> Path:
        path = Path(root) / "cms-online.db"
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                CREATE TABLE cloud_data (
                    fid TEXT PRIMARY KEY,
                    pid TEXT,
                    name TEXT,
                    pick_code TEXT,
                    is_dir INTEGER NOT NULL
                )
                """
            )
            conn.executemany(
                "INSERT INTO cloud_data (fid, pid, name, pick_code, is_dir) VALUES (?, ?, ?, ?, ?)",
                [
                    ("episode", "season", "权力的游戏前传：龙族 (2022) - S03E03.mkv", "episodepick", 0),
                    ("season", "series", "Season 03", "", 1),
                    ("series", "tv-root", "Q-权力的游戏前传：龙族-2022-[tmdb=94997]", "", 1),
                    ("tv-root", "0", "TV", "", 1),
                ],
            )
        return path

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
                    "direct_relative_path": "episode.strm",
                },
            )

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

    def test_prefers_the_most_recent_direct_strm_in_an_existing_series_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO cloud_data (fid, pid, name, pick_code, is_dir) VALUES (?, ?, ?, ?, ?)",
                    ("new-episode", "season", "权力的游戏前传：龙族 (2022) - S03E03.mkv", "newpick", 0),
                )
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
