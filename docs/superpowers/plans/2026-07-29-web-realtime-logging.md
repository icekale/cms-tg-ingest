# Web Realtime Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, restart-aware realtime log page to the Vue admin so operators can inspect this application's recent and live logs without opening `docker logs`.

**Architecture:** A new standard-library logging module owns redaction, stdout output, a bounded in-memory `LogHub`, and a rotating local file. `bridge.py` injects that hub into the existing `ThreadingHTTPServer`; a dedicated SSE branch streams snapshots and bounded per-client updates without changing normal byte-response routes, while a small Vue EventSource controller drives `/app/logs`.

**Tech Stack:** Python 3.12+ standard library (`logging`, `RotatingFileHandler`, `queue`, `ThreadingHTTPServer`), `unittest`, Vue 3, Naive UI, Vite, Node test runner.

## Global Constraints

- Start implementation from current `main`; it must contain design commit `f1eafc8` and this implementation plan.
- Use the isolated worktree `/Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/web-realtime-logging` during implementation.
- Add no Python or frontend dependency and do not introduce WebSocket, a logging database table, or TaskStore writes.
- Keep `LOG_LEVEL` behavior unchanged; default to `INFO` and do not add business polling or external calls.
- Write logs to Docker stdout, an in-memory ring of 5000 entries, and `/data/logs/cms-tg-ingest.log`.
- Rotate at exactly `20 * 1024 * 1024` bytes and retain exactly 4 backups, for about 100 MiB including the current file.
- Recover at most 5000 physical history lines from `cms-tg-ingest.log.4` through the current file; malformed lines become ordinary history entries and cannot abort startup.
- Redact Bot tokens, cookies, passwords, 115 receive/access codes, API keys, authorization values, and sensitive URL query parameters before stdout, file, or SSE output.
- A file-open or rollover failure, slow SSE client, browser disconnect, or malformed history line must never raise into TaskRunner, Telegram, CMS, 115, or Emby work.
- SSE uses `GET /api/v1/logs/stream`, existing Web Token/Cookie authorization, bounded subscriber queues, and no token in the EventSource URL.
- Accept only `filter_type=main|ERROR|all`, `lines=1000|2000|5000`, and a `keyword` of at most 100 Unicode characters; defaults are `main`, `1000`, and empty.
- Emit only `snapshot`, `log`, `heartbeat`, and `gap` SSE events. A live `log` event carries its entry ID; a `gap` closes the stream so the client obtains a fresh snapshot.
- Keep newest entries first in the browser, preserve the viewport while the user reads older entries, and make “清空” browser-local only.
- Do not add log download, disk deletion, Telegram forwarding, per-task log indexing, or a new `/data` volume.
- Run Python tests before `npm ci`, because repository secret-hygiene tests scan the working tree.

---

### Task 1: Build The Redacted Logging Runtime And Bounded LogHub

**Files:**
- Create: `app/logging_system.py`
- Create: `tests/test_logging_system.py`

**Interfaces:**
- Produces: `DEFAULT_LOG_PATH = Path("/data/logs/cms-tg-ingest.log")`.
- Produces: `LOG_HISTORY_LIMIT = 5000`, `LOG_MAX_BYTES = 20 * 1024 * 1024`, and `LOG_BACKUP_COUNT = 4`.
- Produces: `LogEntry(id: int, created_at: float, timestamp: str, level: str, logger: str, text: str)` and `LogEntry.payload() -> dict[str, object]`.
- Produces: `LogFilter(filter_type: str, lines: int, keyword: str)` and `parse_log_filter(filter_type: object = "main", lines: object = 1000, keyword: object = "") -> LogFilter`.
- Produces: `LogEvent(kind: Literal["log", "gap"], entry: LogEntry | None = None)`.
- Produces: `LogStream.snapshot: tuple[LogEntry, ...]`, `LogStream.next_event(timeout: float) -> LogEvent | None`, and `LogStream.close() -> None`.
- Produces: `LogHub(capacity: int = LOG_HISTORY_LIMIT)`, `LogHub.restore(log_path: Path, backup_count: int = LOG_BACKUP_COUNT) -> None`, `LogHub.publish(...) -> LogEntry`, `LogHub.snapshot(spec: LogFilter) -> tuple[LogEntry, ...]`, and `LogHub.open_stream(spec: LogFilter, queue_size: int = 256) -> LogStream`.
- Produces: `redact_text(value: object) -> str`, `RedactingFormatter`, `SafeRotatingFileHandler`, `LoggingRuntime`, and `configure_logging(...) -> LoggingRuntime`.

- [ ] **Step 1: Create the isolated implementation worktree**

Use the `superpowers:using-git-worktrees` skill, then verify:

```bash
git -C /Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/web-realtime-logging status --short --branch
git -C /Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/web-realtime-logging merge-base --is-ancestor f1eafc8 HEAD
```

Expected: a clean feature worktree and a zero exit status from `merge-base`.

- [ ] **Step 2: Write failing redaction and filter tests**

Create `tests/test_logging_system.py` with imports for `io`, `logging`, `tempfile`, `unittest`, `Path`, and the new logging interfaces. Add these tests first:

```python
class LoggingSystemTests(unittest.TestCase):
    def test_redact_text_removes_credentials_and_sensitive_url_values(self):
        bot_token = "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
        source = (
            "Authorization: Bearer bearer-secret\nCookie: session=cookie-secret\n"
            f"password=share-pass api_key=api-secret token={bot_token} "
            "访问码：1212 https://115cdn.com/s/code?password=abcd&access_token=url-secret&safe=yes"
        )

        redacted = redact_text(source)

        for secret in ("bearer-secret", "cookie-secret", "share-pass", "api-secret", "1212", "abcd", "url-secret"):
            self.assertNotIn(secret, redacted)
        self.assertIn("safe=yes", redacted)
        self.assertIn("[REDACTED]", redacted)

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
```

- [ ] **Step 3: Run the focused tests and verify RED**

