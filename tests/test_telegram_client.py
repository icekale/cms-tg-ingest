import unittest
from unittest.mock import patch

from bridge import TelegramClient
from app.clients.http import _redact_url


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


if __name__ == "__main__":
    unittest.main()
