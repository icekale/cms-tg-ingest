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


class QueueHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, method="GET", payload=None, headers=None):
        self.calls.append((url, method, payload, headers))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


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

    def test_existing_episode_keys_are_loaded_from_tmdb_series(self):
        http = QueueHttp([
            {"Items": [{"Id": "series-1", "ProviderIds": {"Tmdb": "1416"}}]},
            {"Items": [
                {"ParentIndexNumber": 1, "IndexNumber": 2},
                {"ParentIndexNumber": 2, "IndexNumber": 1},
            ]},
        ])
        client = EmbyClient("http://emby.test", "secret-key", user_id="user-1", http=http)

        self.assertEqual(client.existing_episode_keys_by_tmdb("1416"), {"S01E02", "S02E01"})
        self.assertNotIn("secret-key", http.calls[0][0])

        first_url = http.calls[0][0]
        self.assertIn("/Users/user-1/Items?", first_url)
        self.assertIn("AnyProviderIdEquals=tmdb.1416", first_url)
        self.assertIn("IncludeItemTypes=Series", first_url)
        self.assertIn("Limit=10", first_url)

        second_url = http.calls[1][0]
        self.assertIn("/Shows/series-1/Episodes?", second_url)
        self.assertIn("UserId=user-1", second_url)
        self.assertIn("Fields=ParentIndexNumber%2CIndexNumber", second_url)

    def test_find_series_by_tmdb_requires_an_exact_series_provider_id(self):
        http = QueueHttp([{
            "Items": [
                {"Id": "wrong", "ProviderIds": {"Tmdb": "14160"}},
                {"Id": "right", "ProviderIds": {"Tmdb": "1416"}},
            ],
        }])
        client = EmbyClient("http://emby.test", "secret-key", user_id="user-1", http=http)

        self.assertEqual(client.find_series_by_tmdb("1416"), {"Id": "right", "ProviderIds": {"Tmdb": "1416"}})

    def test_episode_keys_ignore_missing_non_positive_or_invalid_indexes(self):
        http = QueueHttp([{
            "Items": [
                {"ParentIndexNumber": 1, "IndexNumber": 2},
                {"ParentIndexNumber": 0, "IndexNumber": 1},
                {"ParentIndexNumber": 1, "IndexNumber": 0},
                {"ParentIndexNumber": "bad", "IndexNumber": 3},
                {"ParentIndexNumber": 2},
            ],
        }])
        client = EmbyClient("http://emby.test", "secret-key", user_id="user-1", http=http)

        self.assertEqual(client.episode_keys_for_series("series/id"), {"S01E02"})
        self.assertIn("/Shows/series%2Fid/Episodes?", http.calls[0][0])

    def test_episode_query_http_errors_propagate(self):
        error = RuntimeError("emby unavailable")
        http = QueueHttp([error])
        client = EmbyClient("http://emby.test", "secret-key", user_id="user-1", http=http)

        with self.assertRaisesRegex(RuntimeError, "emby unavailable"):
            client.episode_keys_for_series("series-1")


if __name__ == "__main__":
    unittest.main()
