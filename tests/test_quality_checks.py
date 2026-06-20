import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).resolve().parents[1] / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class CategoryAliasTests(unittest.TestCase):
    def test_animation_movie_alias_maps_to_configured_anime_movie_category(self):
        self.assertEqual(bridge.map_category_label("动画电影", {}), "动漫电影")

    def test_tmdbid_marker_is_extracted_from_title(self):
        self.assertEqual(
            bridge.extract_tmdb_id_from_name("金福南杀人事件始末 (2010) {tmdbid-59421}"),
            "59421",
        )


class ExpectedTmdbPriorityTests(unittest.TestCase):
    def test_recognition_tmdb_wins_over_wrong_self_share_folder_marker(self):
        row = {
            "title": "Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv",
            "recognition_json": '{"title":"双喜","tmdb_id":"1570664","share_name":"Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv"}',
            "own_share_file_name": "D-得闲谨制-2025-[tmdb=1356454]",
            "dest_path": "/media/D-得闲谨制-2025-[tmdb=1356454]",
            "emby_path": "/media/D-得闲谨制-2025-[tmdb=1356454]/x.strm",
        }

        self.assertEqual(bridge.expected_task_tmdb_id(bridge.parse_recognition_json(row), row), "1570664")

    def test_quality_issue_flags_wrong_folder_against_recognition_tmdb(self):
        row = {
            "title": "Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv",
            "emby_status": "confirmed",
            "emby_title": "得闲谨制",
            "recognition_json": '{"title":"双喜","tmdb_id":"1570664","share_name":"Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv"}',
            "own_share_file_name": "D-得闲谨制-2025-[tmdb=1356454]",
            "dest_path": "/media/D-得闲谨制-2025-[tmdb=1356454]",
            "emby_path": "/media/D-得闲谨制-2025-[tmdb=1356454]/x.strm",
        }

        issue = bridge.quality_issue_for_row(row)

        self.assertIn("任务 TMDB 1570664", issue)
        self.assertIn("路径 TMDB 1356454", issue)


class EmbyQualityMatchTests(unittest.TestCase):
    def test_match_emby_item_rejects_different_task_tmdb(self):
        item = {
            "Name": "我是余欢水",
            "Path": "/mnt/user/Unraid/strm/转存/TVCN/W-我是余欢水-2020-[tmdb=101588]",
            "ProviderIds": {"Tmdb": "101588"},
        }
        recognition = {
            "title": "航海王",
            "share_name": "航海王 (1999) {tmdb=37854}",
        }
        row = {"title": "航海王 (1999) {tmdb=37854}"}

        self.assertIsNone(bridge.match_emby_item([item], recognition, row))

    def test_quality_report_flags_confirmed_emby_title_mismatch(self):
        rows = [
            {
                "id": 72,
                "title": "航海王 (1999) {tmdb=37854}",
                "emby_status": "confirmed",
                "emby_title": "我是余欢水",
                "emby_path": "/mnt/user/Unraid/strm/转存/TVCN/W-我是余欢水-2020-[tmdb=101588]",
                "recognition_json": "{}",
            }
        ]

        report = bridge.format_quality_report(rows)

        self.assertIn("疑似错配", report)
        self.assertIn("航海王", report)
        self.assertIn("我是余欢水", report)


if __name__ == "__main__":
    unittest.main()

