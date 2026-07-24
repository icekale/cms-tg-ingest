import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import patch

from app.clients.emby import EmbyClient
from app.clients.http import HttpJson


class RecordingHttp:
    def __init__(self):
        self.calls = []

    def request(self, url, method="GET", payload=None, headers=None):
        self.calls.append((url, method, payload, headers))
        return []


class EmbyClientTests(unittest.TestCase):
    def test_api_key_is_sent_as_emby_token_header_not_url_parameter(self):
        http = RecordingHttp()
        client = EmbyClient("http://emby.test", "secret-key", http=http)

        client._get("/Users")

        url, _method, _payload, headers = http.calls[0]
        self.assertNotIn("secret-key", url)
        self.assertNotIn("api_key", url)
        self.assertEqual(headers["X-Emby-Token"], "secret-key")

    def test_http_errors_redact_api_key_from_error_messages(self):
        error = HTTPError(
            "https://emby.test/Items?api_key=secret-key",
            400,
            "server error",
            {},
            BytesIO(b"server error"),
        )
        with patch("app.clients.http.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                HttpJson(timeout=1).request("https://emby.test/Items?api_key=secret-key")

        self.assertNotIn("secret-key", str(raised.exception))
        self.assertIn("api_key=%3Credacted%3E", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
