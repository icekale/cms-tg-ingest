from __future__ import annotations

import json
import http.client
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


_RETRYABLE_HTTP_STATUS = {408, 425, 429}
_MAX_SAFE_GET_ATTEMPTS = 2
_TRANSIENT_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected)
_SENSITIVE_URL_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "auth_token",
    "bearer_token",
    "cookie",
    "csrf_token",
    "hdhive_token",
    "key",
    "password",
    "passwd",
    "p115_cookie",
    "pwd",
    "refresh_token",
    "secret",
    "session_token",
    "sessdata",
    "share_password",
    "share_pwd",
    "token",
}
_SENSITIVE_URL_KEYS_NORMALIZED = {
    re.sub(r"[^a-z0-9]+", "", key.lower()) for key in _SENSITIVE_URL_KEYS
}
_SENSITIVE_KEY_PATTERN = "|".join(
    sorted(
        {
            "api[_-]?key",
            "apikey",
            "access[_-]?token",
            "auth(?:orization|[_-]?token)?",
            "bearer[_-]?token",
            "cookie",
            "csrf[_-]?token",
            "hdhive[_-]?token",
            "key",
            "password",
            "passwd",
            "p115[_-]?cookie",
            "pwd",
            "refresh[_-]?token",
            "secret",
            "session[_-]?token",
            "sessdata",
            "share[_-]?(?:password|pwd)",
            "token",
        },
        key=len,
        reverse=True,
    )
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"(?<![\w-])(?P<key_quote>['\"]?)(?P<key>{_SENSITIVE_KEY_PATTERN})"
    rf"(?P=key_quote)\s*[:=]\s*"
    rf"(?:(?P<value_quote>['\"])(?P<quoted_value>.*?)(?P=value_quote)|"
    rf"(?P<bare_value>[^,;&\s}}]+))",
    re.IGNORECASE,
)


class HttpRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, retry_after_seconds: int = 0):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.retry_after_seconds = max(0, int(retry_after_seconds or 0))


def _retry_after_seconds(error: urllib.error.HTTPError) -> int:
    try:
        value = str(error.headers.get("Retry-After") or "").strip()
    except (AttributeError, TypeError):
        return 0
    if value.isdecimal():
        return int(value)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0, math.ceil(retry_at.timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError):
        return 0


_SENSITIVE_PATH_VALUE_RE = re.compile(
    rf"(?P<prefix>/(?:{_SENSITIVE_KEY_PATTERN})(?:/|=))(?P<value>[^/?#]+)",
    re.IGNORECASE,
)
# A misbehaving or hostile upstream must not be able to park a worker thread
# for hours via a large Retry-After header.
_MAX_RETRY_AFTER_SECONDS = 300.0
_SENSITIVE_ENCODED_ASSIGNMENT_RE = re.compile(
    rf"(?P<key>{_SENSITIVE_KEY_PATTERN})(?P<separator>%3d|=)"
    rf"(?P<value>[^&;\s<>\"']+)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def _normalized_url_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key or "").lower())


def _is_sensitive_url_key(key: str) -> bool:
    return _normalized_url_key(key) in _SENSITIVE_URL_KEYS_NORMALIZED


def _redact_assignments(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        matched = match.group(0)
        value_name = "quoted_value" if match.group("quoted_value") is not None else "bare_value"
        start = match.start(value_name) - match.start()
        end = match.end(value_name) - match.start()
        return matched[:start] + "<redacted>" + matched[end:]

    return _SENSITIVE_ASSIGNMENT_RE.sub(replace, str(value or ""))


def _redact_fragment(fragment: str) -> str:
    decoded = urllib.parse.unquote(str(fragment or ""))
    pairs = urllib.parse.parse_qsl(decoded, keep_blank_values=True)
    if pairs and any(_is_sensitive_url_key(key) for key, _value in pairs):
        return urllib.parse.urlencode(
            [
                (key, "<redacted>" if _is_sensitive_url_key(key) else value)
                for key, value in pairs
            ]
        )
    redacted = _redact_assignments(decoded)
    return redacted if redacted != decoded else fragment


def _redact_netloc(netloc: str) -> str:
    if "@" not in netloc:
        return netloc
    credentials, host = netloc.rsplit("@", 1)
    if ":" in credentials:
        username, _password = credentials.split(":", 1)
        credentials = f"{username}:<redacted>"
    else:
        credentials = "<redacted>"
    return f"{credentials}@{host}"


def _redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url))
    path = _redact_assignments(parsed.path)
    if (parsed.hostname or "").lower() == "api.telegram.org":
        path = re.sub(r"^/bot[^/]+(?=/|$)", "/bot<redacted>", path, count=1)
    path = _SENSITIVE_PATH_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        path,
    )
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if _is_sensitive_url_key(key):
            value = "<redacted>"
        else:
            value = _redact_text(value)
        query.append((key, value))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            _redact_netloc(parsed.netloc),
            path,
            urllib.parse.urlencode(query),
            _redact_fragment(parsed.fragment),
        )
    )