```bash
python3 -m unittest -v \
  tests.test_logging_system.LoggingSystemTests.test_redact_text_removes_credentials_and_sensitive_url_values \
  tests.test_logging_system.LoggingSystemTests.test_parse_log_filter_accepts_only_documented_values \
  tests.test_logging_system.LoggingSystemTests.test_snapshot_filters_level_keyword_and_returns_newest_first
```

Expected: import failure for missing `app.logging_system`, not a fixture or syntax failure.

- [ ] **Step 4: Add immutable entries, strict filters, and centralized redaction**

Start `app/logging_system.py` with the standard-library imports and these constants/data contracts:

```python
from __future__ import annotations

import logging
import logging.handlers
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TextIO


DEFAULT_LOG_PATH = Path("/data/logs/cms-tg-ingest.log")
LOG_HISTORY_LIMIT = 5000
LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_BACKUP_COUNT = 4
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_FILTER_LEVELS = {"main": logging.INFO, "ERROR": logging.ERROR, "all": logging.DEBUG}
_LEVEL_VALUES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_ALLOWED_LINE_LIMITS = frozenset({1000, 2000, 5000})
_HANDLER_MARKER = "_cms_tg_ingest_logging_handler"


@dataclass(frozen=True)
class LogEntry:
    id: int
    created_at: float
    timestamp: str
    level: str
    logger: str
    text: str

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "text": self.text,
        }


@dataclass(frozen=True)
class LogFilter:
    filter_type: str
    lines: int
    keyword: str

    def matches(self, entry: LogEntry) -> bool:
        if _LEVEL_VALUES.get(entry.level, logging.INFO) < _FILTER_LEVELS[self.filter_type]:
            return False
        return not self.keyword or self.keyword.casefold() in entry.text.casefold()


@dataclass(frozen=True)
class LogEvent:
    kind: Literal["log", "gap"]
    entry: LogEntry | None = None


def parse_log_filter(filter_type: object = "main", lines: object = 1000, keyword: object = "") -> LogFilter:
    normalized_type = str(filter_type or "main")
    if normalized_type not in _FILTER_LEVELS:
        raise ValueError("filter_type must be main, ERROR, or all")
    try:
        normalized_lines = int(lines)
    except (TypeError, ValueError) as exc:
        raise ValueError("lines must be 1000, 2000, or 5000") from exc
    if normalized_lines not in _ALLOWED_LINE_LIMITS:
        raise ValueError("lines must be 1000, 2000, or 5000")
    normalized_keyword = str(keyword or "")
    if len(normalized_keyword) > 100:
        raise ValueError("keyword must be at most 100 characters")
    return LogFilter(normalized_type, normalized_lines, normalized_keyword)
```

Implement `redact_text` as a deterministic sequence over the formatted string. Use case-insensitive patterns for:

```python
_URL_SECRET_RE = re.compile(
    r"([?&](?:password|passwd|pwd|receive_code|access_code|token|access_token|refresh_token|api_key|apikey|key|secret)=)[^&#\s]+",
    re.IGNORECASE,
)
_HEADER_SECRET_RE = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)\b\s*[:=]\s*([^\r\n]+)",
    re.IGNORECASE,
)
_VALUE_SECRET_RE = re.compile(
    r"\b(password|passwd|pwd|receive_code|access_code|token|api_key|apikey|access_token|refresh_token|secret)\b\s*[:=]\s*([^\s,;&]+)",
    re.IGNORECASE,
)
_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_CHINESE_CODE_RE = re.compile(r"(访问码\s*[：:]\s*)[^\s,，;；]+")
```

Replace only captured values with `[REDACTED]`, preserve header/key names for diagnosis, and run `_URL_SECRET_RE` before the generic key/value pattern. Ensure `redact_text` accepts non-string objects by calling `str(value)` once.

- [ ] **Step 5: Implement LogHub, atomic stream opening, and slow-client gaps**

Use one `threading.RLock` around the ring, ID counter, and subscriber set. `open_stream` must snapshot and register the subscriber under the same lock so a record cannot fall between the initial snapshot and live queue.

```python
class LogStream:
    def __init__(self, hub: "LogHub", spec: LogFilter, snapshot: tuple[LogEntry, ...], queue_size: int):
        self.snapshot = snapshot
        self._hub = hub
        self._spec = spec
        self._queue: queue.Queue[LogEvent] = queue.Queue(maxsize=max(1, queue_size))
        self._closed = False

    def _offer(self, entry: LogEntry) -> None:
        if self._closed or not self._spec.matches(entry):
            return
        try:
            self._queue.put_nowait(LogEvent("log", entry))
        except queue.Full:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queue.put_nowait(LogEvent("gap"))

    def next_event(self, timeout: float) -> LogEvent | None:
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._hub._unsubscribe(self)


class LogHub:
    def __init__(self, capacity: int = LOG_HISTORY_LIMIT):
        self._entries: deque[LogEntry] = deque(maxlen=max(1, capacity))
        self._next_id = 1
        self._streams: set[LogStream] = set()
        self._lock = threading.RLock()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._streams)

    def publish(self, created_at: float, level: str, logger: str, text: str) -> LogEntry:
        with self._lock:
            entry = LogEntry(
                id=self._next_id,
                created_at=float(created_at),
                timestamp=datetime.fromtimestamp(created_at).astimezone().isoformat(timespec="milliseconds"),
                level=str(level or "INFO").upper(),
                logger=str(logger or "root"),
                text=redact_text(text),
            )
            self._next_id += 1
            self._entries.append(entry)
            for stream in tuple(self._streams):
                stream._offer(entry)
            return entry

    def snapshot(self, spec: LogFilter) -> tuple[LogEntry, ...]:
        with self._lock:
            matches = [entry for entry in reversed(self._entries) if spec.matches(entry)]
            return tuple(matches[: spec.lines])

    def open_stream(self, spec: LogFilter, queue_size: int = 256) -> LogStream:
        with self._lock:
            stream = LogStream(self, spec, self.snapshot(spec), queue_size)
            self._streams.add(stream)
            return stream

    def _unsubscribe(self, stream: LogStream) -> None:
        with self._lock:
            self._streams.discard(stream)
```

