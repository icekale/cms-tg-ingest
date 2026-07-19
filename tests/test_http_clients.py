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


if __name__ == "__main__":
    unittest.main()
