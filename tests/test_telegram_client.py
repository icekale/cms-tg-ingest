import unittest
from unittest.mock import patch

from bridge import TelegramClient
from app.clients.http import _redact_url
from app.clients.http import HttpJson
from app.clients.http import HttpRequestError
from app.telegram_rich import RichDocument, heading, table


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


class TelegramRichClientTests(unittest.TestCase):
    def test_send_rich_message_posts_blocks(self):
        http = SequenceHttp([{"ok": True}])
        doc = RichDocument((heading("健康检查"), table(("组件", "状态"), (("CMS", "OK"),))))
        keyboard = {"inline_keyboard": [[{"text": "x", "callback_data": "x"}]]}

        TelegramClient("secret", http=http).send_rich_message(1, doc, reply_markup=keyboard)

        url, kwargs = http.calls[0]
        self.assertTrue(url.endswith("/sendRichMessage"))
        payload = kwargs["payload"]
        self.assertEqual(payload["chat_id"], 1)
        self.assertTrue(payload["rich_message"]["skip_entity_detection"])
        self.assertEqual(payload["rich_message"]["blocks"][0]["type"], "heading")
        self.assertEqual(payload["reply_markup"], keyboard)
        self.assertEqual(len(http.calls), 1)

    def test_empty_document_does_not_send(self):
        http = SequenceHttp([])
        TelegramClient("secret", http=http).send_rich_message(1, RichDocument())
        self.assertEqual(http.calls, [])

    def test_http_400_falls_back_to_send_message(self):
        http = SequenceHttp(
            [
                HttpRequestError("HTTP 400 from https://api.telegram.org/bot<redacted>/sendRichMessage: bad", status_code=400),
                {"ok": True},
            ]
        )
        doc = RichDocument((heading("任务统计"),))
        keyboard = {"inline_keyboard": []}

        TelegramClient("secret", http=http).send_rich_message(9, doc, reply_markup=keyboard)

        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0][0].endswith("/sendRichMessage"))
        self.assertEqual(
            sum(url.endswith("/sendMessage") for url, _kwargs in http.calls),
            1,
        )
        self.assertEqual(http.calls[1][1]["payload"]["chat_id"], 9)
        self.assertEqual(http.calls[1][1]["payload"]["text"], doc.to_plain())
        self.assertEqual(http.calls[1][1]["payload"]["reply_markup"], keyboard)

    def test_ok_false_400_falls_back(self):
        http = SequenceHttp(
            [
                {"ok": False, "error_code": 400, "description": "Bad Request: can't parse rich blocks"},
                {"ok": True},
            ]
        )
        doc = RichDocument((heading("最近任务"), table(("状态", "结果"), (("任务", "等待"),))))
        keyboard = {"inline_keyboard": [[{"text": "刷新", "callback_data": "refresh"}]]}

        TelegramClient("secret", http=http).send_rich_message(1, doc, reply_markup=keyboard)

        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0][0].endswith("/sendRichMessage"))
        self.assertEqual(
            sum(url.endswith("/sendMessage") for url, _kwargs in http.calls),
            1,
        )
        self.assertEqual(http.calls[1][1]["payload"]["chat_id"], 1)
        self.assertEqual(http.calls[1][1]["payload"]["text"], doc.to_plain())
        self.assertEqual(http.calls[1][1]["payload"]["reply_markup"], keyboard)

    def test_unknown_method_falls_back(self):
        http = SequenceHttp(
            [
                HttpRequestError("HTTP 404 from https://api.telegram.org/bot<redacted>/sendRichMessage: unknown method", status_code=404),
                {"ok": True},
            ]
        )
        doc = RichDocument((heading("最近历史"),))
        keyboard = {"inline_keyboard": [[{"text": "详情", "callback_data": "detail"}]]}

        TelegramClient("secret", http=http).send_rich_message(1, doc, reply_markup=keyboard)

        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0][0].endswith("/sendRichMessage"))
        self.assertEqual(
            sum(url.endswith("/sendMessage") for url, _kwargs in http.calls),
            1,
        )
        self.assertEqual(http.calls[1][1]["payload"]["chat_id"], 1)
        self.assertEqual(http.calls[1][1]["payload"]["text"], doc.to_plain())
        self.assertEqual(http.calls[1][1]["payload"]["reply_markup"], keyboard)

    def test_network_error_does_not_fall_back(self):
        http = SequenceHttp(
            [RuntimeError("Cannot reach https://api.telegram.org/bot<redacted>/sendRichMessage: Remote end closed")]
        )
        with self.assertRaises(RuntimeError):
            TelegramClient("secret", http=http).send_rich_message(1, RichDocument((heading("健康检查"),)))
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0][0].endswith("/sendRichMessage"))
        self.assertFalse(any(url.endswith("/sendMessage") for url, _kwargs in http.calls))

    def test_edit_rich_message_posts_rich_message(self):
        http = SequenceHttp([{"ok": True}])
        doc = RichDocument((heading("健康检查"),))
        TelegramClient("secret", http=http).edit_rich_message(1, 17, doc)
        url, kwargs = http.calls[0]
        self.assertTrue(url.endswith("/editMessageText"))
        self.assertEqual(kwargs["payload"]["message_id"], 17)
        self.assertTrue(kwargs["payload"]["rich_message"]["skip_entity_detection"])

    def test_edit_rich_not_modified_is_success(self):
        http = SequenceHttp(
            [{"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}]
        )
        TelegramClient("secret", http=http).edit_rich_message(1, 17, RichDocument((heading("最近任务"),)))
        self.assertEqual(len(http.calls), 1)

    def test_edit_rich_400_falls_back_to_edit_text_not_new_message(self):
        http = SequenceHttp(
            [
                HttpRequestError("HTTP 400 from https://api.telegram.org/bot<redacted>/editMessageText: can't parse blocks", status_code=400),
                {"ok": True},
            ]
        )
        doc = RichDocument((heading("任务统计"),))
        keyboard = {"inline_keyboard": [[{"text": "重试", "callback_data": "retry"}]]}
        TelegramClient("secret", http=http).edit_rich_message(1, 17, doc, reply_markup=keyboard)

        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[0][0].endswith("/editMessageText"))
        self.assertTrue(http.calls[1][0].endswith("/editMessageText"))
        rich_payload = http.calls[0][1]["payload"]
        self.assertIn("rich_message", rich_payload)
        self.assertEqual(rich_payload["message_id"], 17)
        fallback_payload = http.calls[1][1]["payload"]
        self.assertEqual(fallback_payload["chat_id"], 1)
        self.assertEqual(fallback_payload["message_id"], 17)
        self.assertEqual(fallback_payload["text"], doc.to_plain())
        self.assertEqual(fallback_payload["reply_markup"], keyboard)
        self.assertNotIn("rich_message", fallback_payload)
        self.assertFalse(any(url.endswith("/sendMessage") for url, _kwargs in http.calls))


if __name__ == "__main__":
    unittest.main()
