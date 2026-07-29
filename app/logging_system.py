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
_QUOTED_VALUE_SECRET_RE = re.compile(
    r"""((?:["'])(?:password|passwd|pwd|receive_code|access_code|token|api_key|apikey|access_token|refresh_token|secret)(?:["'])\s*[:=]\s*["'])([^"'\r\n]*)(["'])""",
    re.IGNORECASE,
)
_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_CHINESE_CODE_RE = re.compile(r"((?:访问码|接收码)\s*[：:]\s*)[^\s,，;；]+")
_HISTORY_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) (?P<logger>\S+) (?P<text>.*)$"
)
_CONFIGURE_LOCK = threading.RLock()


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


def _replace_captured_value(match: re.Match[str]) -> str:
    value_start = match.start(2) - match.start()
    value_end = match.end(2) - match.start()
    return f"{match.group(0)[:value_start]}[REDACTED]{match.group(0)[value_end:]}"


def redact_text(value: object) -> str:
    text = str(value)
    text = _URL_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _HEADER_SECRET_RE.sub(_replace_captured_value, text)
    text = _QUOTED_VALUE_SECRET_RE.sub(_replace_captured_value, text)
    text = _VALUE_SECRET_RE.sub(_replace_captured_value, text)
    text = _BOT_TOKEN_RE.sub("[REDACTED]", text)
    return _CHINESE_CODE_RE.sub(r"\1[REDACTED]", text)


def _history_paths_newest_first(log_path: Path, backup_count: int) -> tuple[Path, ...]:
    rotated = tuple(log_path.with_name(f"{log_path.name}.{index}") for index in range(1, backup_count + 1))
    return (log_path, *rotated)


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

    def restore(self, log_path: Path, backup_count: int = LOG_BACKUP_COUNT) -> None:
        remaining = self._entries.maxlen
        blocks: list[deque[str]] = []
        for path in _history_paths_newest_first(Path(log_path), backup_count):
            if remaining == 0:
                break
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    block = deque(handle, maxlen=remaining)
            except (OSError, UnicodeError):
                continue
            blocks.append(block)
            remaining -= len(block)

        for block in reversed(blocks):
            for line in block:
                stripped = line.strip()
                match = _HISTORY_RE.match(stripped)
                if match is None:
                    self.publish(time.time(), "INFO", "history", stripped)
                    continue
                try:
                    created_at = datetime.strptime(match.group("timestamp"), LOG_DATE_FORMAT).astimezone().timestamp()
                except ValueError:
                    self.publish(time.time(), "INFO", "history", stripped)
                    continue
                self.publish(created_at, match.group("level"), match.group("logger"), stripped)

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

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if super().shouldRollover(record):
            return True
        try:
            return bool(
                self.maxBytes > 0
                and self.stream is not None
                and self.stream.tell() == 0
                and len(f"{self.format(record)}\n") >= self.maxBytes
            )
        except Exception:
            return False

    def handleError(self, record: logging.LogRecord) -> None:
        self._logging_disabled = True
        if not self._failure_reported:
            self._failure_reported = True
            try:
                sys.__stderr__.write("cms-tg-ingest: file logging disabled after write failure\n")
            except Exception:
                pass


class LoggingRuntime:
    def __init__(
        self,
        hub: LogHub,
        logger: logging.Logger,
        handlers: tuple[logging.Handler, ...],
        file_handler: SafeRotatingFileHandler | None,
        file_error: str | None,
    ):
        self.hub = hub
        self.logger = logger
        self.handlers = handlers
        self.file_handler = file_handler
        self.file_error = file_error
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handler in self.handlers:
            if getattr(handler, _HANDLER_MARKER, False):
                try:
                    self.logger.removeHandler(handler)
                except Exception:
                    pass
                try:
                    handler.flush()
                except Exception:
                    pass
                try:
                    handler.close()
                except Exception:
                    pass


def _close_handler(logger: logging.Logger, handler: logging.Handler) -> None:
    try:
        logger.removeHandler(handler)
    except Exception:
        pass
    try:
        handler.flush()
    except Exception:
        pass
    try:
        handler.close()
    except Exception:
        pass


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
    logger = root_logger if root_logger is not None else logging.getLogger()
    with _CONFIGURE_LOCK:
        for handler in logger.handlers:
            if getattr(handler, _HANDLER_MARKER, False):
                runtime = getattr(handler, "_cms_logging_runtime", None)
                if isinstance(runtime, LoggingRuntime):
                    logger.setLevel(level)
                    return runtime

        hub = LogHub(history_limit)
        path = Path(log_path)
        hub.restore(path, backup_count)

        for handler in tuple(logger.handlers):
            _close_handler(logger, handler)

        formatter = RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
        handlers: list[logging.Handler] = []
        stream_handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)
        hub_handler = LogHubHandler(hub)
        hub_handler.setFormatter(formatter)
        handlers.append(hub_handler)

        file_handler: SafeRotatingFileHandler | None = None
        file_error: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = SafeRotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except (OSError, UnicodeError):
            file_error = "file logging unavailable"
            try:
                sys.__stderr__.write("cms-tg-ingest: file logging unavailable\n")
            except Exception:
                pass

        runtime = LoggingRuntime(hub, logger, tuple(handlers), file_handler, file_error)
        for handler in handlers:
            setattr(handler, _HANDLER_MARKER, True)
            setattr(handler, "_cms_logging_runtime", runtime)
            logger.addHandler(handler)
        logger.setLevel(level)
        return runtime