The queue replacement on overflow is intentionally non-blocking: discard queued live records, enqueue one `gap`, and let the HTTP layer close that stream after sending the event.

- [ ] **Step 6: Add failing stream, recovery, and runtime tests**

Append focused tests:

```python
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
        path.with_name(path.name + ".2").write_text("2026-01-01 00:00:01 INFO old oldest\n", encoding="utf-8")
        path.with_name(path.name + ".1").write_text("damaged history line\n", encoding="utf-8")
        path.write_text(
            "2026-01-01 00:00:02 ERROR current newer\n2026-01-01 00:00:03 INFO current newest\n",
            encoding="utf-8",
        )
        hub = LogHub(capacity=3)

        hub.restore(path, backup_count=2)

        rows = hub.snapshot(LogFilter("all", 1000, ""))
        self.assertEqual([row.text for row in rows], [
            "2026-01-01 00:00:03 INFO current newest",
            "2026-01-01 00:00:02 ERROR current newer",
            "damaged history line",
        ])
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
        self.assertEqual(sum(bool(getattr(handler, "_cms_tg_ingest_logging_handler", False)) for handler in logger.handlers), 3)
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
        self.assertEqual(sorted(item.name for item in path.parent.glob("app.log.*")), [
            "app.log.1", "app.log.2", "app.log.3", "app.log.4",
        ])

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
        self.assertIn("runner remains alive", runtime.hub.snapshot(LogFilter("main", 1000, ""))[0].text)
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
        self.assertEqual(runtime.hub.snapshot(LogFilter("main", 1000, ""))[0].text.endswith("still reaches memory"), True)
        runtime.close()
```

Import `unittest.mock` explicitly. Keep the test logger isolated so configuring it cannot disturb the suite's root logger.

- [ ] **Step 7: Implement safe history recovery and the three-output runtime**

Use this physical-line parser and inspect the current file before only the necessary older rotations:

```python
_HISTORY_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) (?P<logger>\S+) (?P<text>.*)$"
)

def _history_paths_newest_first(log_path: Path, backup_count: int) -> tuple[Path, ...]:
    rotated = tuple(log_path.with_name(f"{log_path.name}.{index}") for index in range(1, backup_count + 1))
    return (log_path, *rotated)
```

`LogHub.restore` starts with `remaining = self._entries.maxlen`, visits `_history_paths_newest_first`, and reads each existing file with `deque(handle, maxlen=remaining)` and `errors="replace"`. Append each file's retained block to a list, subtract its row count, and stop opening older rotations as soon as `remaining == 0`. Reverse the blocks, but not the lines inside each block, before calling `publish`; this restores chronological order without scanning rotations that cannot contribute to the last 5000 rows. Parsed rows use their level/logger and `datetime.strptime(timestamp, LOG_DATE_FORMAT).astimezone().timestamp()`; unmatched or date-invalid rows use level `INFO`, logger `history`, `time.time()`, and the complete stripped line. Catch `OSError`, `UnicodeError`, and malformed rows locally; do not log through the root logger from this path.

Add the output classes:

```python
class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class LogHubHandler(logging.Handler):
    def __init__(self, hub: LogHub):
        super().__init__()
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.hub.publish(record.created, record.levelname, record.name, self.format(record))
        except Exception:
            return


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, *args, **kwargs):
        self._logging_disabled = False
        self._failure_reported = False
        super().__init__(*args, **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        if not self._logging_disabled:
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self._logging_disabled = True
        if not self._failure_reported:
            self._failure_reported = True
            try:
                sys.__stderr__.write("cms-tg-ingest: file logging disabled after write failure\n")
            except Exception:
                pass
```

Define `LoggingRuntime` with `hub`, `logger`, `handlers`, `file_handler`, and `file_error`. Its `close()` removes only marked handlers from that logger, flushes/closes them with local exception handling, and is idempotent.

Implement this exact production/test seam:

```python
def configure_logging(
    level: str | int = "INFO",
    *,
    log_path: str | Path = DEFAULT_LOG_PATH,
    stream: TextIO | None = None,
    root_logger: logging.Logger | None = None,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    history_limit: int = LOG_HISTORY_LIMIT,
) -> LoggingRuntime:
```

Under a module-level lock, return an existing `LoggingRuntime` found on any marked handler and update only the logger level. Otherwise:

1. Build and restore `LogHub` before opening the rotating file.
2. Remove and close all pre-existing handlers on the target logger so an inherited unredacted output cannot duplicate or leak the same record.
3. Create a `StreamHandler(stream if stream is not None else sys.stdout)` and `LogHubHandler`, both with `RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT)`.
4. Attempt `log_path.parent.mkdir(parents=True, exist_ok=True)` and `SafeRotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")` with the same formatter.
5. Catch file setup failures, set a generic `file_error`, and write one credential-free line to `sys.__stderr__`; do not call `logging` recursively.
6. Construct `LoggingRuntime`, set `_cms_tg_ingest_logging_handler = True` and `_cms_logging_runtime = runtime` on each successful handler, attach them, set the requested logger level, and return the runtime. Idempotent lookup reads `_cms_logging_runtime` from a marked handler.

- [ ] **Step 8: Run the complete logging module tests and verify GREEN**

```bash
python3 -W error::ResourceWarning -m unittest -v tests.test_logging_system
```

Expected: all logging tests pass, exactly four rotated backups remain in the small-limit test, and no resource warning is emitted.

- [ ] **Step 9: Review and commit the logging core**

```bash
git diff --check
git diff -- app/logging_system.py tests/test_logging_system.py
git add app/logging_system.py tests/test_logging_system.py
git commit -m "feat: add redacted logging runtime"
```

---

### Task 2: Wire One Logging Runtime Into Bridge And Web Startup

**Files:**
- Modify: `bridge.py:1-190`
- Modify: `bridge.py:452-573`
- Modify: `bridge.py:4337-4699`
- Test: `tests/test_bridge_v02_integration.py`
- Test: `tests/test_bridge_task_engine.py`

