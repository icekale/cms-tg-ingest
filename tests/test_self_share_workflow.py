import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).resolve().parents[1] / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class P115WebClientTests(unittest.TestCase):
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

    def test_cleanup_waits_until_strm_move_is_done(self):
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
        self.assertIn("等待 STRM 移动完成", line)


    def test_cleanup_waits_when_moved_dest_has_no_strm_file(self):
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

            self.assertEqual(p115.deleted, [])
            self.assertEqual(store.cleanup["status"], "pending")
            self.assertIn("等待 STRM 文件确认", line)

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
            (source / "Season 01" / "梦魇绝镇.strm").write_text("http://example", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("dummyshare002", "pass002"), "https://115cdn.com/s/dummyshare002?password=pass002", "submitted", title="梦魇绝镇 (2022)")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name="M-梦魇绝镇-2022-[tmdb=124364]",
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
            (source / strm_name).write_text("http://cms/s/own-share.mp4", encoding="utf-8")
            (dest / strm_name).write_text("http://cms/d/direct.mp4", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(bridge.ShareKey("dummyshare003", "pass003"), "https://115cdn.com/s/dummyshare003?password=pass003", "submitted", title="长安的荔枝")
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name="Z-长安的荔枝-2025-[tmdb=1356587]",
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
            self.assertEqual((dest / strm_name).read_text(encoding="utf-8"), "http://cms/s/own-share.mp4")
            self.assertEqual(updated["move_status"], "moved")

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

class P115FailureHandlingTests(unittest.TestCase):
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