class EmbyRecheckTests(unittest.TestCase):
    def test_quality_keyboard_contains_recheck_buttons_for_issue_rows(self):
        rows = [
            {
                "id": 72,
                "title": "航海王 (1999) {tmdb=37854}",
                "emby_status": "confirmed",
                "emby_title": "我是余欢水",
                "emby_path": "/mnt/user/Unraid/strm/转存/TVCN/W-我是余欢水-2020-[tmdb=101588]",
                "recognition_json": "{}",
            }
        ]

        keyboard = bridge.quality_keyboard(rows)

        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "重新确认：72")
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "emby_recheck:72")

    def test_parse_emby_recheck_callback(self):
        self.assertEqual(bridge.parse_emby_recheck_callback("emby_recheck:72"), 72)
        self.assertIsNone(bridge.parse_emby_recheck_callback("emby_recheck:x"))
        self.assertIsNone(bridge.parse_emby_recheck_callback("cat:72:cn_tv"))

    def test_recheck_emby_row_updates_to_exact_tmdb_match(self):
        class FakeStore:
            def __init__(self):
                self.updated = None
            def update_emby(self, row_id, status, item_id=None, title=None, path=None, parent=None):
                self.updated = {
                    "id": row_id,
                    "emby_status": status,
                    "emby_item_id": item_id,
                    "emby_title": title,
                    "emby_path": path,
                    "emby_parent": parent,
                }
                return self.updated

        class FakeEmby:
            enabled = True
            def recent_items(self, limit=100):
                return [
                    {"Id": "bad", "Name": "我是余欢水", "Path": "/x/W-我是余欢水-2020-[tmdb=101588]", "ProviderIds": {"Tmdb": "101588"}},
                    {"Id": "good", "Name": "航海王", "Path": "/x/H-航海王-1999-[tmdb=37854]", "ProviderIds": {"Tmdb": "37854"}},
                ]
            def library_name_for_item(self, item):
                return "Strm番剧"

        row = {
            "id": 72,
            "title": "航海王 (1999) {tmdb=37854}",
            "recognition_json": "{}",
        }
        store = FakeStore()

        updated, message = bridge.recheck_emby_row(store, row, FakeEmby())

        self.assertIsNotNone(updated)
        self.assertEqual(updated["emby_item_id"], "good")
        self.assertEqual(updated["emby_title"], "航海王")
        self.assertEqual(updated["emby_parent"], "Strm番剧")
        self.assertIn("已重新确认", message)

class QualityNoiseTests(unittest.TestCase):
    def test_english_release_name_to_chinese_emby_title_is_not_flagged_without_tmdb_conflict(self):
        row = {
            "id": 54,
            "title": "Widows.Bay.S01.2160p.ATVP.WEB-DL.DDP5.1.Atmos.DV.H.265-HiveWeb",
            "emby_status": "confirmed",
            "emby_title": "寡妇湾",
            "emby_path": "/mnt/user/Unraid/strm/转存/TV/W-寡妇湾-2026-[tmdb=123456]",
            "recognition_json": "{}",
        }

        self.assertEqual(bridge.quality_issue_for_row(row), "")

class EmbyProviderSearchTests(unittest.TestCase):
    def test_find_item_by_tmdb_uses_emby_provider_query(self):
        class FakeEmby(bridge.EmbyClient):
            def __init__(self):
                self.calls = []
                self.user_id = "u1"
                self.base_url = "http://emby"
                self.api_key = "key"
                self.http = None
                self._library_roots = []
            @property
            def enabled(self):
                return True
            def get_user_id(self):
                return "u1"
            def _get(self, path, params=None):
                self.calls.append((path, dict(params or {})))
                return {"Items": [{"Id": "good", "Name": "航海王", "ProviderIds": {"Tmdb": "37854"}}]}

        emby = FakeEmby()
        item = emby.find_item_by_tmdb("37854")

        self.assertEqual(item["Id"], "good")
        self.assertEqual(emby.calls[0][1]["AnyProviderIdEquals"], "tmdb.37854")
        self.assertEqual(emby.calls[0][1]["Recursive"], "true")

    def test_recheck_emby_row_prefers_full_library_tmdb_search_before_recent_items(self):
        class FakeStore:
            def update_emby(self, row_id, status, item_id=None, title=None, path=None, parent=None):
                return {"id": row_id, "emby_status": status, "emby_item_id": item_id, "emby_title": title, "emby_path": path, "emby_parent": parent}

        class FakeEmby:
            enabled = True
            def __init__(self):
                self.recent_called = False
            def find_item_by_tmdb(self, tmdb_id):
                return {"Id": "good", "Name": "航海王", "Path": "/x/H-航海王-1999-[tmdb=37854]", "ProviderIds": {"Tmdb": tmdb_id}}
            def recent_items(self, limit=100):
                self.recent_called = True
                return []
            def library_name_for_item(self, item):
                return "Strm番剧"

        emby = FakeEmby()
        row = {"id": 72, "title": "航海王 (1999) {tmdb=37854}", "recognition_json": "{}"}

        updated, message = bridge.recheck_emby_row(FakeStore(), row, emby)

        self.assertFalse(emby.recent_called)
        self.assertEqual(updated["emby_item_id"], "good")
        self.assertIn("已重新确认", message)

