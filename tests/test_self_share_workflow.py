import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).resolve().parents[1] / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class P115WebClientTests(unittest.TestCase):
    def test_requests_are_rate_limited_between_115_api_calls(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {"state": True, "data": []}

        now = [100.0]
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        client = bridge.P115WebClient(
            "UID=1",
            http=FakeHttp(),
            timeout=3,
            min_interval_seconds=2.0,
            clock=lambda: now[0],
            sleeper=fake_sleep,
        )

        client.search_files("first")
        client.search_files("second")

        self.assertEqual(sleeps, [2.0])

    def test_create_long_share_keeps_share_and_sets_permanent_duration(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []
            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {}), dict(params or {}), dict(headers or {})))
                if url.endswith("/share/send"):
                    return {"state": True, "data": {"share_code": "dummytest", "receive_code": "1212", "share_url": "https://115cdn.com/s/dummytest"}}
                if url.endswith("/share/updateshare"):
                    return {"state": True, "data": {"dummytest": {"share_ex_time": -1}}}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1;CID=2;SEID=3;KID=4", http=http, timeout=3)

        share = client.create_long_share("345")

        self.assertEqual(share["share_code"], "dummytest")
        self.assertEqual(share["receive_code"], "1212")
        self.assertEqual(share["share_url"], "https://115cdn.com/s/dummytest")
        self.assertEqual(http.calls[0][2]["file_ids"], "345")
        self.assertEqual(http.calls[1][2]["share_duration"], -1)
        self.assertNotIn("action", http.calls[1][2])

    def test_receive_share_to_cid_gets_snap_file_ids_then_receives_to_target_cid(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []
            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {}), dict(params or {})))
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "示例电影"},
                            "list": [{"fid": "fid-1", "n": "示例电影.mkv"}],
                        },
                    }
                if url.endswith("/share/receive"):
                    return {"state": True, "data": {"receive_title": "示例电影"}}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1;CID=2;SEID=3;KID=4", http=http, timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(result["title"], "示例电影")
        self.assertEqual(result["file_ids"], ["fid-1"])
        self.assertEqual(http.calls[0][0], "https://webapi.115.com/share/snap")
        self.assertEqual(http.calls[0][3]["share_code"], "abc")
        self.assertEqual(http.calls[1][0], "https://webapi.115.com/share/receive")
        self.assertEqual(http.calls[1][2]["file_id"], "fid-1")
        self.assertEqual(http.calls[1][2]["cid"], "pending-cid")


