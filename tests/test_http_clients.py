import http.client
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.clients.http import FormHttp, HttpJson, _redact_text, _redact_url


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

    def test_form_get_retries_one_transient_network_error(self):
        with (
            patch("app.clients.http.urllib.request.urlopen", side_effect=[URLError("temporary"), FakeResponse('{"ok": true}')]) as urlopen,
            patch("app.clients.http.time.sleep") as sleep,
        ):
            result = FormHttp(timeout=1).request("https://example.test/status")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

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
