import contextlib
import io
import logging
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from app.logging_system import (
    LogFilter,
    LogHub,
    configure_logging,
    parse_log_filter,
    redact_text,
)
from app.web import encode_sse_event


class LoggingSystemTests(unittest.TestCase):
    def test_redact_text_removes_credentials_and_sensitive_url_values(self):
        bot_token = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
        source = (
            "Authorization: Bearer bearer-secret\nCookie: session=cookie-secret\n"
            f"password=share-pass api_key=api-secret token={bot_token} "
            "访问码：1212 https://115cdn.com/s/code?password=abcd&access_token=url-secret&safe=yes"
        )

        redacted = redact_text(source)

        for secret in (
            "bearer-secret",
            "cookie-secret",
            "share-pass",
            "api-secret",
            "1212",
            "abcd",
            "url-secret",
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn("safe=yes", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_text_removes_the_full_multiword_plain_secret_value(self):
        redacted = redact_text("password: correct horse safe=blue battery-staple")

        for secret in ("correct horse", "safe=blue", "battery-staple"):
            self.assertNotIn(secret, redacted)

    def test_redact_text_removes_structured_mapping_credentials_and_receive_codes(self):
        source = '{"password": "json-secret", "token": "json-token"} {\'api_key\': \'python-secret\'} 接收码：1212'

        redacted = redact_text(source)

        for secret in ("json-secret", "json-token", "python-secret", "1212"):
            self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_configure_logging_redacts_structured_credentials_from_every_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = io.StringIO()
            logger = logging.Logger("structured-runtime", logging.INFO)
            logger.propagate = False
            path = Path(tmp) / "app.log"
            runtime = configure_logging("INFO", log_path=path, stream=stream, root_logger=logger)
            logger.info('{"password": "json-secret"} {\'token\': \'python-secret\'} 接收码：1212')
            for handler in logger.handlers:
                handler.flush()

            for output in (
                stream.getvalue(),
                path.read_text(encoding="utf-8"),
                runtime.hub.snapshot(LogFilter("all", 1000, ""))[0].text,
            ):
                for secret in ("json-secret", "python-secret", "1212"):
                    self.assertNotIn(secret, output)
            runtime.close()

    def test_structured_header_credentials_and_numeric_codes_are_redacted_from_every_sink(self):
        source = (
            '{"Authorization":"Bearer bearer-secret","Cookie":"session=cookie-secret",'
            '"receive_code":1212,"access_code":1234,"key":"generic-key-secret"} '
            "{'proxy-authorization': 'Bearer proxy-secret', 'x-api-key': 'api-secret', "
            "'receive_code': 5678, 'access_code': 9876} "
            "{'Cookie': {'session': 'nested-cookie-secret', 'other': 'nested-value-secret'}} "
            "{'Authorization': Bearer bare-bearer-secret} "
            r'{"C\u006fokie":"escaped-cookie-secret","\u0041uthorization":"escaped-auth-secret"}'
        )
        secrets = (
            "bearer-secret",
            "cookie-secret",
            "1212",
            "1234",
            "generic-key-secret",
            "proxy-secret",
            "api-secret",
            "5678",
            "9876",
            "nested-cookie-secret",
            "nested-value-secret",
            "bare-bearer-secret",
            "escaped-cookie-secret",
            "escaped-auth-secret",
        )

        direct_redacted = redact_text(source)
        for secret in secrets:
            self.assertNotIn(secret, direct_redacted)

        with tempfile.TemporaryDirectory() as tmp:
            stream = io.StringIO()
            logger = logging.Logger("structured-headers", logging.INFO)
            logger.propagate = False
            path = Path(tmp) / "app.log"
            runtime = configure_logging("INFO", log_path=path, stream=stream, root_logger=logger)
            logger.info(source)
            for handler in logger.handlers:
                handler.flush()

            hub_text = runtime.hub.snapshot(LogFilter("all", 1000, ""))[0].text
            sse_frame = encode_sse_event("log", {"text": hub_text})
            for output in (stream.getvalue(), path.read_text(encoding="utf-8"), hub_text, sse_frame.decode("utf-8")):
                for secret in secrets:
                    self.assertNotIn(secret, output)
                self.assertIn("[REDACTED]", output)
            runtime.close()

    def test_parse_log_filter_accepts_only_documented_values(self):
        self.assertEqual(parse_log_filter(), LogFilter("main", 1000, ""))
        self.assertEqual(parse_log_filter("ERROR", "2000", "CMS"), LogFilter("ERROR", 2000, "CMS"))
        self.assertEqual(parse_log_filter("all", 5000, "中文"), LogFilter("all", 5000, "中文"))

        for values in (("debug", 1000, ""), ("main", 100, ""), ("main", 1000, "x" * 101)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_log_filter(*values)

    def test_snapshot_filters_level_keyword_and_returns_newest_first(self):
        hub = LogHub(capacity=10)
        hub.publish(1, "DEBUG", "worker", "debug cms")
        hub.publish(2, "INFO", "worker", "CMS submitted")
        hub.publish(3, "WARNING", "runner", "waiting")
        hub.publish(4, "ERROR", "runner", "CMS failed")

        main = hub.snapshot(LogFilter("main", 1000, "cms"))
        errors = hub.snapshot(LogFilter("ERROR", 1000, ""))
        all_rows = hub.snapshot(LogFilter("all", 1000, ""))

        self.assertEqual([entry.id for entry in main], [4, 2])
        self.assertEqual([entry.level for entry in errors], ["ERROR"])
        self.assertEqual([entry.id for entry in all_rows], [4, 3, 2, 1])

    def test_open_stream_has_atomic_snapshot_and_nonblocking_gap(self):
        hub = LogHub(capacity=10)
        hub.publish(1, "INFO", "worker", "first")
        stream = hub.open_stream(LogFilter("all", 1000, ""), queue_size=1)
        self.assertEqual([entry.text for entry in stream.snapshot], ["first"])
        self.assertEqual(hub.subscriber_count, 1)

        hub.publish(2, "INFO", "worker", "second")
        hub.publish(3, "INFO", "worker", "third")

        self.assertEqual(stream.next_event(0).kind, "gap")
        stream.close()
        self.assertEqual(hub.subscriber_count, 0)

    def test_restore_reads_oldest_rotation_first_limits_rows_and_keeps_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cms-tg-ingest.log"
            path.with_name(path.name + ".2").write_text(
                "2026-01-01 00:00:01 INFO old oldest\n", encoding="utf-8"
            )
            path.with_name(path.name + ".1").write_text("damaged history line\n", encoding="utf-8")
            path.write_text(
                "2026-01-01 00:00:02 ERROR current newer\n"
                "2026-01-01 00:00:03 INFO current newest\n",
                encoding="utf-8",
            )
            hub = LogHub(capacity=3)

            hub.restore(path, backup_count=2)

            rows = hub.snapshot(LogFilter("all", 1000, ""))
            self.assertEqual(
                [row.text for row in rows],
                [
                    "2026-01-01 00:00:03 INFO current newest",
                    "2026-01-01 00:00:02 ERROR current newer",
                    "damaged history line",
                ],
            )
            self.assertEqual(rows[-1].logger, "history")

    def test_configure_logging_is_idempotent_and_redacts_all_three_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = io.StringIO()
            unsafe_stream = io.StringIO()
            logger = logging.Logger("isolated-runtime", logging.DEBUG)
            logger.propagate = False
            logger.addHandler(logging.StreamHandler(unsafe_stream))
            path = Path(tmp) / "logs" / "app.log"

            first = configure_logging("DEBUG", log_path=path, stream=stream, root_logger=logger)
            second = configure_logging("INFO", log_path=path, stream=stream, root_logger=logger)
            logger.error("Authorization: Bearer top-secret password=share-secret")
            for handler in logger.handlers:
                handler.flush()

            self.assertIs(first, second)
            self.assertEqual(len(logger.handlers), 3)
            self.assertEqual(
                sum(bool(getattr(handler, "_cms_tg_ingest_logging_handler", False)) for handler in logger.handlers),
                3,
            )
            self.assertEqual(unsafe_stream.getvalue(), "")
            self.assertNotIn("top-secret", stream.getvalue())
            self.assertNotIn("share-secret", path.read_text(encoding="utf-8"))
            self.assertNotIn("top-secret", first.hub.snapshot(LogFilter("all", 1000, ""))[0].text)
            first.close()

    def test_small_rotation_keeps_exact_backup_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = logging.Logger("rotation", logging.INFO)
            logger.propagate = False
            path = Path(tmp) / "app.log"
            runtime = configure_logging(
                "INFO",
                log_path=path,
                stream=io.StringIO(),
                root_logger=logger,
                max_bytes=128,
                backup_count=4,
            )
            for index in range(80):
                logger.info("row-%03d %s", index, "x" * 40)
            runtime.close()

            self.assertTrue(path.is_file())
            self.assertEqual(
                sorted(item.name for item in path.parent.glob("app.log.*")),
                ["app.log.1", "app.log.2", "app.log.3", "app.log.4"],
            )

    def test_configure_logging_prunes_stale_numeric_backups_beyond_configured_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            for index in range(1, 7):
                path.with_name(f"{path.name}.{index}").write_text(str(index), encoding="utf-8")
            logger = logging.Logger("stale-rotation", logging.INFO)
            logger.propagate = False

            runtime = configure_logging("INFO", log_path=path, stream=io.StringIO(), root_logger=logger)

            self.assertEqual(
                sorted(item.name for item in path.parent.glob("app.log.*")),
                ["app.log.1", "app.log.2", "app.log.3", "app.log.4"],
            )
            runtime.close()

    def test_stale_backup_pruning_failure_does_not_block_logging_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.with_name(f"{path.name}.5").write_text("stale", encoding="utf-8")
            path.with_name(f"{path.name}.6").mkdir()
            stream = io.StringIO()
            logger = logging.Logger("stale-pruning-failure", logging.INFO)
            logger.propagate = False

            runtime = configure_logging("INFO", log_path=path, stream=stream, root_logger=logger)
            logger.info("logging remains available")

            self.assertFalse(path.with_name(f"{path.name}.5").exists())
            self.assertTrue(path.with_name(f"{path.name}.6").is_dir())
            self.assertIn("logging remains available", stream.getvalue())
            runtime.close()

    def test_file_handler_creation_failure_keeps_stdout_and_hub_alive(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch(
            "app.logging_system.SafeRotatingFileHandler", side_effect=OSError("read only")
        ):
            stream = io.StringIO()
            logger = logging.Logger("file-failure", logging.INFO)
            logger.propagate = False
            runtime = configure_logging(
                "INFO", log_path=Path(tmp) / "blocked" / "app.log", stream=stream, root_logger=logger
            )

            logger.info("runner remains alive")

            self.assertIn("runner remains alive", stream.getvalue())
            self.assertIn(
                "runner remains alive",
                runtime.hub.snapshot(LogFilter("main", 1000, ""))[0].text,
            )
            self.assertTrue(runtime.file_error)
            runtime.close()

    def test_rollover_failure_disables_only_file_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream = io.StringIO()
            logger = logging.Logger("rollover-failure", logging.INFO)
            logger.propagate = False
            runtime = configure_logging(
                "INFO",
                log_path=Path(tmp) / "app.log",
                stream=stream,
                root_logger=logger,
                max_bytes=1,
                backup_count=4,
            )
            with unittest.mock.patch.object(runtime.file_handler, "doRollover", side_effect=OSError("blocked")):
                logger.info("survives rollover")

            logger.info("still reaches memory")

            self.assertTrue(runtime.file_handler._logging_disabled)
            self.assertIn("still reaches memory", stream.getvalue())
            self.assertEqual(
                runtime.hub.snapshot(LogFilter("main", 1000, ""))[0].text.endswith("still reaches memory"),
                True,
            )
            runtime.close()

    def test_broken_stdout_never_leaks_log_records_to_stderr_and_keeps_other_sinks_alive(self):
        class BrokenStream:
            def write(self, _text):
                raise OSError("stdout unavailable")

            def flush(self):
                raise OSError("stdout unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            logger = logging.Logger("broken-stdout", logging.INFO)
            logger.propagate = False
            path = Path(tmp) / "app.log"
            runtime = configure_logging("INFO", log_path=path, stream=BrokenStream(), root_logger=logger)
            previous_raise_exceptions = logging.raiseExceptions
            logging.raiseExceptions = True
            try:
                with contextlib.redirect_stderr(stderr), unittest.mock.patch.object(sys, "__stderr__", stderr):
                    logger.info("Authorization: Bearer %s", "stdout-leak-secret")
            finally:
                logging.raiseExceptions = previous_raise_exceptions

            self.assertNotIn("stdout-leak-secret", stderr.getvalue())
            self.assertNotIn("stdout-leak-secret", path.read_text(encoding="utf-8"))
            self.assertNotIn(
                "stdout-leak-secret",
                runtime.hub.snapshot(LogFilter("all", 1000, ""))[0].text,
            )
            runtime.close()