class OrganizedFolderSelectionTests(unittest.TestCase):
    def test_selects_tmdb_folder_outside_pending_redundant_and_exists_bins(self):
        items = [
            {"cid": "pending", "n": "高地战 (2011) {tmdb-79553}", "pid": "pending_root", "tu": "100"},
            {"cid": "redundant", "n": "高地战 (2011) {tmdb-79553}", "pid": "redundant_root", "tu": "101"},
            {"cid": "final", "n": "G-高地战-2011-[tmdb=79553]", "pid": "movie_root", "tu": "99"},
            {"fid": "file", "n": "高地战.2011.mkv", "cid": "final", "pid": "movie_root", "tu": "102"},
        ]

        selected = bridge.select_organized_115_folder(
            items,
            {"title": "高地战", "tmdb_id": "79553"},
            "高地战 (2011) {tmdb-79553}",
            excluded_parent_ids={"pending_root", "redundant_root", "exists_root"},
        )

        self.assertEqual(selected["file_id"], "final")
        self.assertEqual(selected["file_name"], "G-高地战-2011-[tmdb=79553]")

    def test_select_organized_folder_rejects_mismatched_tmdb_when_share_has_explicit_tmdb(self):
        items = [
            {"cid": "wrong", "n": "C-初恋了那么多年-2020-[tmdb=110493]", "pid": "tv_root", "tu": "200"},
        ]

        selected = bridge.select_organized_115_folder(
            items,
            {"title": "似是故人来", "tmdb_id": ""},
            "似是故人来 (1993) {tmdb-1049}",
            excluded_parent_ids=set(),
        )

        self.assertIsNone(selected)

    def test_select_organized_folder_rejects_mismatched_year_for_broad_chinese_token(self):
        items = [
            {"cid": "wrong", "n": "0-007：黑日危机-1999-[tmdb=36643]", "pid": "movie_root", "tu": "1782466088"},
        ]

        selected = bridge.select_organized_115_folder(
            items,
            {"ok": False, "title": "", "tmdb_id": ""},
            "危机13小时 (2016)",
            excluded_parent_ids=set(),
        )

        self.assertIsNone(selected)

    def test_find_organized_folder_searches_short_chinese_title_from_quality_folder_name(self):
        class FakeHttp:
            def __init__(self):
                self.queries = []
            def request(self, url, method="GET", data=None, headers=None, params=None):
                query = (params or {}).get("search_value", "")
                self.queries.append(query)
                if query == "蜘蛛侠":
                    return {
                        "state": True,
                        "data": [
                            {"cid": "target", "n": "Z-蜘蛛侠-2002-[tmdb=557]", "pid": "western_movie_root", "t": "1782033679"},
                        ],
                    }
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        selected = client.find_organized_folder(
            {"ok": False, "title": "", "tmdb_id": ""},
            "蜘蛛侠 4K原盘REMUX [HDR] [国英双语] [内封简英双字]",
            min_update_time=1782033600,
        )

        self.assertEqual(selected["file_id"], "target")
        self.assertIn("蜘蛛侠", http.queries)

    def test_find_organized_folder_searches_exact_tokens_before_tree_scan(self):
        class FakeHttp:
            def __init__(self):
                self.scan_calls = 0

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/files") and not url.endswith("/files/search"):
                    self.scan_calls += 1
                    return {"state": True, "data": []}
                query = (params or {}).get("search_value", "")
                if query == "1049":
                    return {
                        "state": True,
                        "data": [
                            {
                                "cid": "target",
                                "n": "S-似是故人来-1993-[tmdb=1049]",
                                "pid": "western-parent",
                                "dp": "欧美电影",
                                "t": "1782054787",
                            },
                            {
                                "cid": "wrong",
                                "n": "C-初恋了那么多年-2020-[tmdb=110493]",
                                "pid": "tv-parent",
                                "dp": "国产电视",
                                "t": "1782054790",
                            },
                        ],
                    }
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        selected = client.find_organized_folder(
            {"title": "似是故人来", "tmdb_id": ""},
            "似是故人来 (1993) {tmdb-1049}",
            scan_parent_ids={"exists-root"},
        )

        self.assertEqual(selected["file_id"], "target")
        self.assertEqual(http.scan_calls, 0)

    def test_find_organized_folder_falls_back_to_exists_tree_after_search_index_misses(self):
        class FakeHttp:
            def __init__(self):
                self.file_cids = []
                self.searches = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                params = params or {}
                if url.endswith("/files"):
                    cid = params.get("cid", "")
                    self.file_cids.append(cid)
                    tree = {
                        "exists-root": [{"cid": "movie-root", "pid": "exists-root", "n": "电影"}],
                        "movie-root": [{"cid": "western-root", "pid": "movie-root", "n": "欧美电影"}],
                        "western-root": [
                            {
                                "cid": "target",
                                "pid": "western-root",
                                "n": "Z-蜘蛛侠-2002-[tmdb=557]",
                                "t": "1782033679",
                            }
                        ],
                    }
                    return {"state": True, "data": tree.get(cid, [])}
                if url.endswith("/files/search"):
                    self.searches.append(params.get("search_value", ""))
                    return {"state": True, "data": []}
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        selected = client.find_organized_folder(
            {"ok": False, "title": "", "tmdb_id": ""},
            "蜘蛛侠 4K原盘REMUX [HDR] [国英双语] [内封简英双字]",
            min_update_time=1782033600,
            scan_parent_ids={"exists-root"},
        )

        self.assertEqual(selected["file_id"], "target")
        self.assertEqual(selected["category"], "欧美电影")
        self.assertEqual(http.file_cids, ["exists-root", "movie-root", "western-root"])
        self.assertEqual(http.searches, ["蜘蛛侠4k原盘remuxhdr国英双语内封简英双字", "蜘蛛侠"])

    def test_find_organized_folder_uses_search_before_scan_fallback(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                params = dict(params or {})
                self.calls.append((url, params))
                if url.endswith("/files"):
                    raise RuntimeError("HTTP 405 from https://webapi.115.com/files: Method Not Allowed")
                if url.endswith("/files/search") and params.get("search_value") == "556509":
                    return {
                        "state": True,
                        "data": [
                            {"cid": "target", "n": "S-娑婆诃-2019-[tmdb=556509]", "pid": "asia-parent", "tu": "1782050000"},
                        ],
                    }
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        selected = client.find_organized_folder(
            {"tmdb_id": "556509", "title": "娑婆诃"},
            "娑婆诃 (2019) {tmdb-556509}",
            scan_parent_ids={"exists-root"},
        )

        self.assertEqual(selected["file_id"], "target")
        self.assertFalse(any(call[0].endswith("/files") for call in http.calls))
        self.assertTrue(any(call[0].endswith("/files/search") for call in http.calls))

    def test_find_organized_folder_allows_direct_child_under_configured_exists_scan_root(self):
        class FakeHttp:
            def __init__(self):
                self.file_cids = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                params = params or {}
                if url.endswith("/files"):
                    cid = params.get("cid", "")
                    self.file_cids.append(cid)
                    tree = {
                        "exists-root": [{"cid": "folder-id", "pid": "exists-root", "n": "基督山伯爵士 4K原盘REMUX [HDR 杜比视界] [中英双字 简繁中字]"}],
                        "folder-id": [
                            {
                                "fid": "video-id",
                                "cid": "folder-id",
                                "pid": "folder-id",
                                "n": "Le.Comte.de.Monte-Cristo.2024.2160p.BluRay.REMUX.HDR.DV.mkv",
                                "t": "1782314401",
                            }
                        ],
                    }
                    return {"state": True, "data": tree.get(cid, [])}
                if url.endswith("/files/search"):
                    return {"state": True, "data": []}
                return {"state": True, "data": []}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        selected = client.find_organized_folder(
            {"ok": True, "title": "基督山伯爵", "tmdb_id": "1084736", "share_name": "Le.Comte.de.Monte-Cristo.2024.2160p.BluRay.REMUX.HDR.DV.mkv"},
            "基督山伯爵士 4K原盘REMUX [HDR 杜比视界] [中英双字 简繁中字]",
            excluded_parent_ids={"exists-root"},
            min_update_time=1782314300,
            scan_parent_ids={"exists-root"},
        )

        self.assertEqual(selected["file_id"], "folder-id")
        self.assertEqual(selected["file_name"], "基督山伯爵士 4K原盘REMUX [HDR 杜比视界] [中英双字 简繁中字]")
        self.assertEqual(client.http.file_cids, ["exists-root"])

    def test_find_organized_folder_scans_four_level_cms_library_tree(self):
        class FakeHttp:
            def __init__(self):
                self.file_cids = []
                self.searches = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                params = params or {}
                if url.endswith("/files"):
                    cid = params.get("cid", "")
                    self.file_cids.append(cid)
                    tree = {
                        "exists-root": [{"cid": "movie-root", "pid": "exists-root", "n": "Movie"}],
                        "movie-root": [{"cid": "movie-type-root", "pid": "movie-root", "n": "电影"}],
                        "movie-type-root": [{"cid": "asia-root", "pid": "movie-type-root", "n": "亚洲电影"}],
                        "asia-root": [
                            {
                                "cid": "target",
                                "pid": "asia-root",
                                "n": "W-无声-2020-[tmdb=606740]",
                                "t": "1782033679",
                            }
                        ],
                    }
                    return {"state": True, "data": tree.get(cid, [])}
                if url.endswith("/files/search"):
                    self.searches.append(params.get("search_value", ""))
                    return {"state": True, "data": []}
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        selected = client.find_organized_folder(
            {"ok": True, "title": "无声", "tmdb_id": "606740"},
            "无声 (2020)",
            min_update_time=1782033600,
            scan_parent_ids={"exists-root"},
        )

        self.assertEqual(selected["file_id"], "target")
        self.assertEqual(selected["category"], "亚洲电影")
        self.assertEqual(http.file_cids, ["exists-root", "movie-root", "movie-type-root", "asia-root"])
        self.assertEqual(http.searches, ["606740", "无声", "无声2020"])


    def test_find_organized_folder_falls_back_to_recent_tmdb_year_folder(self):
        class FakeHttp:
            def __init__(self):
                self.queries = []
            def request(self, url, method="GET", data=None, headers=None, params=None):
                query = (params or {}).get("search_value", "")
                self.queries.append(query)
                if query == "theamazingdigitalcircus2023":
                    return {
                        "state": True,
                        "data": [
                            {"cid": "source", "n": "The Amazing Digital Circus (2023)", "pid": "redundant", "t": "1000"},
                        ],
                    }
                if query in {"2023 tmdb", "2023"}:
                    return {
                        "state": True,
                        "data": [
                            {"cid": "target", "n": "S-神奇数字马戏团-2023-[tmdb=261145]", "pid": "tv_root", "t": "1016"},
                            {"cid": "old", "n": "N-奶龙-2023-[tmdb=221425]", "pid": "anime_root", "t": "200"},
                        ],
                    }
                return {"state": True, "data": []}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        selected = client.find_organized_folder(
            {"ok": False, "title": "", "tmdb_id": ""},
            "The Amazing Digital Circus (2023)",
            excluded_parent_ids={"redundant"},
        )

        self.assertEqual(selected["file_id"], "target")
        self.assertEqual(selected["file_name"], "S-神奇数字马戏团-2023-[tmdb=261145]")

    def test_find_organized_folder_does_not_match_unrelated_recent_tmdb_year_folder(self):
        class FakeHttp:
            def __init__(self):
                self.queries = []
            def request(self, url, method="GET", data=None, headers=None, params=None):
                query = (params or {}).get("search_value", "")
                self.queries.append(query)
                if query in {"house", "dragon", "龙之家族", "龙之家族第二季houseofthedragons022024uhdblurayremux2160phevcdovihdrtruehd71atmoscmct等2个文件夹"}:
                    return {
                        "state": True,
                        "data": [
                            {"cid": "exists", "n": "[龙之家族.第二季].House.of.the.Dragon.S02.2024.UHD.BluRay.Remux.2160p.HEVC.DoVi.HDR.TrueHD7.1.Atmos-CMCT", "pid": "exists_root", "t": "1781950669"},
                        ],
                    }
                if query in {"2024 tmdb", "2024"}:
                    return {
                        "state": True,
                        "data": [
                            {"cid": "wrong", "n": "G-诡才之道-2024-[tmdb=1006724]", "pid": "movie_root", "t": "1781928598"},
                        ],
                    }
                return {"state": True, "data": []}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        selected = client.find_organized_folder(
            {"ok": False, "title": "", "tmdb_id": ""},
            "[龙之家族.第二季].House.of.the.Dragon.S02.2024.UHD.BluRay.Remux.2160p.HEVC.DoVi.HDR.TrueHD7.1.Atmos-CMCT等2个文件(夹)",
            excluded_parent_ids={"exists_root"},
            min_update_time=1781950000,
        )

        self.assertIsNone(selected)

    def test_find_organized_folder_allows_recent_tmdb_year_folder_after_task_created(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                query = (params or {}).get("search_value", "")
                if query in {"2024 tmdb", "2024"}:
                    return {
                        "state": True,
                        "data": [
                            {"cid": "target", "n": "L-测试剧-2024-[tmdb=94997]", "pid": "tv_root", "t": "1781950800"},
                        ],
                    }
                return {"state": True, "data": []}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        selected = client.find_organized_folder(
            {"ok": False, "title": "", "tmdb_id": ""},
            "[龙之家族.第二季].House.of.the.Dragon.S02.2024.UHD.BluRay.Remux.2160p.HEVC.DoVi.HDR.TrueHD7.1.Atmos-CMCT等2个文件(夹)",
            min_update_time=1781950000,
        )

        self.assertEqual(selected["file_id"], "target")

    def test_find_organized_folder_with_tmdb_does_not_fallback_to_unrelated_year_match(self):
        class FakeHttp:
            def __init__(self):
                self.queries = []
            def request(self, url, method="GET", data=None, headers=None, params=None):
                query = (params or {}).get("search_value", "")
                self.queries.append(query)
                if query in {"1570664", "双喜", "doublehappiness20252160pnfwebdlddp51h265hivewebmkv"}:
                    return {"state": True, "data": []}
                if query in {"2025 tmdb", "2025"}:
                    return {
                        "state": True,
                        "data": [
                            {"cid": "wrong", "n": "D-得闲谨制-2025-[tmdb=1356454]", "pid": "movie_root", "t": "1781967089"},
                        ],
                    }
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        selected = client.find_organized_folder(
            {"ok": True, "title": "双喜", "tmdb_id": "1570664", "share_name": "Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv"},
            "Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv",
            min_update_time=1781967000,
        )

        self.assertIsNone(selected)
        self.assertNotIn("2025 tmdb", http.queries)
        self.assertNotIn("2025", http.queries)

    def test_select_source_residue_files_matches_recent_receive_file_by_title_year(self):
        items = [
            {"fid": "recent-file", "n": "银行家.2020.1080p.BluRay.REMUX.TrueHD.7.1.mkv", "cid": "recent", "tu": "1781962470"},
            {"fid": "old-file", "n": "银行家.2020.1080p.BluRay.REMUX.TrueHD.7.1.mkv", "cid": "recent", "tu": "1780000000"},
            {"fid": "wrong-file", "n": "我是余欢水.2020.S01E01.mkv", "cid": "recent", "tu": "1781962470"},
        ]

        selected = bridge.select_source_residue_115_files(
            items,
            {"title": "银行家", "tmdb_id": "627725"},
            "The.Banker.2020.1080p.BluRay.REMUX.AVC.DTS-HD.MA.TrueHD.7.1-FGT.mkv",
            excluded_file_ids={"organized-folder"},
            min_update_time=1781962277,
        )

        self.assertEqual([item["file_id"] for item in selected], ["recent-file"])


class SelfShareWorkflowTests(unittest.TestCase):
    def test_self_share_skipped_move_is_retryable(self):
        self.assertTrue(bridge.should_attempt_strm_move({"move_status": "skipped"}, self_share_enabled=True))
        self.assertFalse(bridge.should_attempt_strm_move({"move_status": "skipped"}, self_share_enabled=False))
        self.assertFalse(bridge.should_attempt_strm_move({"move_status": "moved"}, self_share_enabled=True))


    def test_self_share_does_not_wait_on_probing_recognition(self):
        row = {"category_status": "probing"}

        self.assertFalse(bridge.should_defer_for_probing(row, {"ok": False}, self_share_enabled=True))
        self.assertTrue(bridge.should_defer_for_probing(row, {"ok": False}, self_share_enabled=False))

    def test_prepare_triggers_auto_organize_creates_own_share_and_submits_share_sync_once(self):
        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1}
                self.updates = []
            def update_self_share(self, row_id, **fields):
                self.updates.append(fields)
                self.row.update(fields)
                return dict(self.row)

        class FakeCms:
            def __init__(self):
                self.auto_runs = 0
                self.sync_payloads = []
            def run_auto_organize(self):
                self.auto_runs += 1
                return {"code": 200}
            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                self.sync_payloads.append({"share_code": share_code, "receive_code": receive_code, "cid": cid, "local_path": local_path})
                return {"code": 200}

        class FakeP115:
            def __init__(self):
                self.searches = []
                self.created = []
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                self.searches.append((dict(recognition), share_name, set(excluded_parent_ids or [])))
                return {"file_id": "fid-final", "file_name": "G-高地战-2011-[tmdb=79553]"}
            def create_long_share(self, file_id):
                self.created.append(file_id)
                return {"share_code": "dummyown", "receive_code": "1212", "share_url": "https://115cdn.com/s/dummyown"}

        store = FakeStore()
        cms = FakeCms()
        p115 = FakeP115()
        config = bridge.SelfShareConfig(
            enabled=True,
            strm_root=Path("/tmp/no-such-root"),
            cms_local_path="/media/share",
            cms_cid="0",
            excluded_parent_ids={"pending_root"},
        )
        workflow = bridge.SelfShareWorkflow(config, cms, p115, store)

        row, source_path = workflow.prepare(dict(store.row), {"title": "高地战", "tmdb_id": "79553"}, "高地战 (2011) {tmdb-79553}")

        self.assertEqual(cms.auto_runs, 1)
        self.assertEqual(p115.created, ["fid-final"])
        self.assertEqual(cms.sync_payloads, [{"share_code": "dummyown", "receive_code": "1212", "cid": "0", "local_path": "/media/share"}])
        self.assertEqual(row["own_share_code"], "dummyown")
        self.assertEqual(row["own_share_file_id"], "fid-final")
        self.assertEqual(row["workflow_phase"], "share_sync_submitted")
        self.assertIsNone(source_path)



    def test_prepare_deletes_115_source_immediately_after_own_share_created(self):
        events = []

        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1}
                self.cleanup = None
            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)
            def update_cleanup(self, row_id, status, file_id=None, error=None):
                self.cleanup = {"row_id": row_id, "status": status, "file_id": file_id, "error": error}
                self.row.update({"cleanup_status": status, "cleanup_file_id": file_id, "cleanup_error": error})
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                events.append("organize")
                return {"code": 200}
            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                events.append(f"sync:{share_code}")
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "fid-final", "file_name": "G-高地战-2011-[tmdb=79553]"}
            def create_long_share(self, file_id):
                events.append(f"share:{file_id}")
                return {"share_code": "dummyown", "receive_code": "1212", "share_url": "https://115cdn.com/s/dummyown"}
            def delete_file(self, file_id):
                events.append(f"delete:{file_id}")
                return {"state": True}

        store = FakeStore()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path("/tmp/no-such-root"),
                cms_local_path="/media/share",
                cms_cid="0",
                cleanup_after_emby=True,
            ),
            FakeCms(),
            FakeP115(),
            store,
        )

        row, _source_path = workflow.prepare(dict(store.row), {"title": "高地战", "tmdb_id": "79553"}, "高地战 (2011) {tmdb-79553}")

        self.assertEqual(events, ["organize", "share:fid-final", "delete:fid-final", "sync:dummyown"])
        self.assertEqual(row["cleanup_status"], "deleted")
        self.assertEqual(store.cleanup["file_id"], "fid-final")

    def test_prepare_deletes_receive_residue_immediately_after_own_share_created(self):
        events = []

        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1781962277}
            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)
            def update_cleanup(self, row_id, status, file_id=None, error=None):
                self.row.update({"cleanup_status": status, "cleanup_file_id": file_id, "cleanup_error": error})
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                events.append("organize")
                return {"code": 200}
            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                events.append(f"sync:{share_code}")
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "fid-final", "file_name": "Y-银行家-2020-[tmdb=627725]"}
            def create_long_share(self, file_id):
                events.append(f"share:{file_id}")
                return {"share_code": "dummyown", "receive_code": "1212", "share_url": "https://115cdn.com/s/dummyown"}
            def find_source_residue_files(self, recognition, share_name, parent_ids, excluded_file_ids=None, min_update_time=0):
                events.append(f"find_residue:{','.join(sorted(parent_ids))}:{min_update_time}")
                return [{"file_id": "fid-recent", "file_name": "银行家.2020.1080p.mkv", "parent_id": "recent"}]
            def delete_file(self, file_id):
                events.append(f"delete:{file_id}")
                return {"state": True}

        store = FakeStore()
        config = bridge.SelfShareConfig(
            enabled=True,
            strm_root=Path("/tmp/no-such-root"),
            cms_local_path="/media/share",
            cms_cid="0",
            cleanup_after_emby=True,
        )
        config.source_cleanup_parent_ids = {"recent"}
        workflow = bridge.SelfShareWorkflow(config, FakeCms(), FakeP115(), store)

        row, _source_path = workflow.prepare(
            dict(store.row),
            {"title": "银行家", "tmdb_id": "627725"},
            "The.Banker.2020.1080p.BluRay.REMUX.AVC.DTS-HD.MA.TrueHD.7.1-FGT.mkv",
        )

        self.assertEqual(
            events,
            [
                "organize",
                "share:fid-final",
                "find_residue:recent:1781962277.0",
                "delete:fid-recent",
                "delete:fid-final",
                "sync:dummyown",
            ],
        )
        self.assertEqual(row["cleanup_status"], "deleted")

    def test_prepare_sets_category_from_organized_folder_parent_cid(self):
        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1}
                self.categories = []
            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)
            def update_category(self, row_id, category, status):
                self.categories.append((row_id, category, status))
                self.row.update({"category_choice": category, "category_status": status})
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                return {"code": 200}
            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "fid-final", "file_name": "S-神奇数字马戏团-2023-[tmdb=261145]", "parent_id": "3254119954860998447"}
            def create_long_share(self, file_id):
                return {"share_code": "dummyown", "receive_code": "1212", "share_url": "https://115cdn.com/s/dummyown"}

        store = FakeStore()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path("/tmp/no-such-root"),
                parent_cid_category_map={"3254119954860998447": "外国电视"},
            ),
            FakeCms(),
            FakeP115(),
            store,
        )

        row, _source_path = workflow.prepare(dict(store.row), {"ok": False}, "The Amazing Digital Circus (2023)")

        self.assertEqual(store.categories, [(8, "外国电视", "selected")])
        self.assertEqual(row["category_choice"], "外国电视")

    def test_prepare_enriches_recognition_from_organized_folder(self):
        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1}
                self.recognitions = []
            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)
            def update_category(self, row_id, category, status):
                self.row.update({"category_choice": category, "category_status": status})
                return dict(self.row)
            def update_recognition(self, row_id, recognition, status):
                self.recognitions.append((row_id, dict(recognition), status))
                self.row.update({"recognition_json": "stored", "category_status": status})
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                return {"code": 200}
            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "fid-final", "file_name": "S-神奇数字马戏团-2023-[tmdb=261145]", "parent_id": "3254119954860998447"}
            def create_long_share(self, file_id):
                return {"share_code": "dummyown", "receive_code": "1212", "share_url": "https://115cdn.com/s/dummyown"}

        store = FakeStore()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path("/tmp/no-such-root"),
                parent_cid_category_map={"3254119954860998447": "外国电视"},
            ),
            FakeCms(),
            FakeP115(),
            store,
        )

        workflow.prepare(dict(store.row), {"ok": False}, "The Amazing Digital Circus (2023)")

        self.assertEqual(store.recognitions[0][2], "self_share_resolved")
        self.assertEqual(store.recognitions[0][1]["tmdb_id"], "261145")
        self.assertEqual(store.recognitions[0][1]["category"], "外国电视")
        self.assertEqual(store.recognitions[0][1]["type"], "tv")

    def test_expected_tmdb_uses_self_share_folder_when_recognition_failed(self):
        row = {
            "title": "The Amazing Digital Circus (2023)",
            "own_share_file_name": "S-神奇数字马戏团-2023-[tmdb=261145]",
        }

        self.assertEqual(bridge.expected_task_tmdb_id({"ok": False, "tmdb_id": ""}, row), "261145")

    def test_self_share_prepare_recomputes_category_after_parent_mapping(self):
        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1000}
            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)
            def update_category(self, row_id, category, status):
                self.row.update({"category_choice": category, "category_status": status})
                return dict(self.row)
            def update_recognition(self, row_id, recognition, status):
                self.row.update({"recognition_json": "stored", "category_status": status})
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                return {"code": 200}
            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "fid-final", "file_name": "L-龙之家族-2022-[tmdb=94997]", "parent_id": "3254119954860998447"}
            def create_long_share(self, file_id):
                return {"share_code": "dummyown", "receive_code": "1212", "share_url": "https://115cdn.com/s/dummyown"}

        store = FakeStore()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path("/tmp/no-such-root"),
                parent_cid_category_map={"3254119954860998447": "外国电视"},
            ),
            FakeCms(),
            FakeP115(),
            store,
        )

        prepared_row, _source_path = workflow.prepare(dict(store.row), {"ok": False}, "House.of.the.Dragon.S02.2024")
        category = bridge.final_category_for_move(prepared_row, {"ok": False})

        self.assertEqual(category, "外国电视")

    def test_prepare_self_share_move_inputs_recomputes_category_after_prepare(self):
        class FakeWorkflow:
            config = bridge.SelfShareConfig(enabled=True, strm_root=Path("/share"))
            def prepare(self, row, recognition, title):
                prepared = dict(row)
                prepared["category_choice"] = "外国电视"
                return prepared, Path("/share/L-龙之家族-2022-[tmdb=94997]")

        row, source_dir, category = bridge.prepare_self_share_move_inputs(
            {"id": 8},
            {"ok": False},
            "House.of.the.Dragon.S02.2024",
            FakeWorkflow(),
            None,
        )

        self.assertEqual(row["category_choice"], "外国电视")
        self.assertEqual(source_dir, Path("/share/L-龙之家族-2022-[tmdb=94997]"))
        self.assertEqual(category, "外国电视")

    def test_self_share_source_selection_does_not_fall_back_to_library_dir(self):
        library_source = Path("/mnt/user/Unraid/strm/转存/TV/Q-权力的游戏前传：龙族-2022-[tmdb=94997]")

        selected = bridge.select_move_source_for_workflow(
            existing_source=library_source,
            prepared_self_share_source=None,
            self_share_enabled=True,
        )

        self.assertIsNone(selected)

    def test_resolve_self_share_recognition_uses_openai_tmdb_before_prepare(self):
        class FakeStore:
            def __init__(self):
                self.recognition = None
            def update_recognition(self, row_id, recognition, status):
                self.recognition = (row_id, dict(recognition), status)
                return {"id": row_id, "category_status": status}

        class FakeClassifier:
            enabled = True
            high_confidence = 0.75
            suggest_confidence = 0.45
            def classify_media(self, recognition, share_name):
                return {
                    "category": "外国电视",
                    "confidence": 0.92,
                    "media_type": "tv",
                    "title": "权力的游戏前传：龙族",
                    "tmdb_id": "94997",
                    "reason": "文件名包含 House.of.the.Dragon.S02",
                }

        store = FakeStore()

        updated_row, recognition = bridge.resolve_self_share_recognition_before_prepare(
            store,
            {"id": 8, "category_status": "probing"},
            {"ok": False, "title": "", "tmdb_id": ""},
            "[龙之家族.第二季].House.of.the.Dragon.S02.2024",
            openai_classifier=FakeClassifier(),
            tmdb_resolver=None,
        )

        self.assertEqual(updated_row["category_status"], "openai_confident")
        self.assertEqual(recognition["tmdb_id"], "94997")
        self.assertEqual(recognition["category"], "外国电视")
        self.assertEqual(store.recognition[2], "openai_confident")

    def test_emby_confirmation_deletes_own_share_source_after_strm_is_moved(self):
        class FakeStore:
            def __init__(self):
                self.cleanup = None
                self.emby = None
            def update_emby(self, row_id, status, item_id=None, title=None, path=None, parent=None):
                self.emby = {"id": row_id, "emby_status": status, "emby_item_id": item_id, "emby_title": title, "emby_path": path, "emby_parent": parent, "own_share_file_id": "fid-final", "own_share_code": "dummyown", "move_status": "moved"}
                return self.emby
            def update_cleanup(self, row_id, status, file_id=None, error=None):
                self.cleanup = {"row_id": row_id, "status": status, "file_id": file_id, "error": error}
                return dict(self.emby, cleanup_status=status, cleanup_file_id=file_id, cleanup_error=error)

        class FakeTelegram:
            def __init__(self):
                self.messages = []
            def send_message(self, chat_id, text, reply_markup=None):
                self.messages.append(text)

        class FakeP115:
            def __init__(self):
                self.deleted = []
                self.cancelled = []
            def delete_file(self, file_id):
                self.deleted.append(file_id)
                return {"state": True}
            def cancel_share(self, share_code):
                self.cancelled.append(share_code)

        store = FakeStore()
        telegram = FakeTelegram()
        p115 = FakeP115()
        row = {"id": 9, "own_share_file_id": "fid-final", "own_share_code": "dummyown"}
        item = {"Id": "emby1", "Name": "高地战", "Path": "/media/G-高地战-2011-[tmdb=79553]"}

        bridge.send_emby_confirmed(telegram, 464100862, store, row, item, emby=None, cleanup_client=p115)

        self.assertEqual(p115.deleted, ["fid-final"])
        self.assertEqual(p115.cancelled, [])
        self.assertEqual(store.cleanup["status"], "deleted")
        self.assertIn("115转存源已删除", telegram.messages[0])

    def test_cleanup_waits_until_own_share_is_created(self):
        class FakeStore:
            def __init__(self):
                self.cleanup = None
            def update_cleanup(self, row_id, status, file_id=None, error=None):
                self.cleanup = {"row_id": row_id, "status": status, "file_id": file_id, "error": error}
                return {"id": row_id, "cleanup_status": status, "cleanup_file_id": file_id, "cleanup_error": error}

        class FakeP115:
            def __init__(self):
                self.deleted = []
            def delete_file(self, file_id):
                self.deleted.append(file_id)
                return {"state": True}

        row = {"id": 9, "own_share_file_id": "fid-final", "move_status": "conflict"}
        store = FakeStore()
        p115 = FakeP115()

        _updated, line = bridge.cleanup_own_share_source(store, row, p115)

        self.assertEqual(p115.deleted, [])
        self.assertEqual(store.cleanup["status"], "pending")
        self.assertIn("等待自有分享创建完成", line)


    def test_cleanup_deletes_after_own_share_even_when_dest_has_no_strm_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "Movie" / "X-新·驯龙高手-2025-[tmdb=1087192]"

            class FakeStore:
                def __init__(self):
                    self.cleanup = None
                def update_cleanup(self, row_id, status, file_id=None, error=None):
                    self.cleanup = {"row_id": row_id, "status": status, "file_id": file_id, "error": error}
                    return {"id": row_id, "cleanup_status": status, "cleanup_file_id": file_id, "cleanup_error": error}

            class FakeP115:
                def __init__(self):
                    self.deleted = []
                def delete_file(self, file_id):
                    self.deleted.append(file_id)
                    return {"state": True}

            row = {
                "id": 9,
                "own_share_file_id": "fid-final",
                "own_share_code": "dummyown",
                "move_status": "moved",
                "dest_path": str(dest),
            }
            store = FakeStore()
            p115 = FakeP115()

            _updated, line = bridge.cleanup_own_share_source(store, row, p115)

            self.assertEqual(p115.deleted, ["fid-final"])
            self.assertEqual(store.cleanup["status"], "deleted")
            self.assertIn("115转存源已删除", line)

    def test_move_notification_reports_merged_conflict_as_moved(self):
        class FakeTelegram:
            def __init__(self):
                self.messages = []
            def send_message(self, chat_id, text, reply_markup=None):
                self.messages.append(text)

        moved_row = {"move_status": "moved", "dest_path": "/library/X-新·驯龙高手-2025-[tmdb=1087192]"}
        plan = bridge.MovePlan(
            status="conflict",
            reason="目标目录已存在，按策略跳过",
            source_path=Path("/share/X-新·驯龙高手-2025-[tmdb=1087192]"),
            dest_path=Path("/library/X-新·驯龙高手-2025-[tmdb=1087192]"),
            category="欧美电影",
        )
        telegram = FakeTelegram()

        bridge.send_move_result(telegram, 464100862, plan, moved_row)

        self.assertEqual(telegram.messages, ["STRM 已移动：/library/X-新·驯龙高手-2025-[tmdb=1087192]"])

    def test_self_share_source_preferred_over_existing_library_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_source = root / "share" / "H-环太平洋-2013-[tmdb=68726]"
            library_source = root / "Movie" / "H-环太平洋-2013-[tmdb=68726]"

            selected = bridge.select_move_source_for_workflow(
                existing_source=library_source,
                prepared_self_share_source=share_source,
                self_share_enabled=True,
            )

            self.assertEqual(selected, share_source)

    def test_self_share_move_config_allows_share_root_for_prepared_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            direct_root = root / "direct"
            movie_root = root / "Movie"
            share_source = share_root / "H-环太平洋-2013-[tmdb=68726]"
            config = bridge.MoveConfig(source_roots=[direct_root], library_roots={"欧美电影": movie_root}, stable_seconds=0)
            self_share = bridge.SelfShareConfig(enabled=True, strm_root=share_root)

            selected = bridge.move_config_for_workflow_source(config, share_source, self_share)

            self.assertEqual(selected.source_roots, [share_root])
            self.assertEqual(selected.library_roots, config.library_roots)

    def test_self_share_move_config_caps_stability_wait_for_share_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            share_source = share_root / "H-环太平洋-2013-[tmdb=68726]"
            config = bridge.MoveConfig(source_roots=[root / "direct"], library_roots={"欧美电影": movie_root}, stable_seconds=30)
            self_share = bridge.SelfShareConfig(enabled=True, strm_root=share_root)

            selected = bridge.move_config_for_workflow_source(config, share_source, self_share)

            self.assertEqual(selected.stable_seconds, 5)

    def test_merge_self_share_folder_rejects_direct_strm_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/d/direct.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "欧美电影")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("发现直链 STRM", updated["move_error"])
            self.assertFalse((dest / "movie.strm").exists())

    def test_merge_self_share_folder_rejects_uppercase_direct_strm_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "MOVIE.STRM").write_text("http://cms/d/direct.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "欧美电影")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("发现直链 STRM", updated["move_error"])
            self.assertFalse((dest / "MOVIE.STRM").exists())

    def test_merge_self_share_strm_folder_rejects_direct_strm_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "J-杰克・莱恩-2018-[tmdb=73375]"
            dest = root / "library" / "J-杰克・莱恩-2018-[tmdb=73375]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "episode.strm").write_text(
                "http://cms/d/file.mkv?/杰克・莱恩.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1212"),
                "https://115cdn.com/s/abc?password=1212",
                "submitted",
                title="杰克・莱恩 (2018) {tmdb-73375}",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
                own_share_receive_code="1212",
                own_share_file_name=source.name,
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "外国电视")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("发现直链 STRM", updated["move_error"])
            self.assertTrue((source / "episode.strm").exists())

    def test_merge_self_share_folder_rejects_unexpected_share_code_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/othershare_1212_file.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "欧美电影")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("STRM 不是预期的分享链接", updated["move_error"])
            self.assertFalse((dest / "movie.strm").exists())

    def test_merge_self_share_folder_rejects_tmdb_mismatched_folder_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Z-长安的荔枝-2025-[tmdb=1356587]"
            dest = root / "library" / "Z-长安的荔枝-2025-[tmdb=1356587]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/ownshare_1212_file.mp4", encoding="utf-8")
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
                title="S 沙尘暴(2025)",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
            ) or row
            row = store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "沙尘暴", "tmdb_id": "299165", "category": "外国电视", "type": "tv"},
                "confident",
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "外国电视")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("任务 TMDB 299165", updated["move_error"])
            self.assertIn("文件夹 TMDB 1356587", updated["move_error"])
            self.assertFalse((dest / "movie.strm").exists())

    def test_cleanup_pending_self_share_sources_after_move_and_emby_confirmed(self):
        class FakeP115:
            def __init__(self):
                self.deleted = []
            def delete_file(self, file_id):
                self.deleted.append(file_id)
                return {"state": True}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "library" / "H"
            dest.mkdir(parents=True)
            (dest / "环太平洋.strm").write_text("http://cms/s/swswyxm3wul_1212_1.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("dummyshare001", "pass001"), "https://115cdn.com/s/dummyshare001?password=pass001", "submitted", title="环太平洋 (2013)")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="fid-final",
                own_share_code="swswyxm3wul",
            )
            store.update_move(int(row["id"]), "moved", source_path="/share/H", dest_path=str(dest), category_final="欧美电影")
            store.update_emby(int(row["id"]), "confirmed", item_id="emby1", title="环太平洋", path=str(dest / "环太平洋.strm"), parent="Strm欧美电影")
            store.update_cleanup(int(row["id"]), "pending", file_id="fid-final", error="等待 STRM 移动完成")
            p115 = FakeP115()

            cleaned = bridge.cleanup_pending_self_share_sources(store, p115, limit=10)
            updated = store.find_by_id(int(row["id"]))

            self.assertEqual(cleaned, 1)
            self.assertEqual(p115.deleted, ["fid-final"])
            self.assertEqual(updated["cleanup_status"], "deleted")

    def test_repair_stranded_self_share_folder_moves_it_to_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            tv_root = root / "TV"
            source = share_root / "M-梦魇绝镇-2022-[tmdb=124364]"
            (source / "Season 01").mkdir(parents=True)
            (source / "Season 01" / "梦魇绝镇.strm").write_text(
                "http://cms/s/swswrepair_1212_1.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("dummyshare002", "pass002"), "https://115cdn.com/s/dummyshare002?password=pass002", "submitted", title="梦魇绝镇 (2022)")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name="M-梦魇绝镇-2022-[tmdb=124364]",
                own_share_code="swswrepair",
            )
            store.update_category(int(row["id"]), "外国电视", "selected")
            store.update_move(
                int(row["id"]),
                "skipped",
                source_path="/missing/old",
                dest_path="/missing/old",
                category_final="外国电视",
                error="CMS/Emby 已入库，无需人工分类",
            )
            config = bridge.MoveConfig(
                source_roots=[share_root],
                library_roots={"外国电视": tv_root},
                stable_seconds=0,
            )

            repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)
            updated = store.find_by_id(int(row["id"]))

            self.assertEqual(repaired, 1)
            self.assertFalse(source.exists())
            self.assertTrue((tv_root / "M-梦魇绝镇-2022-[tmdb=124364]" / "Season 01" / "梦魇绝镇.strm").exists())
            self.assertEqual(updated["move_status"], "moved")

    def test_repair_stranded_self_share_folder_merges_when_target_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            source = share_root / "Z-长安的荔枝-2025-[tmdb=1356587]"
            dest = movie_root / "Z-长安的荔枝-2025-[tmdb=1356587]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            strm_name = "长安的荔枝.strm"
            (source / strm_name).write_text("http://cms/s/swswmerge_1212_1.mp4", encoding="utf-8")
            (dest / strm_name).write_text("http://cms/d/direct.mp4", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("dummyshare003", "pass003"), "https://115cdn.com/s/dummyshare003?password=pass003", "submitted", title="长安的荔枝")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name="Z-长安的荔枝-2025-[tmdb=1356587]",
                own_share_code="swswmerge",
            )
            store.update_category(int(row["id"]), "华语电影", "selected")
            store.update_move(
                int(row["id"]),
                "conflict",
                source_path=str(source),
                dest_path=str(dest),
                category_final="华语电影",
                error="目标目录已存在，按策略跳过",
            )
            config = bridge.MoveConfig(
                source_roots=[share_root],
                library_roots={"华语电影": movie_root},
                stable_seconds=0,
            )

            repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)
            updated = store.find_by_id(int(row["id"]))

            self.assertEqual(repaired, 1)
            self.assertFalse(source.exists())
            self.assertEqual((dest / strm_name).read_text(encoding="utf-8"), "http://cms/s/swswmerge_1212_1.mp4")
            self.assertEqual(updated["move_status"], "moved")

    def test_remove_direct_strm_files_deletes_uppercase_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "library" / "Movie"
            dest.mkdir(parents=True)
            direct = dest / "MOVIE.STRM"
            direct.write_text("http://cms/d/direct.mp4", encoding="utf-8")

            removed = bridge.remove_direct_strm_files(dest)

            self.assertEqual(removed, 1)
            self.assertFalse(direct.exists())

    def test_restore_missing_self_share_library_folder_resubmits_share_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            dest = movie_root / "Y-一战再战-2025-[tmdb=1054867]"
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("dummyshare004", "pass004"), "https://115cdn.com/s/dummyshare004?password=pass004", "submitted", title="一战再战 (2025) {tmdb-1054867}")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name="Y-一战再战-2025-[tmdb=1054867]",
                own_share_code="swsw43a3wul",
                own_share_receive_code="1212",
                share_sync_status="submitted",
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "moved", source_path=str(share_root / dest.name), dest_path=str(dest), category_final="欧美电影")
            store.update_cleanup(int(row["id"]), "deleted", file_id="3455387442163482590")

            class FakeCms:
                def __init__(self):
                    self.sync_payloads = []
                def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                    self.sync_payloads.append({"share_code": share_code, "receive_code": receive_code, "cid": cid, "local_path": local_path})
                    return {"code": 200}

            cms = FakeCms()
            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms,
                bridge.SelfShareConfig(enabled=True, strm_root=share_root, cms_local_path="/media/share", cms_cid="0"),
                bridge.MoveConfig(source_roots=[share_root], library_roots={"欧美电影": movie_root}, stable_seconds=0),
                limit=10,
            )
            updated = store.find_by_id(int(row["id"]))

            self.assertEqual(restored, 0)
            self.assertEqual(cms.sync_payloads, [{"share_code": "swsw43a3wul", "receive_code": "1212", "cid": "0", "local_path": "/media/share"}])
            self.assertEqual(updated["workflow_phase"], "restore_share_sync_submitted")

    def test_restore_missing_self_share_library_folder_moves_regenerated_share_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            source = share_root / "Y-一战再战-2025-[tmdb=1054867]"
            dest = movie_root / "Y-一战再战-2025-[tmdb=1054867]"
            source.mkdir(parents=True)
            (source / "一战再战.strm").write_text("http://cms/s/swsw43a3wul_1212_3455387345258282790.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("dummyshare004", "pass004"), "https://115cdn.com/s/dummyshare004?password=pass004", "submitted", title="一战再战 (2025) {tmdb-1054867}")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="swsw43a3wul",
                own_share_receive_code="1212",
                share_sync_status="restore_submitted",
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "moved", source_path=str(source), dest_path=str(dest), category_final="欧美电影")
            store.update_cleanup(int(row["id"]), "deleted", file_id="3455387442163482590")

            class FakeCms:
                def __init__(self):
                    self.sync_payloads = []
                def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                    self.sync_payloads.append({"share_code": share_code, "receive_code": receive_code, "cid": cid, "local_path": local_path})
                    return {"code": 200}

            cms = FakeCms()
            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms,
                bridge.SelfShareConfig(enabled=True, strm_root=share_root, cms_local_path="/media/share", cms_cid="0"),
                bridge.MoveConfig(source_roots=[root / "direct"], library_roots={"欧美电影": movie_root}, stable_seconds=0),
                limit=10,
            )
            updated = store.find_by_id(int(row["id"]))

            self.assertEqual(restored, 1)
            self.assertFalse(source.exists())
            self.assertEqual((dest / "一战再战.strm").read_text(encoding="utf-8"), "http://cms/s/swsw43a3wul_1212_3455387345258282790.mkv")
            self.assertEqual(cms.sync_payloads, [])
            self.assertEqual(updated["move_status"], "moved")


