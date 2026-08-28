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
from typing import BinaryIO, Iterator, Literal, TextIO

from .clients.http import _redact_url


DEFAULT_LOG_PATH = Path("/data/logs/cms-tg-ingest.log")
LOG_HISTORY_LIMIT = 5000
LOG_ENTRY_MAX_BYTES = 64 * 1024
LOG_HISTORY_MAX_BYTES = 5 * 1024 * 1024
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
_TRUNCATION_MARKER = " [TRUNCATED]"

_URL_SECRET_RE = re.compile(
    r"([?&](?:password|passwd|pwd|code|share_code|receive_code|access_code|share_password|share_pwd|cms_password|self_share_own_share_password|token|access_token|refresh_token|auth_token|bearer_token|csrf_token|hdhive_token|session_token|web_token|emby_token|tmdb_bearer_token|p115_cookie|sessdata|api_key|apikey|emby_api_key|openai_api_key|tmdb_api_key|key|secret)=)[^&#\s]+",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_HEADER_SECRET_RE = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-web-token|x-emby-token)\b\s*[:=]\s*([^\r\n]+)",
    re.IGNORECASE,
)
_SENSITIVE_MAPPING_KEYS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-web-token",
    "x-emby-token",
    "password",
    "passwd",
    "pwd",
    "receive_code",
    "share_code",
    "access_code",
    "token",
    "api_key",
    "apikey",
    "key",
    "access_token",
    "refresh_token",
    "auth_token",
    "bearer_token",
    "csrf_token",
    "hdhive_token",
    "session_token",
    "web_token",
    "emby_token",
    "p115_cookie",
    "sessdata",
    "share_password",
    "share_pwd",
    "cms_password",
    "self_share_own_share_password",
    "emby_api_key",
    "openai_api_key",
    "tmdb_api_key",
    "tmdb_bearer_token",
    "secret",
)


def _mapping_key_pattern(key: str) -> str:
    parts = []
    for character in key:
        codepoints = [ord(character)]
        if character.isalpha():
            codepoints.append(ord(character.upper()))
        alternatives = [re.escape(character), *(rf"\\u{codepoint:04x}" for codepoint in codepoints)]
        parts.append("(?:" + "|".join(dict.fromkeys(alternatives)) + ")")
    return "".join(parts)


_SENSITIVE_MAPPING_KEY_RE = "(?:" + "|".join(_mapping_key_pattern(key) for key in _SENSITIVE_MAPPING_KEYS) + ")"
_MAPPING_SECRET_KEY_RE = re.compile(
    r"(?P<prefix>(?P<key_quote>[\"'])"
    + _SENSITIVE_MAPPING_KEY_RE
    + r"(?P=key_quote)\s*[:=]\s*)",
    re.IGNORECASE,
)
_VALUE_SECRET_RE = re.compile(
    r"\b(password|passwd|pwd|receive_code|share_code|access_code|share_password|share_pwd|cms_password|self_share_own_share_password|token|api_key|apikey|emby_api_key|openai_api_key|tmdb_api_key|access_token|refresh_token|auth_token|bearer_token|tmdb_bearer_token|csrf_token|hdhive_token|session_token|web_token|emby_token|p115_cookie|sessdata|secret)\b\s*[:=]\s*"
    r"(.*?)(?=(?:\s+https?://)|[,;&\r\n]|$)",
    re.IGNORECASE,
)
_QUOTED_VALUE_SECRET_RE = re.compile(
    r"""((?:["'])(?:password|passwd|pwd|receive_code|share_code|access_code|share_password|share_pwd|cms_password|self_share_own_share_password|token|api_key|apikey|emby_api_key|openai_api_key|tmdb_api_key|access_token|refresh_token|auth_token|bearer_token|tmdb_bearer_token|csrf_token|hdhive_token|session_token|web_token|emby_token|p115_cookie|sessdata|secret)(?:["'])\s*[:=]\s*["'])([^"'\r\n]*)(["'])""",
    re.IGNORECASE,
)
_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_CHINESE_CODE_RE = re.compile(r"((?:访问码|接收码|提取码)\s*[：:]\s*)[^\s,，;；]+")
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
    logger: str = ""

    def matches(self, entry: LogEntry) -> bool:
        if _LEVEL_VALUES.get(entry.level, logging.INFO) < _FILTER_LEVELS[self.filter_type]:
            return False
        if self.logger and entry.logger.casefold() != self.logger.casefold():
            return False
        return not self.keyword or self.keyword.casefold() in entry.text.casefold()


