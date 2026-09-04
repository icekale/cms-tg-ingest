"""release_quality：资源质量解析、洗版严格比较与旧版本 strm 清理。"""
import time
import unittest
from pathlib import Path
import tempfile

from app.release_quality import (
    is_upgrade,
    normalize_strm_base,
    parse_release_quality,
    quality_from_names,
    remove_superseded_strms,
)


class ParseReleaseQualityTests(unittest.TestCase):
    def test_resolution_and_source(self):
        cases = [
            ("House.S03E08.2160p.HMAX.WEB-DL.DDP5.1.H.265.mkv", "2160p", "web-dl"),
            ("Movie.1080p.BluRay.x265.mkv", "1080p", "blu-ray"),
            ("Show.S01E01.REMUX.2160p.mkv", "2160p", "remux"),
            ("Some.Show.S01E01.HDTV.720p", "720p", "hdtv"),
            ("哑舍 (2025) - S01E01 - 2160p.mp4", "2160p", ""),
        ]
        for text, resolution, source in cases:
            quality = parse_release_quality(text)
            self.assertIsNotNone(quality, text)
            self.assertEqual(quality.resolution, resolution, text)
            self.assertEqual(quality.source, source, text)

    def test_no_quality_returns_none(self):
        self.assertIsNone(parse_release_quality("no-quality-here.mkv"))
        self.assertIsNone(parse_release_quality(""))

    def test_label_and_webdl_not_matched_as_web(self):
        quality = parse_release_quality("x.1080p.WEB-DL.mkv")
        self.assertEqual(quality.source, "web-dl")
        self.assertIn("1080P", quality.label)

    def test_quality_from_names_first_match_wins(self):
        quality = quality_from_names("unknown.mkv", "Show.1080p.WEB-DL.mkv")
        self.assertEqual(quality.resolution, "1080p")


class IsUpgradeTests(unittest.TestCase):
    def test_resolution_upgrade(self):
        old = parse_release_quality("Show.1080p.WEB-DL.mkv")
        new = parse_release_quality("Show.2160p.REMUX.mkv")
        self.assertTrue(is_upgrade(old, new))

    def test_same_quality_not_upgrade(self):
        old = parse_release_quality("Show.1080p.WEB-DL.mkv")
        same = parse_release_quality("Show.1080p.WEB-DL.other.mkv")
        self.assertFalse(is_upgrade(old, same))

    def test_same_resolution_better_source(self):
        old = parse_release_quality("Show.2160p.WEB-DL.mkv")
        new = parse_release_quality("Show.2160p.REMUX.mkv")
        self.assertTrue(is_upgrade(old, new))
        self.assertFalse(is_upgrade(new, old))

    def test_lower_resolution_never_upgrade(self):
        old = parse_release_quality("Show.2160p.WEB-DL.mkv")
        new = parse_release_quality("Show.1080p.REMUX.mkv")
        self.assertFalse(is_upgrade(old, new))

    def test_unknown_sides_not_upgrade(self):
        known = parse_release_quality("Show.2160p.WEB-DL.mkv")
        self.assertFalse(is_upgrade(None, known))
        self.assertFalse(is_upgrade(known, None))
        self.assertFalse(is_upgrade(None, None))


class NormalizeStrmBaseTests(unittest.TestCase):
    def test_same_episode_different_resolution_share_base(self):
        a = normalize_strm_base("权力的游戏前传：龙族 (2022) - S03E03 - 第 3 集 - 2160p.strm")
        b = normalize_strm_base("权力的游戏前传：龙族 (2022) - S03E03 - 第 3 集 - 1080p.strm")
        self.assertEqual(a, b)

    def test_different_episodes_differ(self):
        a = normalize_strm_base("Show - S03E03 - 2160p.strm")
        b = normalize_strm_base("Show - S03E04 - 2160p.strm")
        self.assertNotEqual(a, b)


class RemoveSupersededStrmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, mtime: float | None = None) -> Path:
        path = self.root / name
        path.write_text("http://example.test/s/x_1_2.mkv", encoding="utf-8")
        if mtime is not None:
            import os

            os.utime(path, (mtime, mtime))
        return path

    def test_removes_old_version_keeps_newest(self):
        now = time.time()
        old = self._write("Show - S01E01 - 1080p.strm", mtime=now - 86400)
        new = self._write("Show - S01E01 - 2160p.strm", mtime=now)
        removed = remove_superseded_strms(str(self.root), older_than=now - 60)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_never_removes_files_newer_than_cutoff(self):
        now = time.time()
        a = self._write("Show - S01E01 - 1080p.strm", mtime=now + 5)
        b = self._write("Show - S01E01 - 2160p.strm", mtime=now + 6)
        removed = remove_superseded_strms(str(self.root), older_than=now)
        self.assertEqual(removed, 0)
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())

    def test_single_file_group_untouched(self):
        now = time.time()
        only = self._write("Show - S01E02 - 1080p.strm", mtime=now - 86400)
        self.assertEqual(remove_superseded_strms(str(self.root), older_than=now), 0)
        self.assertTrue(only.exists())

    def test_missing_dir_returns_zero(self):
        self.assertEqual(remove_superseded_strms(str(self.root / "missing"), older_than=time.time()), 0)


if __name__ == "__main__":
    unittest.main()
