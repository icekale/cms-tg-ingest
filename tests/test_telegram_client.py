import unittest
from unittest.mock import patch

from bridge import TelegramClient
from app.clients.http import _redact_url
from app.clients.http import HttpJson


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class SequenceHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TelegramClientTests(unittest.TestCase):
    def test_remote_end_closed_is_a_transient_get_updates_error(self):
        error = RuntimeError("Cannot reach https://api.telegram.org: Remote end closed connection without response")

        self.assertTrue(TelegramClient._is_transient_get_updates_error(error))

    def test_answer_callback_query_retries_transient_eof(self):
        http = SequenceHttp(
            [
                RuntimeError(
                    "Cannot reach https://api.telegram.org/botsecret/answerCallbackQuery: "
                    "UNEXPECTED_EOF_WHILE_READING EOF occurred"
                ),
                {"ok": True},
            ]
        )

        with patch("bridge.time.sleep") as sleep:
            TelegramClient("secret", http=http).answer_callback_query("callback-1", "完成")

        self.assertEqual(len(http.calls), 2)
        sleep.assert_called_once_with(0.2)

    def test_answer_callback_query_network_failure_is_best_effort(self):
        http = SequenceHttp(
            [RuntimeError("Cannot reach https://api.telegram.org/botsecret/answerCallbackQuery: EOF")]
        )

        TelegramClient("secret", http=http).answer_callback_query("callback-1", "完成")

        self.assertEqual(len(http.calls), 1)

    def test_redact_url_hides_telegram_bot_token(self):
        url = "https://api.telegram.org/bot123456:secret-token/answerCallbackQuery"

        redacted = _redact_url(url)

        self.assertEqual(redacted, "https://api.telegram.org/bot<redacted>/answerCallbackQuery")

    def test_non_json_telegram_response_redacts_bot_token(self):
        with patch(
            "app.clients.http.urllib.request.urlopen",
            return_value=FakeResponse("<html>bad gateway</html>"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                HttpJson(timeout=1).request("https://api.telegram.org/botSECRET/getMe")

        self.assertNotIn("SECRET", str(raised.exception))
        self.assertIn("bot<redacted>", str(raised.exception))

    def test_get_updates_halves_timeout_after_transient_eof(self):
        eof = RuntimeError(
            "Cannot reach https://api.telegram.org/botsecret/getUpdates: "
            "UNEXPECTED_EOF_WHILE_READING EOF occurred"
        )
        # each call: attempt0 EOF (bumps counter), attempt1 success
        http = SequenceHttp([eof, {"ok": True, "result": []}, eof, {"ok": True, "result": []}])

        with patch("bridge.time.sleep"):
            client = TelegramClient("secret", http=http)
            client.get_updates(offset=None, timeout=30)
            client.get_updates(offset=None, timeout=30)

        # counter: 1 after first call, 2 after second -> second call polls at 15
        self.assertEqual(client._consecutive_transient, 2)
        urls = [url for url, _kwargs in http.calls]
        self.assertTrue(any("timeout=30" in url for url in urls))
        self.assertTrue(any("timeout=15" in url for url in urls))

    def test_get_updates_timeout_recovers_after_success(self):
        eof = RuntimeError(
            "Cannot reach https://api.telegram.org/botsecret/getUpdates: "
            "UNEXPECTED_EOF_WHILE_READING EOF occurred"
        )
        http = SequenceHttp([eof, {"ok": True, "result": []}, {"ok": True, "result": []}])

        with patch("bridge.time.sleep"):
            client = TelegramClient("secret", http=http)
            client.get_updates(offset=None, timeout=30)
            client.get_updates(offset=None, timeout=30)

        # counter decays back to 0 after consecutive success
        self.assertEqual(client._consecutive_transient, 0)
        urls = [url for url, _kwargs in http.calls]
        self.assertTrue(any("timeout=30" in url for url in urls))

    def test_get_updates_success_without_transient_keeps_configured_timeout(self):
        http = SequenceHttp([{"ok": True, "result": []}])

        client = TelegramClient("secret", http=http)
        client.get_updates(offset=None, timeout=30)

        self.assertEqual(client._consecutive_transient, 0)
        self.assertTrue(any("timeout=30" in url for url, _kwargs in http.calls))


if __name__ == "__main__":
    unittest.main()