@dataclass(frozen=True)
class LogEvent:
    kind: Literal["log", "gap", "closed"]
    entry: LogEntry | None = None
    dropped: int = 0


def parse_log_filter(
    filter_type: object = "main",
    lines: object = 1000,
    keyword: object = "",
    logger: object = "",
) -> LogFilter:
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
    normalized_logger = str(logger or "").strip()
    if len(normalized_logger) > 100:
        raise ValueError("logger must be at most 100 characters")
    return LogFilter(normalized_type, normalized_lines, normalized_keyword, normalized_logger)


def _replace_captured_value(match: re.Match[str]) -> str:
    value_start = match.start(2) - match.start()
    value_end = match.end(2) - match.start()
    return f"{match.group(0)[:value_start]}[REDACTED]{match.group(0)[value_end:]}"


def _redact_http_url(match: re.Match[str]) -> str:
    try:
        return _redact_url(match.group(0))
    except (TypeError, UnicodeError, ValueError):
        return "[REDACTED_URL]"


def _structured_value_end(text: str, start: int) -> int:
    if start >= len(text):
        return start
    quote = text[start]
    if quote in {"'", '"'}:
        cursor = start + 1
        while cursor < len(text):
            if text[cursor] == "\\":
                cursor += 2
            elif text[cursor] == quote:
                return cursor + 1
            else:
                cursor += 1
        return len(text)

    pairs = {"{": "}", "[": "]", "(": ")"}
    if quote in pairs:
        stack = [pairs[quote]]
        cursor = start + 1
        while cursor < len(text) and stack:
            character = text[cursor]
            if character in {"'", '"'}:
                cursor = _structured_value_end(text, cursor)
                continue
            if character in pairs:
                stack.append(pairs[character])
            elif character == stack[-1]:
                stack.pop()
            cursor += 1
        return cursor if not stack else len(text)

    cursor = start
    while cursor < len(text) and text[cursor] not in ",;;&\r\n]}":
        cursor += 1
    return cursor


def _redact_mapping_values(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _MAPPING_SECRET_KEY_RE.finditer(text):
        if match.start() < cursor:
            continue
        value_start = match.end()
        value_end = _structured_value_end(text, value_start)
        if value_end <= value_start:
            continue
        parts.append(text[cursor:value_start])
        value = text[value_start:value_end]
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = f"{value[0]}[REDACTED]{value[-1]}"
        else:
            value = "[REDACTED]"
        parts.append(value)
        cursor = value_end
    parts.append(text[cursor:])
    return "".join(parts)


def redact_text(value: object) -> str:
    text = str(value)
    text = _URL_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _HEADER_SECRET_RE.sub(_replace_captured_value, text)
    text = _redact_mapping_values(text)
    text = _QUOTED_VALUE_SECRET_RE.sub(_replace_captured_value, text)
    text = _VALUE_SECRET_RE.sub(_replace_captured_value, text)
    text = _BOT_TOKEN_RE.sub("[REDACTED]", text)
    text = _CHINESE_CODE_RE.sub(r"\1[REDACTED]", text)
    return _HTTP_URL_RE.sub(_redact_http_url, text)


def _replace_blocked_values(text: str, blocked_values: object) -> str:
    if not blocked_values:
        return text
    values = sorted(
        {
            str(value).strip()
            for value in blocked_values
            if str(value or "").strip()
        },
        key=len,
        reverse=True,
    )
    if not values:
        return text
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(value) for value in values) + r")(?![\w-])")

    def replace(match: re.Match[str]) -> str:
        value = match.group(1)
        # Short numeric codes overlap normal task numbers; a path segment is an
        # unambiguous code context while labels such as "任务 #1234" are not.
        if value.isdigit() and len(value) < 6 and not text[: match.start()].endswith("/"):
            return value
        return "<redacted>"

    return pattern.sub(replace, text)