**Interfaces:**
- Consumes: `configure_logging(...) -> LoggingRuntime` and `LogHub` from Task 1.
- Produces: `run_forever(config: Config, stop_event: threading.Event | None = None, *, log_hub: LogHub | None = None) -> None`.
- Produces: optional `log_hub` keyword propagation through `call_maybe_start_web_server`, `maybe_start_web_server`, and `start_web_server`.
- Preserves: legacy two-argument/mocked Web starter compatibility through the existing signature inspection.

- [ ] **Step 1: Write failing bridge injection and startup tests**

Add to `BridgeV02IntegrationTests`:

```python
def test_maybe_start_web_server_passes_log_hub_only_to_supporting_starter(self):
    with tempfile.TemporaryDirectory() as tmp:
        env = self.required_env(tmp)
        with patch.dict(os.environ, env, clear=True):
            config = bridge.Config.from_env()
            store = bridge.create_task_store(config)
            hub = object()
            calls = []

            def modern_starter(task_store, host, port, **kwargs):
                calls.append((task_store, host, port, kwargs))
                return "modern"

            result = bridge.maybe_start_web_server(config, store, starter=modern_starter, log_hub=hub)

        self.assertEqual(result, "modern")
        self.assertIs(calls[0][3]["log_hub"], hub)

def test_maybe_start_web_server_does_not_break_legacy_starter_without_log_hub(self):
    with tempfile.TemporaryDirectory() as tmp:
        env = self.required_env(tmp)
        with patch.dict(os.environ, env, clear=True):
            config = bridge.Config.from_env()
            store = bridge.create_task_store(config)

            def legacy_starter(task_store, host, port, web_token="", task_engine_enabled=None):
                return (task_store, host, port, web_token, task_engine_enabled)

            result = bridge.maybe_start_web_server(config, store, starter=legacy_starter, log_hub=object())

        self.assertEqual(result[1:], ("127.0.0.1", 8787, "secret", True))

def test_main_configures_logging_once_and_injects_hub_into_runtime(self):
    runtime = SimpleNamespace(hub=object())
    config = SimpleNamespace()
    with patch.object(bridge, "configure_logging", return_value=runtime) as configure, patch.object(
        bridge.Config, "from_env", return_value=config
    ), patch.object(bridge, "run_forever") as run, patch.object(bridge.signal, "signal"):
        exit_code = bridge.main()

    self.assertEqual(exit_code, 0)
    configure.assert_called_once_with(os.environ.get("LOG_LEVEL", "INFO"))
    self.assertIs(run.call_args.kwargs["log_hub"], runtime.hub)
```

Add `SimpleNamespace` to the existing test imports if it is not already present.

In the runtime wiring test in `tests/test_bridge_task_engine.py`, extend `capture_web_server` to record `kwargs.get("log_hub")`, call `run_forever(..., log_hub=hub)`, and assert identity:

```python
def capture_web_server(*_args, **kwargs):
    captured["web_background_jobs"] = kwargs["background_jobs"]
    captured["web_log_hub"] = kwargs.get("log_hub")
    return None

hub = object()
bridge.run_forever(config, stop_event=stop_event, log_hub=hub)
self.assertIs(captured["web_log_hub"], hub)
```

- [ ] **Step 2: Run the new bridge tests and verify RED**

```bash
python3 -m unittest -v \
  tests.test_bridge_v02_integration.BridgeV02IntegrationTests.test_maybe_start_web_server_passes_log_hub_only_to_supporting_starter \
  tests.test_bridge_v02_integration.BridgeV02IntegrationTests.test_maybe_start_web_server_does_not_break_legacy_starter_without_log_hub \
  tests.test_bridge_v02_integration.BridgeV02IntegrationTests.test_main_configures_logging_once_and_injects_hub_into_runtime \
  tests.test_bridge_task_engine.DirectTaskEngineBridgeTests.test_direct_task_engine_run_forever_constructs_runtime_dependencies_once
```

Expected: failures are limited to the missing `log_hub` signatures and `configure_logging` call.

- [ ] **Step 3: Replace `basicConfig` and thread the hub through compatible seams**

Import:

```python
from app.logging_system import LogHub, configure_logging
```

Change the runtime signature and Web call:

```python
def run_forever(
    config: Config,
    stop_event: threading.Event | None = None,
    *,
    log_hub: LogHub | None = None,
) -> None:
```

```python
web_server = call_maybe_start_web_server(
    config,
    task_store,
    submission_store=store,
    quality_automation=quality_automation,
    hdhive_service=hdhive_subscription_service,
    hdhive_scheduler=hdhive_subscription_scheduler,
    frontend_dist_path=getattr(config, "frontend_dist_path", "/app/frontend/dist"),
    background_jobs=background_jobs,
    log_hub=log_hub,
)
```

Add `log_hub: LogHub | None = None` to the keyword-only portion of `maybe_start_web_server`. Inspect `starter` exactly as is already done for `self_share_config`, `frontend_dist_path`, `max_retries`, and `background_jobs`; add `kwargs["log_hub"] = log_hub` only when the starter accepts `log_hub` or `**kwargs`, and remove it otherwise.

Add optional `log_hub` to `call_maybe_start_web_server`, detect whether the currently patched `maybe_start_web_server` accepts it, and include it only when supported. Preserve the existing two-positional-argument fallback.

Replace `main()` startup with:

```python
def main() -> int:
    logging_runtime = configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    stop_event = threading.Event()

    def request_stop(signum, _frame):
        LOG.info("Received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run_forever(Config.from_env(), stop_event=stop_event, log_hub=logging_runtime.hub)
    return 0
```

Do not close the logging runtime before process exit: shutdown logs from `run_forever` and Python's normal logging shutdown still need all outputs.

- [ ] **Step 4: Run bridge tests and verify GREEN**

Run the Step 2 command, then:

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_bridge_v02_integration \
  tests.test_bridge_task_engine \
  tests.test_hdhive_web