class EmbyAutoMatchTests(unittest.TestCase):
    def test_find_emby_match_prefers_full_library_tmdb_lookup(self):
        class FakeEmby:
            enabled = True
            def __init__(self):
                self.recent_called = False
            def find_item_by_tmdb(self, tmdb_id):
                return {"Id": "exact", "Name": "航海王", "ProviderIds": {"Tmdb": tmdb_id}}
            def recent_items(self, limit=30):
                self.recent_called = True
                return [{"Id": "wrong", "Name": "我是余欢水", "ProviderIds": {"Tmdb": "101588"}}]

        emby = FakeEmby()
        row = {"title": "航海王 (1999) {tmdb=37854}"}
        recognition = {"title": "航海王", "tmdb_id": "37854"}

        match = bridge.find_emby_match(emby, recognition, row)

        self.assertEqual(match["Id"], "exact")
        self.assertFalse(emby.recent_called)

    def test_find_emby_match_falls_back_to_recent_items_without_tmdb(self):
        class FakeEmby:
            enabled = True
            def __init__(self):
                self.recent_called = False
            def find_item_by_tmdb(self, tmdb_id):
                raise AssertionError("tmdb lookup should not run")
            def recent_items(self, limit=30):
                self.recent_called = True
                return [{"Id": "recent", "Name": "Some Movie", "Path": "/x/Some Movie.strm"}]

        emby = FakeEmby()
        row = {"title": "Some Movie"}
        recognition = {"title": "Some Movie"}

        match = bridge.find_emby_match(emby, recognition, row)

        self.assertEqual(match["Id"], "recent")
        self.assertTrue(emby.recent_called)