def safe_telegram_text(value: object, limit: int = 200, *, blocked_values: object = None) -> str:
    """Redact logging-sensitive values, known context codes, hide URLs, and bound Telegram text."""
    text = _HTTP_URL_RE.sub("<redacted-url>", redact_text("" if value is None else value))
    text = _replace_blocked_values(text, blocked_values)
    if len(text) <= limit:
        return text
    tail_len = min(80, max(0, int(limit) // 3))
    head_len = max(0, int(limit) - tail_len - 3)
    return f"{text[:head_len]}...{text[-tail_len:]}"


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8", errors="backslashreplace"))


def _truncate_utf8(text: str, max_bytes: int = LOG_ENTRY_MAX_BYTES) -> str:
    if _utf8_size(text) <= max_bytes:
        return text
    prefix_budget = max(0, max_bytes - _utf8_size(_TRUNCATION_MARKER))
    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if _utf8_size(text[:midpoint]) <= prefix_budget:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low] + _TRUNCATION_MARKER


def _bounded_history_lines(handle: BinaryIO) -> Iterator[str]:
    read_limit = LOG_ENTRY_MAX_BYTES + 1
    while True:
        chunk = handle.readline(read_limit)
        if not chunk:
            return
        oversized = len(chunk) > LOG_ENTRY_MAX_BYTES and not chunk.endswith(b"\n")
        if oversized:
            head = chunk
            while chunk and not chunk.endswith(b"\n"):
                chunk = handle.readline(read_limit)
            yield _truncate_utf8(head.decode("utf-8", errors="replace"))
        else:
            yield chunk.decode("utf-8", errors="replace")


def _history_paths_newest_first(log_path: Path, backup_count: int) -> tuple[Path, ...]:
    rotated = tuple(log_path.with_name(f"{log_path.name}.{index}") for index in range(1, backup_count + 1))
    return (log_path, *rotated)


def _prune_stale_backups(log_path: Path, backup_count: int) -> None:
    try:
        candidates = tuple(log_path.parent.glob(f"{log_path.name}.*"))
    except OSError:
        return
    for candidate in candidates:
        try:
            index = int(candidate.name.rsplit(".", 1)[1])
        except (IndexError, ValueError):
            continue
        if index > max(0, backup_count):
            try:
                candidate.unlink()
            except OSError:
                continue


class LogStream:
    def __init__(self, hub: "LogHub", spec: LogFilter, snapshot: tuple[LogEntry, ...], queue_size: int):
        self.snapshot = snapshot
        self._hub = hub
        self._spec = spec
        self._queue: queue.Queue[LogEvent] = queue.Queue(maxsize=max(1, queue_size))
        self._closed = False
        self._dropped = 0

    def _offer(self, entry: LogEntry) -> None:
        if self._closed or not self._spec.matches(entry):
            return
        try:
            self._queue.put_nowait(LogEvent("log", entry))
        except queue.Full:
            drained = 0
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                drained += 1
            self._dropped += max(1, drained)
            self._queue.put_nowait(LogEvent("gap", dropped=self._dropped))

    def next_event(self, timeout: float) -> LogEvent | None:
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._hub._unsubscribe(self)
            try:
                self._queue.put_nowait(LogEvent("closed"))
            except queue.Full:
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                self._queue.put_nowait(LogEvent("closed"))


class LogHub:
    def __init__(self, capacity: int = LOG_HISTORY_LIMIT, rate_limit: bool = False):
        self._entries: deque[LogEntry] = deque(maxlen=max(1, capacity))
        self._total_bytes = 0
        self._next_id = 1
        self._streams: set[LogStream] = set()
        self._lock = threading.RLock()
        self._rate_limit = bool(rate_limit)
        self._rate_buckets: dict[tuple[str, str, str], float] = {}

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._streams)

    def publish(self, created_at: float, level: str, logger: str, text: str) -> LogEntry | None:
        with self._lock:
            safe_text = _truncate_utf8(redact_text(text))
            if self._rate_limit:
                key = (str(level or "INFO").upper(), str(logger or "root"), safe_text)
                previous = self._rate_buckets.get(key)
                if previous is not None and float(created_at) - previous < 1.0:
                    return None
                self._rate_buckets[key] = float(created_at)
            entry = LogEntry(
                id=self._next_id,
                created_at=float(created_at),
                timestamp=datetime.fromtimestamp(created_at).astimezone().isoformat(timespec="milliseconds"),
                level=str(level or "INFO").upper(),
                logger=str(logger or "root"),
                text=safe_text,
            )
            self._next_id += 1
            if len(self._entries) == self._entries.maxlen:
                self._total_bytes -= _utf8_size(self._entries.popleft().text)
            self._entries.append(entry)
            self._total_bytes += _utf8_size(safe_text)
            while self._entries and self._total_bytes > LOG_HISTORY_MAX_BYTES:
                self._total_bytes -= _utf8_size(self._entries.popleft().text)
            for stream in tuple(self._streams):
                stream._offer(entry)
            return entry

    def restore(self, log_path: Path, backup_count: int = LOG_BACKUP_COUNT) -> None:
        remaining = self._entries.maxlen
        remaining_bytes = LOG_HISTORY_MAX_BYTES
        blocks: list[deque[str]] = []
        for path in _history_paths_newest_first(Path(log_path), backup_count):
            if remaining == 0 or remaining_bytes == 0:
                break
            try:
                with path.open("rb") as handle:
                    block: deque[str] = deque()
                    block_bytes = 0
                    for line in _bounded_history_lines(handle):
                        stripped = line.strip()
                        block.append(stripped)
                        block_bytes += _utf8_size(stripped)
                        while len(block) > remaining or block_bytes > remaining_bytes:
                            block_bytes -= _utf8_size(block.popleft())
            except OSError:
                continue
            blocks.append(block)
            remaining -= len(block)
            remaining_bytes -= block_bytes

        for block in reversed(blocks):
            for stripped in block:
                match = _HISTORY_RE.match(stripped)
                if match is None:
                    self.publish(time.time(), "INFO", "history", stripped)
                    continue
                raw_timestamp = match.group("timestamp")
                if "," in raw_timestamp:
                    raw_timestamp = raw_timestamp.split(",", 1)[0]
                try:
                    created_at = datetime.strptime(raw_timestamp, LOG_DATE_FORMAT).astimezone().timestamp()
                except ValueError:
                    self.publish(time.time(), "INFO", "history", stripped)
                    continue
                self.publish(created_at, match.group("level"), match.group("logger"), match.group("text"))

    def snapshot(self, spec: LogFilter) -> tuple[LogEntry, ...]:
        with self._lock:
            matches = [entry for entry in reversed(self._entries) if spec.matches(entry)]
            return tuple(matches[: spec.lines])

    def open_stream(self, spec: LogFilter, queue_size: int = 256) -> LogStream:
        with self._lock:
            stream = LogStream(self, spec, self.snapshot(spec), queue_size)
            self._streams.add(stream)
            return stream

    def close_streams(self) -> None:
        with self._lock:
            streams = tuple(self._streams)
        for stream in streams:
            stream.close()

    def _unsubscribe(self, stream: LogStream) -> None:
        with self._lock:
            self._streams.discard(stream)


