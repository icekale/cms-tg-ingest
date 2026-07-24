from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


_RETRYABLE_HTTP_STATUS = {408, 425, 429}
_MAX_SAFE_GET_ATTEMPTS = 2
_TRANSIENT_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected)


def _redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url))
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"api_key", "apikey", "token", "access_token", "authorization"}:
            value = "<redacted>"
        query.append((key, value))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def _safe_get_retryable(req: urllib.request.Request, error: BaseException) -> bool:
    if str(req.get_method()).upper() not in {"GET", "HEAD"}:
        return False
    if isinstance(error, urllib.error.HTTPError):
        return error.code in _RETRYABLE_HTTP_STATUS or error.code >= 500
    return isinstance(error, _TRANSIENT_NETWORK_ERRORS)


def _read_response(req: urllib.request.Request, timeout: int) -> str:
    attempts = _MAX_SAFE_GET_ATTEMPTS if str(req.get_method()).upper() in {"GET", "HEAD"} else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if attempt + 1 < attempts and _safe_get_retryable(req, exc):
                exc.close()
                time.sleep(0.2)
                continue
            raise
        except _TRANSIENT_NETWORK_ERRORS as exc:
            if attempt + 1 < attempts and _safe_get_retryable(req, exc):
                time.sleep(0.2)
                continue
            raise
    raise RuntimeError("HTTP request attempts exhausted")


class HttpJson:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def request(self, url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Accept": "application/json"}
        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            raw = _read_response(req, self.timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {_redact_url(url)}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {exc}") from exc
        except http.client.RemoteDisconnected as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON response from {url}: {raw[:300]}") from exc


class FormHttp:
    def __init__(self, timeout: int):
        self.timeout = timeout

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
            raw = _read_response(req, self.timeout)
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {_redact_url(url)}: {body_text[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {exc}") from exc
        except http.client.RemoteDisconnected as exc:
            raise RuntimeError(f"Cannot reach {_redact_url(url)}: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON response from {url}: {raw[:300]}") from exc


def load_cookie_value(value_or_path: str) -> str:
    value = str(value_or_path or "").strip()
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return value
