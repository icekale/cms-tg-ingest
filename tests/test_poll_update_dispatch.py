"""轮询循环的消息投递策略：失败的消息不许无声消失（0.5.46 线上事故回归）。

事故形态：用户发消息 → handle_update 抛异常 → 老代码已经把 offset 推到这条之后
→ Telegram 认为已确认，永久丢弃 → 用户看到“毫无反应”，日志里也什么都没有。
"""

import unittest

import bridge


class DispatchUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.state = {"update_id": None, "attempts": 0}

    def test_offset_advances_after_each_success(self):
        handled = []

        offset = bridge.dispatch_updates(
            [{"update_id": 7}, {"update_id": 8}],
            lambda update: handled.append(update["update_id"]),
            offset=None,
            failure_state=self.state,
        )

        self.assertEqual(handled, [7, 8])
        self.assertEqual(offset, 9)
        self.assertIsNone(self.state["update_id"])
        self.assertEqual(self.state["attempts"], 0)

    def test_failed_update_keeps_offset_so_telegram_redelivers(self):
        def handler(_update):
            raise RuntimeError("boom")

        with self.assertLogs("cms-tg-ingest", level="ERROR") as captured:
            offset = bridge.dispatch_updates(
                [{"update_id": 7, "message": {"chat": {"id": 464100862}, "text": "/搜索"}}],
                handler,
                offset=None,
                failure_state=self.state,
            )

        self.assertIsNone(offset)  # 关键：没有确认这条 update，下一轮会重投
        self.assertEqual(self.state["update_id"], 7)
        self.assertEqual(self.state["attempts"], 1)
        text = "\n".join(captured.output)
        self.assertIn("Failed to handle update_id=7", text)
        self.assertIn("/搜索", text)  # 日志里能看到用户实际发了什么

    def test_retry_then_give_up_advances_offset_and_preserves_payload(self):
        def handler(_update):
            raise RuntimeError("boom")

        update = {"update_id": 7, "message": {"chat": {"id": 464100862}, "text": "/搜索 沙丘"}}

        with self.assertLogs("cms-tg-ingest", level="ERROR") as captured:
            first = bridge.dispatch_updates([update], handler, offset=None, failure_state=self.state)
            second = bridge.dispatch_updates([update], handler, offset=first, failure_state=self.state)
            third = bridge.dispatch_updates([update], handler, offset=second, failure_state=self.state)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(third, 8)  # 超过重试上限才跳过，队列不会被一条毒消息堵死
        self.assertIsNone(self.state["update_id"])
        self.assertEqual(self.state["attempts"], 0)
        text = "\n".join(captured.output)
        self.assertIn("Giving up on update_id=7", text)
        self.assertIn("Dropped update payload", text)
        self.assertIn("/搜索 沙丘", text)  # payload 留在日志里，可人工补投

    def test_batch_keeps_moving_after_giving_up(self):
        self.state.update({"update_id": 7, "attempts": 2})
        handled = []

        def handler(update):
            if update["update_id"] == 7:
                raise RuntimeError("boom")
            handled.append(update["update_id"])

        with self.assertLogs("cms-tg-ingest", level="ERROR"):
            offset = bridge.dispatch_updates(
                [{"update_id": 7}, {"update_id": 8}],
                handler,
                offset=None,
                failure_state=self.state,
            )

        self.assertEqual(handled, [8])
        self.assertEqual(offset, 9)

    def test_describe_update_names_kind_and_text(self):
        callback = {"update_id": 1, "callback_query": {"data": "hd:pick:1", "message": {"chat": {"id": 42}}}}
        message = {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/搜索"}}

        self.assertIn("callback_query", bridge.describe_update(callback))
        self.assertIn("hd:pick:1", bridge.describe_update(callback))
        self.assertIn("/搜索", bridge.describe_update(message))
        self.assertEqual(bridge.describe_update({"update_id": 3}), "unknown")


class FakeTelegram:
    def __init__(self, transient: bool):
        self.transient = transient

    def _is_transient_get_updates_error(self, _exc):
        return self.transient


class LogPollingErrorTests(unittest.TestCase):
    def test_send_endpoint_failure_is_not_downgraded_to_polling_noise(self):
        """handler 里的发送失败文本与轮询失败一模一样，不能因此被当成无害抖动。"""
        exc = RuntimeError(
            "Cannot reach https://api.telegram.org/bot<redacted>/sendRichMessage: "
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        )

        with self.assertLogs("cms-tg-ingest", level="ERROR") as captured:
            try:
                raise exc
            except RuntimeError as caught:
                bridge.log_polling_error(FakeTelegram(transient=True), caught)

        self.assertTrue(any("Polling loop failed" in line for line in captured.output))

    def test_get_updates_transient_error_still_logged_as_noise(self):
        exc = RuntimeError(
            "Cannot reach https://api.telegram.org/bot<redacted>/getUpdates?timeout=10: "
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        )

        with self.assertLogs("cms-tg-ingest", level="WARNING") as captured:
            bridge.log_polling_error(FakeTelegram(transient=True), exc)

        self.assertTrue(any("polling transient error" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