```

Expected: startup compatibility, runtime shutdown, and HDHive Web injection tests all pass.

- [ ] **Step 5: Review and commit runtime wiring**

```bash
git diff --check
git diff -- bridge.py tests/test_bridge_v02_integration.py tests/test_bridge_task_engine.py
git add bridge.py tests/test_bridge_v02_integration.py tests/test_bridge_task_engine.py
git commit -m "feat: wire logging hub into web runtime"
```

---

### Task 3: Add Authenticated SSE Snapshot And Live Streaming

**Files:**
- Modify: `app/web.py:1-45`
- Modify: `app/web.py:1310-1410`
- Modify: `app/web.py:1941-2055`
- Create: `tests/test_web_logs.py`

**Interfaces:**
- Consumes: `LogHub`, `LogFilter`, `LogEvent`, and `parse_log_filter` from Task 1.
- Produces: `SSE_HEARTBEAT_SECONDS = 15.0` and `SSE_CLIENT_QUEUE_SIZE = 256`.
- Produces: `encode_sse_event(event: str, payload: dict[str, object], event_id: int | None = None) -> bytes`.
- Produces: `WebApp.prepare_log_stream(path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes, LogFilter | None]`.
- Produces: `start_web_server(..., log_hub: LogHub | None = None) -> ThreadingHTTPServer`.
- Preserves: `WebApp.handle_request(...) -> tuple[int, dict[str, str], bytes]` for every existing HTML, asset, and JSON route.

- [ ] **Step 1: Write failing parameter, authentication, and event encoding tests**

Create `tests/test_web_logs.py`:

```python
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
```

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
python3 -m unittest -v \
  tests.test_web_logs.WebLogTests.test_prepare_log_stream_validates_query_and_preserves_cookie_auth \
  tests.test_web_logs.WebLogTests.test_query_token_redirect_sets_cookie_and_removes_token_from_location \
  tests.test_web_logs.WebLogTests.test_encode_sse_event_is_single_json_data_frame
```

Expected: failures identify missing `log_hub`, `prepare_log_stream`, and `encode_sse_event` interfaces.

- [ ] **Step 3: Implement strict stream preparation without changing byte routes**

Import `urlencode` and the logging interfaces. Add constants and encoder:

```python
SSE_HEARTBEAT_SECONDS = 15.0
SSE_CLIENT_QUEUE_SIZE = 256
_LOG_STREAM_PATH = "/api/v1/logs/stream"
_LOG_QUERY_KEYS = frozenset({"filter_type", "lines", "keyword", "token"})


def encode_sse_event(event: str, payload: dict[str, object], event_id: int | None = None) -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n\n").encode("utf-8")
```

Add `log_hub: LogHub | None = None` to `WebApp.__init__` and store it. Implement `prepare_log_stream` in this order:

1. Require `urlparse(path).path == _LOG_STREAM_PATH`; return `404` otherwise.
2. Reuse `_authorization_source`; return plain `403` when unauthorized.
3. Parse with `parse_qs(..., keep_blank_values=True)`. Reject unknown keys and any key with more than one value.
4. For query-token authorization, remove `token`, rebuild the location with `urlencode(..., doseq=True)`, return `303`, and set the existing HttpOnly cookie. The redirect must never include the token.
5. Return `503` when `self.log_hub is None`.
6. Call `parse_log_filter` with defaults and convert `ValueError` to a credential-free `400` body.
7. For header authorization, set the same cookie as normal routes; cookie/anonymous authorization requires no extra header.

The exact signature remains:

```python
def prepare_log_stream(
    self,
    path: str,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes, LogFilter | None]:
```

- [ ] **Step 4: Write failing end-to-end snapshot, live, heartbeat, gap, and cleanup tests**

Add helpers that keep the HTTP connection open and parse one blank-line-delimited SSE frame:

```python
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
```

Then add:

```python
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
```

- [ ] **Step 5: Run streaming tests and verify RED**

```bash
python3 -m unittest -v \
  tests.test_web_logs.WebLogTests.test_sse_sends_newest_first_snapshot_then_live_log_with_id \
  tests.test_web_logs.WebLogTests.test_sse_heartbeat_and_disconnect_release_subscription \
  tests.test_web_logs.WebLogTests.test_sse_gap_is_sent_and_stream_is_closed
```

Expected: the server currently routes SSE through the normal finite `bytes` response and cannot satisfy the tests.

- [ ] **Step 6: Add the dedicated streaming branch and bounded cleanup**

Add `log_hub` to `start_web_server` and pass it to `WebApp`. In `Handler.do_GET`, branch before `_serve()`:

```python
def do_GET(self):
    if urlparse(self.path).path == _LOG_STREAM_PATH:
        self._serve_log_stream()
        return
    self._serve()
```

Implement `_serve_log_stream` with these exact behaviors:

```python
def _serve_log_stream(self):
    status, headers, body, spec = app.prepare_log_stream(self.path, dict(self.headers))
    if status != 200 or spec is None or app.log_hub is None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True
        return

    stream = app.log_hub.open_stream(spec, queue_size=SSE_CLIENT_QUEUE_SIZE)
    try:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encode_sse_event("snapshot", {
            "entries": [entry.payload() for entry in stream.snapshot],
            "filter_type": spec.filter_type,
            "lines": spec.lines,
            "keyword": spec.keyword,
        }))
        self.wfile.flush()
        while True:
            event = stream.next_event(SSE_HEARTBEAT_SECONDS)
            if event is None:
                frame = encode_sse_event("heartbeat", {"time": time.time()})
            elif event.kind == "gap":
                self.wfile.write(encode_sse_event("gap", {"reason": "slow_client"}))
                self.wfile.flush()
                break
            else:
                frame = encode_sse_event("log", event.entry.payload(), event_id=event.entry.id)
            self.wfile.write(frame)
            self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        self.close_connection = True
    finally:
        stream.close()
```

Keep the Handler's default HTTP/1.0 protocol so existing finite routes do not need a broad Content-Length rewrite. Mark request threads as daemon threads on the server instance before `serve_forever` starts so an abandoned SSE browser cannot delay application shutdown:

