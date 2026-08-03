import http.client
import json
import socket
import socketserver
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
            with_logger = app.prepare_log_stream(
                "/api/v1/logs/stream?filter_type=main&lines=1000&logger=task_runner",
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
        self.assertEqual(with_logger[3], LogFilter("main", 1000, "", "task_runner"))
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

    def test_special_character_token_round_trips_through_cookie_and_case_insensitive_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(tmp, token="a/b", hub=LogHub())
            redirect = app.prepare_log_stream("/api/v1/logs/stream?token=a%2Fb", {})
            cookie = redirect[1]["Set-Cookie"].split(";", 1)[0]
            cookie_auth = app.prepare_log_stream("/api/v1/logs/stream", {"cOoKiE": cookie})
            header_auth = app.prepare_log_stream("/api/v1/logs/stream", {"X-WEB-TOKEN": "a/b"})

        self.assertEqual(redirect[0], 303)
        self.assertEqual(cookie_auth[0], 200)
        self.assertEqual(header_auth[0], 200)

    def test_encode_sse_event_is_single_json_data_frame(self):
        frame = encode_sse_event("log", {"text": "line one\nline two"}, event_id=7)
        self.assertEqual(
            frame,
            b'id: 7\nevent: log\ndata: {"text":"line one\\nline two"}\n\n',
        )

    def test_encode_sse_event_keeps_valid_unicode_and_escapes_unpaired_surrogates(self):
        text = "snowman \u2603 " + chr(0xD800)
        frame = encode_sse_event("log", {"text": text})

        self.assertIn("snowman \u2603", frame.decode("utf-8"))
        self.assertIn(b"\\ud800", frame)
        self.assertEqual(json.loads(frame.decode("utf-8").splitlines()[1].removeprefix("data: ")), {"text": text})

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

    def test_sse_snapshot_handles_unpaired_surrogate_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = LogHub()
            text = "snapshot \u2603 " + chr(0xD800)
            hub.publish(1, "INFO", "worker", text)
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            connection = response = None
            try:
                connection, response = self.open_stream(server)
                snapshot = self.read_event(response)

                self.assertEqual(response.status, 200)
                self.assertEqual(snapshot.get("event"), "snapshot")
                self.assertEqual(snapshot.get("data", {}).get("entries", [{}])[0].get("text"), text)
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

    def test_sse_connection_limit_rejects_excess_clients_and_releases_capacity(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.web.SSE_MAX_CLIENTS", 1, create=True), patch(
            "app.web.SSE_HEARTBEAT_SECONDS", 0.05
        ):
            hub = LogHub()
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            first_connection = first_response = None
            second_connection = second_response = None
            third_connection = third_response = None
            try:
                first_connection, first_response = self.open_stream(server)
                self.read_event(first_response)

                second_connection, second_response = self.open_stream(server)
                self.assertEqual(second_response.status, 429)
                second_response.read()
                second_response.close()
                second_connection.close()
                second_response = second_connection = None

                first_response.close()
                first_connection.close()
                first_response = first_connection = None
                deadline = time.monotonic() + 1
                while hub.subscriber_count and time.monotonic() < deadline:
                    time.sleep(0.02)

                third_connection, third_response = self.open_stream(server)
                self.assertEqual(third_response.status, 200)
                self.read_event(third_response)
            finally:
                for response in (first_response, second_response, third_response):
                    if response is not None:
                        response.close()
                for connection in (first_connection, second_connection, third_connection):
                    if connection is not None:
                        connection.close()
                bridge.stop_web_server(server)

    def test_sse_stream_close_failure_still_releases_connection_capacity(self):
        close_attempted = Event()

        class FailingCloseStream:
            snapshot = ()

            def next_event(self, _timeout):
                return LogEvent("gap")

            def close(self):
                close_attempted.set()
                raise RuntimeError("simulated close failure")

        class FailingCloseHub:
            def open_stream(self, _spec, queue_size=256):
                return FailingCloseStream()

        with tempfile.TemporaryDirectory() as tmp, patch("app.web.SSE_MAX_CLIENTS", 1, create=True):
            server = start_web_server(
                TaskStore(Path(tmp) / "tasks.db"),
                "127.0.0.1",
                0,
                log_hub=FailingCloseHub(),
            )
            first_connection = first_response = None
            second_connection = second_response = None
            try:
                first_connection, first_response = self.open_stream(server)
                self.read_event(first_response)
                self.read_event(first_response)
                self.assertTrue(close_attempted.wait(1))

                second_connection, second_response = self.open_stream(server)
                self.assertEqual(second_response.status, 200)
            finally:
                for response in (first_response, second_response):
                    if response is not None:
                        response.close()
                for connection in (first_connection, second_connection):
                    if connection is not None:
                        connection.close()
                bridge.stop_web_server(server)

    def test_sse_write_timeout_releases_nonreading_subscription(self):
        class TrackingHub(LogHub):
            def __init__(self):
                super().__init__()
                self.opened = Event()

            def open_stream(self, spec, queue_size=256):
                stream = super().open_stream(spec, queue_size)
                self.opened.set()
                return stream

        observed_timeouts = []
        write_started = Event()

        def timed_out_write(writer, _data):
            observed_timeouts.append(writer._sock.gettimeout())
            write_started.set()
            raise socket.timeout("simulated slow client")

        with tempfile.TemporaryDirectory() as tmp, patch("app.web.SSE_WRITE_TIMEOUT_SECONDS", 0.05), patch.object(
            socketserver._SocketWriter, "write", new=timed_out_write
        ):
            hub = TrackingHub()
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            client = socket.create_connection(server.server_address, timeout=1)
            try:
                client.sendall(b"GET /api/v1/logs/stream HTTP/1.0\r\nHost: localhost\r\n\r\n")
                self.assertTrue(hub.opened.wait(1))
                self.assertTrue(write_started.wait(1))
                deadline = time.monotonic() + 1
                while hub.subscriber_count and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(observed_timeouts, [0.05])
                self.assertEqual(hub.subscriber_count, 0)
            finally:
                client.close()
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
                self.assertEqual(response.version, 10)
                self.assertIsNone(response.getheader("Content-Length"))
                self.read_event(response)
                gap = self.read_event(response)
                self.assertEqual(gap["event"], "gap")
                self.assertEqual(gap["data"], {"reason": "slow_client", "dropped": 0})
                self.assertTrue(closed.wait(1))
                self.assertEqual(response.fp.readline(), b"")
            finally:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
                bridge.stop_web_server(server)

    def test_log_analysis_endpoint_returns_summary_entries_and_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = LogHub(capacity=100)
            hub.publish(1, "INFO", "worker", "received link")
            hub.publish(2, "ERROR", "task_runner", "Task stage failed: stage_wait_timeout 等待超时")
            hub.publish(3, "WARNING", "runner", "115 风控，暂停重试")
            app = self.make_app(tmp, hub=hub)

            status, headers, body = app.handle_log_analysis(
                "/api/v1/logs/analyze?lines=500&logger=task_runner",
                {},
            )
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertEqual(payload["summary"]["total"], 1)
            self.assertEqual(payload["summary"]["error_count"], 1)
            self.assertEqual(payload["entries"][0]["text"], "Task stage failed: stage_wait_timeout 等待超时")
            self.assertTrue(any(hint["marker"] == "等待超时" for hint in payload["repair_hints"]))

    def test_log_analysis_endpoint_requires_auth_and_valid_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(tmp, token="web-secret", hub=LogHub())

            forbidden = app.handle_log_analysis("/api/v1/logs/analyze", {})
            unknown = app.handle_log_analysis(
                "/api/v1/logs/analyze?extra=value",
                {"Cookie": "cms_web_token=web-secret"},
            )
            bad_lines = app.handle_log_analysis(
                "/api/v1/logs/analyze?lines=abc",
                {"Cookie": "cms_web_token=web-secret"},
            )

            self.assertEqual(forbidden[0], 403)
            self.assertEqual(unknown[0], 400)
            self.assertEqual(bad_lines[0], 400)

    def test_server_shutdown_closes_active_sse_subscription_immediately(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.web.SSE_HEARTBEAT_SECONDS", 30):
            hub = LogHub()
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            connection = response = None
            stopped = False
            try:
                connection, response = self.open_stream(server)
                self.read_event(response)
                self.assertEqual(hub.subscriber_count, 1)

                bridge.stop_web_server(server, join_timeout=1)
                stopped = True

                deadline = time.monotonic() + 1
                while hub.subscriber_count and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(hub.subscriber_count, 0)
            finally:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
                if not stopped:
                    bridge.stop_web_server(server)

    def test_server_shutdown_rejects_sse_subscription_registered_after_cleanup_snapshot(self):
        class DelayedOpenHub(LogHub):
            def __init__(self):
                super().__init__()
                self.entered = Event()
                self.release = Event()
                self.opened = Event()

            def open_stream(self, spec, queue_size=256):
                self.entered.set()
                self.release.wait(2)
                stream = super().open_stream(spec, queue_size=queue_size)
                self.opened.set()
                return stream

        with tempfile.TemporaryDirectory() as tmp, patch("app.web.SSE_HEARTBEAT_SECONDS", 30):
            hub = DelayedOpenHub()
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            client = socket.create_connection(server.server_address, timeout=1)
            stopped = False
            try:
                client.sendall(b"GET /api/v1/logs/stream HTTP/1.0\r\nHost: localhost\r\n\r\n")
                self.assertTrue(hub.entered.wait(1))

                bridge.stop_web_server(server, join_timeout=1)
                stopped = True
                hub.release.set()

                self.assertTrue(hub.opened.wait(1))
                deadline = time.monotonic() + 1
                while hub.subscriber_count and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(hub.subscriber_count, 0)
            finally:
                hub.release.set()
                hub.close_streams()
                client.close()
                if not stopped:
                    bridge.stop_web_server(server)

    def test_malformed_get_target_returns_400_and_server_remains_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = LogHub()
            server = start_web_server(TaskStore(Path(tmp) / "tasks.db"), "127.0.0.1", 0, log_hub=hub)
            connection = response = None
            try:
                for target in (
                    b"http://[",
                    b"http://localhost:bad",
                    b"http:///api/v1/logs/stream",
                    b"http://@/api/v1/logs/stream",
                    b"http://:80/api/v1/logs/stream",
                ):
                    with socket.create_connection(server.server_address, timeout=1) as client:
                        client.sendall(b"GET " + target + b" HTTP/1.0\r\nHost: localhost\r\n\r\n")
                        malformed_response = client.recv(512)
                    self.assertTrue(malformed_response.startswith(b"HTTP/1.0 400"))
                connection = http.client.HTTPConnection(*server.server_address, timeout=1)
                connection.request("GET", "/")
                response = connection.getresponse()

                self.assertEqual(hub.subscriber_count, 0)
                self.assertEqual(response.status, 302)
            finally:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
                bridge.stop_web_server(server)
