import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.clients import cms as cms_client

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).resolve().parents[1] / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class P115WebClientTests(unittest.TestCase):
    def test_task_workflow_uses_persisted_receive_cid_override(self):
        workflow = bridge.BridgeSelfShareTaskWorkflow.__new__(bridge.BridgeSelfShareTaskWorkflow)
        workflow.receive_cid = "3298928530653445613"
        workflow.task_store = SimpleNamespace(
            get_self_share_receive_cid_override=lambda: "3481694068122059860"
        )

        self.assertEqual(workflow._configured_receive_cid(), "3481694068122059860")

    def test_share_snap_raises_unavailable_for_permanently_invalid_share(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.url = url
                self.params = params
                return {"state": False, "error": "分享已失效"}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaises(bridge.P115ShareUnavailableError):
            client.share_snap("invalid-share", "1212", limit=1)

    def test_share_snap_raises_unavailable_when_115_rejects_share_in_msg(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {"state": False, "msg": "分享已拒绝", "errno": 4100009}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaises(bridge.P115ShareUnavailableError):
            client.share_snap("rejected-share", "1212", limit=1)

    def test_share_snap_accepts_zero_share_state_when_request_succeeds(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {"state": True, "data": {"shareinfo": {"share_state": "0"}}}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        response = client.share_snap("valid-share", "1212", limit=1)

        self.assertTrue(response["state"])

    def test_inspect_share_preserves_numeric_zero_share_state(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {
                    "state": True,
                    "data": {"shareinfo": {"share_state": 0, "have_vio_file": 0}},
                }

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        status = client.inspect_share("valid-share", "1212")

        self.assertEqual(status["share_state"], "0")

    def test_inspect_share_reports_violation_flag_as_warning_not_invalid(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {
                    "state": True,
                    "data": {"shareinfo": {"share_state": "0", "have_vio_file": "1"}},
                }

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        status = client.inspect_share("valid-share", "1212")

        self.assertTrue(status["available"])
        self.assertTrue(status["have_vio_file"])
        self.assertEqual(status["share_state"], "0")

    def test_rename_file_uses_115_edit_endpoint(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, data))
                return {"state": True}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        client.rename_file("fid-1", "asset-42-abcd")

        self.assertEqual(http.calls[0][0], "https://webapi.115.com/files/edit")
        self.assertEqual(http.calls[0][1], "POST")
        self.assertEqual(http.calls[0][2], {"fid": "fid-1", "file_name": "asset-42-abcd"})

    def test_find_organized_folder_caps_search_and_scan_requests_at_eight(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, dict(params or {})))
                if url.endswith("/files"):
                    parent_id = str((params or {}).get("cid") or "")
                    if parent_id == "exists-root":
                        return {
                            "state": True,
                            "data": [
                                {"cid": f"child-{index}", "pid": parent_id, "n": f"child-{index}"}
                                for index in range(20)
                            ],
                        }
                    return {
                        "state": True,
                        "data": [{"cid": f"child-{parent_id}", "pid": parent_id, "n": f"child-{parent_id}"}],
                    }
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        result = client.find_organized_folder(
            {"title": "测试影片", "tmdb_id": "999999"},
            "测试影片 2026",
            scan_parent_ids={"exists-root"},
            return_scan_state=True,
        )

        self.assertIsNone(result["folder"])
        self.assertTrue(result["organized_scan_cursor"])
        self.assertLessEqual(len(http.calls), 8)

    def test_find_organized_folder_resumes_scan_from_cursor_without_relisting_root(self):
        class FakeHttp:
            def __init__(self):
                self.file_cids = []
                self.searches = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                params = params or {}
                if url.endswith("/files"):
                    parent_id = str(params.get("cid") or "")
                    self.file_cids.append(parent_id)
                    if parent_id == "exists-root":
                        return {
                            "state": True,
                            "data": [
                                {"cid": f"child-{index}", "pid": parent_id, "n": f"child-{index}"}
                                for index in range(10)
                            ],
                        }
                    if parent_id == "child-9":
                        return {
                            "state": True,
                            "data": [{"cid": "target", "pid": parent_id, "n": "T-目标-2026-[tmdb=999999]"}],
                        }
                    return {"state": True, "data": []}
                self.searches.append(dict(params))
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3, cache_ttl_seconds=0)
        recognition = {"title": "目标", "tmdb_id": "999999"}

        first = client.find_organized_folder(
            recognition,
            "目标 2026",
            scan_parent_ids={"exists-root"},
            return_scan_state=True,
        )
        self.assertIsNone(first["folder"])
        self.assertTrue(first["organized_scan_cursor"])
        first_calls = list(http.file_cids)
        first_search_count = len(http.searches)

        second = client.find_organized_folder(
            recognition,
            "目标 2026",
            scan_parent_ids={"exists-root"},
            organized_scan_cursor=first["organized_scan_cursor"],
            return_scan_state=True,
        )

        self.assertEqual(second["folder"]["file_id"], "target")
        self.assertIsNone(second["organized_scan_cursor"])
        self.assertNotIn("exists-root", http.file_cids[len(first_calls):])
        self.assertEqual(len(http.searches), first_search_count)

    def test_scan_organized_folders_persists_directory_page_offset(self):
        class FakeHttp:
            def __init__(self):
                self.offsets = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                params = params or {}
                if url.endswith("/files"):
                    self.offsets.append(int(params.get("offset") or 0))
                    if int(params.get("offset") or 0) == 0:
                        return {
                            "state": True,
                            "data": [
                                {"cid": "child-0", "pid": "exists-root", "n": "child-0"},
                                {"cid": "child-1", "pid": "exists-root", "n": "child-1"},
                            ],
                        }
                    return {
                        "state": True,
                        "data": [{"cid": "target", "pid": "exists-root", "n": "T-目标-2026-[tmdb=999999]"}],
                    }
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)
        recognition = {"title": "目标", "tmdb_id": "999999"}

        first = client.scan_organized_folders(
            {"exists-root"},
            limit=2,
            max_list_calls=1,
            recognition=recognition,
            share_name="目标 2026",
            return_scan_state=True,
        )
        second = client.scan_organized_folders(
            {"exists-root"},
            limit=2,
            max_list_calls=1,
            recognition=recognition,
            share_name="目标 2026",
            scan_cursor=first["organized_scan_cursor"],
            return_scan_state=True,
        )

        self.assertEqual(first["organized_scan_cursor"]["queue"][0]["offset"], 2)
        self.assertEqual(second["folders"][0]["cid"], "target")
        self.assertEqual(http.offsets, [0, 2])

    def test_find_organized_folder_propagates_115_risk_control_from_scan(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/files"):
                    raise bridge.P115RiskControlError("操作过于频繁，请稍后再试")
                return {"state": True, "data": []}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaises(bridge.P115RiskControlError):
            client.find_organized_folder(
                {"title": "测试影片", "tmdb_id": "999999"},
                "测试影片 2026",
                scan_parent_ids={"exists-root"},
                return_scan_state=True,
            )



class CmsPlaybackProbeTests(unittest.TestCase):
    def _client(self):
        config = bridge.Config(
            tg_bot_token="tg",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
        )
        return bridge.CmsClient(config)

    def test_probe_percent_encodes_unicode_and_spaces(self):
        captured = []

        class FakeResponse:
            status = 206

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        def fake_urlopen(request, timeout):
            captured.append((request.full_url, timeout))
            return FakeResponse()

        raw_url = "http://cms/s/code_1212_file.mkv?/幼女战记 (2017) - S01E01.mkv"
        with patch("app.clients.cms.urllib.request.urlopen", side_effect=fake_urlopen):
            try:
                result = self._client().probe_strm_url(raw_url)
            except Exception as exc:
                self.fail(f"probe rejected an encodable URL: {type(exc).__name__}")

        self.assertTrue(result)
        self.assertNotIn(" ", captured[0][0])
        self.assertIn("%E5%B9%BC%E5%A5%B3%E6%88%98%E8%AE%B0", captured[0][0])
        self.assertIn("%20%282017%29", captured[0][0])

    def test_probe_classifies_cms_share_resolution_failure(self):
        def fake_urlopen(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "Internal Server Error",
                {"Content-Type": "application/json"},
                io.BytesIO('"获取分享直连失败"'.encode("utf-8")),
            )

        with patch("app.clients.cms.urllib.request.urlopen", side_effect=fake_urlopen):
            try:
                self._client().probe_strm_url("http://cms/s/code_1212_file.mkv?/episode.mkv")
            except Exception as exc:
                self.assertEqual(type(exc).__name__, "CmsSharePlaybackUnavailableError")
            else:
                self.fail("probe did not classify the CMS share resolution failure")

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
        self.assertEqual(client.request_count, 2)

    def test_identical_get_requests_use_short_cache_without_extra_115_call(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, params))
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3, cache_ttl_seconds=5)

        client.search_files("same")
        client.search_files("same")

        self.assertEqual(len(http.calls), 1)
        self.assertEqual(client.request_count, 1)

    def test_post_request_invalidates_short_get_cache(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, data, params))
                if method == "POST":
                    return {"state": True}
                return {"state": True, "data": []}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3, cache_ttl_seconds=5)

        client.search_files("same")
        client.rename_file("fid-1", "renamed")
        client.search_files("same")

        self.assertEqual([call[1] for call in http.calls], ["GET", "POST", "GET"])

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

    def test_create_long_share_applies_preferred_receive_code(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {})))
                if url.endswith("/share/send"):
                    return {
                        "state": True,
                        "data": {
                            "share_code": "preferredtest",
                            "receive_code": "random",
                            "share_url": "https://115cdn.com/s/preferredtest",
                        },
                    }
                if url.endswith("/share/updateshare"):
                    return {"state": True}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        share = client.create_long_share("345", preferred_receive_code="a1B2")

        self.assertEqual(http.calls[1][2]["receive_code"], "a1B2")
        self.assertEqual(share["receive_code"], "a1B2")

    def test_create_long_share_uses_receive_code_returned_by_update(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/send"):
                    return {"state": True, "data": {"share_code": "updatedtest", "receive_code": "random"}}
                if url.endswith("/share/updateshare"):
                    return {"state": True, "data": {"receive_code": "Z9Y8"}}
                raise AssertionError(url)

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        share = client.create_long_share("345", preferred_receive_code="a1B2")

        self.assertEqual(share["receive_code"], "Z9Y8")

    def test_create_long_share_confirms_115_warning(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {})))
                if url.endswith("/share/send"):
                    return {"state": True, "data": {"share_code": "dummytest", "receive_code": "1212"}}
                if url.endswith("/share/updateshare"):
                    return {"state": True}
                raise AssertionError(url)

        http = FakeHttp()
        bridge.P115WebClient("UID=1", http=http, timeout=3).create_long_share("folder-id")

        self.assertEqual(http.calls[0][2]["ignore_warn"], 1)

    def test_create_long_share_reports_success_without_share_code_as_pending(self):
        from app.clients.p115 import P115SharePendingError

        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/send"):
                    return {"state": True, "data": {"share_state": "processing"}}
                raise AssertionError(url)

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaises(P115SharePendingError):
            client.create_long_share("folder-id")

    def test_create_long_share_extracts_share_code_from_processing_share_url(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, data))
                if url.endswith("/share/send"):
                    return {
                        "state": True,
                        "data": {
                            "share_state": "processing",
                            "share_url": "https://115cdn.com/s/swso9jn3wul?password=1212&#",
                        },
                    }
                if url.endswith("/share/updateshare"):
                    return {"state": True, "data": {}}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        result = client.create_long_share("folder-id", preferred_receive_code="1212")

        self.assertEqual(result["share_code"], "swso9jn3wul")
        self.assertEqual(http.calls[1][1]["share_code"], "swso9jn3wul")

    def test_list_own_share_states_returns_state_and_violation_flags_with_cache(self):
        class FakeHttp:
            def __init__(self):
                self.calls = 0

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls += 1
                assert url == "https://webapi.115.com/share/slist"
                return {
                    "state": True,
                    "data": {
                        "list": [
                            {"share_code": "share-zero", "share_state": 0, "have_vio_file": 0, "create_time": 9},
                            {"share_code": "share-a", "share_state": 1, "have_vio_file": 0, "create_time": 10},
                            {"share_code": "share-b", "share_state": 6, "have_vio_file": 1, "create_time": 11},
                        ]
                    },
                }

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3, share_list_cache_ttl_seconds=300)

        first = client.list_own_share_states()
        second = client.list_own_share_states()

        self.assertEqual(first["share-zero"], {"share_state": "0", "have_vio_file": False, "create_time": 9})
        self.assertEqual(first["share-a"], {"share_state": "1", "have_vio_file": False, "create_time": 10})
        self.assertEqual(first["share-b"], {"share_state": "6", "have_vio_file": True, "create_time": 11})
        self.assertEqual(first, second)
        self.assertEqual(http.calls, 1)

    def test_find_own_share_by_title_returns_the_latest_exact_match(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                assert url == "https://webapi.115.com/share/slist"
                return {
                    "state": True,
                    "data": {
                        "list": [
                            {"share_code": "other", "share_title": "其他", "create_time": 30},
                            {
                                "share_code": "older",
                                "share_title": "H-后天(2024)[tmdbid=435]",
                                "receive_code": "0000",
                                "share_url": "https://115cdn.com/s/older",
                                "create_time": 40,
                            },
                            {
                                "share_code": "latest",
                                "share_title": "H-后天(2024)[tmdbid=435]",
                                "receive_code": "1212",
                                "share_url": "https://115cdn.com/s/latest",
                                "create_time": 50,
                            },
                        ]
                    },
                }

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3, share_list_cache_ttl_seconds=0)

        result = client.find_own_share_by_title(
            "H-后天(2024)[tmdbid=435]",
            min_create_time=45,
        )

        self.assertEqual(result["share_code"], "latest")
        self.assertEqual(result["receive_code"], "1212")
        self.assertEqual(result["create_time"], "50.0")

    def test_find_own_share_by_title_returns_explicit_ambiguity_for_multiple_eligible_matches(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                assert url == "https://webapi.115.com/share/slist"
                return {
                    "state": True,
                    "data": {
                        "list": [
                            {
                                "share_code": "task-a-share",
                                "share_title": "Same title",
                                "receive_code": "1111",
                                "create_time": 1000,
                            },
                            {
                                "share_code": "task-b-share",
                                "share_title": "Same title",
                                "receive_code": "2222",
                                "create_time": 1001,
                            },
                        ]
                    },
                }

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3, share_list_cache_ttl_seconds=0)

        result = client.find_own_share_by_title("Same title", min_create_time=1000)

        self.assertEqual(result, {"recovery_status": "ambiguous", "match_count": 2})

    def test_find_own_share_by_title_rejects_missing_time_when_minimum_is_required(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                assert url == "https://webapi.115.com/share/slist"
                return {
                    "state": True,
                    "data": {
                        "list": [
                            {
                                "share_code": "missing-time",
                                "share_title": "Exact title",
                                "receive_code": "1212",
                            },
                            {
                                "share_code": "too-old",
                                "share_title": "Exact title",
                                "receive_code": "1212",
                                "create_time": 99,
                            },
                        ]
                    },
                }

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3, share_list_cache_ttl_seconds=0)

        result = client.find_own_share_by_title("Exact title", min_create_time=100)

        self.assertIsNone(result)

    def test_find_own_share_by_title_accepts_create_time_from_request_second(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {
                    "state": True,
                    "data": {
                        "list": [
                            {
                                "share_code": "same-second",
                                "share_title": "Exact title",
                                "receive_code": "1212",
                                "create_time": 1000,
                            }
                        ]
                    },
                }

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3, share_list_cache_ttl_seconds=0)

        result = client.find_own_share_by_title("Exact title", min_create_time=1000.999)

        self.assertIsNotNone(result)
        self.assertEqual(result["share_code"], "same-second")

    def test_find_own_share_by_title_rejects_create_time_before_request_second(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {
                    "state": True,
                    "data": {
                        "list": [
                            {
                                "share_code": "previous-second",
                                "share_title": "Exact title",
                                "receive_code": "1212",
                                "create_time": 999,
                            }
                        ]
                    },
                }

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3, share_list_cache_ttl_seconds=0)

        result = client.find_own_share_by_title("Exact title", min_create_time=1000.999)

        self.assertIsNone(result)


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

    def test_receive_share_to_cid_resolves_local_output_id_for_explicit_tmdb_name(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []
                self.after_receive = False

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {}), dict(params or {})))
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "123 (2026) {tmdb-1228710}"},
                            "list": [{"fid": "source-id", "n": "123 (2026) {tmdb-1228710}.mkv"}],
                        },
                    }
                if url.endswith("/share/receive"):
                    self.after_receive = True
                    return {"state": True, "data": {"receive_title": "123 (2026) {tmdb-1228710}"}}
                if url.endswith("/files"):
                    if not self.after_receive:
                        return {"state": True, "data": []}
                    return {
                        "state": True,
                        "data": [
                            {
                                "fid": "local-id",
                                "pid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                                "t": "1780000000",
                            }
                        ],
                    }
                raise AssertionError(url)

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(result["file_ids"], ["source-id"])
        self.assertEqual(result["received_items"], [{
            "file_id": "local-id",
            "file_name": "123 (2026) {tmdb-1228710}.mkv",
            "is_folder": False,
            "parent_id": "pending-cid",
            "received_item_verified": True,
        }])
        self.assertTrue(result["received_items_complete"])

    def test_receive_share_to_cid_does_not_reuse_old_same_name_item(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []
                self.after_receive = False

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {}), dict(params or {})))
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "123 (2026) {tmdb-1228710}"},
                            "list": [{"fid": "source-id", "n": "123 (2026) {tmdb-1228710}.mkv"}],
                        },
                    }
                if url.endswith("/share/receive"):
                    self.after_receive = True
                    return {"state": True, "data": {"receive_title": "123 (2026) {tmdb-1228710}"}}
                if url.endswith("/files"):
                    old = {
                        "fid": "old-local-id",
                        "pid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "t": "1780000000",
                    }
                    new = {
                        "fid": "new-local-id",
                        "pid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "t": "1780000100",
                    }
                    return {"state": True, "data": [old, new] if self.after_receive else [old]}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(result["received_items"][0]["file_id"], "new-local-id")
        self.assertEqual(result["received_existing_file_ids"], ["old-local-id"])
        self.assertTrue(result["received_snapshot_complete"])

    def test_receive_share_to_cid_defers_when_multiple_new_same_name_items_are_ambiguous(self):
        class FakeHttp:
            def __init__(self):
                self.after_receive = False

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "123 (2026) {tmdb-1228710}"},
                            "list": [{"fid": "source-id", "n": "123 (2026) {tmdb-1228710}.mkv"}],
                        },
                    }
                if url.endswith("/files"):
                    if not self.after_receive:
                        return {
                            "state": True,
                            "data": [{
                                "fid": "old-local-id",
                                "pid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                            }],
                        }
                    return {
                        "state": True,
                        "data": [
                            {
                                "fid": "old-local-id",
                                "pid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                            },
                            {
                                "fid": "new-local-a",
                                "pid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                                "t": "1780000100",
                            },
                            {
                                "fid": "new-local-b",
                                "pid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                                "t": "1780000200",
                            },
                        ],
                    }
                if url.endswith("/share/receive"):
                    self.after_receive = True
                    return {"state": True, "data": {"receive_title": "123 (2026) {tmdb-1228710}"}}
                raise AssertionError(url)

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(result["received_items"], [])
        self.assertFalse(result["received_items_complete"])

    def test_receive_share_to_cid_serializes_calls_across_client_instances(self):
        clients = [
            bridge.P115WebClient("UID=1", http=object(), timeout=3),
            bridge.P115WebClient("UID=1", http=object(), timeout=3),
        ]
        self.assertTrue(hasattr(clients[0], "_receive_share_to_cid"))
        active = 0
        maximum_active = 0
        counter_lock = threading.Lock()
        first_started = threading.Event()

        def fake_receive(*_args, **_kwargs):
            nonlocal active, maximum_active
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                first_started.set()
            time.sleep(0.05)
            with counter_lock:
                active -= 1
            return {"title": "", "file_ids": []}

        for client in clients:
            client._receive_share_to_cid = fake_receive
        threads = [
            threading.Thread(target=client.receive_share_to_cid, args=("abc", "1234", "pending-cid"))
            for client in clients
        ]
        threads[0].start()
        self.assertTrue(first_started.wait(timeout=1))
        threads[1].start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(maximum_active, 1)

    def test_receive_share_to_cid_defers_when_only_old_same_name_item_exists(self):
        class FakeHttp:
            def __init__(self):
                self.after_receive = False

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "123 (2026) {tmdb-1228710}"},
                            "list": [{"fid": "source-id", "n": "123 (2026) {tmdb-1228710}.mkv"}],
                        },
                    }
                if url.endswith("/share/receive"):
                    self.after_receive = True
                    return {"state": True, "data": {"receive_title": "123 (2026) {tmdb-1228710}"}}
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [{
                            "fid": "old-local-id",
                            "pid": "pending-cid",
                            "n": "123 (2026) {tmdb-1228710}.mkv",
                        }],
                    }
                raise AssertionError(url)

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(result["received_items"], [])
        self.assertFalse(result["received_items_complete"])
        self.assertEqual(result["received_existing_file_ids"], ["old-local-id"])

    def test_receive_share_to_cid_does_not_mark_a_full_pending_page_as_complete(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "123 (2026) {tmdb-1228710}"},
                            "list": [{"fid": "source-id", "n": "123 (2026) {tmdb-1228710}.mkv"}],
                        },
                    }
                if url.endswith("/share/receive"):
                    return {"state": True, "data": {"receive_title": "123 (2026) {tmdb-1228710}"}}
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {
                                "fid": f"old-local-{index}",
                                "pid": "pending-cid",
                                "n": f"old-{index}.mkv",
                            }
                            for index in range(500)
                        ],
                    }
                raise AssertionError(url)

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertFalse(result["received_snapshot_complete"])
        self.assertEqual(result["received_items"], [])

    def test_receive_share_to_cid_paginates_all_root_items_before_receiving(self):
        class FakeHttp:
            def __init__(self):
                self.snap_offsets = []
                self.received_file_ids = None

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    offset = int(params["offset"])
                    self.snap_offsets.append(offset)
                    items = [{"fid": f"fid-{index}"} for index in range(101)]
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "大型分享"},
                            "list": items[offset : offset + 100],
                        },
                    }
                if url.endswith("/share/receive"):
                    self.received_file_ids = data["file_id"].split(",")
                    return {"state": True, "data": {"receive_title": "大型分享"}}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(http.snap_offsets, [0, 100])
        self.assertEqual(len(result["file_ids"]), 101)
        self.assertEqual(len(set(result["file_ids"])), 101)
        self.assertEqual(http.received_file_ids, result["file_ids"])

    def test_receive_share_to_cid_deduplicates_root_file_ids_across_pages(self):
        class FakeHttp:
            def __init__(self):
                self.received_file_ids = None

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    offset = int(params["offset"])
                    page = [{"fid": f"fid-{index}"} for index in range(100)]
                    if offset == 100:
                        page = [{"fid": "fid-99"}, {"fid": "fid-100"}]
                    return {"state": True, "data": {"list": page}}
                if url.endswith("/share/receive"):
                    self.received_file_ids = data["file_id"].split(",")
                    return {"state": True, "data": {}}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(result["file_ids"], [f"fid-{index}" for index in range(101)])
        self.assertEqual(http.received_file_ids, result["file_ids"])

    def test_receive_share_to_cid_uses_fid_when_share_item_also_has_cid(self):
        class FakeHttp:
            def __init__(self):
                self.received_file_ids = None

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "双标识分享"},
                            "list": [
                                {"fid": "fid-1", "cid": "same-cid"},
                                {"fid": "fid-2", "cid": "same-cid"},
                            ],
                        },
                    }
                if url.endswith("/share/receive"):
                    self.received_file_ids = data["file_id"].split(",")
                    return {"state": True, "data": {}}
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        result = client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(result["file_ids"], ["fid-1", "fid-2"])
        self.assertEqual(http.received_file_ids, ["fid-1", "fid-2"])

    def test_share_root_items_caps_limit_99_requests_at_5000_entries_before_post(self):
        class FakeHttp:
            def __init__(self):
                self.snap_requests = []
                self.post_called = False

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    offset = int(params["offset"])
                    limit = int(params["limit"])
                    self.snap_requests.append((offset, limit))
                    return {
                        "state": True,
                        "data": {"list": [{"fid": f"fid-{index}"} for index in range(offset, offset + limit)]},
                    }
                self.post_called = True
                raise AssertionError("share receive must not be called")

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        with self.assertRaisesRegex(RuntimeError, "^115 share root exceeds 5000 entries$"):
            client.share_root_items("abc", "1234", limit=99)

        self.assertFalse(http.post_called)
        self.assertEqual(sum(limit for _offset, limit in http.snap_requests), 5000)
        self.assertLessEqual(max(offset + limit for offset, limit in http.snap_requests), 5000)

    def test_share_root_items_rejects_initial_offset_at_safety_boundary(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                raise AssertionError("share snap must not be requested beyond the safety boundary")

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaisesRegex(RuntimeError, "^115 share root exceeds 5000 entries$"):
            client.share_root_items("abc", "1234", offset=5000)

    def test_share_snap_rejects_offset_at_safety_boundary(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                raise AssertionError("share snap must not be requested beyond the safety boundary")

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaisesRegex(RuntimeError, "^115 share root exceeds 5000 entries$"):
            client.share_snap("abc", "1234", offset=5000)

    def test_receive_share_to_cid_rejects_root_at_safe_entry_limit_before_post(self):
        class FakeHttp:
            def __init__(self):
                self.snap_offsets = []
                self.receive_called = False

            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/share/snap"):
                    offset = int(params["offset"])
                    self.snap_offsets.append(offset)
                    return {
                        "state": True,
                        "data": {"list": [{"fid": f"fid-{index}"} for index in range(offset, offset + 100)]},
                    }
                if url.endswith("/share/receive"):
                    self.receive_called = True
                    raise AssertionError("receive must not be called")
                raise AssertionError(url)

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        with self.assertRaisesRegex(RuntimeError, "^115 share root exceeds 5000 entries$"):
            client.receive_share_to_cid("abc", "1234", "pending-cid")

        self.assertEqual(http.snap_offsets, list(range(0, 5000, 100)))
        self.assertFalse(http.receive_called)

    def test_115_risk_control_response_raises_specific_error(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {"state": False, "error": "操作过于频繁，请稍后再试"}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaises(bridge.P115RiskControlError):
            client.search_files("蜘蛛侠")

    def test_115_share_restriction_raises_specific_error(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                return {"state": False, "error": "你已被限制分享，如有疑问请联系客服"}

        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        with self.assertRaises(bridge.P115RiskControlError):
            client.create_long_share("folder-id")


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

    def test_find_organized_folder_stops_after_tmdb_search_hit(self):
        class FakeHttp:
            def __init__(self):
                self.queries = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                query = (params or {}).get("search_value", "")
                self.queries.append(query)
                if query == "556509":
                    return {
                        "state": True,
                        "data": [
                            {"cid": "target", "n": "S-娑婆诃-2019-[tmdb=556509]", "pid": "asia-parent", "tu": "1782050000"},
                        ],
                    }
                raise AssertionError(f"unexpected extra 115 search: {query}")

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        selected = client.find_organized_folder(
            {"tmdb_id": "556509", "title": "娑婆诃"},
            "娑婆诃 (2019) {tmdb-556509}",
            scan_parent_ids={"exists-root"},
        )

        self.assertEqual(selected["file_id"], "target")
        self.assertEqual(http.queries, ["556509"])

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

    def test_scan_organized_folders_respects_list_call_budget(self):
        class FakeHttp:
            def __init__(self):
                self.file_cids = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                cid = (params or {}).get("cid", "")
                self.file_cids.append(cid)
                tree = {
                    "exists-root": [
                        {"cid": "movie-root", "pid": "exists-root", "n": "Movie"},
                        {"cid": "tv-root", "pid": "exists-root", "n": "TV"},
                    ],
                    "movie-root": [{"cid": "western-root", "pid": "movie-root", "n": "欧美电影"}],
                    "tv-root": [{"cid": "foreign-tv-root", "pid": "tv-root", "n": "外国电视"}],
                }
                return {"state": True, "data": tree.get(cid, [])}

        http = FakeHttp()
        client = bridge.P115WebClient("UID=1", http=http, timeout=3)

        folders = client.scan_organized_folders({"exists-root"}, max_list_calls=2)

        self.assertEqual(http.file_cids, ["exists-root", "movie-root"])
        self.assertEqual([folder["cid"] for folder in folders], ["movie-root", "tv-root", "western-root"])

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

    def test_select_source_residue_files_rejects_candidates_without_remote_timestamp(self):
        selected = bridge.select_source_residue_115_files(
            [
                {
                    "fid": "unknown-time",
                    "n": "银行家.2020.1080p.BluRay.mkv",
                    "cid": "receive",
                }
            ],
            {"title": "银行家", "tmdb_id": "627725"},
            "The.Banker.2020.1080p.BluRay.mkv",
        )

        self.assertEqual(selected, [])

    def test_select_source_residue_files_rejects_partial_title_match(self):
        selected = bridge.select_source_residue_115_files(
            [
                {
                    "fid": "different-movie",
                    "n": "银行家传奇.2020.1080p.BluRay.mkv",
                    "cid": "receive",
                    "tu": "1781962470",
                }
            ],
            {"title": "银行家", "tmdb_id": "627725"},
            "The.Banker.2020.1080p.BluRay.mkv",
            min_update_time=1781962277,
        )

        self.assertEqual(selected, [])

    def test_select_source_residue_files_rejects_conflicting_release_year(self):
        selected = bridge.select_source_residue_115_files(
            [
                {
                    "fid": "wrong-year",
                    "n": "Crash.1996.1080p.BluRay.mkv",
                    "cid": "receive",
                    "tu": "1781962470",
                }
            ],
            {"title": "Crash"},
            "Crash.2004.1080p.BluRay.mkv",
            min_update_time=1781962277,
        )

        self.assertEqual(selected, [])


class SelfShareWorkflowTests(unittest.TestCase):
    def test_self_share_maintenance_loop_honors_stop_event(self):
        stop_event = threading.Event()
        stop_event.set()

        thread = bridge.start_self_share_maintenance_loop(
            None,
            None,
            None,
            None,
            interval_seconds=1,
            stop_event=stop_event,
        )

        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

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
            def create_long_share(self, file_id, preferred_receive_code=""):
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

    def test_legacy_prepare_uses_task_store_receive_code_override(self):
        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1}

            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                return {"code": 200}

            def add_share115_sync_task(self, *args, **kwargs):
                return {"code": 200}

        class FakeP115:
            def __init__(self):
                self.preferred_receive_code = ""

            def find_organized_folder(self, *args, **kwargs):
                return {"file_id": "fid-final", "file_name": "G-高地战-2011-[tmdb=79553]"}

            def create_long_share(self, file_id, preferred_receive_code=""):
                self.preferred_receive_code = preferred_receive_code
                return {
                    "share_code": "dummyown",
                    "receive_code": preferred_receive_code,
                    "share_url": "https://115cdn.com/s/dummyown",
                }

        with tempfile.TemporaryDirectory() as tmp:
            task_store = bridge.TaskStore(Path(tmp) / "tasks.db")
            task_store.set_own_share_receive_code_override("web9")
            p115 = FakeP115()
            workflow = bridge.SelfShareWorkflow(
                bridge.SelfShareConfig(enabled=True, cms_state_db_path=Path(tmp) / "missing-cms.db"),
                FakeCms(),
                p115,
                FakeStore(),
                settings_store=task_store,
            )

            workflow.prepare({"id": 8, "created_at": 1}, {"title": "高地战", "tmdb_id": "79553"}, "高地战")

        self.assertEqual(p115.preferred_receive_code, "web9")



    def test_prepare_does_not_delete_115_source_before_task_runner_review(self):
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

            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                events.append(f"sync:{share_code}")
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "fid-final", "file_name": "G-高地战-2011-[tmdb=79553]"}
            def create_long_share(self, file_id, preferred_receive_code=""):
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

        self.assertEqual(events, ["organize", "share:fid-final", "sync:dummyown"])
        self.assertIsNone(store.cleanup)
        self.assertNotEqual(row.get("cleanup_status"), "deleted")

    def test_prepare_does_not_scan_or_delete_receive_residue_before_task_runner_review(self):
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

            def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                events.append(f"sync:{share_code}")
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "fid-final", "file_name": "Y-银行家-2020-[tmdb=627725]"}
            def create_long_share(self, file_id, preferred_receive_code=""):
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

        self.assertEqual(events, ["organize", "share:fid-final", "sync:dummyown"])
        self.assertNotEqual(row.get("cleanup_status"), "deleted")

    def test_prepare_rejects_cms_folder_with_tmdb_id_different_from_source_hint(self):
        events = []

        class FakeStore:
            def __init__(self):
                self.row = {
                    "id": 8,
                    "created_at": 1781962277,
                    "title": "123 (2026) {tmdb-1228710}",
                }

            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                events.append("organize")

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "wrong-folder", "file_name": "1-123-2026-[tmdb=952936]"}

        store = FakeStore()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(enabled=True, strm_root=Path("/tmp/no-such-root")),
            FakeCms(),
            FakeP115(),
            store,
        )

        row, source_path = workflow.prepare(
            dict(store.row),
            {"ok": True, "title": "错误影片", "tmdb_id": "952936", "category": "欧美电影", "type": "movie"},
            "错误影片",
        )

        self.assertIsNone(source_path)
        self.assertEqual(events, ["organize"])
        self.assertNotIn("own_share_file_id", row)

    def test_prepare_rejects_unmarked_cms_folder_when_source_has_explicit_tmdb_hint(self):
        events = []

        class FakeStore:
            def __init__(self):
                self.row = {
                    "id": 8,
                    "created_at": 1781962277,
                    "title": "123 (2026) {tmdb-1228710}",
                }

            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                events.append("organize")

            def add_share115_sync_task(self, *args, **kwargs):
                events.append("sync")
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                return {"file_id": "unmarked-folder", "file_name": "123 (2026)"}

            def create_long_share(self, file_id, preferred_receive_code=""):
                events.append(f"share:{file_id}")
                return {"share_code": "should-not-exist", "receive_code": "1212"}

        store = FakeStore()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(enabled=True, strm_root=Path("/tmp/no-such-root")),
            FakeCms(),
            FakeP115(),
            store,
        )

        row, source_path = workflow.prepare(
            dict(store.row),
            {"ok": True, "title": "错误影片", "tmdb_id": "952936", "category": "欧美电影", "type": "movie"},
            "错误影片",
        )

        self.assertIsNone(source_path)
        self.assertEqual(events, ["organize"])
        self.assertNotIn("own_share_file_id", row)

    def test_legacy_prepare_anchors_folder_search_to_explicit_source_tmdb(self):
        calls = []

        class FakeStore:
            def __init__(self):
                self.row = {"id": 8, "created_at": 1781962277, "title": "123 (2026) {tmdb-1228710}"}

            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)

        class FakeCms:
            def run_auto_organize(self):
                return {"code": 200}

            def add_share115_sync_task(self, *args, **kwargs):
                return {"code": 200}

        class FakeP115:
            def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
                calls.append(dict(recognition))
                if recognition.get("tmdb_id") != "1228710":
                    return None
                return {"file_id": "correct-folder", "file_name": "1-正确影片-2026-[tmdb=1228710]", "category": "欧美电影"}

            def create_long_share(self, file_id, preferred_receive_code=""):
                return {"share_code": "own-share", "receive_code": "1212"}

        store = FakeStore()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(enabled=True, strm_root=Path("/tmp/no-such-root")),
            FakeCms(),
            FakeP115(),
            store,
        )

        row, _source_path = workflow.prepare(
            dict(store.row),
            {"ok": True, "title": "错误影片", "tmdb_id": "952936", "category": "欧美电影", "type": "movie"},
            "123 (2026) {tmdb-1228710}",
        )

        self.assertEqual(calls[0]["tmdb_id"], "1228710")
        self.assertEqual(row["own_share_file_id"], "correct-folder")

    def test_prepare_rejects_existing_self_share_state_with_wrong_tmdb_identity(self):
        class FakeStore:
            def __init__(self):
                self.row = {
                    "id": 8,
                    "created_at": 1781962277,
                    "title": "123 (2026) {tmdb-1228710}",
                    "workflow_mode": "self_share_sync",
                    "workflow_phase": "organized_found",
                    "own_share_file_id": "wrong-folder",
                    "own_share_file_name": "1-123-2026-[tmdb=952936]",
                }

            def update_self_share(self, row_id, **fields):
                self.row.update(fields)
                return dict(self.row)

        class FakeCms:
            def add_share115_sync_task(self, *args, **kwargs):
                raise AssertionError("must not submit a share sync for stale state")

        class FakeP115:
            def __init__(self):
                self.created = []

            def create_long_share(self, file_id, preferred_receive_code=""):
                self.created.append(file_id)
                return {"share_code": "should-not-exist", "receive_code": "1212"}

        store = FakeStore()
        p115 = FakeP115()
        workflow = bridge.SelfShareWorkflow(
            bridge.SelfShareConfig(enabled=True, strm_root=Path("/tmp/no-such-root")),
            FakeCms(),
            p115,
            store,
        )

        row, source_path = workflow.prepare(
            dict(store.row),
            {"ok": True, "title": "错误影片", "tmdb_id": "952936", "category": "欧美电影", "type": "movie"},
            "123 (2026) {tmdb-1228710}",
        )

        self.assertIsNone(source_path)
        self.assertEqual(p115.created, [])
        self.assertEqual(row["own_share_file_id"], "wrong-folder")

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
            def create_long_share(self, file_id, preferred_receive_code=""):
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
            def create_long_share(self, file_id, preferred_receive_code=""):
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
            def create_long_share(self, file_id, preferred_receive_code=""):
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

    def test_legacy_emby_confirmation_does_not_delete_own_share_source(self):
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

        self.assertEqual(p115.deleted, [])
        self.assertEqual(p115.cancelled, [])
        self.assertIsNone(store.cleanup)
        self.assertNotIn("115转存源已删除", telegram.messages[0])

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

    def test_execute_strm_move_journals_moving_before_filesystem_and_moved_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            events = []

            class FakeStore:
                def __init__(self):
                    self.row = {"id": 1, "move_status": "error", "move_error": "stale"}
                    self.updates = []

                def update_move(self, row_id, status, **fields):
                    events.append(f"store:{status}")
                    self.updates.append((status, fields))
                    self.row.update({"move_status": status, "move_error": fields.get("error")})
                    return dict(self.row)

            store = FakeStore()
            plan = bridge.MovePlan("pending", "ready", source, dest, "华语电影")

            with patch("app.media.strm.Path.mkdir", side_effect=lambda *args, **kwargs: events.append("mkdir")), patch(
                "app.media.strm.shutil.move", side_effect=lambda *args, **kwargs: events.append("move")
            ):
                updated = bridge.execute_strm_move(plan, store, {"id": 1})

            self.assertEqual(events, ["store:moving", "mkdir", "move", "store:moved"])
            self.assertEqual(store.updates[0][0], "moving")
            self.assertEqual(store.updates[0][1]["source_path"], str(source))
            self.assertEqual(store.updates[0][1]["dest_path"], str(dest))
            self.assertEqual(store.updates[0][1]["category_final"], "华语电影")
            self.assertEqual(store.updates[0][1]["error"], "")
            self.assertEqual(updated["move_status"], "moved")

    def test_execute_strm_move_does_not_touch_filesystem_when_moving_prewrite_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)

            class RaisingStore:
                def update_move(self, row_id, status, **fields):
                    raise RuntimeError("journal unavailable")

            plan = bridge.MovePlan("pending", "ready", source, dest, "华语电影")
            with patch("app.media.strm.Path.mkdir") as mkdir, patch("app.media.strm.shutil.move") as move:
                with self.assertRaisesRegex(RuntimeError, "journal unavailable"):
                    bridge.execute_strm_move(plan, RaisingStore(), {"id": 1})

            mkdir.assert_not_called()
            move.assert_not_called()

    def test_execute_strm_move_does_not_touch_filesystem_when_moving_prewrite_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            calls = []

            class UnconfirmedStore:
                def update_move(self, row_id, status, **fields):
                    calls.append((status, fields))
                    return {"id": row_id, "move_status": "error", "move_error": "journal rejected"}

            plan = bridge.MovePlan("pending", "ready", source, dest, "华语电影")
            with patch("app.media.strm.Path.mkdir") as mkdir, patch("app.media.strm.shutil.move") as move:
                updated = bridge.execute_strm_move(plan, UnconfirmedStore(), {"id": 1})

            mkdir.assert_not_called()
            move.assert_not_called()
            self.assertEqual([status for status, _fields in calls], ["moving"])
            self.assertEqual(updated["move_status"], "error")

    def test_execute_strm_move_records_error_when_destination_mkdir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)

            class FakeStore:
                def __init__(self):
                    self.statuses = []

                def update_move(self, row_id, status, **fields):
                    self.statuses.append((status, fields))
                    return {
                        "id": row_id,
                        "move_status": status,
                        "move_error": fields.get("error"),
                    }

            store = FakeStore()
            plan = bridge.MovePlan("pending", "ready", source, dest, "华语电影")
            with patch("app.media.strm.Path.mkdir", side_effect=OSError("mkdir failed")), patch(
                "app.media.strm.shutil.move"
            ) as move:
                updated = bridge.execute_strm_move(plan, store, {"id": 1})

            self.assertEqual([status for status, _fields in store.statuses], ["moving", "error"])
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "mkdir failed")
            move.assert_not_called()

    def test_merge_self_share_folder_does_not_touch_filesystem_when_moving_is_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text("https://115.com/s/share_1212_movie.mkv", encoding="utf-8")
            existing = dest / "movie.strm"
            existing.write_text("https://115.com/d/existing/movie.mkv", encoding="utf-8")

            class UnconfirmedStore:
                def update_move(self, row_id, status, **fields):
                    return {"id": row_id, "move_status": "error", "move_error": "journal rejected"}

            plan = bridge.MovePlan("conflict", "ready", source, dest, "华语电影")
            with patch("app.media.strm.Path.mkdir") as mkdir, patch("app.media.strm.shutil.copy2") as copy2, patch(
                "app.media.strm.shutil.rmtree"
            ) as rmtree, patch("os.replace") as replace:
                updated = bridge.merge_self_share_strm_folder(
                    plan,
                    UnconfirmedStore(),
                    {
                        "id": 1,
                        "workflow_mode": "self_share_sync",
                        "own_share_code": "share",
                        "own_share_receive_code": "1212",
                    },
                )

            self.assertEqual(updated["move_status"], "error")
            mkdir.assert_not_called()
            copy2.assert_not_called()
            rmtree.assert_not_called()
            replace.assert_not_called()
            self.assertEqual(existing.read_text(encoding="utf-8"), "https://115.com/d/existing/movie.mkv")

    def test_merge_self_share_folder_journals_moving_and_atomically_replaces_each_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text("https://115.com/s/share_1212_movie.mkv", encoding="utf-8")
            events = []

            class FakeStore:
                def __init__(self):
                    self.row = {"id": 1, "move_status": "conflict", "move_error": "old"}
                    self.updates = []

                def update_move(self, row_id, status, **fields):
                    events.append(f"store:{status}")
                    self.updates.append((status, fields))
                    self.row.update({"move_status": status, "move_error": fields.get("error")})
                    return dict(self.row)

            def copy_to_temp(source_path, target_path):
                events.append("copy")
                Path(target_path).write_bytes(Path(source_path).read_bytes())

            real_replace = os.replace

            def replace_temp(temp_path, target_path):
                events.append("replace")
                real_replace(temp_path, target_path)

            store = FakeStore()
            plan = bridge.MovePlan("conflict", "ready", source, dest, "华语电影")
            with patch("app.media.strm.Path.mkdir", side_effect=lambda *args, **kwargs: events.append("mkdir")), patch(
                "app.media.strm.shutil.copy2", side_effect=copy_to_temp
            ) as copy2, patch("os.replace", side_effect=replace_temp) as replace:
                updated = bridge.merge_self_share_strm_folder(plan, store, {"id": 1})

            self.assertEqual(events[0], "store:moving")
            self.assertLess(events.index("mkdir"), events.index("copy"))
            self.assertLess(events.index("copy"), events.index("replace"))
            self.assertEqual(events[-1], "store:moved")
            resolved_source = bridge.safe_resolve(source)
            resolved_dest = bridge.safe_resolve(dest)
            copy2.assert_called_once_with(resolved_source / "movie.strm", resolved_dest / "movie.strm.cms-ingest.tmp")
            replace.assert_called_once_with(resolved_dest / "movie.strm.cms-ingest.tmp", resolved_dest / "movie.strm")
            self.assertEqual((dest / "movie.strm").read_text(encoding="utf-8"), "https://115.com/s/share_1212_movie.mkv")
            self.assertFalse(source.exists())
            self.assertEqual(updated["move_status"], "moved")

    def test_merge_self_share_folder_cleans_temp_and_records_error_when_strm_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            strm_path = source / "movie.strm"
            strm_path.write_text("https://115.com/s/share_1212_movie.mkv", encoding="utf-8")
            existing = dest / "movie.strm"
            existing.write_text("https://115.com/d/existing/movie.mkv", encoding="utf-8")

            class FakeStore:
                def __init__(self):
                    self.row = {"id": 1, "move_status": "conflict", "move_error": "old"}
                    self.statuses = []

                def update_move(self, row_id, status, **fields):
                    self.statuses.append((status, fields))
                    self.row.update({"move_status": status, "move_error": fields.get("error")})
                    return dict(self.row)

            def partial_copy_then_fail(source_path, target_path):
                Path(target_path).write_text("partial", encoding="utf-8")
                raise OSError("copy failed")

            store = FakeStore()
            plan = bridge.MovePlan("conflict", "ready", source, dest, "华语电影")
            with patch("app.media.strm.shutil.copy2", side_effect=partial_copy_then_fail), patch("os.replace") as replace:
                updated = bridge.merge_self_share_strm_folder(
                    plan,
                    store,
                    {
                        "id": 1,
                        "workflow_mode": "self_share_sync",
                        "own_share_code": "share",
                        "own_share_receive_code": "1212",
                    },
                )

            self.assertEqual([status for status, _fields in store.statuses], ["moving", "error"])
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "copy failed")
            self.assertFalse((dest / "movie.strm.cms-ingest.tmp").exists())
            self.assertTrue(strm_path.exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "https://115.com/d/existing/movie.mkv")
            replace.assert_not_called()

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

    def test_merge_self_share_folder_replaces_stale_matching_direct_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text(
                "http://cms/s/ownshare_1212_movie.mkv",
                encoding="utf-8",
            )
            (dest / "movie.strm").write_text(
                "http://cms/d/old-direct.mkv",
                encoding="utf-8",
            )
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
                own_share_receive_code="1212",
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "欧美电影")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "moved")
            self.assertEqual(
                (dest / "movie.strm").read_text(encoding="utf-8"),
                "http://cms/s/ownshare_1212_movie.mkv",
            )
            self.assertFalse(source.exists())

    def test_merge_self_share_folder_replaces_changed_strm_with_same_share_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            expected = "http://cms/s/ownshare_1212_new-file.mkv"
            (source / "movie.strm").write_text(expected, encoding="utf-8")
            (dest / "movie.strm").write_text(
                "http://cms/s/ownshare_1212_old-file.mkv",
                encoding="utf-8",
            )
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
                own_share_receive_code="1212",
            ) or row
            plan = bridge.MovePlan("conflict", "ready", source, dest, "欧美电影")

            updated = bridge.merge_self_share_strm_folder(plan, store, row)

            self.assertEqual(updated["move_status"], "moved")
            self.assertEqual((dest / "movie.strm").read_text(encoding="utf-8"), expected)
            self.assertFalse(source.exists())

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

    def test_merge_self_share_folder_rejects_wrong_destination_tmdb_for_required_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Show-[tmdb=73375]"
            dest = root / "library" / "Show-[tmdb=99999]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            marker = "http://cms/s/ownshare_1212_episode.mkv"
            (source / "episode.strm").write_text(marker, encoding="utf-8")
            (dest / "episode.strm").write_text(marker, encoding="utf-8")
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1212"),
                "https://115cdn.com/s/abc?password=1212",
                "submitted",
                title="Show (2020)",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
                own_share_receive_code="1212",
                own_share_file_name=source.name,
            ) or row
            row = store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "Show", "tmdb_id": "73375", "category": "外国电视", "type": "tv"},
                "confident",
            ) or row
            config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"外国电视": root / "library"},
            )
            plan = bridge.MovePlan("conflict", "ready", source, dest, "外国电视")

            updated = bridge.merge_self_share_strm_folder(
                plan,
                store,
                row,
                config,
                required_relative_path="episode.strm",
            )

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("任务 TMDB 73375", updated["move_error"])
            self.assertIn("文件夹 TMDB 99999", updated["move_error"])
            self.assertTrue(source.exists())
            self.assertEqual((dest / "episode.strm").read_text(encoding="utf-8"), marker)

    def test_merge_rejects_wrong_destination_tmdb_when_required_episode_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Show-[tmdb=73375]"
            dest = root / "library" / "Show-[tmdb=99999]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            marker = "http://cms/s/ownshare_1212_episode.mkv"
            (source / "episode.strm").write_text(marker, encoding="utf-8")
            store = bridge.SubmissionStore(root / "db.sqlite")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1212"),
                "https://115cdn.com/s/abc?password=1212",
                "submitted",
                title="Show (2020)",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
                own_share_receive_code="1212",
                own_share_file_name=source.name,
            ) or row
            row = store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "Show", "tmdb_id": "73375", "category": "外国电视", "type": "tv"},
                "confident",
            ) or row
            config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"外国电视": root / "library"},
            )
            plan = bridge.MovePlan("conflict", "ready", source, dest, "外国电视")

            updated = bridge.merge_self_share_strm_folder(
                plan,
                store,
                row,
                config,
                required_relative_path="episode.strm",
            )

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("任务 TMDB 73375", updated["move_error"])
            self.assertIn("文件夹 TMDB 99999", updated["move_error"])
            self.assertTrue(source.exists())
            self.assertFalse((dest / "episode.strm").exists())

    def test_required_missing_episode_rejects_destination_without_confirmable_tmdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "library" / "Show"
            destination.mkdir(parents=True)
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "ownshare",
                "own_share_receive_code": "1212",
                "recognition_json": json.dumps({"tmdb_id": "73375"}),
            }

            issue = bridge.validate_self_share_strm_destination(destination, row, "episode.strm")

            self.assertIn("TMDB 无法确认", issue)

    def test_explicit_source_tmdb_rejects_unmarked_strm_source_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "share" / "123 (2026)"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/ownshare_1212_movie.mkv", encoding="utf-8")
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "ownshare",
                "own_share_receive_code": "1212",
                "title": "123 (2026) {tmdb-1228710}",
            }

            issue = bridge.validate_self_share_strm_source(source, row)

            self.assertIn("任务 TMDB 1228710", issue)
            self.assertIn("未知", issue)

    def test_source_validation_allows_other_share_markers_when_current_share_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "share" / "Show-[tmdb=73375]"
            source.mkdir(parents=True)
            (source / "old.strm").write_text("http://cms/s/othershare_1212_old.mkv", encoding="utf-8")
            (source / "current.strm").write_text("http://cms/s/ownshare_1212_current.mkv", encoding="utf-8")
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "ownshare",
                "own_share_receive_code": "1212",
                "recognition_json": json.dumps({"tmdb_id": "73375"}),
            }

            issue = bridge.validate_self_share_strm_source(source, row)

            self.assertEqual(issue, "")

    def test_source_validation_still_fails_when_no_marker_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "share" / "Show-[tmdb=73375]"
            source.mkdir(parents=True)
            (source / "old.strm").write_text("http://cms/s/othershare_1212_old.mkv", encoding="utf-8")
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "ownshare",
                "own_share_receive_code": "1212",
                "recognition_json": json.dumps({"tmdb_id": "73375"}),
            }

            issue = bridge.validate_self_share_strm_source(source, row)

            self.assertIn("STRM 不是预期的分享链接", issue)

    def test_merge_validation_allows_other_share_markers_when_current_share_present(self):
        from app.media.strm import validate_self_share_strm_merge

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "share" / "Show-[tmdb=73375]"
            dest = Path(tmp) / "library" / "Show-[tmdb=73375]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "old.strm").write_text("http://cms/s/othershare_1212_old.mkv", encoding="utf-8")
            (source / "current.strm").write_text("http://cms/s/ownshare_1212_current.mkv", encoding="utf-8")
            (dest / "legacy.strm").write_text("http://cms/s/othershare_1212_legacy.mkv", encoding="utf-8")
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "ownshare",
                "own_share_receive_code": "1212",
            }

            issue = validate_self_share_strm_merge(source, dest, row)

            self.assertEqual(issue, "")

    def test_merge_validation_fails_without_any_matching_source(self):
        from app.media.strm import validate_self_share_strm_merge

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "share" / "Show-[tmdb=73375]"
            dest = Path(tmp) / "library" / "Show-[tmdb=73375]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "old.strm").write_text("http://cms/s/othershare_1212_old.mkv", encoding="utf-8")
            (dest / "legacy.strm").write_text("http://cms/s/othershare_1212_legacy.mkv", encoding="utf-8")
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "ownshare",
                "own_share_receive_code": "1212",
            }

            issue = validate_self_share_strm_merge(source, dest, row)

            self.assertIn("STRM 不是预期的分享链接", issue)

    def test_explicit_source_tmdb_rejects_unmarked_strm_destination_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "library" / "123 (2026)"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("http://cms/s/ownshare_1212_movie.mkv", encoding="utf-8")
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "ownshare",
                "own_share_receive_code": "1212",
                "title": "123 (2026) {tmdb-1228710}",
            }

            issue = bridge.validate_self_share_strm_destination(destination, row)

            self.assertIn("任务 TMDB 1228710", issue)
            self.assertIn("未知", issue)

    def test_missing_required_episode_rejects_existing_unsafe_destination_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Show-[tmdb=73375]"
            dest = root / "library" / "Show-[tmdb=73375]"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "episode.strm").write_text(
                "http://cms/s/ownshare_1212_episode.mkv", encoding="utf-8"
            )
            direct_marker = "http://cms/d/direct_other.mkv"
            wrong_marker = "http://cms/s/other-share_1212_other.mkv"
            (dest / "other-direct.strm").write_text(direct_marker, encoding="utf-8")
            (dest / "other-wrong.strm").write_text(wrong_marker, encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("abc", "1212"),
                "https://115cdn.com/s/abc?password=1212",
                "submitted",
                title="Show (2020)",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="ownshare",
                own_share_receive_code="1212",
                own_share_file_name=source.name,
            ) or row
            row = store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "Show", "tmdb_id": "73375", "category": "外国电视", "type": "tv"},
                "confident",
            ) or row
            config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"外国电视": root / "library"},
            )
            plan = bridge.MovePlan("conflict", "ready", source, dest, "外国电视")

            updated = bridge.merge_self_share_strm_folder(
                plan,
                store,
                row,
                config,
                required_relative_path="episode.strm",
            )

            self.assertEqual(updated["move_status"], "error")
            self.assertIn("直链 STRM", updated["move_error"])
            self.assertTrue(source.exists())
            self.assertFalse((dest / "episode.strm").exists())
            self.assertEqual((dest / "other-direct.strm").read_text(encoding="utf-8"), direct_marker)
            self.assertEqual((dest / "other-wrong.strm").read_text(encoding="utf-8"), wrong_marker)

    def test_cleanup_pending_self_share_sources_requires_task_runner_review(self):
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

            self.assertEqual(cleaned, 0)
            self.assertEqual(p115.deleted, [])
            self.assertEqual(updated["cleanup_status"], "pending")

    def _moving_self_share_row(self, root, source, dest, category, share_code):
        store = bridge.SubmissionStore(root / "submissions.db")
        row = store.upsert_submission(
            bridge.ShareKey(f"share-{share_code}", "pass001"),
            f"https://115cdn.com/s/{share_code}",
            "submitted",
            title=source.name,
        )
        row = store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            own_share_file_name=source.name,
            own_share_code=share_code,
        ) or row
        store.update_category(int(row["id"]), category, "selected")
        row = store.update_move(
            int(row["id"]),
            "moving",
            source_path=str(source),
            dest_path=str(dest),
            category_final=category,
        ) or row
        return store, row

    def test_reconcile_stranded_move_marks_valid_existing_destination_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("http://cms/s/reconcilemoved_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcilemoved")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            result = bridge.reconcile_self_share_move(store, config, row)

            self.assertEqual(result, "moved")
            self.assertEqual(store.find_by_id(int(row["id"]))["move_status"], "moved")
            self.assertTrue((dest / "movie.strm").exists())

    def test_reconcile_stranded_move_rejects_empty_source_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("http://cms/s/reconcileempty_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcileempty")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            result = bridge.reconcile_self_share_move(store, config, row)

            self.assertEqual(result, "invalid")
            self.assertTrue(source.exists())
            self.assertTrue((dest / "movie.strm").exists())
            self.assertEqual(store.find_by_id(int(row["id"]))["move_status"], "error")

    def test_reconcile_stranded_move_replays_stale_moving_row_without_rewriting_moved_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("http://cms/s/reconcile_idempotent_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcile_idempotent")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            self.assertEqual(bridge.reconcile_self_share_move(store, config, row), "moved")
            with patch.object(store, "update_move", wraps=store.update_move) as update_move:
                self.assertEqual(bridge.reconcile_self_share_move(store, config, row), "moved")

            update_move.assert_not_called()
            self.assertEqual(store.find_by_id(int(row["id"]))["move_status"], "moved")

    def test_reconcile_stranded_move_replays_source_only_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcilereplay_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcilereplay")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            result = bridge.reconcile_self_share_move(store, config, row)

            self.assertEqual(result, "replayed")
            self.assertFalse(source.exists())
            self.assertTrue((dest / "movie.strm").exists())
            self.assertEqual(store.find_by_id(int(row["id"]))["move_status"], "moved")

    def test_reconcile_stranded_move_merges_without_recopying_valid_target_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            same = "http://cms/s/reconcilemerge_1212_same"
            (source / "same.strm").write_text(same, encoding="utf-8")
            (source / "new.strm").write_text("http://cms/s/reconcilemerge_1212_new", encoding="utf-8")
            (dest / "same.strm").write_text(same, encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcilemerge")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            with patch("app.media.strm.shutil.copy2", wraps=__import__("shutil").copy2) as copy2:
                result = bridge.reconcile_self_share_move(store, config, row)

            self.assertEqual(result, "merged")
            self.assertFalse(source.exists())
            self.assertEqual(copy2.call_args_list, [
                unittest.mock.call(
                    bridge.safe_resolve(source / "new.strm"),
                    bridge.safe_resolve(dest / "new.strm.cms-ingest.tmp"),
                )
            ])
            self.assertEqual((dest / "same.strm").read_text(encoding="utf-8"), same)

    def test_reconcile_stranded_move_rejects_direct_marker_in_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            dest.mkdir(parents=True)
            direct = dest / "movie.strm"
            direct.write_text("http://cms/d/direct_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcileinvalid")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            result = bridge.reconcile_self_share_move(store, config, row)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(result, "invalid")
            self.assertEqual(updated["move_status"], "error")
            self.assertIn("直链 STRM", updated["move_error"])
            self.assertEqual(direct.read_text(encoding="utf-8"), "http://cms/d/direct_movie")

    def test_reconcile_stranded_move_reports_missing_source_and_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcilemissing")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            result = bridge.reconcile_self_share_move(store, config, row)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(result, "missing")
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "STRM 源目录和目标目录均不存在")

    def test_reconcile_stranded_move_rejects_destination_outside_library_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            outside = root / "outside" / "Movie"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcileoutside_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, outside, "欧美电影", "reconcileoutside")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            result = bridge.reconcile_self_share_move(store, config, row)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(result, "invalid")
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "目标目录不在媒体库白名单内")
            self.assertTrue(source.exists())
            self.assertFalse(outside.exists())

    def test_reconcile_stranded_move_rejects_source_outside_source_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed_source_root = root / "share"
            source = root / "outside" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcile_source_outside_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcile_source_outside")
            config = bridge.MoveConfig(
                source_roots=[allowed_source_root],
                library_roots={"欧美电影": root / "library"},
            )

            result = bridge.reconcile_self_share_move(store, config, row)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(result, "invalid")
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "源目录不在允许范围内")
            self.assertTrue(source.exists())
            self.assertFalse(dest.exists())

    def test_reconcile_stranded_move_rejects_source_inside_library_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_root = root / "library"
            source = library_root / "Movie"
            dest = library_root / "Other"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcile_library_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcile_library")
            config = bridge.MoveConfig(
                source_roots=[root],
                library_roots={"欧美电影": library_root},
            )

            result = bridge.reconcile_self_share_move(store, config, row)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(result, "invalid")
            self.assertEqual(updated["move_status"], "error")
            self.assertIn("媒体库", updated["move_error"])
            self.assertTrue(source.exists())
            self.assertFalse(dest.exists())

    def test_reconcile_stranded_move_rejects_nested_source_and_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            library_root = source / "library"
            dest = library_root / "Movie"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcile_nested_1212_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcile_nested")
            config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"欧美电影": library_root},
            )

            result = bridge.reconcile_self_share_move(store, config, row)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(result, "invalid")
            self.assertEqual(updated["move_status"], "error")
            self.assertIn("嵌套", updated["move_error"])
            self.assertTrue(source.exists())
            self.assertFalse(dest.exists())

    def test_reconcile_stranded_move_rejects_direct_existing_destination_before_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcilebothdirect_1212_movie", encoding="utf-8")
            direct = dest / "movie.strm"
            direct.write_text("http://cms/d/direct_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "reconcilebothdirect")
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            result = bridge.reconcile_self_share_move(store, config, row)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(result, "invalid")
            self.assertEqual(updated["move_status"], "error")
            self.assertIn("直链 STRM", updated["move_error"])
            self.assertTrue(source.exists())
            self.assertEqual(direct.read_text(encoding="utf-8"), "http://cms/d/direct_movie")

    def test_merge_self_share_folder_replaces_matching_direct_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/direct_target_1212_movie", encoding="utf-8")
            direct = dest / "movie.strm"
            direct.write_text("http://cms/d/direct_movie", encoding="utf-8")
            store, row = self._moving_self_share_row(root, source, dest, "欧美电影", "direct_target")
            config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"欧美电影": root / "library"},
            )
            plan = bridge.MovePlan("conflict", "目标目录已存在，恢复中合并", source, dest, "欧美电影")

            updated = bridge.merge_self_share_strm_folder(plan, store, row, config)

            self.assertEqual(updated["move_status"], "moved")
            self.assertFalse(source.exists())
            self.assertEqual(direct.read_text(encoding="utf-8"), "http://cms/s/direct_target_1212_movie")

    def test_repair_does_not_use_legacy_discovery_for_single_persisted_move_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcilepartial_1212_movie", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("share-reconcilepartial", "pass001"),
                "https://115cdn.com/s/reconcilepartial",
                "submitted",
                title=source.name,
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="reconcilepartial",
            ) or row
            store.update_category(int(row["id"]), "欧美电影", "selected")
            row = store.update_move(
                int(row["id"]),
                "moving",
                source_path=str(source),
                category_final="欧美电影",
            ) or row
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(repaired, 0)
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "STRM 移动记录缺少完整持久化路径")
            self.assertTrue(source.exists())
            self.assertFalse(dest.exists())

    def test_repair_rejects_nonmoving_persisted_path_without_legacy_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/reconcile_nonmoving_1212_movie", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("share-reconcile-nonmoving", "pass001"),
                "https://115cdn.com/s/reconcile-nonmoving",
                "submitted",
                title=source.name,
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="reconcile_nonmoving",
            ) or row
            store.update_category(int(row["id"]), "欧美电影", "selected")
            row = store.update_move(
                int(row["id"]),
                "skipped",
                source_path=str(source),
                category_final="欧美电影",
                error="旧状态",
            ) or row
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": root / "library"})

            candidates = store.stranded_self_share_move_candidates(limit=10)
            repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(candidates, [])
            self.assertEqual(repaired, 0)
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "STRM 移动记录状态或持久化路径无效")
            self.assertTrue(source.exists())
            self.assertFalse(dest.exists())

    def test_repair_skips_existing_error_with_complete_persisted_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "Movie"
            dest = root / "library" / "Movie"
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("share-repair-error", "pass001"),
                "https://115cdn.com/s/repair-error",
                "submitted",
                title="Movie",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="repair_error",
            ) or row
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(
                int(row["id"]),
                "error",
                source_path=str(source),
                dest_path=str(dest),
                category_final="欧美电影",
                error="保留原始错误",
            )
            old_updated_at = 1234.5
            with store._lock, store._connection() as conn:
                conn.execute("UPDATE submissions SET updated_at = ? WHERE id = ?", (old_updated_at, int(row["id"])))
            config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"欧美电影": root / "library"},
            )

            with patch.object(store, "update_move", wraps=store.update_move) as update_move:
                candidates = store.invalid_self_share_move_candidates(limit=10)
                repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(candidates, [])
            self.assertEqual(repaired, 0)
            update_move.assert_not_called()
            self.assertEqual(updated["move_status"], "error")
            self.assertEqual(updated["move_error"], "保留原始错误")
            self.assertEqual(updated["updated_at"], old_updated_at)

    def test_repair_skips_canonical_manifest_rename_for_source_outside_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            outside = root / "outside" / "Movie"
            outside.mkdir(parents=True)
            alias = outside / "alias.strm"
            alias.write_text("http://cms/s/reconcile_manifest_1212_movie", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("share-reconcile-manifest", "pass001"),
                "https://115cdn.com/s/reconcile-manifest",
                "submitted",
                title="Movie",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=str(outside),
                own_share_code="reconcile_manifest",
                canonical_manifest_json=json.dumps(
                    {
                        "root_name": "Movie",
                        "entries": [{"alias_path": "alias", "canonical_path": "canonical"}],
                    }
                ),
            ) or row
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "skipped", category_final="欧美电影")
            config = bridge.MoveConfig(source_roots=[share_root], library_roots={"欧美电影": root / "library"})

            with patch("app.media.strm.Path.replace") as replace:
                repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)

            self.assertEqual(repaired, 0)
            replace.assert_not_called()
            self.assertTrue(alias.exists())
            self.assertFalse((outside / "canonical.strm").exists())

    def test_repair_skips_canonical_manifest_rename_for_source_inside_library_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_root = root / "library"
            source = library_root / "Movie"
            source.mkdir(parents=True)
            alias = source / "alias.strm"
            alias.write_text("http://cms/s/reconcile_library_manifest_1212_movie", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("share-reconcile-library-manifest", "pass001"),
                "https://115cdn.com/s/reconcile-library-manifest",
                "submitted",
                title="Movie",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="reconcile_library_manifest",
                canonical_manifest_json=json.dumps(
                    {
                        "root_name": source.name,
                        "entries": [{"alias_path": "alias", "canonical_path": "canonical"}],
                    }
                ),
            ) or row
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "skipped", category_final="欧美电影")
            config = bridge.MoveConfig(
                source_roots=[library_root],
                library_roots={"欧美电影": library_root},
            )

            with patch("app.media.strm.Path.replace") as replace:
                repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)

            self.assertEqual(repaired, 0)
            replace.assert_not_called()
            self.assertTrue(alias.exists())
            self.assertFalse((source / "canonical.strm").exists())

    def test_repair_skips_canonical_manifest_rename_for_nested_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            source = share_root / "Movie"
            library_root = source / "nested"
            source.mkdir(parents=True)
            (library_root / "Movie").mkdir(parents=True)
            alias = source / "alias.strm"
            alias.write_text("http://cms/s/reconcile_nested_manifest_1212_movie", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("share-reconcile-nested-manifest", "pass001"),
                "https://115cdn.com/s/reconcile-nested-manifest",
                "submitted",
                title="Movie",
            )
            row = store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="reconcile_nested_manifest",
                canonical_manifest_json=json.dumps(
                    {
                        "root_name": "Movie",
                        "entries": [{"alias_path": "alias", "canonical_path": "canonical"}],
                    }
                ),
            ) or row
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "skipped", category_final="欧美电影")
            config = bridge.MoveConfig(
                source_roots=[share_root],
                library_roots={"欧美电影": library_root},
            )

            with patch("app.media.strm.Path.replace") as replace:
                repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)

            self.assertEqual(repaired, 0)
            replace.assert_not_called()
            self.assertTrue(alias.exists())
            self.assertFalse((source / "canonical.strm").exists())

    def test_repair_rejects_non_directory_source_names_before_manifest_restore(self):
        for source_name_case in ("absolute", "..", ".", "foo/bar", "foo/../Movie"):
            with self.subTest(source_name=source_name_case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                share_root = root / "share"
                source = share_root / "Movie"
                share_root.mkdir(parents=True)
                source.mkdir()
                source_name = str(source) if source_name_case == "absolute" else source_name_case
                actual_source = share_root if source_name_case == "." else source
                alias = actual_source / "alias.strm"
                alias.write_text("http://cms/s/repair_name_1212_movie", encoding="utf-8")
                store = bridge.SubmissionStore(root / "submissions.db")
                row = store.upsert_submission(
                    bridge.ShareKey("share-repair-name", "pass001"),
                    "https://115cdn.com/s/repair-name",
                    "submitted",
                    title="Movie",
                )
                row = store.update_self_share(
                    int(row["id"]),
                    workflow_mode="self_share_sync",
                    own_share_file_name=source_name,
                    own_share_code="repair_name",
                    canonical_manifest_json=json.dumps(
                        {
                            "root_name": "Movie",
                            "entries": [{"alias_path": "alias", "canonical_path": "canonical"}],
                        }
                    ),
                ) or row
                store.update_category(int(row["id"]), "欧美电影", "selected")
                store.update_move(int(row["id"]), "skipped", category_final="欧美电影")
                config = bridge.MoveConfig(
                    source_roots=[share_root],
                    library_roots={"欧美电影": root / "library"},
                )

                with patch("app.media.strm.Path.replace") as replace, patch("app.media.strm.shutil.move") as move:
                    repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)

                updated = store.find_by_id(int(row["id"]))
                self.assertEqual(repaired, 0)
                replace.assert_not_called()
                move.assert_not_called()
                self.assertEqual(updated["move_status"], "skipped")
                self.assertTrue(alias.exists())

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

    def test_repair_stranded_self_share_folder_falls_back_from_empty_alias_to_own_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            alias_name = "asset-313-7030e48e"
            own_name = "Y-幼女战记-2017-[tmdb=69346]"
            (share_root / alias_name).mkdir(parents=True)
            source = share_root / own_name
            source.mkdir(parents=True)
            (source / "movie.strm").write_text(
                "http://cms/s/own_share_1212_movie.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("external-alias-fallback", "pass001"),
                "https://115cdn.com/s/external-alias-fallback?password=pass001",
                "submitted",
                title="幼女战记",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                share_alias_name=alias_name,
                own_share_file_name=own_name,
                own_share_code="own_share",
            )
            store.update_category(int(row["id"]), "外国电视", "selected")
            store.update_move(
                int(row["id"]),
                "skipped",
                category_final="外国电视",
                error="等待维护恢复",
            )
            config = bridge.MoveConfig(
                source_roots=[share_root],
                library_roots={"外国电视": movie_root},
                stable_seconds=0,
            )

            repaired = bridge.repair_stranded_self_share_moves(store, config, limit=10)
            updated = store.find_by_id(int(row["id"]))

            self.assertEqual(repaired, 1)
            self.assertFalse(source.exists())
            self.assertTrue((movie_root / own_name / "movie.strm").exists())
            self.assertEqual(updated["move_status"], "moved")

    def test_repair_stranded_self_share_folder_replaces_matching_direct_target(self):
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

    def test_maintenance_submits_only_one_restore_sync_per_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            store = bridge.SubmissionStore(root / "submissions.db")
            rows = []
            for index in range(2):
                folder_name = f"M-缺失任务{index}-2025-[tmdb={1054800 + index}]"
                row = store.upsert_submission(
                    bridge.ShareKey(f"missing-source-{index}", "pass"),
                    f"https://115cdn.com/s/missing-source-{index}?password=pass",
                    "submitted",
                    title=f"缺失任务{index} (2025)",
                )
                store.update_self_share(
                    int(row["id"]),
                    workflow_mode="self_share_sync",
                    own_share_file_name=folder_name,
                    own_share_code=f"missing{index}",
                    own_share_receive_code="1212",
                    share_sync_status="submitted",
                    share_validation_status="valid",
                )
                store.update_category(int(row["id"]), "欧美电影", "selected")
                store.update_move(
                    int(row["id"]),
                    "moved",
                    source_path=str(share_root / folder_name),
                    dest_path=str(movie_root / folder_name),
                    category_final="欧美电影",
                )
                rows.append(store.find_by_id(int(row["id"])))

            class FakeCms:
                def __init__(self):
                    self.sync_payloads = []

                def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                    self.sync_payloads.append({"share_code": share_code, "receive_code": receive_code})
                    return {"code": 200}

            cms = FakeCms()
            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms,
                bridge.SelfShareConfig(enabled=True, strm_root=share_root, cms_local_path="/media/share", cms_cid="0"),
                bridge.MoveConfig(source_roots=[share_root], library_roots={"欧美电影": movie_root}, stable_seconds=0),
                limit=10,
            )

            self.assertEqual(restored, 0)
            self.assertEqual(len(cms.sync_payloads), 1)
            phases = [store.find_by_id(int(row["id"]))["workflow_phase"] for row in rows]
            self.assertEqual(phases.count("restore_share_sync_submitted"), 1)

    def test_restore_share_sync_resubmits_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            folder_name = "M-超时恢复-2025-[tmdb=1054801]"
            dest = movie_root / folder_name
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("timed-out-source", "pass"),
                "https://115cdn.com/s/timed-out-source?password=pass",
                "submitted",
                title="超时恢复 (2025)",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="restore_share_sync_submitted",
                own_share_file_name=folder_name,
                own_share_code="timedout",
                own_share_receive_code="1212",
                share_sync_status="restore_submitted",
                share_validation_status="valid",
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(
                int(row["id"]),
                "moved",
                source_path=str(share_root / folder_name),
                dest_path=str(dest),
                category_final="欧美电影",
            )
            with store._lock, store._connection() as conn:
                conn.execute("UPDATE submissions SET updated_at = ? WHERE id = ?", (time.time() - 120, int(row["id"])))

            class FakeCms:
                def __init__(self):
                    self.sync_payloads = []

                def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                    self.sync_payloads.append({"share_code": share_code, "receive_code": receive_code})
                    return {"code": 200}

            cms = FakeCms()
            status, _metadata = bridge.restore_missing_self_share_library_folder(
                store,
                cms,
                store.find_by_id(int(row["id"])),
                bridge.SelfShareConfig(enabled=True, strm_root=share_root, cms_local_path="/media/share", cms_cid="0"),
                bridge.MoveConfig(source_roots=[share_root], library_roots={"欧美电影": movie_root}, stable_seconds=0),
            )

            self.assertEqual(status, "restore_submitted")
            self.assertEqual(cms.sync_payloads, [{"share_code": "timedout", "receive_code": "1212"}])

    def test_maintenance_waits_for_active_restore_before_newer_missing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            store = bridge.SubmissionStore(root / "submissions.db")

            active_folder = "A-等待恢复-2025-[tmdb=1054802]"
            active = store.upsert_submission(
                bridge.ShareKey("active-restore", "pass"),
                "https://115cdn.com/s/active-restore?password=pass",
                "submitted",
                title="等待恢复 (2025)",
            )
            store.update_self_share(
                int(active["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="restore_share_sync_submitted",
                own_share_file_name=active_folder,
                own_share_code="active",
                own_share_receive_code="1212",
                share_sync_status="restore_submitted",
                share_validation_status="valid",
            )
            store.update_category(int(active["id"]), "欧美电影", "selected")
            store.update_move(
                int(active["id"]),
                "moved",
                source_path=str(share_root / active_folder),
                dest_path=str(movie_root / active_folder),
                category_final="欧美电影",
            )
            with store._lock, store._connection() as conn:
                conn.execute("UPDATE submissions SET updated_at = ? WHERE id = ?", (time.time() - 10, int(active["id"])))

            newer_folder = "N-更新缺失-2025-[tmdb=1054803]"
            newer = store.upsert_submission(
                bridge.ShareKey("newer-missing", "pass"),
                "https://115cdn.com/s/newer-missing?password=pass",
                "submitted",
                title="更新缺失 (2025)",
            )
            store.update_self_share(
                int(newer["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=newer_folder,
                own_share_code="newer",
                own_share_receive_code="1212",
                share_sync_status="submitted",
                share_validation_status="valid",
            )
            store.update_category(int(newer["id"]), "欧美电影", "selected")
            store.update_move(
                int(newer["id"]),
                "moved",
                source_path=str(share_root / newer_folder),
                dest_path=str(movie_root / newer_folder),
                category_final="欧美电影",
            )

            class FakeCms:
                def __init__(self):
                    self.sync_payloads = []

                def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
                    self.sync_payloads.append({"share_code": share_code, "receive_code": receive_code})
                    return {"code": 200}

            cms = FakeCms()
            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms,
                bridge.SelfShareConfig(enabled=True, strm_root=share_root, cms_local_path="/media/share", cms_cid="0"),
                bridge.MoveConfig(source_roots=[share_root], library_roots={"欧美电影": movie_root}, stable_seconds=0),
                limit=10,
            )

            self.assertEqual(restored, 0)
            self.assertEqual(cms.sync_payloads, [])

    def test_restore_sync_claim_allows_only_one_fresh_submission_and_retries_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("claim-source", "pass"),
                "https://115cdn.com/s/claim-source?password=pass",
                "submitted",
            )

            self.assertTrue(store.claim_self_share_restore_sync(int(row["id"]), now=100.0))
            self.assertFalse(store.claim_self_share_restore_sync(int(row["id"]), now=100.0))
            self.assertFalse(store.claim_self_share_restore_sync(int(row["id"]), now=150.0))
            self.assertTrue(store.claim_self_share_restore_sync(int(row["id"]), now=161.0))

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

    def test_restore_missing_library_folder_rejects_library_external_destination_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside" / "Movie"
            outside.mkdir(parents=True)
            (outside / "movie.strm").write_text("http://cms/s/outside_1212_movie", encoding="utf-8")
            row = {
                "id": 1,
                "workflow_mode": "self_share_sync",
                "dest_path": str(outside),
                "category_final": "欧美电影",
                "own_share_file_name": "Movie",
                "own_share_code": "outside",
                "own_share_receive_code": "1212",
            }
            self_share_config = bridge.SelfShareConfig(enabled=True, strm_root=root / "share")
            move_config = bridge.MoveConfig(
                source_roots=[root / "share"],
                library_roots={"欧美电影": root / "library"},
            )

            with patch("app.media.strm.cleanup_direct_strm_for_task_identity") as cleanup:
                status, metadata = bridge.restore_missing_self_share_library_folder(
                    None,
                    None,
                    row,
                    self_share_config,
                    move_config,
                )

            self.assertEqual(status, "skipped")
            self.assertIn("媒体库白名单", metadata["destination_validation_error"])
            cleanup.assert_not_called()
            self.assertTrue((outside / "movie.strm").exists())

    def test_restore_missing_rejects_non_directory_source_name_before_manifest_restore(self):
        for source_name_case in ("absolute", "..", ".", "foo/bar", "foo/../Movie"):
            with self.subTest(source_name=source_name_case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                share_root = root / "share"
                source = share_root / "Movie"
                library_root = root / "library"
                dest = library_root / "Movie"
                source.mkdir(parents=True)
                dest.mkdir(parents=True)
                source_name = str(source) if source_name_case == "absolute" else source_name_case
                row = {
                    "id": 1,
                    "workflow_mode": "self_share_sync",
                    "dest_path": str(dest),
                    "category_final": "欧美电影",
                    "own_share_file_name": source_name,
                    "own_share_code": "restore_name",
                    "own_share_receive_code": "1212",
                    "workflow_phase": "restore_share_sync_submitted",
                    "canonical_manifest_json": json.dumps(
                        {
                            "root_name": "Movie",
                            "entries": [{"alias_path": "alias", "canonical_path": "canonical"}],
                        }
                    ),
                }
                self_share_config = bridge.SelfShareConfig(enabled=True, strm_root=share_root)
                move_config = bridge.MoveConfig(
                    source_roots=[share_root],
                    library_roots={"欧美电影": library_root},
                )

                with patch("app.media.strm.restore_canonical_strm_paths") as restore:
                    status, metadata = bridge.restore_missing_self_share_library_folder(
                        None,
                        None,
                        row,
                        self_share_config,
                        move_config,
                    )

                self.assertEqual(status, "skipped")
                self.assertEqual(metadata["restore_reason"], "自有分享源目录名称无效")
                restore.assert_not_called()

    def test_prepare_direct_file_share_rejects_non_directory_source_names(self):
        for source_name_case in ("absolute", "..", ".", "foo/bar"):
            with self.subTest(source_name=source_name_case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                share_root = root / "share"
                available = share_root / "available"
                available.mkdir(parents=True)
                source_file = available / "episode.strm"
                source_file.write_text("http://cms/s/direct_prepare_1212_episode", encoding="utf-8")
                workflow = bridge.BridgeSelfShareTaskWorkflow.__new__(bridge.BridgeSelfShareTaskWorkflow)
                workflow.self_share_config = bridge.SelfShareConfig(enabled=True, strm_root=share_root)
                task = SimpleNamespace(
                    metadata={
                        "direct_file_share_file_id": "file-1",
                        "direct_file_share_relative_path": "episode.strm",
                    }
                )
                row = {
                    "own_share_file_name": str(root / "outside" / "Movie")
                    if source_name_case == "absolute"
                    else source_name_case,
                    "own_share_code": "direct_prepare",
                    "own_share_receive_code": "1212",
                }

                prepared = workflow._prepare_direct_file_share_strm(task, row)

                self.assertIsNone(prepared)
                self.assertTrue(source_file.exists())


    def test_restore_alias_share_strm_uses_canonical_library_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            canonical_name = "T-特洛伊-2004-[tmdb=652]"
            alias_name = "asset-42-abcd1234"
            source = share_root / alias_name
            dest = movie_root / canonical_name
            source.mkdir(parents=True)
            (source / "asset-42-001.strm").write_text(
                "http://cms/s/owncode_1212_file.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("external", "pass"),
                "https://115cdn.com/s/external?password=pass",
                "submitted",
                title="特洛伊",
            )
            manifest = {
                "version": 1,
                "root_name": canonical_name,
                "alias_name": alias_name,
                "category": "欧美电影",
                "tmdb_id": "652",
                "entries": [
                    {
                        "file_id": "video-id",
                        "canonical_path": "Troy.2004.mkv",
                        "alias_path": "asset-42-001.mkv",
                    }
                ],
            }
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=canonical_name,
                own_share_code="owncode",
                own_share_receive_code="1212",
                share_sync_status="restore_submitted",
                share_alias_name=alias_name,
                share_alias_level=2,
                canonical_manifest_json=json.dumps(manifest, ensure_ascii=False),
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "moved", dest_path=str(dest), category_final="欧美电影")
            current = store.find_by_id(int(row["id"]))

            status, metadata = bridge.restore_missing_self_share_library_folder(
                store,
                object(),
                current,
                bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": movie_root}, stable_seconds=0),
            )

            self.assertEqual(status, "restored")
            self.assertEqual(metadata["dest_path"], str(bridge.safe_resolve(dest)))
            self.assertTrue((dest / "Troy.2004.strm").is_file())
            self.assertFalse((movie_root / alias_name).exists())

    def test_restore_alias_without_strm_falls_back_to_own_share_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            own_name = "T-特洛伊-2004-[tmdb=652]"
            alias_name = "asset-42-abcd1234"
            (share_root / alias_name).mkdir(parents=True)
            source = share_root / own_name
            source.mkdir(parents=True)
            (source / "Troy.2004.strm").write_text(
                "http://cms/s/owncode_1212_file.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("external-fallback", "pass"),
                "https://115cdn.com/s/external-fallback?password=pass",
                "submitted",
                title="特洛伊",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=own_name,
                own_share_code="owncode",
                own_share_receive_code="1212",
                share_alias_name=alias_name,
                share_alias_level=2,
            )
            store.update_recognition(
                int(row["id"]),
                {"title": own_name, "type": "movie", "category": "欧美电影", "tmdb_id": "652"},
                "self_share_resolved",
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(int(row["id"]), "moved", dest_path=str(movie_root / own_name), category_final="欧美电影")

            class FakeCms:
                def add_share115_sync_task(self, *args, **kwargs):
                    raise AssertionError("valid fallback source should not resubmit share sync")

            status, metadata = bridge.restore_missing_self_share_library_folder(
                store,
                FakeCms(),
                store.find_by_id(int(row["id"])),
                bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                bridge.MoveConfig(source_roots=[share_root], library_roots={"欧美电影": movie_root}, stable_seconds=0),
            )

            self.assertEqual(status, "restored")
            self.assertEqual(metadata["source_path"], str(bridge.safe_resolve(source)))
            self.assertTrue((movie_root / own_name / "Troy.2004.strm").exists())
            self.assertFalse(source.exists())

    def test_find_self_share_source_falls_back_when_alias_has_no_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            alias_name = "asset-42-abcd1234"
            own_name = "T-特洛伊-2004-[tmdb=652]"
            (share_root / alias_name).mkdir(parents=True)
            source = share_root / own_name
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("http://cms/s/owncode_1212_file.mkv", encoding="utf-8")

            found = bridge.find_self_share_strm_source_dir(
                bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                {
                    "workflow_mode": "self_share_sync",
                    "share_alias_name": alias_name,
                    "own_share_file_name": own_name,
                },
                {},
                own_name,
            )

            self.assertEqual(found, bridge.safe_resolve(source))

    def test_restore_single_episode_rejects_unrelated_direct_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            tv_root = root / "TV"
            folder_name = "Q-Show-2022-[tmdb=94997]"
            source = share_root / folder_name / "Season 03"
            dest = tv_root / folder_name / "Season 03"
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "Show - S03E03.strm").write_text(
                "http://cms/s/owncode_ownpwd_S03E03.mkv",
                encoding="utf-8",
            )
            preserved = dest / "Show - S03E02.strm"
            preserved.write_text("http://cms/d/direct/S03E02.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("dummyshare007", "pass007"),
                "https://115cdn.com/s/dummyshare007?password=pass007",
                "submitted",
                title="Show S03",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=folder_name,
                own_share_code="owncode",
                own_share_receive_code="ownpwd",
                share_sync_status="restore_submitted",
            )
            store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": folder_name, "type": "tv", "category": "外国电视", "tmdb_id": "94997"},
                "self_share_resolved",
            )
            store.update_category(int(row["id"]), "外国电视", "selected")
            store.update_move(
                int(row["id"]),
                "moved",
                source_path=str(share_root / folder_name),
                dest_path=str(tv_root / folder_name),
                category_final="外国电视",
            )

            status, _metadata = bridge.restore_missing_self_share_library_folder(
                store,
                cms=None,
                row=store.find_by_id(int(row["id"])),
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(source_roots=[share_root], library_roots={"外国电视": tv_root}, stable_seconds=0),
                required_relative_path="Season 03/Show - S03E03.strm",
            )

            self.assertEqual(status, "move_failed")
            self.assertTrue(preserved.exists())
            self.assertEqual(preserved.read_text(encoding="utf-8"), "http://cms/d/direct/S03E02.mkv")
            self.assertTrue((source / "Show - S03E03.strm").exists())
            self.assertFalse((dest / "Show - S03E03.strm").exists())

    def test_restore_single_episode_requires_source_episode_before_merging_other_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            tv_root = root / "TV"
            folder_name = "Q-Show-2022-[tmdb=94997]"
            source = share_root / folder_name
            dest = tv_root / folder_name
            source.mkdir(parents=True)
            dest.mkdir(parents=True)
            (source / "Show - S03E04.strm").write_text(
                "http://cms/s/owncode_ownpwd_S03E04.mkv",
                encoding="utf-8",
            )
            existing = dest / "Show - S03E02.strm"
            existing.write_text("http://cms/s/owncode_ownpwd_S03E02.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("dummyshare008", "pass008"),
                "https://115cdn.com/s/dummyshare008?password=pass008",
                "submitted",
                title="Show S03",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=folder_name,
                own_share_code="owncode",
                own_share_receive_code="ownpwd",
                share_sync_status="restore_submitted",
            )
            store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": folder_name, "type": "tv", "category": "外国电视", "tmdb_id": "94997"},
                "self_share_resolved",
            )
            store.update_category(int(row["id"]), "外国电视", "selected")
            store.update_move(
                int(row["id"]),
                "moved",
                source_path=str(source),
                dest_path=str(dest),
                category_final="外国电视",
            )

            status, _metadata = bridge.restore_missing_self_share_library_folder(
                store,
                cms=None,
                row=store.find_by_id(int(row["id"])),
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(source_roots=[share_root], library_roots={"外国电视": tv_root}, stable_seconds=0),
                required_relative_path="Season 03/Show - S03E03.strm",
            )

            self.assertEqual(status, "move_failed")
            self.assertTrue(source.exists())
            self.assertTrue((source / "Show - S03E04.strm").exists())
            self.assertTrue(existing.exists())
            self.assertFalse((dest / "Season 03" / "Show - S03E03.strm").exists())

    def test_restore_missing_self_share_library_folder_removes_duplicate_direct_tmdb_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            bangumi_root = root / "Dongman"
            source = share_root / "J-JOJO的奇妙冒险-2012-[tmdb=45790]"
            dest = bangumi_root / "J-JOJO的奇妙冒险-2012-[tmdb=45790]"
            duplicate = bangumi_root / "J-JOJO的奇妙冒险(2012)[tmdbid=45790]"
            source.mkdir(parents=True)
            duplicate.mkdir(parents=True)
            (source / "jojo.strm").write_text("http://cms/s/swslig43wul_1212_1.mkv", encoding="utf-8")
            (duplicate / "jojo.strm").write_text("http://cms/d/direct.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("dummyshare005", "pass005"),
                "https://115cdn.com/s/dummyshare005?password=pass005",
                "submitted",
                title="JoJo's.Bizarre.Adventure.S06",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="swslig43wul",
                own_share_receive_code="1212",
                share_sync_status="restore_submitted",
            )
            store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": source.name, "type": "tv", "category": "番剧", "tmdb_id": "45790"},
                "self_share_resolved",
            )
            store.update_category(int(row["id"]), "番剧", "selected")
            store.update_move(int(row["id"]), "moved", source_path=str(source), dest_path=str(dest), category_final="番剧")
            store.update_cleanup(int(row["id"]), "deleted", file_id="3466183474711365370")

            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms=None,
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(source_roots=[share_root], library_roots={"番剧": bangumi_root}, stable_seconds=0),
                limit=10,
            )

            self.assertEqual(restored, 1)
            self.assertFalse((duplicate / "jojo.strm").exists())
            self.assertEqual((dest / "jojo.strm").read_text(encoding="utf-8"), "http://cms/s/swslig43wul_1212_1.mkv")

    def test_existing_self_share_library_folder_removes_duplicate_direct_tmdb_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            bangumi_root = root / "Dongman"
            dest = bangumi_root / "J-JOJO的奇妙冒险-2012-[tmdb=45790]"
            duplicate = bangumi_root / "J-JOJO的奇妙冒险(2012)[tmdbid=45790]"
            dest.mkdir(parents=True)
            duplicate.mkdir(parents=True)
            (dest / "jojo.strm").write_text("http://cms/s/swslig43wul_1212_1.mkv", encoding="utf-8")
            (duplicate / "jojo.strm").write_text("http://cms/d/direct.mkv", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("dummyshare006", "pass006"),
                "https://115cdn.com/s/dummyshare006?password=pass006",
                "submitted",
                title="JoJo's.Bizarre.Adventure.S06",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=dest.name,
                own_share_code="swslig43wul",
                own_share_receive_code="1212",
                share_sync_status="submitted",
            )
            store.update_recognition(
                int(row["id"]),
                {"ok": True, "title": dest.name, "type": "tv", "category": "番剧", "tmdb_id": "45790"},
                "self_share_resolved",
            )
            store.update_category(int(row["id"]), "番剧", "selected")
            store.update_move(int(row["id"]), "moved", source_path=str(share_root / dest.name), dest_path=str(dest), category_final="番剧")
            store.update_emby(int(row["id"]), "confirmed", path=str(dest / "jojo.strm"), parent="Strm番剧")
            store.update_cleanup(int(row["id"]), "deleted", file_id="3466183474711365370")

            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms=None,
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(source_roots=[share_root], library_roots={"番剧": bangumi_root}, stable_seconds=0),
                limit=10,
            )

            self.assertEqual(restored, 0)
            self.assertFalse((duplicate / "jojo.strm").exists())
            self.assertEqual((dest / "jojo.strm").read_text(encoding="utf-8"), "http://cms/s/swslig43wul_1212_1.mkv")


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

    def test_self_share_source_lookup_rejects_absolute_and_parent_folder_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            share_root.mkdir()
            (share_root / "root.strm").write_text("http://cms/s/unsafe_1212_root", encoding="utf-8")
            nested = share_root / "foo" / "bar"
            nested.mkdir(parents=True)
            (nested / "nested.strm").write_text("http://cms/s/unsafe_1212_nested", encoding="utf-8")
            outside = root / "outside" / "Movie"
            outside.mkdir(parents=True)
            (outside / "movie.strm").write_text("http://cms/s/unsafe_1212_movie", encoding="utf-8")
            config = bridge.SelfShareConfig(enabled=True, strm_root=share_root)

            for folder_name in (".", "foo/bar", str(outside), "../outside/Movie"):
                with self.subTest(folder_name=folder_name):
                    found = bridge.find_self_share_strm_source_dir(
                        config,
                        {
                            "workflow_mode": "self_share_sync",
                            "own_share_file_name": folder_name,
                        },
                        {},
                        "Movie",
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

    def test_maintenance_restores_historical_completed_row_and_refreshes_emby(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            source = share_root / "Y-历史任务-2025-[tmdb=1054867]"
            dest = movie_root / source.name
            source.mkdir(parents=True)
            (source / "历史任务.strm").write_text(
                "http://cms/s/swswold_1212_3455387345258282790.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("oldshare", "pass"),
                "https://115cdn.com/s/oldshare?password=pass",
                "submitted",
                title="历史任务 (2025) {tmdb-1054867}",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="swswold",
                own_share_receive_code="1212",
                share_sync_status="submitted",
                share_validation_status="valid",
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(
                int(row["id"]),
                "moved",
                source_path=str(source),
                dest_path=str(dest),
                category_final="欧美电影",
            )
            old_updated_at = time.time() - 86400
            with store._lock, store._connection() as conn:
                conn.execute("UPDATE submissions SET updated_at = ? WHERE id = ?", (old_updated_at, int(row["id"])))

            class FakeEmby:
                enabled = True

                def __init__(self):
                    self.refreshed_paths = []

                def refresh_library_for_path(self, path):
                    self.refreshed_paths.append(str(path))
                    return "电影库"

            emby = FakeEmby()
            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms=None,
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(
                    source_roots=[share_root],
                    library_roots={"欧美电影": movie_root},
                    stable_seconds=0,
                ),
                emby=emby,
                limit=10,
            )

            self.assertEqual(restored, 1)
            self.assertFalse(source.exists())
            self.assertTrue((dest / "历史任务.strm").exists())
            self.assertEqual(emby.refreshed_paths, [str(bridge.safe_resolve(dest))])

    def test_maintenance_does_not_restore_invalid_self_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            source = share_root / "Y-无效分享-2025-[tmdb=1054867]"
            dest = movie_root / source.name
            source.mkdir(parents=True)
            (source / "无效分享.strm").write_text(
                "http://cms/s/invalid_1212_3455387345258282790.mkv",
                encoding="utf-8",
            )
            store = bridge.SubmissionStore(root / "submissions.db")
            row = store.upsert_submission(
                bridge.ShareKey("invalid-source", "pass"),
                "https://115cdn.com/s/invalid-source?password=pass",
                "submitted",
                title="无效分享 (2025) {tmdb-1054867}",
            )
            store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=source.name,
                own_share_code="invalid",
                own_share_receive_code="1212",
                share_sync_status="submitted",
                share_validation_status="invalid",
            )
            store.update_category(int(row["id"]), "欧美电影", "selected")
            store.update_move(
                int(row["id"]),
                "moved",
                source_path=str(source),
                dest_path=str(dest),
                category_final="欧美电影",
            )
            store.update_emby(int(row["id"]), "confirmed")
            store.update_cleanup(int(row["id"]), "deleted", file_id="3455387345258282790")

            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms=None,
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(
                    source_roots=[share_root],
                    library_roots={"欧美电影": movie_root},
                    stable_seconds=0,
                ),
                limit=10,
            )

            self.assertEqual(restored, 0)
            self.assertTrue((source / "无效分享.strm").exists())
            self.assertFalse(dest.exists())

    def test_maintenance_scans_candidates_beyond_action_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            movie_root = root / "Movie"
            healthy_source = share_root / "H-健康任务-2025-[tmdb=1054867]"
            healthy_dest = movie_root / healthy_source.name
            missing_source = share_root / "M-历史任务-2025-[tmdb=1054867]"
            missing_dest = movie_root / missing_source.name
            healthy_source.mkdir(parents=True)
            healthy_dest.mkdir(parents=True)
            missing_source.mkdir(parents=True)
            (healthy_source / "健康任务.strm").write_text("http://cms/s/healthy_1212_movie", encoding="utf-8")
            (healthy_dest / "健康任务.strm").write_text("http://cms/s/healthy_1212_movie", encoding="utf-8")
            (missing_source / "历史任务.strm").write_text("http://cms/s/history_1212_movie", encoding="utf-8")
            store = bridge.SubmissionStore(root / "submissions.db")

            healthy = store.upsert_submission(
                bridge.ShareKey("healthy-source", "pass"),
                "https://115cdn.com/s/healthy-source?password=pass",
                "submitted",
                title="健康任务 (2025) {tmdb-1054867}",
            )
            store.update_self_share(
                int(healthy["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=healthy_source.name,
                own_share_code="healthy",
                own_share_receive_code="1212",
                share_sync_status="submitted",
                share_validation_status="valid",
            )
            store.update_category(int(healthy["id"]), "欧美电影", "selected")
            store.update_move(
                int(healthy["id"]),
                "moved",
                source_path=str(healthy_source),
                dest_path=str(healthy_dest),
                category_final="欧美电影",
            )

            missing = store.upsert_submission(
                bridge.ShareKey("history-source", "pass"),
                "https://115cdn.com/s/history-source?password=pass",
                "submitted",
                title="历史任务 (2025) {tmdb-1054867}",
            )
            store.update_self_share(
                int(missing["id"]),
                workflow_mode="self_share_sync",
                own_share_file_name=missing_source.name,
                own_share_code="history",
                own_share_receive_code="1212",
                share_sync_status="submitted",
                share_validation_status="valid",
            )
            store.update_category(int(missing["id"]), "欧美电影", "selected")
            store.update_move(
                int(missing["id"]),
                "moved",
                source_path=str(missing_source),
                dest_path=str(missing_dest),
                category_final="欧美电影",
            )
            old_updated_at = time.time() - 86400
            with store._lock, store._connection() as conn:
                conn.execute("UPDATE submissions SET updated_at = ? WHERE id = ?", (old_updated_at, int(missing["id"])))

            restored = bridge.restore_missing_self_share_library_folders(
                store,
                cms=None,
                self_share_config=bridge.SelfShareConfig(enabled=True, strm_root=share_root),
                move_config=bridge.MoveConfig(
                    source_roots=[share_root],
                    library_roots={"欧美电影": movie_root},
                    stable_seconds=0,
                ),
                limit=1,
            )

            self.assertEqual(restored, 1)
            self.assertTrue((missing_dest / "历史任务.strm").exists())

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

            def request(self, url, method="POST", payload=None, headers=None, safe_get_attempts=None):
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
