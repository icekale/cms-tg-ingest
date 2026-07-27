import http.client
import unittest
from unittest.mock import patch
from urllib.error import URLError

from app.clients.http import FormHttp, HttpJson


class FakeResponse:
    def __init__(self, payload: str):
        self.payload = payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


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


if __name__ == "__main__":
    unittest.main()