```python
server = ThreadingHTTPServer((host, port), Handler)
server.daemon_threads = True
server.block_on_close = False
```

Do not log client disconnect exceptions through the same LogHub.

- [ ] **Step 7: Run all Web tests and verify GREEN**

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_web_logs \
  tests.test_web_api \
  tests.test_web_admin \
  tests.test_hdhive_web
```

Expected: SSE tests pass, all existing finite API/HTML responses remain unchanged, and server shutdown does not wait for open EventSource threads.

- [ ] **Step 8: Review and commit SSE transport**

```bash
git diff --check
git diff -- app/web.py tests/test_web_logs.py
git add app/web.py tests/test_web_logs.py
git commit -m "feat: stream redacted logs over sse"
```

---

### Task 4: Add The Vue Realtime Log Page And EventSource Controller

**Files:**
- Create: `frontend/src/logView.js`
- Create: `frontend/src/views/Logs.vue`
- Create: `frontend/test/logView.test.js`
- Modify: `frontend/src/router.js`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Modify: `tests/test_frontend.py`

**Interfaces:**
- Consumes: `GET /api/v1/logs/stream` and its four events from Task 3.
- Produces: `buildLogStreamUrl(filters) -> string` without a token parameter.
- Produces: `parseLogEvent(event) -> object`, `prependLog(entries, entry, limit) -> object[]`, and `preservedScrollTop(...) -> number`.
- Produces: `createLogStreamController(callbacks, sourceFactory) -> { connect(filters), close() }`.
- Produces: Vue route `/logs`, rendered by `/app/logs`, and a “实时日志” sidebar item.

- [ ] **Step 1: Write failing pure frontend controller tests**

Create `frontend/test/logView.test.js`:

```javascript
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildLogStreamUrl,
  createLogStreamController,
  parseLogEvent,
  prependLog,
  preservedScrollTop,
} from '../src/logView.js'

test('buildLogStreamUrl sends only documented filters and never a web token', () => {
  const url = buildLogStreamUrl({ filterType: 'ERROR', lines: 2000, keyword: 'CMS 失败' })
  assert.equal(url, '/api/v1/logs/stream?filter_type=ERROR&lines=2000&keyword=CMS+%E5%A4%B1%E8%B4%A5')
  assert.equal(url.includes('token='), false)
})

test('log state keeps newest first, enforces limit, and parses multiline payloads', () => {
  const entry = parseLogEvent({ data: '{"id":3,"level":"ERROR","text":"line one\\nline two"}' })
  const rows = prependLog([{ id: 2 }, { id: 1 }], entry, 2)

  assert.equal(entry.text, 'line one\nline two')
  assert.deepEqual(rows.map((row) => row.id), [3, 2])
  assert.equal(preservedScrollTop(true, 120, 800, 860), 180)
  assert.equal(preservedScrollTop(false, 120, 800, 860), 0)
})

test('controller closes the previous EventSource on reconnect and on disposal', () => {
  const sources = []
  class FakeSource {
    constructor(url, options) {
      this.url = url
      this.options = options
      this.listeners = new Map()
      this.closed = false
      sources.push(this)
    }
    addEventListener(name, callback) { this.listeners.set(name, callback) }
    close() { this.closed = true }
    emit(name, payload) { this.listeners.get(name)?.({ data: JSON.stringify(payload) }) }
  }
  const snapshots = []
  const controller = createLogStreamController(
    { onSnapshot: (rows) => snapshots.push(rows) },
    (url, options) => new FakeSource(url, options),
  )

  controller.connect({ filterType: 'main', lines: 1000, keyword: '' })
  sources[0].emit('snapshot', { entries: [{ id: 1 }] })
  controller.connect({ filterType: 'all', lines: 5000, keyword: '' })

  assert.deepEqual(snapshots, [[{ id: 1 }]])
  assert.equal(sources[0].closed, true)
  assert.equal(sources[1].options.withCredentials, true)
  controller.close()
  assert.equal(sources[1].closed, true)
})
```

- [ ] **Step 2: Run frontend unit tests and verify RED**

```bash
cd frontend && npm test
```

Expected: only the new test fails because `src/logView.js` is absent; existing API, task, and quality helper tests still pass.

- [ ] **Step 3: Implement the small EventSource/state helper module**

Create `frontend/src/logView.js` with these implementations:

```javascript
export function buildLogStreamUrl({ filterType = 'main', lines = 1000, keyword = '' } = {}) {
  const params = new URLSearchParams({
    filter_type: filterType,
    lines: String(lines),
    keyword,
  })
  return `/api/v1/logs/stream?${params.toString()}`
}

export function parseLogEvent(event) {
  const payload = JSON.parse(event.data || '{}')
  if (!payload || typeof payload !== 'object') throw new Error('日志事件格式无效')
  return payload
}

export function prependLog(entries, entry, limit) {
  return [entry, ...entries.filter((item) => item.id !== entry.id)].slice(0, Number(limit) || 1000)
}

export function preservedScrollTop(readingOlder, previousTop, previousHeight, nextHeight) {
  return readingOlder ? previousTop + Math.max(0, nextHeight - previousHeight) : 0
}