class StatusRepairTests(unittest.TestCase):
    def test_submission_store_stale_for_repair_filters_confirmed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            submitted = store.upsert_submission(bridge.ShareKey("submitted", ""), "https://115cdn.com/s/submitted", "submitted")
            uncertain = store.upsert_submission(bridge.ShareKey("uncertain", ""), "https://115cdn.com/s/uncertain", "done")
            timed_out = store.upsert_submission(bridge.ShareKey("timeout", ""), "https://115cdn.com/s/timeout", "done")
            confirmed = store.upsert_submission(bridge.ShareKey("confirmed", ""), "https://115cdn.com/s/confirmed", "submitted")
            store.update_category(int(uncertain["id"]), None, "uncertain")
            store.update_emby(int(timed_out["id"]), "timeout")
            store.update_emby(int(confirmed["id"]), "confirmed")

            rows = store.stale_for_repair(limit=10)

        row_ids = {int(row["id"]) for row in rows}
        self.assertIn(int(submitted["id"]), row_ids)
        self.assertIn(int(uncertain["id"]), row_ids)
        self.assertIn(int(timed_out["id"]), row_ids)
        self.assertNotIn(int(confirmed["id"]), row_ids)

    def test_repair_stale_row_confirms_emby_tmdb_match(self):
        class FakeStore:
            def __init__(self):
                self.recognition = None
                self.category = None
                self.move = None
                self.emby = None
            def update_recognition(self, row_id, recognition, status):
                self.recognition = (row_id, dict(recognition), status)
                return {"id": row_id, "category_status": status}
            def update_category(self, row_id, choice, status):
                self.category = (row_id, choice, status)
                return {"id": row_id, "category_choice": choice, "category_status": status}
            def update_move(self, row_id, status, source_path=None, dest_path=None, category_final=None, error=None):
                self.move = (row_id, status, source_path, dest_path, category_final, error)
                return {"id": row_id, "move_status": status}
            def update_emby(self, row_id, status, item_id=None, title=None, path=None, parent=None):
                self.emby = (row_id, status, item_id, title, path, parent)
                return {"id": row_id, "emby_status": status}

        class FakeEmby:
            enabled = True
            def find_item_by_tmdb(self, tmdb_id):
                return {"Id": "emby-tv", "Name": "周二谋杀定律", "Path": "/mnt/user/Unraid/strm/转存/TV/Z-周二谋杀定律-2026-[tmdb=255522]", "Type": "Series", "ProviderIds": {"Tmdb": tmdb_id}}
            def library_name_for_item(self, item):
                return "Strm外国电视"

        row = {
            "id": 106,
            "title": "周二谋杀定律 (2026) {tmdb-255522}",
            "status": "submitted",
            "category_status": "uncertain",
            "emby_status": "timeout",
            "recognition_json": "{}",
        }
        store = FakeStore()

        repaired = bridge.repair_stale_submission(store, row, FakeEmby(), move_config=None)

        self.assertTrue(repaired)
        self.assertEqual(store.category, (106, "外国电视", "selected"))
        self.assertEqual(store.emby[1], "confirmed")
        self.assertEqual(store.emby[5], "Strm外国电视")
        self.assertEqual(store.move[1], "skipped")
        self.assertEqual(store.recognition[1]["category_status"], "cms_emby_resolved")

    def test_repair_stale_row_preserves_completed_self_share_move_and_marks_cleanup_pending(self):
        class FakeStore:
            def __init__(self):
                self.move = None
                self.cleanup = None
            def update_recognition(self, row_id, recognition, status):
                return {"id": row_id, "category_status": status}
            def update_category(self, row_id, choice, status):
                return {"id": row_id, "category_choice": choice, "category_status": status}
            def update_move(self, row_id, status, source_path=None, dest_path=None, category_final=None, error=None):
                self.move = (row_id, status, source_path, dest_path, category_final, error)
                return {"id": row_id, "move_status": status}
            def update_emby(self, row_id, status, item_id=None, title=None, path=None, parent=None):
                return {"id": row_id, "emby_status": status}
            def update_cleanup(self, row_id, status, file_id=None, error=None):
                self.cleanup = (row_id, status, file_id, error)
                return {"id": row_id, "cleanup_status": status}

        class FakeEmby:
            enabled = True
            def find_item_by_tmdb(self, tmdb_id):
                return {"Id": "emby-tv", "Name": "神奇数字马戏团", "Path": "/mnt/user/Unraid/strm/转存/TV/S-神奇数字马戏团-2023-[tmdb=261145]", "Type": "Series", "ProviderIds": {"Tmdb": tmdb_id}}
            def library_name_for_item(self, item):
                return "Strm外国电视"

        row = {
            "id": 130,
            "title": "The Amazing Digital Circus (2023)",
            "status": "done",
            "category_status": "probing",
            "emby_status": "timeout",
            "recognition_json": "{}",
            "workflow_mode": "self_share_sync",
            "own_share_file_id": "fid-final",
            "own_share_code": "swswdlc3wul",
            "own_share_file_name": "S-神奇数字马戏团-2023-[tmdb=261145]",
            "move_status": "moved",
            "dest_path": "/mnt/user/Unraid/strm/转存/TV/S-神奇数字马戏团-2023-[tmdb=261145]",
            "cleanup_status": "",
        }
        store = FakeStore()

        repaired = bridge.repair_stale_submission(store, row, FakeEmby(), move_config=None)

        self.assertTrue(repaired)
        self.assertIsNone(store.move)
        self.assertEqual(store.cleanup, (130, "pending", "fid-final", "等待确认后删除 115 转存源"))

    def test_stale_row_query_includes_timeout_and_uncertain_records(self):
        class FakeStore:
            def __init__(self):
                self.queries = []
            def stale_for_repair(self, limit=50):
                self.queries.append(limit)
                return []

        store = FakeStore()
        repaired = bridge.repair_stale_submissions(store, emby=None, move_config=None, limit=7)

        self.assertEqual(repaired, 0)
        self.assertEqual(store.queries, [7])