if __name__ == "__main__":
    unittest.main()

class StrmStabilityTests(unittest.TestCase):
    def test_plan_strm_move_reports_stability_remaining_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "S-示例电影-2025-[tmdb=123456]"
            target_root = root / "Movie"
            source.mkdir(parents=True)
            strm = source / "示例.strm"
            strm.write_text("http://cms/s/demo", encoding="utf-8")
            now = time.time()
            recent = now - 10
            os.utime(source, (recent, recent))
            os.utime(strm, (recent, recent))
            config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"欧美电影": target_root},
                stable_seconds=30,
            )

            plan = bridge.plan_strm_move(source, "欧美电影", config)

            self.assertEqual(plan.status, "skipped")
            self.assertEqual(plan.reason, "STRM 源目录仍在更新")
            self.assertGreaterEqual(plan.metadata["stable_remaining_seconds"], 1)
            self.assertLessEqual(plan.metadata["stable_remaining_seconds"], 30)
            self.assertGreaterEqual(plan.metadata["newest_mtime"], recent)


class P115FailureHandlingTests(unittest.TestCase):
    def test_exact_self_share_folder_name_prevents_broad_sibling_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            sibling = share_root / "S-双喜-2025-[tmdb=123456]"
            sibling.mkdir(parents=True)
            (sibling / "movie.strm").write_text("http://cms/s/other_1212_file.mkv", encoding="utf-8")
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_file_name": "S-双喜-2025-[tmdb=654321]",
            }

            found = bridge.find_self_share_strm_source_dir(
                bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                row,
                {"title": "双喜", "tmdb_id": "123456"},
                "双喜",
            )

            self.assertIsNone(found)

    def test_maintenance_restore_skips_stale_completed_rows_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            dest = movie_root / "Y-旧任务-2025-[tmdb=1054867]"
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("oldshare", "pass"), "https://115cdn.com/s/oldshare?password=pass", "submitted", title="旧任务 (2025) {tmdb-1054867}")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=dest.name,
                own_share_code="swswold",
                own_share_receive_code="1212",
                share_sync_status="submitted",
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "moved", source_path=str(share_root / dest.name), dest_path=str(dest), category_final="欧美电影")
            old_updated_at = time.time() - 1000
            with store._lock, store._connection() as conn:
                conn.execute("UPDATE submissions SET updated_at = ? WHERE id = ?", (old_updated_at, int(row["id"])))

            class FakeCms:
                def __init__(self):
                    self.sync_payloads = []
                def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                    self.sync_payloads.append({"share_code": share_code, "receive_code": receive_code, "cid": cid, "local_path": local_path})
                    return {"code": 200}

            cms = FakeCms()
            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms,
                bridge.SelfShareConfig(enabled=True, strm_root=share_root, cms_local_path="/media/share", cms_cid="0"),
                bridge.MoveConfig(source_roots=[share_root], library_roots={"欧美电影": movie_root}, stable_seconds=0),
                limit=10,
                recent_seconds=60,
            )

            self.assertEqual(restored, 0)
            self.assertEqual(cms.sync_payloads, [])

    def test_delete_file_raises_when_115_returns_state_false_without_canceling_share(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {"state": False, "error": "删除操作尚未执行完成", "errno": 990009}

        client = bridge.P115WebClient("UID=1;CID=2;SEID=3;KID=4", http=FakeHttp(), timeout=3)

        with self.assertRaisesRegex(RuntimeError, "删除操作尚未执行完成"):
            client.delete_file("fid-final")

class ParentCidCategoryMapTests(unittest.TestCase):
    def test_env_parent_cid_category_map_overrides_default_mapping(self):
        env_value = "cid_movie=欧美电影,cid_tv=外国电视"

        mapping = bridge.parse_parent_cid_category_map(env_value)

        self.assertEqual(mapping["cid_movie"], "欧美电影")
        self.assertEqual(mapping["cid_tv"], "外国电视")
        self.assertEqual(bridge.category_for_115_parent_id("cid_movie", mapping), "欧美电影")
        self.assertEqual(bridge.category_for_115_parent_id("missing", mapping), "")

    def test_cms_client_reads_existing_folder_for_organized_scan(self):
        class FakeHttp:
            def request(self, url, method="POST", payload=None, headers=None):
                if url.endswith("/api/auth/login"):
                    return {"code": 200, "data": {"token": "token"}}
                if url.endswith("/api/config/auto_organize"):
                    return {"code": 200, "data": {"NEW_MEDIA_EXISTS_CID": "exists-cid"}}
                return {"code": 404}

        config = bridge.Config(
            tg_bot_token="tg",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
        )
        cms = bridge.CmsClient(config, http=FakeHttp())

        self.assertEqual(cms.auto_organize_existing_parent_ids(), {"exists-cid"})

    def test_cms_client_relogs_once_when_token_expires(self):
        class FakeHttp:
            def __init__(self):
                self.login_calls = 0
                self.authorized_headers = []

            def request(self, url, method="POST", payload=None, headers=None):
                if url.endswith("/api/auth/login"):
                    self.login_calls += 1
                    return {"code": 200, "data": {"token": f"token-{self.login_calls}"}}
                self.authorized_headers.append(dict(headers or {}))
                if len(self.authorized_headers) == 1:
                    raise RuntimeError("HTTP 401 from http://cms/api/sync/auto_organize: Unauthorized")
                return {"code": 200, "data": {}}

        config = bridge.Config(
            tg_bot_token="tg",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
        )
        http = FakeHttp()
        cms = bridge.CmsClient(config, http=http)

        resp = cms.run_auto_organize()

        self.assertEqual(resp["code"], 200)
        self.assertEqual(http.login_calls, 2)
        self.assertEqual(http.authorized_headers[0]["Authorization"], "Bearer token-1")
        self.assertEqual(http.authorized_headers[1]["Authorization"], "Bearer token-2")
