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


class MediaStrmRepairTests(unittest.TestCase):
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
                    f_modify_time INTEGER,
                    action TEXT NOT NULL DEFAULT '',
                    status INTEGER NOT NULL DEFAULT 0,
                    local_path TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.executemany(
                "INSERT INTO cloud_data (fid, pid, name, pick_code, is_dir, f_modify_time, action, status, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("fid-missing", "season", "龙族 (2022) - S03E06 - 第 6 集 - 2160p.mkv", "bimn8xuw1izk98sgx", 0, 0, "STRM", 1, "/media/转存/TV/Q-龙族-2022-[tmdb=94997]/Season 03"),
                    ("fid-present", "season", "龙族 (2022) - S03E05 - 第 5 集 - 2160p.mkv", "akrrenvi7l7h4hfud", 0, 0, "STRM", 1, "/media/转存/TV/Q-龙族-2022-[tmdb=94997]/Season 03"),
                    ("fid-disabled", "season", "龙族 (2022) - S03E04 - 第 4 集 - 2160p.mkv", "disabledpick", 0, 0, "STRM", 0, "/media/转存/TV/Q-龙族-2022-[tmdb=94997]/Season 03"),
                ],
            )
            conn.commit()
        return path

    def test_missing_media_strm_candidates_only_reports_absent_status1_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            host_root = Path(tmp) / "strm"
            existing_dir = host_root / "转存" / "TV" / "Q-龙族-2022-[tmdb=94997]" / "Season 03"
            existing_dir.mkdir(parents=True)
            (existing_dir / "龙族 (2022) - S03E05 - 第 5 集 - 2160p.strm").write_text("x", encoding="utf-8")

            index = CmsCloudDataIndex(db_path)
            candidates = index.missing_media_strm_candidates(host_root, limit=50)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["fid"], "fid-missing")
            self.assertEqual(candidates[0]["pick_code"], "bimn8xuw1izk98sgx")
            self.assertEqual(
                Path(candidates[0]["expected_path"]).resolve(),
                (existing_dir / "龙族 (2022) - S03E06 - 第 6 集 - 2160p.strm").resolve(),
            )

    def test_traversal_local_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cms-online.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE cloud_data (
                        fid TEXT PRIMARY KEY,
                        pid TEXT,
                        name TEXT,
                        pick_code TEXT,
                        is_dir INTEGER NOT NULL,
                        f_modify_time INTEGER,
                        action TEXT NOT NULL DEFAULT '',
                        status INTEGER NOT NULL DEFAULT 0,
                        local_path TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO cloud_data (fid, pid, name, pick_code, is_dir, f_modify_time, action, status, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("fid-dotdot", "s", "A.mkv", "pick1", 0, 0, "STRM", 1, "/media/../../etc/escape"),
                        ("fid-dot", "s", "B.mkv", "pick2", 0, 0, "STRM", 1, "/media/./nested/B"),
                        ("fid-ok", "s", "C.mkv", "pick3", 0, 0, "STRM", 1, "/media/nested/C"),
                    ],
                )
                conn.commit()
            host_root = Path(tmp) / "strm"
            (host_root / "nested").mkdir(parents=True)

            index = CmsCloudDataIndex(db_path)
            candidates = index.missing_media_strm_candidates(host_root, limit=50)
            by_fid = {item["fid"]: item for item in candidates}

            # "../.." and "." components escape or anchor outside the media
            # root and must not be repaired into paths.
            self.assertNotIn("fid-dotdot", by_fid)
            self.assertNotIn("fid-dot", by_fid)
            self.assertIn("fid-ok", by_fid)
            self.assertTrue(
                Path(by_fid["fid-ok"]["expected_path"]).resolve().is_relative_to(host_root.resolve())
            )

    def test_repair_missing_media_strms_writes_direct_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            host_root = Path(tmp) / "strm"
            existing_dir = host_root / "转存" / "TV" / "Q-龙族-2022-[tmdb=94997]" / "Season 03"
            existing_dir.mkdir(parents=True)
            (existing_dir / "龙族 (2022) - S03E05 - 第 5 集 - 2160p.strm").write_text("x", encoding="utf-8")
            index = CmsCloudDataIndex(db_path)

            repaired = index.repair_missing_media_strms(
                host_root,
                direct_domain="http://cms.example",
                limit=50,
            )

            self.assertEqual(repaired, 1)
            written = Path(tmp) / "strm" / "转存" / "TV" / "Q-龙族-2022-[tmdb=94997]" / "Season 03" / "龙族 (2022) - S03E06 - 第 6 集 - 2160p.strm"
            self.assertTrue(written.is_file())
            self.assertEqual(
                written.read_text(encoding="utf-8"),
                "http://cms.example/d/bimn8xuw1izk98sgx.mkv?/龙族 (2022) - S03E06 - 第 6 集 - 2160p.mkv",
            )

    def test_repair_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._db(tmp)
            host_root = Path(tmp) / "strm"
            existing_dir = host_root / "转存" / "TV" / "Q-龙族-2022-[tmdb=94997]" / "Season 03"
            existing_dir.mkdir(parents=True)
            (existing_dir / "龙族 (2022) - S03E05 - 第 5 集 - 2160p.strm").write_text("x", encoding="utf-8")
            index = CmsCloudDataIndex(db_path)

            repaired = index.repair_missing_media_strms(
                host_root,
                direct_domain="http://cms.example",
                limit=50,
                dry_run=True,
            )

            self.assertEqual(repaired, 1)
            written = Path(tmp) / "strm" / "转存" / "TV" / "Q-龙族-2022-[tmdb=94997]" / "Season 03" / "龙族 (2022) - S03E06 - 第 6 集 - 2160p.strm"
            self.assertFalse(written.exists())