export function createLogStreamController(callbacks = {}, sourceFactory) {
  const factory = sourceFactory || ((url, options) => new EventSource(url, options))
  let source = null

  function close() {
    if (source) source.close()
    source = null
  }

  function connect(filters) {
    close()
    const current = factory(buildLogStreamUrl(filters), { withCredentials: true })
    source = current
    current.onopen = () => callbacks.onOpen?.()
    current.onerror = () => callbacks.onError?.()
    current.addEventListener('snapshot', (event) => {
      const payload = parseLogEvent(event)
      callbacks.onSnapshot?.(Array.isArray(payload.entries) ? payload.entries : [])
    })
    current.addEventListener('log', (event) => callbacks.onLog?.(parseLogEvent(event)))
    current.addEventListener('heartbeat', (event) => callbacks.onHeartbeat?.(parseLogEvent(event)))
    current.addEventListener('gap', (event) => callbacks.onGap?.(parseLogEvent(event)))
    return current
  }

  return { connect, close }
}
```

Do not import or read `WEB_TOKEN`; browser cookies are the only authenticated EventSource mechanism.

- [ ] **Step 4: Run helper tests and verify GREEN**

```bash
cd frontend && npm test
```

Expected: all Node tests pass.

- [ ] **Step 5: Add failing route, controls, and lifecycle contract checks**

Extend `tests/test_frontend.py`:

```python
def test_vue_admin_exposes_realtime_logs_route_and_lifecycle_controls(self):
    router = (ROOT / "frontend/src/router.js").read_text(encoding="utf-8")
    shell = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/views/Logs.vue").read_text(encoding="utf-8")
    helper = (ROOT / "frontend/src/logView.js").read_text(encoding="utf-8")

    self.assertIn("/logs", router)
    self.assertIn("实时日志", shell)
    for text in ("重要", "错误", "全部", "1000", "2000", "5000", "重连", "清空"):
        self.assertIn(text, page)
    self.assertIn("onBeforeUnmount", page)
    self.assertIn("preservedScrollTop", page)
    self.assertIn("withCredentials: true", helper)
    self.assertNotIn("WEB_TOKEN", helper)
```

Also add `"/logs"` to the route tuple in `test_vue_admin_shell_has_expected_routes_and_build_contract`.

- [ ] **Step 6: Run the static frontend contract and verify RED**

```bash
python3 -m unittest -v \
  tests.test_frontend.FrontendTests.test_vue_admin_shell_has_expected_routes_and_build_contract \
  tests.test_frontend.FrontendTests.test_vue_admin_exposes_realtime_logs_route_and_lifecycle_controls
```

Expected: failure because the route, menu, and page are not present.

- [ ] **Step 7: Build `Logs.vue` with local clear, safe reconnect, and scroll preservation**

Add the route/import:

```javascript
import Logs from './views/Logs.vue'
// ...
{ path: '/logs', component: Logs },
```

Add `{ label: '实时日志', key: '/logs' }` immediately before “设置” in `App.vue`.

Create `Logs.vue` using this complete component skeleton; keep local clear out of `api.js`:

```vue
<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NCard, NInput, NSelect, NSpace, NTag, useMessage } from 'naive-ui'
import {
  createLogStreamController,
  prependLog,
  preservedScrollTop,
} from '../logView'

const message = useMessage()
const entries = ref([])
const connectionState = ref('connecting')
const filterType = ref('main')
const lineLimit = ref(1000)
const keywordDraft = ref('')
const keyword = ref('')
const logViewport = ref(null)
const filterOptions = [
  { label: '重要', value: 'main' },
  { label: '错误', value: 'ERROR' },
  { label: '全部', value: 'all' },
]
const lineOptions = [1000, 2000, 5000].map((value) => ({ label: `${value} 行`, value }))
const statusLabel = computed(() => ({ connecting: '连接中', connected: '已连接', failed: '连接失败' }[connectionState.value]))
const statusType = computed(() => ({ connecting: 'warning', connected: 'success', failed: 'error' }[connectionState.value]))
let reconnectTimer
let disposed = false

function currentFilters() {
  return { filterType: filterType.value, lines: lineLimit.value, keyword: keyword.value }
}

const controller = createLogStreamController({
  onOpen: () => { connectionState.value = 'connected' },
  onError: () => { connectionState.value = 'failed' },
  onSnapshot: async (rows) => {
    entries.value = rows.slice(0, lineLimit.value)
    await nextTick()
    if (logViewport.value) logViewport.value.scrollTop = 0
  },
  onLog: async (entry) => {
    const viewport = logViewport.value
    const readingOlder = Boolean(viewport && viewport.scrollTop > 24)
    const previousTop = viewport?.scrollTop || 0
    const previousHeight = viewport?.scrollHeight || 0
    entries.value = prependLog(entries.value, entry, lineLimit.value)
    await nextTick()
    if (logViewport.value) {
      logViewport.value.scrollTop = preservedScrollTop(
        readingOlder,
        previousTop,
        previousHeight,
        logViewport.value.scrollHeight,
      )
    }
  },
  onGap: () => {
    if (disposed) return
    message.warning('日志更新过快，正在重新获取快照')
    controller.close()
    clearTimeout(reconnectTimer)
    reconnectTimer = setTimeout(reconnect, 500)
  },
})

function reconnect() {
  if (disposed) return
  clearTimeout(reconnectTimer)
  connectionState.value = 'connecting'
  controller.connect(currentFilters())
}
function applyKeyword() {
  keyword.value = keywordDraft.value.trim()
  reconnect()
}
function clearVisibleLogs() {
  entries.value = []
  message.success('已清空当前页面，磁盘日志未删除')
}
function levelClass(level) {
  return `log-level-${String(level || 'info').toLowerCase()}`
}

watch([filterType, lineLimit], reconnect)
onMounted(reconnect)
onBeforeUnmount(() => {
  disposed = true
  clearTimeout(reconnectTimer)
  controller.close()
})
</script>

<template>
  <div class="page-title">
    <div><h1>实时日志</h1><p>查看本程序最近和实时输出，不包含 CMS 自身日志。</p></div>
    <n-tag :type="statusType">{{ statusLabel }}</n-tag>
  </div>
  <n-card>
    <div class="log-toolbar">
      <n-select v-model:value="filterType" :options="filterOptions" style="width: 120px" />
      <n-select v-model:value="lineLimit" :options="lineOptions" style="width: 120px" />
      <n-input v-model:value="keywordDraft" clearable placeholder="关键字" style="max-width: 280px" @keyup.enter="applyKeyword" />
      <n-space>
        <n-button secondary @click="applyKeyword">筛选</n-button>
        <n-button secondary @click="reconnect">重连</n-button>
        <n-button secondary @click="clearVisibleLogs">清空</n-button>
      </n-space>
    </div>
    <div ref="logViewport" class="log-viewport">
      <pre v-for="entry in entries" :key="entry.id" class="log-entry" :class="levelClass(entry.level)">{{ entry.text }}</pre>
      <div v-if="!entries.length" class="log-empty">当前页面暂无日志</div>
    </div>
  </n-card>
