import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.assistant_library import apply_library_action
from app.config import MoveConfig, safe_resolve


class _FakeEmby:
    enabled = True

    def __init__(self):
        self.refreshed: list[str] = []

    def refresh_library_for_path(self, item_path):
        self.refreshed.append(str(item_path))
        return "国产电视"


class AssistantLibraryActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cn = self.tmp / "TVCN"
        self.foreign = self.tmp / "TV"
        self.cn.mkdir()
        self.foreign.mkdir()
        self.config = MoveConfig(
            source_roots=[self.tmp],
            library_roots={"国产电视": self.cn, "外国电视": self.foreign},
        )

    def test_delete_refuses_path_outside_library(self):
        outside = self.tmp / "not-a-library"
        outside.mkdir()
        result = apply_library_action("delete", str(outside), move_config=self.config)
        self.assertFalse(result["applied"])
        self.assertTrue(outside.exists())
        self.assertIn("媒体库", result["reason"])

    def test_delete_refuses_library_root(self):
        result = apply_library_action("delete", str(self.cn), move_config=self.config)
        self.assertFalse(result["applied"])
        self.assertTrue(self.cn.exists())
        self.assertIn("根目录", result["reason"])

    def test_delete_refuses_library_name_as_root(self):
        result = apply_library_action("delete", "国产电视", move_config=self.config)
        self.assertFalse(result["applied"])
        self.assertTrue(self.cn.exists())

    def test_delete_removes_extra_folder_inside_library(self):
        extra = self.cn / "误入国产的美剧"
        extra.mkdir()
        (extra / "note.txt").write_text("strm", encoding="utf-8")
        result = apply_library_action("delete", str(extra), move_config=self.config)
        self.assertTrue(result["applied"], result)
        self.assertFalse(extra.exists())
        self.assertTrue(self.cn.exists())
        self.assertEqual(result["library"], "国产电视")

    def test_delete_refuses_when_automated(self):
        extra = self.cn / "keep-me"
        extra.mkdir()
        result = apply_library_action("delete", str(extra), move_config=self.config, automated=True)
        self.assertFalse(result["applied"])
        self.assertTrue(extra.exists())

    def test_delete_refuses_file(self):
        target = self.cn / "file.strm"
        target.write_text("http://example", encoding="utf-8")
        result = apply_library_action("delete", str(target), move_config=self.config)
        self.assertFalse(result["applied"])
        self.assertTrue(target.exists())

    def test_delete_refuses_missing_path(self):
        result = apply_library_action("delete", str(self.cn / "no-such"), move_config=self.config)
        self.assertFalse(result["applied"])

    def test_delete_blocks_symlink_escape(self):
        outside = self.tmp / "secret"
        outside.mkdir()
        (outside / "x").write_text("no", encoding="utf-8")
        link = self.cn / "escape"
        link.symlink_to(outside)
        result = apply_library_action("delete", str(link), move_config=self.config)
        self.assertFalse(result["applied"])
        self.assertTrue(outside.exists())
        self.assertTrue((outside / "x").exists())

    def test_emby_scan_accepts_library_name(self):
        emby = _FakeEmby()
        result = apply_library_action("emby_scan", "国产电视", move_config=self.config, emby=emby)
        self.assertTrue(result["applied"], result)
        self.assertEqual(emby.refreshed, [str(safe_resolve(self.cn))])

    def test_emby_scan_accepts_folder_inside_library(self):
        show = self.cn / "某剧"
        show.mkdir()
        emby = _FakeEmby()
        result = apply_library_action("emby_scan", str(show), move_config=self.config, emby=emby)
        self.assertTrue(result["applied"], result)
        self.assertEqual(emby.refreshed, [str(safe_resolve(show))])

    def test_unknown_action_refused(self):
        result = apply_library_action("rm", str(self.cn), move_config=self.config)
        self.assertFalse(result["applied"])


class AssistantLibraryOpsCliTests(unittest.TestCase):
    def test_ops_script_exposes_library_subcommand(self):
        text = (Path(__file__).resolve().parent.parent / "scripts" / "assistant_ops.py").read_text()
        self.assertIn('add_parser("library")', text)
        self.assertIn("cmd_library", text)
