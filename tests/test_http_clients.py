import http.client
import json
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.clients.cms import CmsClient
from app.clients.http import FormHttp, HttpJson, _redact_text, _redact_url
from app.clients.p115 import P115RiskControlError, P115WebClient
from app.config import Config


class FakeResponse:
    def __init__(self, payload: str):
        self.payload = payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class TrackingHTTPError(HTTPError):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_by_client = False

    def close(self):
        self.closed_by_client = True
        return super().close()


class HttpClientTests(unittest.TestCase):
    def test_create_share_only_sends_share_request(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {})))
                if url.endswith("/share/send"):
                    return {
                        "state": True,
                        "data": {
                            "share_code": "created-code",
                            "receive_code": "generated-code",
                            "share_url": "https://115cdn.com/s/created-code",
                        },
                    }
                raise AssertionError(url)

        http = FakeHttp()
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.create_share("folder-id")

        self.assertEqual(
            result,
            {
                "share_code": "created-code",
                "receive_code": "generated-code",
                "share_url": "https://115cdn.com/s/created-code",
            },
        )
        self.assertEqual(
            http.calls,
            [
                (
                    "https://webapi.115.com/share/send",
                    "POST",
                    {"file_ids": "folder-id", "ignore_warn": 1},
                )
            ],
        )

    def test_ensure_share_settings_sets_receive_code_and_unlimited_duration(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {})))
                if url.endswith("/share/updateshare"):
                    return {
                        "state": True,
                        "data": {"created-code": {"receive_code": "saved-code"}},
                    }
                raise AssertionError(url)

        http = FakeHttp()
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.ensure_share_settings("created-code", "requested-code")

        self.assertEqual(result["share_code"], "created-code")
        self.assertEqual(result["receive_code"], "saved-code")
        self.assertEqual(
            http.calls,
            [
                (
                    "https://webapi.115.com/share/updateshare",
                    "POST",
                    {
                        "share_code": "created-code",
                        "receive_code": "requested-code",
                        "share_duration": -1,
                        "auto_fill_recvcode": 1,
                    },
                )
            ],
        )

    def test_file_exists_in_parent_uses_fresh_parent_listing(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []
                self.items = [{"cid": "stale-id", "pid": "parent-id", "n": "Old"}]

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, dict(params or {})))
                if url.endswith("/files"):
                    return {"state": True, "data": list(self.items)}
                raise AssertionError(url)

        http = FakeHttp()
        client = P115WebClient("UID=1", http=http, timeout=3, cache_ttl_seconds=60, clock=lambda: 100)
        client.list_files("parent-id")
        http.items = [{"fid": "target-id", "cid": "parent-id", "n": "Target.mkv"}]

        exists = client.file_exists_in_parent("target-id", "parent-id")

        self.assertTrue(exists)
        self.assertEqual(len(http.calls), 2)

    def test_prepare_share_receive_reads_and_serializes_exact_snapshot(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {}), dict(params or {})))
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "Multi-root share"},
                            "list": [
                                {"fid": "source-a", "n": "Root A"},
                                {"fid": "source-b", "n": "Extras"},
                            ],
                        },
                    }
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {
                                "cid": "old-local-a",
                                "pid": "pending-cid",
                                "n": "Root A",
                            }
                        ],
                    }
                raise AssertionError(url)

        http = FakeHttp()
        client = P115WebClient("UID=1", http=http, timeout=3)

        intent = client.prepare_share_receive("abc", "1234", "pending-cid")

        self.assertEqual(intent["share_code"], "abc")
        self.assertEqual(intent["receive_code"], "1234")
        self.assertEqual(intent["target_cid"], "pending-cid")
        self.assertEqual(intent["source_file_ids"], ["source-a", "source-b"])
        self.assertEqual(
            intent["source_file_names"],
            ["Root A", "Extras"],
        )
        self.assertEqual(intent["title"], "Multi-root share")
        self.assertEqual(intent["target_pre_call_file_ids"], ["old-local-a"])
        self.assertTrue(intent["target_snapshot_complete"])
        json.dumps(intent)
        self.assertTrue(all(method == "GET" for _url, method, _data, _params in http.calls))
        self.assertFalse(any(url.endswith("/share/receive") for url, *_rest in http.calls))

    def test_prepare_share_receive_snapshots_real_file_id_not_parent_cid(self):
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
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {
                                "fid": "old-local-id",
                                "cid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                                "fc": 1,
                            }
                        ],
                    }
                raise AssertionError(url)

        client = P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        intent = client.prepare_share_receive("abc", "1234", "pending-cid")

        self.assertEqual(intent["target_pre_call_file_ids"], ["old-local-id"])

    def test_prepare_share_receive_bypasses_preseeded_source_and_target_caches(self):
        class FakeHttp:
            def __init__(self):
                self.source_id = "stale-source"
                self.target_id = "stale-target"
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append(url)
                if url.endswith("/share/snap"):
                    return {
                        "state": True,
                        "data": {
                            "shareinfo": {"share_title": "Root A"},
                            "list": [{"fid": self.source_id, "n": "Root A"}],
                        },
                    }
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {"cid": self.target_id, "pid": "pending-cid", "n": "Existing Root"}
                        ],
                    }
                raise AssertionError(url)

        http = FakeHttp()
        client = P115WebClient("UID=1", http=http, timeout=3, cache_ttl_seconds=60, clock=lambda: 100)
        client.share_root_items("abc", "1234", cid="0", limit=100)
        client.list_files("pending-cid", limit=500)
        http.source_id = "fresh-source"
        http.target_id = "fresh-target"

        intent = client.prepare_share_receive("abc", "1234", "pending-cid")

        self.assertEqual(intent["source_file_ids"], ["fresh-source"])
        self.assertEqual(intent["target_pre_call_file_ids"], ["fresh-target"])
        self.assertEqual(sum(url.endswith("/share/snap") for url in http.calls), 2)
        self.assertEqual(sum(url.endswith("/files") for url in http.calls), 2)

    def test_execute_prepared_share_receive_posts_saved_source_ids_once(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {}), dict(params or {})))
                if url.endswith("/share/receive"):
                    return {"state": True, "data": {"receive_title": "Saved title"}}
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {"cid": "old-a", "pid": "saved-target", "n": "Root A"},
                            {"cid": "new-a", "pid": "saved-target", "n": "Root A"},
                            {"cid": "new-b", "pid": "saved-target", "n": "Root B"},
                        ],
                    }
                raise AssertionError(url)

        http = FakeHttp()
        client = P115WebClient("UID=1", http=http, timeout=3)
        intent = {
            "share_code": "saved-share",
            "receive_code": "saved-code",
            "target_cid": "saved-target",
            "source_file_ids": ["saved-source-a", "saved-source-b"],
            "source_file_names": ["Root A", "Root B"],
            "title": "Saved title",
            "target_pre_call_file_ids": ["old-a"],
            "target_snapshot_complete": True,
        }

        result = client.execute_prepared_share_receive(intent)

        receive_calls = [call for call in http.calls if call[0].endswith("/share/receive")]
        self.assertEqual(len(receive_calls), 1)
        self.assertEqual(receive_calls[0][1], "POST")
        self.assertEqual(
            receive_calls[0][2],
            {
                "share_code": "saved-share",
                "receive_code": "saved-code",
                "file_id": "saved-source-a,saved-source-b",
                "cid": "saved-target",
            },
        )
        self.assertEqual(
            [item["file_id"] for item in result["received_items"]],
            ["new-a", "new-b"],
        )
        self.assertTrue(result["received_items_complete"])

    def test_reconcile_prepared_share_receive_excludes_pre_call_same_name_items(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", data=None, headers=None, params=None):
                self.calls.append((url, method, dict(data or {}), dict(params or {})))
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {"cid": "old-a", "pid": "pending-cid", "n": "Root A"},
                            {"cid": "new-a", "pid": "pending-cid", "n": "Root A"},
                            {"cid": "new-b", "pid": "pending-cid", "n": "Root B"},
                        ],
                    }
                raise AssertionError(url)

        http = FakeHttp()
        client = P115WebClient("UID=1", http=http, timeout=3)
        intent = {
            "share_code": "abc",
            "receive_code": "1234",
            "target_cid": "pending-cid",
            "source_file_ids": ["source-a", "source-b"],
            "source_file_names": ["Root A", "Root B"],
            "title": "Multi-root share",
            "target_pre_call_file_ids": ["old-a"],
            "target_snapshot_complete": True,
        }

        result = client.reconcile_prepared_share_receive(intent)

        self.assertIsNotNone(result)
        self.assertEqual(
            [item["file_id"] for item in result["received_items"]],
            ["new-a", "new-b"],
        )
        self.assertTrue(result["received_items_complete"])
        self.assertTrue(all(method == "GET" for _url, method, _data, _params in http.calls))
        self.assertFalse(any(url.endswith("/share/receive") for url, *_rest in http.calls))

    def test_reconcile_prepared_share_receive_handles_real_file_records(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {
                                "fid": "old-local-id",
                                "cid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                                "fc": 1,
                            },
                            {
                                "fid": "new-local-id",
                                "cid": "pending-cid",
                                "n": "123 (2026) {tmdb-1228710}.mkv",
                                "fc": 1,
                            },
                        ],
                    }
                raise AssertionError(url)

        client = P115WebClient("UID=1", http=FakeHttp(), timeout=3)
        intent = {
            "share_code": "abc",
            "receive_code": "1234",
            "target_cid": "pending-cid",
            "source_file_ids": ["source-id"],
            "source_file_names": ["123 (2026) {tmdb-1228710}.mkv"],
            "title": "123 (2026) {tmdb-1228710}",
            "target_pre_call_file_ids": ["old-local-id"],
            "target_snapshot_complete": True,
        }

        result = client.reconcile_prepared_share_receive(intent)

        self.assertIsNotNone(result)
        self.assertEqual(
            result["received_items"],
            [
                {
                    "file_id": "new-local-id",
                    "file_name": "123 (2026) {tmdb-1228710}.mkv",
                    "is_folder": False,
                    "parent_id": "pending-cid",
                    "received_item_verified": True,
                }
            ],
        )
        self.assertTrue(result["received_items_complete"])

    def test_reconcile_prepared_share_receive_rejects_unrelated_single_delta(self):
        class FakeHttp:
            def request(self, url, method="GET", data=None, headers=None, params=None):
                if url.endswith("/files"):
                    return {
                        "state": True,
                        "data": [
                            {
                                "cid": "unrelated-new-id",
                                "pid": "pending-cid",
                                "n": "Unrelated Upload",
                            }
                        ],
                    }
                raise AssertionError(url)

        client = P115WebClient("UID=1", http=FakeHttp(), timeout=3)
        intent = {
            "share_code": "abc",
            "receive_code": "1234",
            "target_cid": "pending-cid",
            "source_file_ids": ["source-a"],
            "source_file_names": ["Root A"],
            "title": "Root A",
            "target_pre_call_file_ids": [],
            "target_snapshot_complete": True,
        }

        result = client.reconcile_prepared_share_receive(intent)

        self.assertIsNone(result)

    def test_json_get_retries_remote_disconnect(self):
        with (
            patch(
                "app.clients.http.urllib.request.urlopen",
                side_effect=[http.client.RemoteDisconnected("remote closed"), FakeResponse('{"ok": true}')],
            ) as urlopen,
            patch("app.clients.http.time.sleep") as sleep,
        ):
            result = HttpJson(timeout=1).request("https://example.test/getUpdates")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_json_get_retries_one_transient_network_error(self):
        with (
            patch("app.clients.http.urllib.request.urlopen", side_effect=[URLError("temporary"), FakeResponse('{"ok": true}')]) as urlopen,
            patch("app.clients.http.time.sleep") as sleep,
        ):
            result = HttpJson(timeout=1).request("https://example.test/status")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_cms_auto_organize_does_not_retry_after_lost_response(self):
        config = Config(
            tg_bot_token="tg",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            http_timeout=1,
        )
        responses = [
            FakeResponse('{"code": 200, "data": {"token": "cms-token"}}'),
            http.client.RemoteDisconnected("lost response"),
            FakeResponse('{"code": 200, "data": {}}'),
        ]

        with patch("app.clients.http.urllib.request.urlopen", side_effect=responses) as urlopen:
            with self.assertRaisesRegex(RuntimeError, "lost response"):
                CmsClient(config).run_auto_organize()

        self.assertEqual(urlopen.call_count, 2)

    def test_form_get_retries_one_transient_network_error(self):
        with (
            patch("app.clients.http.urllib.request.urlopen", side_effect=[URLError("temporary"), FakeResponse('{"ok": true}')]) as urlopen,
            patch("app.clients.http.time.sleep") as sleep,
        ):
            result = FormHttp(timeout=1).request("https://example.test/status")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_p115_http_429_is_not_retried_inside_transport(self):
        def rate_limited(*_args, **_kwargs):
            raise TrackingHTTPError(
                "https://webapi.115.com/files/search",
                429,
                "Too Many Requests",
                {"Retry-After": "120"},
                BytesIO(b'{"message":"too many requests"}'),
            )

        with patch("app.clients.http.urllib.request.urlopen", side_effect=rate_limited) as urlopen:
            client = P115WebClient("UID=1", timeout=1, cache_ttl_seconds=0)

            with self.assertRaises(P115RiskControlError) as raised:
                client.search_files("movie")

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(client.request_count, 1)
        self.assertEqual(getattr(raised.exception, "retry_after_seconds", 0), 120)

    def test_p115_http_429_parses_http_date_retry_after(self):
        def rate_limited(*_args, **_kwargs):
            raise TrackingHTTPError(
                "https://webapi.115.com/files/search",
                429,
                "Too Many Requests",
                {"Retry-After": "Thu, 01 Jan 1970 00:18:40 GMT"},
                BytesIO(b'{"message":"too many requests"}'),
            )

        with (
            patch("app.clients.http.urllib.request.urlopen", side_effect=rate_limited),
            patch("app.clients.http.time.time", return_value=1000),
        ):
            client = P115WebClient("UID=1", timeout=1, cache_ttl_seconds=0)

            with self.assertRaises(P115RiskControlError) as raised:
                client.search_files("movie")

        self.assertEqual(getattr(raised.exception, "retry_after_seconds", 0), 120)

    def test_post_does_not_retry_transient_network_error(self):
        with patch("app.clients.http.urllib.request.urlopen", side_effect=URLError("temporary")) as urlopen:
            with self.assertRaises(RuntimeError):
                HttpJson(timeout=1).request("https://example.test/submit", method="POST", payload={"id": 1})

        self.assertEqual(urlopen.call_count, 1)

    def test_non_json_json_response_redacts_telegram_bot_token(self):
        with patch(
            "app.clients.http.urllib.request.urlopen",
            return_value=FakeResponse("<html>bad gateway</html>"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                HttpJson(timeout=1).request("https://api.telegram.org/botSECRET/getMe")

        self.assertNotIn("SECRET", str(raised.exception))
        self.assertIn("bot<redacted>", str(raised.exception))

    def test_non_json_form_response_redacts_query_token(self):
        with patch(
            "app.clients.http.urllib.request.urlopen",
            return_value=FakeResponse("not json"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                FormHttp(timeout=1).request(
                    "https://example.test/status",
                    params={"token": "SECRET"},
                )

        self.assertNotIn("SECRET", str(raised.exception))
        self.assertIn("token=%3Credacted%3E", str(raised.exception))

    def test_json_decode_error_redacts_url_and_truncates_response(self):
        body = "!" * 400
        with patch(
            "app.clients.http.urllib.request.urlopen",
            return_value=FakeResponse(body),
        ):
            with self.assertRaises(RuntimeError) as raised:
                FormHttp(timeout=1).request(
                    "https://example.test/status?access_token=SECRET",
                )

        message = str(raised.exception)
        self.assertNotIn("SECRET", message)
        self.assertIn("access_token=%3Credacted%3E", message)
        self.assertTrue(message.endswith(": " + ("!" * 300)))

    def test_redact_url_hides_password_and_sensitive_fragment(self):
        redacted = _redact_url(
            "https://example.test/share?name=movie&password=URL_SECRET#sessdata=FRAGMENT_SECRET"
        )

        self.assertIn("name=movie", redacted)
        self.assertIn("password=%3Credacted%3E", redacted)
        self.assertIn("sessdata=%3Credacted%3E", redacted)
        self.assertNotIn("URL_SECRET", redacted)
        self.assertNotIn("FRAGMENT_SECRET", redacted)

    def test_http_error_redacts_sensitive_response_body(self):
        body = (
            "bad gateway "
            "https://api.telegram.org/botBOT_SECRET/getMe?api_key=API_SECRET "
            "cookie=COOKIE_SECRET"
        )
        error = HTTPError(
            "https://example.test/status?password=URL_SECRET#token=FRAGMENT_SECRET",
            400,
            "bad gateway",
            {},
            BytesIO(body.encode("utf-8")),
        )

        with patch("app.clients.http.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                HttpJson(timeout=1).request(
                    "https://example.test/status?password=URL_SECRET#token=FRAGMENT_SECRET"
                )

        message = str(raised.exception)
        for secret in ("BOT_SECRET", "API_SECRET", "COOKIE_SECRET", "URL_SECRET", "FRAGMENT_SECRET"):
            self.assertNotIn(secret, message)
        self.assertIn("bad gateway", message)

    def test_http_error_response_body_is_closed_after_error_message_is_read(self):
        for client_type in (HttpJson, FormHttp):
            error = TrackingHTTPError(
                "https://example.test/status",
                400,
                "service unavailable",
                {},
                BytesIO(b"temporary failure"),
            )
            with patch("app.clients.http.urllib.request.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError):
                    client_type(timeout=1).request("https://example.test/status")
            self.assertTrue(error.closed_by_client)

    def test_non_json_response_redacts_sensitive_body_before_truncation(self):
        body = (
            'upstream url=https://example.test/status?secret=URL_SECRET '
            'api_key="API_SECRET" access_token="ACCESS_SECRET" cookie="COOKIE_SECRET" '
            'https://api.telegram.org/botBOT_SECRET/getMe '
            + ("!" * 400)
        )
        with patch(
            "app.clients.http.urllib.request.urlopen",
            return_value=FakeResponse(body),
        ):
            with self.assertRaises(RuntimeError) as raised:
                FormHttp(timeout=1).request("https://example.test/status")

        message = str(raised.exception)
        for secret in ("BOT_SECRET", "API_SECRET", "ACCESS_SECRET", "COOKIE_SECRET", "URL_SECRET"):
            self.assertNotIn(secret, message)
        self.assertIn("upstream url=https://example.test/status?secret=%3Credacted%3E", message)
        self.assertEqual(len(message.split(": ", 1)[1]), 300)

    def test_redact_url_hides_camel_case_userinfo_path_and_nested_credentials(self):
        redacted = _redact_url(
            "https://user:USER_SECRET@example.test/v1/accessToken/PATH_SECRET"
            "?accessToken=QUERY_SECRET#refreshToken=FRAGMENT_SECRET"
        )
        body = _redact_text(
            "upstream next=https%3A%2F%2Fexample.test%2Fstatus%3FaccessToken%3D"
            "ENCODED_SECRET%2520TAIL_SECRET"
        )
        nested_json = _redact_url(
            "https://example.test/status?payload=%7B%22accessToken%22%3A%22JSON_SECRET%22%7D"
        )

        for secret in (
            "USER_SECRET",
            "PATH_SECRET",
            "QUERY_SECRET",
            "FRAGMENT_SECRET",
            "ENCODED_SECRET",
            "TAIL_SECRET",
            "JSON_SECRET",
        ):
            self.assertNotIn(secret, redacted)
            self.assertNotIn(secret, body)
            self.assertNotIn(secret, nested_json)
        self.assertIn("user:<redacted>@example.test", redacted)
        self.assertIn("accessToken=%3Credacted%3E", redacted)


if __name__ == "__main__":
    unittest.main()