</template>
```

Add scoped/global styles in `frontend/src/styles.css`:

```css
.log-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 14px; }
.log-viewport { height: min(68vh, 760px); overflow: auto; border: 1px solid #e2e7ef; border-radius: 8px; background: #111827; }
.log-entry { margin: 0; padding: 9px 12px; border-bottom: 1px solid #243044; color: #d7deea; font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.log-level-debug { color: #8d99aa; }
.log-level-info { color: #d7deea; }
.log-level-warning { color: #f6c85f; }
.log-level-error, .log-level-critical { color: #ff8585; }
.log-empty { padding: 48px 20px; color: #8d99aa; text-align: center; }
```

Keep the page functional at the existing 320 px minimum width by allowing the toolbar to wrap.

- [ ] **Step 8: Run frontend tests and production build**

```bash
python3 -m unittest -v tests.test_frontend
cd frontend && npm test && npm run build
```

Expected: Python contracts and Node tests pass; Vite emits `frontend/dist` without Vue warnings or unresolved imports.

- [ ] **Step 9: Review and commit the Vue page**

```bash
git diff --check
git diff -- frontend/src/logView.js frontend/src/views/Logs.vue frontend/src/router.js frontend/src/App.vue frontend/src/styles.css frontend/test/logView.test.js tests/test_frontend.py
git add frontend/src/logView.js frontend/src/views/Logs.vue frontend/src/router.js frontend/src/App.vue frontend/src/styles.css frontend/test/logView.test.js tests/test_frontend.py
git commit -m "feat: add realtime log console"
```

---

### Task 5: Document Operations And Run Full Regression

**Files:**
- Modify: `README.md`
- Modify: `docs/dockerhub-overview.md`
- Modify: `tests/test_docs_v02.py`

**Interfaces:**
- Documents: `/app/logs`, `/api/v1/logs/stream`, `/data/logs/cms-tg-ingest.log`, 20 MiB plus four backups, local-only clear, and existing `/data` mount.
- Preserves: existing Compose file and all CMS, 115, STRM, Emby, Telegram, healthcheck, backup, and HDHive behavior.

- [ ] **Step 1: Write failing documentation contract tests**

Add to `tests/test_docs_v02.py`:

```python
def test_realtime_logging_is_documented_without_new_volume(self):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    dockerhub = (ROOT / "docs/dockerhub-overview.md").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for document in (readme, dockerhub):
        for text in ("实时日志", "/app/logs", "/data/logs/cms-tg-ingest.log", "20 MiB", "4 个备份"):
            self.assertIn(text, document)
        self.assertIn("只清空当前浏览器", document)
    self.assertIn("./data:/data", compose)
    self.assertNotIn("/data/logs:/data/logs", compose)
```

- [ ] **Step 2: Run the documentation test and verify RED**

```bash
python3 -m unittest -v tests.test_docs_v02.V02DocsTests.test_realtime_logging_is_documented_without_new_volume
```

Expected: missing realtime logging documentation assertions fail.

- [ ] **Step 3: Add concise Chinese usage and troubleshooting documentation**

In both documents, add a “实时日志” subsection stating:

```text
- 打开 http://<unraid-ip>:8788/app/logs 查看本程序日志；页面支持重要/错误/全部、关键字和 1000/2000/5000 行筛选。
- “清空”只清空当前浏览器内容，不删除磁盘日志。
- 日志同时输出到 docker logs 和 /data/logs/cms-tg-ingest.log；当前文件达到 20 MiB 后轮转，保留 4 个备份。
- 容器继续使用现有 ./data:/data 挂载，不需要增加日志 volume；重启后恢复最近最多 5000 行。
- 配置 WEB_TOKEN 时，先通过 /app/?token=... 建立 HttpOnly Cookie，再进入 /app/logs；EventSource URL 不携带 Token。
```

Mention `/api/v1/logs/stream` only as the internal read-only SSE endpoint. Do not document disk deletion or log download because neither exists.

- [ ] **Step 4: Run focused security and feature suites before installing frontend packages**

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_logging_system \
  tests.test_web_logs \
  tests.test_bridge_v02_integration \
  tests.test_bridge_task_engine \
  tests.test_web_api \
  tests.test_web_admin \
  tests.test_frontend \
  tests.test_docs_v02 \
  tests.test_secret_hygiene
```

Expected: all focused Python tests pass and no real credential appears in source, fixtures, stdout assertions, or generated documentation.

- [ ] **Step 5: Run the complete Python regression**

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: the complete suite passes. Logging file failures and SSE client disconnects produce no TaskRunner, CMS, 115, or Emby test regression.

- [ ] **Step 6: Run frontend regression and build**

```bash
cd frontend
npm ci --ignore-scripts
npm test
npm run build
```

Expected: all Node tests pass and the production bundle contains the `/logs` route.

- [ ] **Step 7: Perform static release checks**

```bash
cd /Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/web-realtime-logging
git diff --check
python3 -m py_compile bridge.py app/logging_system.py app/web.py
rg -n "(/api/v1/logs/stream|/app/logs|cms-tg-ingest.log|LOG_MAX_BYTES|LOG_BACKUP_COUNT)" app frontend/src README.md docs/dockerhub-overview.md
git status --short
```

Expected: no whitespace or syntax errors; all five runtime/documentation contracts are present; only intended source, test, documentation, and ignored build files differ.

- [ ] **Step 8: Commit documentation and verified release state**

```bash
git add README.md docs/dockerhub-overview.md tests/test_docs_v02.py
git commit -m "docs: explain realtime log operations"
git status --short --branch
```

Expected: a clean feature branch. Publishing, version bumping, Docker Hub building, and Unraid deployment remain separate release work and are not performed by this implementation plan.
