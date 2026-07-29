import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import bridge
from app.logging_system import LogEvent, LogFilter, LogHub
from app.task_store import TaskStore
from app.web import WebApp, encode_sse_event, start_web_server


class WebLogTests(unittest.TestCase):
    def make_app(self, tmp: str, *, token: str = "", hub=None) -> WebApp:
        return WebApp(TaskStore(Path(tmp) / "tasks.db"), web_token=token, log_hub=hub)

    def test_prepare_log_stream_validates_query_and_preserves_cookie_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(tmp, token="web-secret", hub=LogHub())
            forbidden = app.prepare_log_stream("/api/v1/logs/stream", {})
            accepted = app.prepare_log_stream(
                "/api/v1/logs/stream?filter_type=ERROR&lines=2000&keyword=CMS",
                {"Cookie": "cms_web_token=web-secret"},
            )
            invalid_type = app.prepare_log_stream(
                "/api/v1/logs/stream?filter_type=debug", {"Cookie": "cms_web_token=web-secret"}
            )
            invalid_lines = app.prepare_log_stream(
                "/api/v1/logs/stream?lines=999", {"Cookie": "cms_web_token=web-secret"}
            )
            long_keyword = app.prepare_log_stream(
                f"/api/v1/logs/stream?keyword={'x' * 101}", {"Cookie": "cms_web_token=web-secret"}
            )
            duplicate = app.prepare_log_stream(
                "/api/v1/logs/stream?lines=1000&lines=2000", {"Cookie": "cms_web_token=web-secret"}
            )
            unknown = app.prepare_log_stream(
                "/api/v1/logs/stream?extra=value", {"Cookie": "cms_web_token=web-secret"}
            )

        self.assertEqual(forbidden[0], 403)
        self.assertEqual(accepted[0], 200)
        self.assertEqual(accepted[3], LogFilter("ERROR", 2000, "CMS"))
        self.assertEqual(
            [invalid_type[0], invalid_lines[0], long_keyword[0], duplicate[0], unknown[0]],
            [400, 400, 400, 400, 400],
        )

    def test_query_token_redirect_sets_cookie_and_removes_token_from_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(tmp, token="web-secret", hub=LogHub())
            status, headers, body, spec = app.prepare_log_stream(
                "/api/v1/logs/stream?filter_type=main&token=web-secret&keyword=CMS", {}
            )

        self.assertEqual(status, 303)
        self.assertIn("cms_web_token=", headers["Set-Cookie"])
        self.assertEqual(headers["Location"], "/api/v1/logs/stream?filter_type=main&keyword=CMS")
        self.assertNotIn("token", headers["Location"])
        self.assertEqual(body, b"")
        self.assertIsNone(spec)

    def test_encode_sse_event_is_single_json_data_frame(self):
        frame = encode_sse_event("log", {"text": "line one\nline two"}, event_id=7)
        self.assertEqual(
            frame,
            b'id: 7\nevent: log\ndata: {"text":"line one\\nline two"}\n\n',
        )

    def open_stream(self, server, path="/api/v1/logs/stream", headers=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=1)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return connection, response

    def read_event(self, response):
        fields = {}
        while True:
            line = response.fp.readline().decode("utf-8")
            if line in {"", "\n", "\r\n"}:
                break
            name, value = line.rstrip("\r\n").split(":", 1)
            fields[name] = value.lstrip()
        if "data" in fields:
            fields["data"] = json.loads(fields["data"])
        return fields

    def test_sse_sends_newest_first_snapshot_then_live_log_with_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = LogHub()
            hub.publish(1, "INFO", "worker", "older")
            hub.publish(2, "ERROR", "worker", "newer")
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            connection = response = None
            try:
                connection, response = self.open_stream(server)
                snapshot = self.read_event(response)
                hub.publish(3, "INFO", "runner", "live")
                live = self.read_event(response)

                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
                self.assertEqual(snapshot["event"], "snapshot")
                self.assertEqual([row["text"] for row in snapshot["data"]["entries"]], ["newer", "older"])
                self.assertEqual(live["event"], "log")
                self.assertEqual(live["id"], str(live["data"]["id"]))
                self.assertEqual(live["data"]["text"], "live")
            finally:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
                bridge.stop_web_server(server)

    def test_sse_heartbeat_and_disconnect_release_subscription(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.web.SSE_HEARTBEAT_SECONDS", 0.05):
            hub = LogHub()
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            connection = response = None
            try:
                connection, response = self.open_stream(server)
                self.read_event(response)
                heartbeat = self.read_event(response)
                self.assertEqual(heartbeat["event"], "heartbeat")
                self.assertEqual(hub.subscriber_count, 1)
                response.close()
                connection.close()
                deadline = time.monotonic() + 1
                while hub.subscriber_count and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(hub.subscriber_count, 0)
            finally:
                bridge.stop_web_server(server)

    def test_sse_gap_is_sent_and_stream_is_closed(self):
        closed = Event()

        class FakeStream:
            snapshot = ()

            def next_event(self, _timeout):
                return LogEvent("gap")

            def close(self):
                closed.set()

        class FakeHub:
            def open_stream(self, _spec, queue_size=256):
                self.queue_size = queue_size
                return FakeStream()

        with tempfile.TemporaryDirectory() as tmp:
            hub = FakeHub()
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            connection = response = None
            try:
                connection, response = self.open_stream(server)
                self.read_event(response)
                gap = self.read_event(response)
                self.assertEqual(gap["event"], "gap")
                self.assertEqual(gap["data"], {"reason": "slow_client"})
                self.assertTrue(closed.wait(1))
            finally:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
                bridge.stop_web_server(server)