def _redact_text(value: str) -> str:
    redacted_value = _redact_assignments(str(value or ""))
    redacted_value = _SENSITIVE_ENCODED_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}%3Credacted%3E",
        redacted_value,
    )

    def replace_url(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        suffix = ""
        while raw_url and raw_url[-1] in ".,;:)]}":
            suffix = raw_url[-1] + suffix
            raw_url = raw_url[:-1]
        return _redact_url(raw_url) + suffix

    return _URL_RE.sub(replace_url, redacted_value)


def _safe_get_retryable(req: urllib.request.Request, error: BaseException) -> bool:
    if str(req.get_method()).upper() not in {"GET", "HEAD"}:
        return False
    if isinstance(error, urllib.error.HTTPError):
        return error.code in _RETRYABLE_HTTP_STATUS or error.code >= 500
    return isinstance(error, _TRANSIENT_NETWORK_ERRORS)


def _read_response(req: urllib.request.Request, timeout: int, safe_get_attempts: int = _MAX_SAFE_GET_ATTEMPTS) -> str:
    attempts = max(1, int(safe_get_attempts)) if str(req.get_method()).upper() in {"GET", "HEAD"} else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if attempt + 1 < attempts and _safe_get_retryable(req, exc):
                delay = min(
                    _MAX_RETRY_AFTER_SECONDS,
                    max(0.2, float(_retry_after_seconds(exc))),
                )
                exc.close()
                time.sleep(delay)
                continue
            raise
        except _TRANSIENT_NETWORK_ERRORS as exc:
            if attempt + 1 < attempts and _safe_get_retryable(req, exc):
                time.sleep(0.2)
                continue
            raise
    raise RuntimeError("HTTP request attempts exhausted")


class HttpJson:
    def __init__(self, timeout: int, safe_get_attempts: int = _MAX_SAFE_GET_ATTEMPTS):
        self.timeout = timeout
        self.safe_get_attempts = max(1, int(safe_get_attempts))

    def request(
        self,
        url: str,
        method: str = "GET",
        payload: dict | None = None,
        headers: dict | None = None,
        safe_get_attempts: int | None = None,
    ) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Accept": "application/json"}
        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            attempts = self.safe_get_attempts if safe_get_attempts is None else max(1, int(safe_get_attempts))
            raw = _read_response(req, self.timeout, attempts)
        except urllib.error.HTTPError as exc:
            try:
                body = _redact_text(exc.read().decode("utf-8", "replace"))[:300]
            finally:
                exc.close()
            raise HttpRequestError(
                f"HTTP {exc.code} from {_redact_url(url)}: {body[:300]}",
                status_code=exc.code,
                retry_after_seconds=_retry_after_seconds(exc),
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {_redact_text(str(exc.reason))}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {_redact_text(str(exc))}") from exc
        except http.client.RemoteDisconnected as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {_redact_text(str(exc))}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            body = _redact_text(raw)[:300]
            raise RuntimeError(f"Non-JSON response from {_redact_url(url)}: {body}") from exc


class FormHttp:
    def __init__(self, timeout: int, safe_get_attempts: int = _MAX_SAFE_GET_ATTEMPTS):
        self.timeout = timeout
        self.safe_get_attempts = max(1, int(safe_get_attempts))

    def request(
        self,
        url: str,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        body = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
        req_headers = {"Accept": "application/json, text/plain, */*"}
        if data is not None:
            req_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            raw = _read_response(req, self.timeout, self.safe_get_attempts)
        except urllib.error.HTTPError as exc:
            try:
                body_text = _redact_text(exc.read().decode("utf-8", "replace"))[:300]
            finally:
                exc.close()
            raise HttpRequestError(
                f"HTTP {exc.code} from {_redact_url(url)}: {body_text}",
                status_code=exc.code,
                retry_after_seconds=_retry_after_seconds(exc),
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {_redact_text(str(exc.reason))}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {_redact_text(str(exc))}") from exc
        except http.client.RemoteDisconnected as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {_redact_text(str(exc))}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            body_text = _redact_text(raw)[:300]
            raise RuntimeError(f"Non-JSON response from {_redact_url(url)}: {body_text}") from exc


def load_cookie_value(value_or_path: str) -> str:
    value = str(value_or_path or "").strip()
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return value