class RedactingFormatter(logging.Formatter):
    def __init__(self, *args, escape_invalid_unicode: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.escape_invalid_unicode = escape_invalid_unicode

    def format(self, record: logging.LogRecord) -> str:
        text = redact_text(super().format(record))
        if self.escape_invalid_unicode:
            return text.encode("utf-8", errors="backslashreplace").decode("utf-8")
        return text


class LogHubHandler(logging.Handler):
    def __init__(self, hub: LogHub):
        super().__init__()
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.hub.publish(record.created, record.levelname, record.name, record.getMessage())
        except Exception:
            return


class SafeStreamHandler(logging.StreamHandler):
    def __init__(self, stream: TextIO | None = None):
        self._logging_disabled = False
        self._failure_reported = False
        super().__init__(stream)

    def emit(self, record: logging.LogRecord) -> None:
        if not self._logging_disabled:
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self._logging_disabled = True
        if not self._failure_reported:
            self._failure_reported = True
            try:
                sys.__stderr__.write("cms-tg-ingest: stdout logging disabled after write failure\n")
            except Exception:
                pass


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
    rate_limit: bool = False,
) -> LoggingRuntime:
    logger = root_logger if root_logger is not None else logging.getLogger()
    with _CONFIGURE_LOCK:
        for handler in logger.handlers:
            if getattr(handler, _HANDLER_MARKER, False):
                runtime = getattr(handler, "_cms_logging_runtime", None)
                if isinstance(runtime, LoggingRuntime):
                    runtime.close()
                break

        hub = LogHub(history_limit, rate_limit=rate_limit)
        path = Path(log_path)
        _prune_stale_backups(path, backup_count)
        hub.restore(path, backup_count)

        for handler in tuple(logger.handlers):
            _close_handler(logger, handler)

        output_formatter = RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT, escape_invalid_unicode=True)
        hub_formatter = RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
        handlers: list[logging.Handler] = []
        stream_handler = SafeStreamHandler(stream if stream is not None else sys.stdout)
        stream_handler.setFormatter(output_formatter)
        handlers.append(stream_handler)
        hub_handler = LogHubHandler(hub)
        hub_handler.setFormatter(hub_formatter)
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
            file_handler.setFormatter(output_formatter)
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
