from __future__ import annotations

import json
import logging
import re
import threading
import time
from copy import deepcopy
from typing import Any

from app.clients.http import FormHttp, load_cookie_value
from app.clients.p115_cipher import lixian_rsa_encrypt
from app.config import default_library_roots
from app.media.classify import candidate_tokens, extract_tmdb_id_from_name, extract_year_from_name, normalize_text

LOG = logging.getLogger("cms-tg-ingest")
CMS_PARENT_CID_CATEGORY_MAP: dict[str, str] = {}
DEFAULT_ORGANIZED_SCAN_MAX_LIST_CALLS = 8
_RECEIVE_SHARE_LOCK = threading.Lock()
PAN115_LIXIAN_SSP_URL = "https://lixian.115.com/lixianssp/"
PAN115_LIXIAN_WEB_URL = "https://lixian.115.com/lixian/"
PAN115_ANDROID_USER_AGENT = "Mozilla/5.0 115disk/99.99.99.99 115Browser/99.99.99.99 115wangpan_android/99.99.99.99"


class P115RiskControlError(RuntimeError):
    """Raised when 115 asks callers to slow down or stops automated actions."""


class P115ShareUnavailableError(RuntimeError):
    """Raised when 115 confirms that a share no longer exists or is invalid."""


class P115SharePendingError(RuntimeError):
    """Raised when 115 accepts share creation but has not issued a share code."""


def is_p115_risk_control_message(value: str) -> bool:
    text = str(value or "")
    return any(
        token in text
        for token in (
            "限制接收",
            "被限制接收",
            "限制分享",
            "被限制分享",
            "操作过于频繁",
            "访问过于频繁",
            "请求过于频繁",
            "稍后再试",
            "风控",
        )
    )


def is_p115_share_unavailable_message(value: str) -> bool:
    text = str(value or "")
    return any(
        token in text
        for token in (
            "分享不存在",
            "分享已失效",
            "分享已取消",
            "分享已过期",
            "分享已拒绝",
            "链接已失效",
            "链接不存在",
        )
    )


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def iter_items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "records", "data", "rows", "tasks", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def p115_file_id(item: dict[str, Any]) -> str:
    return str(item.get("cid") or item.get("fid") or item.get("file_id") or "").strip()


def p115_share_item_id(item: dict[str, Any]) -> str:
    return str(item.get("fid") or item.get("file_id") or item.get("cid") or "").strip()


def p115_parent_id(item: dict[str, Any]) -> str:
    return str(item.get("pid") or item.get("parent_id") or item.get("wp_path_id") or "").strip()


def p115_residue_file_id(item: dict[str, Any]) -> str:
    return str(item.get("fid") or item.get("file_id") or item.get("cid") or "").strip()


def p115_residue_parent_id(item: dict[str, Any]) -> str:
    return str(item.get("cid") or item.get("pid") or item.get("parent_id") or "").strip()


def p115_item_id(item: dict[str, Any]) -> str:
    """Return the local ID for either a file record or a folder record."""
    return p115_residue_file_id(item) if item.get("fid") else p115_file_id(item)


def p115_item_parent_id(item: dict[str, Any]) -> str:
    if item.get("fid"):
        return str(item.get("cid") or item.get("pid") or item.get("parent_id") or "").strip()
    return p115_parent_id(item)


def normalize_cloud_status(item: dict[str, Any]) -> str:
    raw = item.get("status", item.get("stat", item.get("state", "")))
    value = str(raw).strip().lower()
    if value in {"2", "11", "completed", "complete", "success", "succeeded", "done"}:
        return "completed"
    if value in {"0", "1", "12", "running", "downloading", "queued", "pending", "wait"}:
        return "running"
    if value in {"-1", "9", "failed", "failure", "error", "cancelled", "canceled"}:
        return "failed"
    return "unknown"


def _cloud_task_item(resp: dict[str, Any], identity: dict[str, Any] | None = None) -> dict[str, Any]:
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    items = iter_items(data)
    if items:
        if identity:
            expected_info_hash = str(identity.get("info_hash") or "").strip().lower()
            expected_task_id = str(identity.get("task_id") or "").strip()
            for item in items:
                candidate = _cloud_identity(item)
                if expected_info_hash and candidate["info_hash"] == expected_info_hash:
                    return dict(item)
                if expected_task_id and candidate["task_id"] == expected_task_id:
                    return dict(item)
            raise RuntimeError("115 cloud download task identity was not found")
        return dict(items[0])
    for key in ("task", "item", "record"):
        if isinstance(data.get(key), dict):
            return dict(data[key])
    return dict(data)


def _cloud_identity(item: dict[str, Any]) -> dict[str, str]:
    info_hash = str(item.get("info_hash") or item.get("hash") or item.get("infohash") or "").strip().lower()
    task_id = str(item.get("task_id") or item.get("id") or item.get("taskid") or "").strip()
    return {"info_hash": info_hash, "task_id": task_id}


def _cloud_source_hash(source_url: str) -> str:
    value = str(source_url or "").strip()
    match = re.search(r"urn:btih:([0-9a-z]+)", value, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    match = re.search(r"ed2k://\|file\|[^|]*\|[0-9]+\|([0-9a-f]{32})\|/", value, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def validate_cloud_output(output: dict[str, Any], target_cid: str) -> dict[str, str]:
    file_id = p115_file_id(output)
    parent_id = p115_parent_id(output)
    target = str(target_cid or "").strip()
    if not file_id:
        raise RuntimeError("115 cloud download completed without an output file id")
    if not target or parent_id != target:
        raise RuntimeError("115 cloud download output is outside the configured receive CID")
    return {
        "file_id": file_id,
        "parent_id": parent_id,
        "file_name": p115_file_name(output),
    }


def category_for_115_parent_id(parent_id: str, mapping: dict[str, str] | None = None) -> str:
    category_map = mapping if mapping is not None else CMS_PARENT_CID_CATEGORY_MAP
    return category_map.get(str(parent_id or "").strip(), "")


def p115_file_name(item: dict[str, Any]) -> str:
    return str(item.get("n") or item.get("file_name") or item.get("name") or "").strip()


def _organized_scan_cursor(parent_ids: set[str], cursor: dict[str, Any] | None) -> dict[str, Any]:
    roots = sorted({str(parent_id).strip() for parent_id in parent_ids if str(parent_id).strip()})
    if not isinstance(cursor, dict) or sorted(str(value) for value in cursor.get("root_parent_ids") or []) != roots:
        return {
            "version": 1,
            "root_parent_ids": roots,
            "queue": [
                {"parent_id": parent_id, "parts": [], "depth": 0, "offset": 0}
                for parent_id in roots
            ],
            "seen": roots[:],
        }

    queue: list[dict[str, Any]] = []
    for raw in cursor.get("queue") or []:
        if not isinstance(raw, dict):
            continue
        parent_id = str(raw.get("parent_id") or "").strip()
        if not parent_id:
            continue
        parts = raw.get("parts") if isinstance(raw.get("parts"), list) else []
        try:
            depth = max(0, int(raw.get("depth") or 0))
            offset = max(0, int(raw.get("offset") or 0))
        except (TypeError, ValueError):
            continue
        queue.append(
            {
                "parent_id": parent_id,
                "parts": [str(part) for part in parts],
                "depth": depth,
                "offset": offset,
            }
        )
    seen = {str(value).strip() for value in cursor.get("seen") or [] if str(value).strip()}
    seen.update(roots)
    return {"version": 1, "root_parent_ids": roots, "queue": queue, "seen": sorted(seen)}


def p115_is_folder(item: dict[str, Any]) -> bool:
    return bool(p115_file_id(item) and not item.get("fid"))


def infer_category_from_115_path(parts: list[str], category_names: set[str] | None = None) -> str:
    categories = category_names or set(default_library_roots())
    for part in reversed(parts):
        if part in categories:
            return part
    return ""


def infer_category_from_115_item(item: dict[str, Any]) -> str:
    category = str(item.get("_category") or "").strip()
    if category:
        return category
    parent_name = str(item.get("dp") or "").strip()
    return parent_name if parent_name in set(default_library_roots()) else ""


def select_organized_115_folder(
    items: list[dict[str, Any]],
    recognition: dict[str, Any],
    share_name: str,
    excluded_parent_ids: set[str] | None = None,
    allowed_parent_ids: set[str] | None = None,
) -> dict[str, str] | None:
    excluded = {str(value) for value in (excluded_parent_ids or set()) if str(value)}
    allowed = {str(value) for value in (allowed_parent_ids or set()) if str(value)}
    tokens = candidate_tokens(recognition, share_name)
    tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
    share_year = extract_year_from_name(share_name) or extract_year_from_name(str(recognition.get("title") or ""))
    if tmdb_id:
        tokens.insert(0, tmdb_id)
    matches: list[tuple[int, float, dict[str, str]]] = []
    for item in items:
        file_id = p115_file_id(item)
        name = p115_file_name(item)
        if not file_id or not name:
            continue
        if "fid" in item and "cid" in item:
            continue
        parent_id = p115_parent_id(item)
        if parent_id in excluded and parent_id not in allowed:
            continue
        norm_name = normalize_text(name)
        name_tmdb = extract_tmdb_id_from_name(name)
        name_year = extract_year_from_name(name)
        if tmdb_id and name_tmdb and name_tmdb != tmdb_id:
            continue
        if not tmdb_id and share_year and name_year and name_year != share_year:
            continue
        score = 0
        if tmdb_id and tmdb_id in name:
            score += 8
        if any(token and token in norm_name for token in tokens):
            score += 3
        if "[tmdb" in name.lower() or "{tmdb" in name.lower():
            score += 2
        if score <= 0:
            continue
        try:
            update_time = float(item.get("tu") or item.get("t") or item.get("te") or 0)
        except (TypeError, ValueError):
            update_time = 0.0
        matches.append(
            (
                score,
                update_time,
                {
                    "file_id": file_id,
                    "file_name": name,
                    "parent_id": parent_id,
                    "category": infer_category_from_115_item(item),
                },
            )
        )
    if not matches:
        return None
    matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return matches[0][2]


def select_recent_tmdb_115_folder(
    items: list[dict[str, Any]],
    year: str,
    excluded_parent_ids: set[str] | None = None,
    min_update_time: float = 0,
) -> dict[str, str] | None:
    excluded = {str(value) for value in (excluded_parent_ids or set()) if str(value)}
    matches: list[tuple[float, dict[str, str]]] = []
    for item in items:
        file_id = p115_file_id(item)
        name = p115_file_name(item)
        if not file_id or not name:
            continue
        if "fid" in item and "cid" in item:
            continue
        if p115_parent_id(item) in excluded:
            continue
        low_name = name.lower()
        if year and year not in name:
            continue
        if "[tmdb" not in low_name and "{tmdb" not in low_name:
            continue
        try:
            update_time = float(item.get("tu") or item.get("t") or item.get("te") or 0)
        except (TypeError, ValueError):
            update_time = 0.0
        if min_update_time and update_time and update_time < min_update_time:
            continue
        matches.append((update_time, {"file_id": file_id, "file_name": name, "parent_id": p115_parent_id(item)}))
    if not matches:
        return None
    matches.sort(key=lambda value: value[0], reverse=True)
    return matches[0][1]


def select_source_residue_115_files(
    items: list[dict[str, Any]],
    recognition: dict[str, Any],
    share_name: str,
    excluded_file_ids: set[str] | None = None,
    min_update_time: float = 0,
) -> list[dict[str, str]]:
    excluded = {str(value) for value in (excluded_file_ids or set()) if str(value)}
    tokens = candidate_tokens(recognition, share_name)
    year = extract_year_from_name(share_name) or extract_year_from_name(str(recognition.get("title") or ""))
    matches: list[tuple[int, float, dict[str, str]]] = []
    for item in items:
        file_id = p115_residue_file_id(item)
        name = p115_file_name(item)
        if not file_id or not name or file_id in excluded:
            continue
        update_time = as_float(item.get("tu") or item.get("t") or item.get("te"), 0.0)
        if min_update_time and update_time and update_time < min_update_time:
            continue
        norm_name = normalize_text(name)
        score = 0
        if any(token and token in norm_name for token in tokens):
            score += 5
        if year and year in name:
            score += 2
        if score < 5:
            continue
        matches.append(
            (
                score,
                update_time,
                {
                    "file_id": file_id,
                    "file_name": name,
                    "parent_id": p115_residue_parent_id(item),
                },
            )
        )
    matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return [match[2] for match in matches]


class P115WebClient:
    def __init__(
        self,
        cookie: str,
        http: Any | None = None,
        timeout: int = 60,
        min_interval_seconds: float = 0.0,
        cache_ttl_seconds: float = 3.0,
        share_list_cache_ttl_seconds: float = 300.0,
        clock: Any | None = None,
        sleeper: Any | None = None,
    ):
        self.cookie = load_cookie_value(cookie)
        self.http = http or FormHttp(timeout)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds or 0.0))
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds or 0.0))
        self.share_list_cache_ttl_seconds = max(0.0, float(share_list_cache_ttl_seconds or 0.0))
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self._last_request_at: float | None = None
        self._request_lock = threading.Lock()
        self._get_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._share_list_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self.request_count = 0
        if not self.cookie:
            raise RuntimeError("115 cookie is empty")

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "Origin": "https://115.com",
            "Referer": "https://115.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        }

    def _rate_limit(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        now = float(self.clock())
        if self._last_request_at is not None:
            wait_seconds = self.min_interval_seconds - (now - self._last_request_at)
            if wait_seconds > 0:
                self.sleeper(wait_seconds)
        self._last_request_at = float(self.clock())

    def _request(
        self,
        url: str,
        method: str = "GET",
        data: dict | None = None,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        with self._request_lock:
            method = str(method or "GET").upper()
            cache_key = None
            if method == "GET" and self.cache_ttl_seconds > 0:
                cache_key = json.dumps(
                    {
                        "url": str(url),
                        "params": params or {},
                        "headers": headers or {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                cached = self._get_cache.get(cache_key)
                if cached is not None:
                    expires_at, response = cached
                    if float(self.clock()) < expires_at:
                        return deepcopy(response)
                    self._get_cache.pop(cache_key, None)
            else:
                # Any mutating request can invalidate a previously cached listing or share snapshot.
                self._get_cache.clear()
                self._share_list_cache = None
            self._rate_limit()
            self.request_count += 1
            request_headers = self._headers()
            if headers:
                request_headers.update(headers)
            response = self.http.request(url, method=method, data=data, params=params, headers=request_headers)
            if cache_key and isinstance(response, dict) and response.get("state") is not False:
                self._get_cache[cache_key] = (float(self.clock()) + self.cache_ttl_seconds, deepcopy(response))
            return response

    @staticmethod
    def _ensure_state(resp: dict, fallback: str) -> dict:
        if resp.get("state") is True:
            return resp
        if "state" not in resp and resp.get("code") in {0, "", None}:
            return resp
        message = str(resp.get("error") or resp.get("message") or resp.get("msg") or fallback)
        if is_p115_risk_control_message(message):
            raise P115RiskControlError(message)
        raise RuntimeError(message)

    def search_files(self, search_value: str, limit: int = 20) -> list[dict[str, Any]]:
        resp = self._request(
            "https://webapi.115.com/files/search",
            params={"search_value": search_value, "limit": limit, "offset": 0, "fc_mix": 1},
        )
        self._ensure_state(resp, "115 search failed")
        return iter_items(resp.get("data") or resp)

    def share_snap(
        self,
        share_code: str,
        receive_code: str,
        cid: str = "0",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        page_size = max(1, min(int(limit), 100))
        page_offset = max(0, int(offset))
        if page_offset >= 5000:
            raise RuntimeError("115 share root exceeds 5000 entries")
        resp = self._request(
            "https://webapi.115.com/share/snap",
            params={
                "share_code": share_code,
                "receive_code": receive_code,
                "cid": cid,
                "offset": page_offset,
                "limit": page_size,
            },
        )
        try:
            self._ensure_state(resp, "115 share snap failed")
        except RuntimeError as exc:
            if is_p115_share_unavailable_message(str(exc)):
                raise P115ShareUnavailableError(str(exc)) from exc
            raise
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        share_info = data.get("shareinfo") if isinstance(data.get("shareinfo"), dict) else {}
        share_state = str(share_info.get("share_state") or "").strip().lower()
        if share_state and share_state not in {"0", "1", "true"}:
            raise P115ShareUnavailableError(f"115 分享状态不可用: {share_state}")
        return resp

    def share_root_items(
        self,
        share_code: str,
        receive_code: str,
        cid: str = "0",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        page_size = max(1, min(int(limit), 100))
        page_offset = max(0, int(offset))
        items: list[dict[str, Any]] = []
        seen_file_ids: set[str] = set()
        first_snap: dict[str, Any] | None = None

        if page_offset >= 5000:
            raise RuntimeError("115 share root exceeds 5000 entries")

        while True:
            request_limit = min(page_size, 5000 - page_offset)
            snap = self.share_snap(
                share_code,
                receive_code,
                cid=cid,
                limit=request_limit,
                offset=page_offset,
            )
            if first_snap is None:
                first_snap = snap
            data = snap.get("data") if isinstance(snap.get("data"), dict) else snap
            page = iter_items(data)
            page = page[:request_limit]
            for item in page:
                file_id = p115_share_item_id(item)
                if file_id and file_id not in seen_file_ids:
                    seen_file_ids.add(file_id)
                    items.append(item)
            if len(page) < request_limit:
                break
            page_offset += len(page)
            if page_offset >= 5000:
                raise RuntimeError("115 share root exceeds 5000 entries")

        return items, first_snap or {}

    def inspect_share(self, share_code: str, receive_code: str) -> dict[str, Any]:
        snap = self.share_snap(share_code, receive_code, cid="0", limit=1)
        data = snap.get("data") if isinstance(snap.get("data"), dict) else {}
        share_info = data.get("shareinfo") if isinstance(data.get("shareinfo"), dict) else {}
        share_state = str(share_info.get("share_state") or "").strip().lower()
        raw_vio = share_info.get("have_vio_file", data.get("have_vio_file", 0))
        have_vio_file = str(raw_vio).strip().lower() in {"1", "true", "yes"}
        return {
            "available": True,
            "share_state": share_state,
            "have_vio_file": have_vio_file,
        }

    def list_own_share_states(self, limit: int = 100) -> dict[str, dict[str, Any]]:
        now = float(self.clock())
        if self._share_list_cache is not None:
            expires_at, cached = self._share_list_cache
            if now < expires_at:
                return deepcopy(cached)
        resp = self._request(
            "https://webapi.115.com/share/slist",
            params={
                "limit": max(1, min(int(limit), 100)),
                "offset": 0,
                "order": "create_time",
                "asc": 0,
                "show_cancel_share": 1,
            },
        )
        self._ensure_state(resp, "115 share list failed")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        states: dict[str, dict[str, Any]] = {}
        for item in iter_items(data):
            share_code = str(item.get("share_code") or item.get("sharecode") or "").strip()
            if not share_code:
                continue
            raw_vio = item.get("have_vio_file", item.get("is_collect", 0))
            states[share_code] = {
                "share_state": str(item.get("share_state") or item.get("state") or "").strip().lower(),
                "have_vio_file": str(raw_vio).strip().lower() in {"1", "true", "yes"},
                "create_time": item.get("create_time") or item.get("share_time") or 0,
            }
        self._share_list_cache = (now + self.share_list_cache_ttl_seconds, deepcopy(states))
        return states

    def find_own_share_by_title(
        self,
        share_title: str,
        *,
        min_create_time: float = 0,
        limit: int = 100,
    ) -> dict[str, str] | None:
        expected_title = str(share_title or "").strip()
        if not expected_title:
            return None
        resp = self._request(
            "https://webapi.115.com/share/slist",
            params={
                "limit": max(1, min(int(limit), 100)),
                "offset": 0,
                "order": "create_time",
                "asc": 0,
                "show_cancel_share": 1,
            },
        )
        self._ensure_state(resp, "115 share list failed")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        matches: list[tuple[float, dict[str, Any]]] = []
        for item in iter_items(data):
            title = str(item.get("share_title") or item.get("title") or item.get("name") or "").strip()
            if title != expected_title:
                continue
            create_time = as_float(item.get("create_time") or item.get("share_time"), 0.0)
            if min_create_time and create_time and create_time < float(min_create_time):
                continue
            share_code = str(item.get("share_code") or item.get("sharecode") or "").strip()
            if not share_code:
                continue
            receive_code = str(item.get("receive_code") or item.get("receivecode") or "").strip()
            share_url = str(item.get("share_url") or "").strip() or f"https://115cdn.com/s/{share_code}"
            matches.append(
                (
                    create_time,
                    {
                        "share_code": share_code,
                        "receive_code": receive_code,
                        "share_url": share_url,
                        "create_time": str(create_time),
                    },
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda value: value[0], reverse=True)
        return matches[0][1]

    def receive_share_to_cid(self, share_code: str, receive_code: str, target_cid: str) -> dict[str, Any]:
        # Serialize the snapshot/receive/resolve transaction across client
        # instances so concurrent same-name receives cannot cross-match.
        with _RECEIVE_SHARE_LOCK:
            return self._receive_share_to_cid(share_code, receive_code, target_cid)

    def _receive_share_to_cid(self, share_code: str, receive_code: str, target_cid: str) -> dict[str, Any]:
        items, snap = self.share_root_items(share_code, receive_code, cid="0", limit=100)
        data = snap.get("data") if isinstance(snap.get("data"), dict) else {}
        file_ids = [p115_share_item_id(item) for item in items]
        if not file_ids:
            raise RuntimeError("115 share snap did not return file ids")
        info = data.get("shareinfo") if isinstance(data.get("shareinfo"), dict) else {}
        receive_data = {}
        title = str(
            info.get("share_title")
            or (p115_file_name(items[0]) if items else "")
            or ""
        ).strip()
        has_tmdb_hint = bool(extract_tmdb_id_from_name(title)) or any(
            extract_tmdb_id_from_name(p115_file_name(item)) for item in items
        )
        existing_file_ids: list[str] = []
        snapshot_complete = False
        if has_tmdb_hint:
            # A share snapshot ID is not a local file ID. Capture the target
            # root before receiving so a later same-name lookup cannot select
            # an older file already waiting in the pending directory.
            existing_items = self.list_files(str(target_cid), limit=500)
            existing_file_ids = [
                file_id
                for file_id in (p115_file_id(item) for item in existing_items)
                if file_id
            ]
            snapshot_complete = len(existing_items) < 500
        resp = self._request(
            "https://webapi.115.com/share/receive",
            method="POST",
            data={
                "share_code": share_code,
                "receive_code": receive_code,
                "file_id": ",".join(file_ids),
                "cid": str(target_cid),
            },
        )
        self._ensure_state(resp, "115 receive share failed")
        receive_data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        title = str(
            receive_data.get("receive_title")
            or info.get("share_title")
            or (p115_file_name(items[0]) if items else "")
            or ""
        ).strip()
        received_items = self._resolve_received_root_items(
            items,
            resp,
            target_cid,
            title,
            excluded_file_ids=set(existing_file_ids),
            require_new=snapshot_complete,
        )
        return {
            "title": title,
            # These are the IDs accepted by /share/receive and are retained
            # only as provenance; they are not trusted as local output IDs.
            "file_ids": file_ids,
            "received_items": received_items,
            "received_items_complete": len(received_items) == len(items),
            "received_expected_item_count": len(items),
            "received_existing_file_ids": existing_file_ids,
            "received_snapshot_complete": snapshot_complete,
            "response": resp,
        }

    @staticmethod
    def _nested_response_dicts(value: Any, depth: int = 0):
        if depth > 4:
            return
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from P115WebClient._nested_response_dicts(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                yield from P115WebClient._nested_response_dicts(child, depth + 1)

    @staticmethod
    def _normalized_received_item(item: dict[str, Any], target_cid: str) -> dict[str, Any] | None:
        file_id = p115_file_id(item)
        file_name = p115_file_name(item)
        if not file_id or not file_name or file_id == str(target_cid or "").strip():
            return None
        parent_id = p115_parent_id(item) or str(target_cid or "").strip()
        if parent_id and str(target_cid or "").strip() and parent_id != str(target_cid).strip():
            return None
        try:
            update_time = float(item.get("tu") or item.get("t") or item.get("te") or 0)
        except (TypeError, ValueError):
            update_time = 0.0
        return {
            "file_id": file_id,
            "file_name": file_name,
            "is_folder": p115_is_folder(item),
            "parent_id": parent_id,
            "_update_time": update_time,
        }

    def _resolve_received_root_items(
        self,
        source_items: list[dict[str, Any]],
        response: dict[str, Any],
        target_cid: str,
        share_title: str,
        excluded_file_ids: set[str] | None = None,
        require_new: bool = False,
    ) -> list[dict[str, Any]]:
        source_ids = {p115_share_item_id(item) for item in source_items if p115_share_item_id(item)}
        excluded_ids = source_ids | {str(value).strip() for value in (excluded_file_ids or set()) if str(value).strip()}
        source_names = [p115_file_name(item) for item in source_items if p115_file_name(item)]
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in self._nested_response_dicts(response):
            normalized = self._normalized_received_item(item, target_cid)
            if not normalized or normalized["file_id"] in excluded_ids:
                continue
            key = (normalized["file_id"], normalized["file_name"])
            if key not in seen:
                seen.add(key)
                candidates.append(normalized)

        has_tmdb_hint = bool(extract_tmdb_id_from_name(share_title)) or any(
            extract_tmdb_id_from_name(name) for name in source_names
        )
        if has_tmdb_hint and not require_new:
            # The marker appeared only after the receive response, so there
            # is no pre-receive baseline with which to prove a local ID is
            # new. Do not guess from a same-name pending file.
            return []
        if not candidates and has_tmdb_hint:
            try:
                for item in self.list_files(str(target_cid), limit=500):
                    normalized = self._normalized_received_item(item, target_cid)
                    if not normalized or normalized["file_id"] in excluded_ids:
                        continue
                    key = (normalized["file_id"], normalized["file_name"])
                    if key not in seen:
                        seen.add(key)
                        candidates.append(normalized)
            except Exception:
                LOG.debug("Failed to resolve local 115 receive output items", exc_info=True)

        if not candidates:
            return []
        resolved: list[dict[str, Any]] = []
        remaining = list(candidates)
        for source_name in source_names:
            source_norm = normalize_text(source_name)
            matches = [item for item in remaining if normalize_text(item["file_name"]) == source_norm]
            if not matches and len(source_items) == 1 and len(remaining) == 1:
                matches = remaining[:]
            if not matches:
                continue
            if len(matches) > 1:
                # A timestamp cannot identify which concurrent receive owns
                # a same-name file. Let the workflow retry after the caller
                # has made the receive result unambiguous.
                return []
            matches.sort(key=lambda item: item.get("_update_time") or 0, reverse=True)
            selected = matches[0]
            remaining = [item for item in remaining if item is not selected]
            resolved.append(selected)
        for item in resolved:
            item.pop("_update_time", None)
            if require_new:
                item["received_item_verified"] = True
        return resolved

    def cloud_download_add(self, url: str, target_cid: str) -> dict[str, str]:
        payload = json.dumps(
            {
                "url": str(url),
                "wp_path_id": str(target_cid),
                "ac": "add_task_url",
                "app_ver": "99.99.99.99",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        resp = self._request(
            PAN115_LIXIAN_SSP_URL,
            method="POST",
            data={"data": lixian_rsa_encrypt(payload)},
            headers={"User-Agent": PAN115_ANDROID_USER_AGENT},
        )
        self._ensure_state(resp, "115 cloud download submit failed")
        item = _cloud_task_item(resp)
        identity = _cloud_identity(item)
        if not identity["info_hash"] and not identity["task_id"]:
            item = self._find_cloud_task_by_source(url)
            identity = _cloud_identity(item)
        if not identity["info_hash"] and not identity["task_id"]:
            raise RuntimeError("115 cloud download did not return task identity")
        return {
            **identity,
            "file_id": p115_file_id(item),
            "parent_id": p115_parent_id(item),
            "file_name": p115_file_name(item),
            "status": normalize_cloud_status(item),
        }

    def cloud_download_status(self, identity: dict[str, Any]) -> dict[str, Any]:
        resp = self._request(
            PAN115_LIXIAN_WEB_URL,
            params={"ct": "lixian", "ac": "task_lists", "page": 1, "page_size": 30},
        )
        self._ensure_state(resp, "115 cloud download status failed")
        item = _cloud_task_item(resp, identity=identity)
        normalized_identity = _cloud_identity(item)
        return {
            **normalized_identity,
            "status": normalize_cloud_status(item),
            "raw_status": str(item.get("status", item.get("stat", item.get("state", "")))),
            "file_id": p115_file_id(item),
            "parent_id": p115_parent_id(item),
            "file_name": p115_file_name(item),
            "raw": item,
        }

    def _find_cloud_task_by_source(self, source_url: str) -> dict[str, Any]:
        resp = self._request(
            PAN115_LIXIAN_WEB_URL,
            params={"ct": "lixian", "ac": "task_lists", "page": 1, "page_size": 30},
        )
        self._ensure_state(resp, "115 cloud download task list failed")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        candidates = iter_items(data)
        source_hash = _cloud_source_hash(source_url)
        source_text = str(source_url or "").strip().lower()
        for candidate in candidates:
            candidate_hash = _cloud_identity(candidate)["info_hash"]
            candidate_url = str(candidate.get("url") or "").strip().lower()
            if source_hash and candidate_hash == source_hash:
                return dict(candidate)
            if candidate_url and candidate_url == source_text:
                return dict(candidate)
        return {}

    def cloud_download_output(self, identity: dict[str, Any], target_cid: str) -> dict[str, str]:
        status = self.cloud_download_status(identity)
        if status["status"] != "completed":
            raise RuntimeError(f"115 cloud download is not completed: {status['status']}")
        return self.resolve_cloud_download_output(status, target_cid)

    def resolve_cloud_download_output(self, status: dict[str, Any], target_cid: str) -> dict[str, str]:
        """Locate the media inside 115's cloud-download container and move it to target."""
        target = str(target_cid or "").strip()
        output_id = p115_file_id(status)
        output_name = p115_file_name(status)
        if not output_id:
            raise RuntimeError("115 cloud download completed without an output file id")
        if not target:
            raise RuntimeError("115 cloud download output target CID is empty")

        item = dict(status)
        try:
            children = self.list_files(output_id, limit=500)
        except P115RiskControlError:
            raise
        except Exception:
            LOG.debug("Cloud output is not a readable container id=%s", output_id, exc_info=True)
            children = []
        if children:
            normalized_name = normalize_text(output_name)
            matches = [
                child
                for child in children
                if normalized_name and normalize_text(p115_file_name(child)) == normalized_name
            ]
            if not matches and len(children) == 1:
                matches = children
            if len(matches) != 1:
                raise RuntimeError("115 cloud download output contains no unique media item")
            item = dict(matches[0])

        file_id = p115_item_id(item)
        parent_id = p115_item_parent_id(item) or p115_parent_id(status)
        file_name = p115_file_name(item) or output_name
        if not file_id or not file_name:
            raise RuntimeError("115 cloud download output is missing media identity")
        if parent_id != target:
            self.move_file(file_id, target)
            parent_id = target
        return {"file_id": file_id, "parent_id": parent_id, "file_name": file_name}

    def list_files(self, parent_id: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        resp = self._request(
            "https://webapi.115.com/files",
            params={"cid": str(parent_id), "limit": limit, "offset": max(0, int(offset)), "show_dir": 1, "fc_mix": 1},
        )
        self._ensure_state(resp, "115 list files failed")
        return iter_items(resp.get("data") or resp)

    def scan_organized_folders(
        self,
        parent_ids: set[str],
        category_names: set[str] | None = None,
        max_depth: int = 4,
        limit: int = 500,
        recognition: dict[str, Any] | None = None,
        share_name: str = "",
        excluded_parent_ids: set[str] | None = None,
        allowed_parent_ids: set[str] | None = None,
        max_list_calls: int = DEFAULT_ORGANIZED_SCAN_MAX_LIST_CALLS,
        scan_cursor: dict[str, Any] | None = None,
        return_scan_state: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        root_parent_ids = {str(parent_id).strip() for parent_id in parent_ids if str(parent_id).strip()}
        try:
            budget = max(1, min(8, int(max_list_calls)))
        except (TypeError, ValueError):
            budget = DEFAULT_ORGANIZED_SCAN_MAX_LIST_CALLS
        cursor = _organized_scan_cursor(root_parent_ids, scan_cursor)
        queue = list(cursor["queue"])
        seen = set(cursor["seen"])
        folders: list[dict[str, Any]] = []
        list_calls = 0
        while queue and list_calls < budget:
            node = queue.pop(0)
            parent_id = str(node["parent_id"])
            parts = list(node.get("parts") or [])
            depth = int(node.get("depth") or 0)
            offset = int(node.get("offset") or 0)
            if depth >= max_depth:
                continue
            list_calls += 1
            page_limit = max(1, min(int(limit), 500))
            page = self.list_files(parent_id, limit=page_limit, offset=offset)
            page_folders: list[dict[str, Any]] = []
            for item in page:
                if not p115_is_folder(item):
                    continue
                name = p115_file_name(item)
                file_id = p115_file_id(item)
                if not name or not file_id:
                    continue
                child_parts = parts + [name]
                folder = dict(item)
                folder["_category"] = infer_category_from_115_path(child_parts, category_names)
                page_folders.append(folder)
                if file_id not in seen:
                    seen.add(file_id)
                    queue.append(
                        {"parent_id": file_id, "parts": child_parts, "depth": depth + 1, "offset": 0}
                    )
            folders.extend(page_folders)
            if recognition is not None:
                selected = select_organized_115_folder(
                    page_folders,
                    recognition,
                    share_name,
                    excluded_parent_ids=excluded_parent_ids,
                    allowed_parent_ids=allowed_parent_ids or root_parent_ids,
                )
                if selected:
                    queue = []
                    break
            if len(page) >= page_limit:
                queue.insert(
                    0,
                    {"parent_id": parent_id, "parts": parts, "depth": depth, "offset": offset + len(page)},
                )

        next_cursor = None
        if queue:
            next_cursor = {
                "version": 1,
                "root_parent_ids": sorted(root_parent_ids),
                "queue": queue,
                "seen": sorted(seen),
            }
        if return_scan_state:
            return {
                "folders": folders,
                "organized_scan_cursor": next_cursor,
                "scan_complete": next_cursor is None,
                "list_request_count": list_calls,
            }
        return folders

    def find_source_residue_files(
        self,
        recognition: dict[str, Any],
        share_name: str,
        parent_ids: set[str],
        excluded_file_ids: set[str] | None = None,
        min_update_time: float = 0,
    ) -> list[dict[str, str]]:
        items: list[dict[str, Any]] = []
        for parent_id in parent_ids:
            parent_id = str(parent_id or "").strip()
            if parent_id:
                items.extend(self.list_files(parent_id, limit=100))
        return select_source_residue_115_files(
            items,
            recognition,
            share_name,
            excluded_file_ids=excluded_file_ids,
            min_update_time=min_update_time,
        )

    def find_organized_folder(
        self,
        recognition: dict[str, Any],
        share_name: str,
        excluded_parent_ids: set[str] | None = None,
        min_update_time: float = 0,
        scan_parent_ids: set[str] | None = None,
        category_names: set[str] | None = None,
        organized_scan_cursor: dict[str, Any] | None = None,
        max_requests: int = 8,
        return_scan_state: bool = False,
    ) -> dict[str, str] | dict[str, Any] | None:
        def result(folder: dict[str, str] | None, cursor: dict[str, Any] | None, complete: bool, requests: int):
            if return_scan_state:
                return {
                    "folder": folder,
                    "organized_scan_cursor": cursor,
                    "scan_complete": complete,
                    "request_count": requests,
                }
            return folder

        try:
            request_budget = max(1, min(8, int(max_requests)))
        except (TypeError, ValueError):
            request_budget = 8
        request_count = 0
        # A persisted queue is more valuable than repeating the same search index queries.
        has_scan_cursor = isinstance(organized_scan_cursor, dict) and bool(organized_scan_cursor.get("queue"))
        search_values = [] if has_scan_cursor else candidate_tokens(recognition, share_name)
        tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
        if tmdb_id and not has_scan_cursor:
            search_values.insert(0, tmdb_id)
        seen = set()
        items: list[dict[str, Any]] = []
        for value in search_values:
            if request_count >= request_budget:
                break
            value = str(value or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            items.extend(self.search_files(value, limit=20))
            request_count += 1
            selected = select_organized_115_folder(items, recognition, share_name, excluded_parent_ids=excluded_parent_ids)
            if selected:
                return result(selected, None, True, request_count)
        if scan_parent_ids and request_count < request_budget:
            scan_request_count_before = self.request_count
            try:
                scan_state = self.scan_organized_folders(
                    scan_parent_ids,
                    category_names=category_names,
                    recognition=recognition,
                    share_name=share_name,
                    excluded_parent_ids=excluded_parent_ids,
                    allowed_parent_ids=scan_parent_ids,
                    max_list_calls=request_budget - request_count,
                    scan_cursor=organized_scan_cursor,
                    return_scan_state=True,
                )
            except P115RiskControlError:
                raise
            except Exception:
                request_count += max(0, self.request_count - scan_request_count_before)
                LOG.debug("115 organized folder scan failed; falling back to search", exc_info=True)
                if isinstance(organized_scan_cursor, dict) and organized_scan_cursor.get("queue"):
                    return result(None, organized_scan_cursor, False, request_count)
            else:
                request_count += int(scan_state.get("list_request_count") or 0)
                scanned = scan_state.get("folders") or []
                selected = select_organized_115_folder(
                    scanned,
                    recognition,
                    share_name,
                    excluded_parent_ids=excluded_parent_ids,
                    allowed_parent_ids=scan_parent_ids,
                )
                if selected:
                    return result(selected, None, True, request_count)
                next_cursor = scan_state.get("organized_scan_cursor")
                if next_cursor:
                    return result(None, next_cursor, False, request_count)
        # If CMS/TMDB already identified the item, do not guess by year; wait for the exact TMDB folder.
        if tmdb_id:
            return result(None, None, True, request_count)
        year = extract_year_from_name(share_name)
        if year:
            fallback_items: list[dict[str, Any]] = []
            for value in (f"{year} tmdb", year):
                if request_count >= request_budget:
                    break
                if value in seen:
                    continue
                seen.add(value)
                fallback_items.extend(self.search_files(value, limit=20))
                request_count += 1
            return result(
                select_recent_tmdb_115_folder(
                    fallback_items,
                    year,
                    excluded_parent_ids=excluded_parent_ids,
                    min_update_time=min_update_time,
                ),
                None,
                True,
                request_count,
            )
        return result(None, None, True, request_count)

    def create_long_share(self, file_id: str, preferred_receive_code: str = "") -> dict[str, str]:
        resp = self._request(
            "https://webapi.115.com/share/send",
            method="POST",
            data={"file_ids": str(file_id), "ignore_warn": 0},
        )
        self._ensure_state(resp, "115 create share failed")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        share_code = str(resp.get("share_code") or data.get("share_code") or "").strip()
        receive_code = str(resp.get("receive_code") or data.get("receive_code") or "").strip()
        share_url = str(resp.get("share_url") or data.get("share_url") or "").strip()
        if not share_code and share_url:
            match = re.search(r"/s/([A-Za-z0-9]+)", share_url)
            share_code = match.group(1) if match else ""
        if not share_code:
            raise P115SharePendingError("115 create share did not return share_code")
        actual_receive_code = str(preferred_receive_code or receive_code or "1212").strip() or "1212"
        update = self._request(
            "https://webapi.115.com/share/updateshare",
            method="POST",
            data={
                "share_code": share_code,
                "receive_code": actual_receive_code,
                "share_duration": -1,
                "auto_fill_recvcode": 1,
            },
        )
        self._ensure_state(update, "115 update share failed")
        update_data = update.get("data") if isinstance(update.get("data"), dict) else {}
        share_update = update_data.get(share_code) if isinstance(update_data.get(share_code), dict) else {}
        returned_receive_code = str(
            update.get("receive_code")
            or update_data.get("receive_code")
            or share_update.get("receive_code")
            or ""
        ).strip()
        return {
            "share_code": share_code,
            "receive_code": returned_receive_code or actual_receive_code,
            "share_url": share_url,
        }

    def rename_file(self, file_id: str, file_name: str) -> dict:
        resp = self._request(
            "https://webapi.115.com/files/edit",
            method="POST",
            data={"fid": str(file_id), "file_name": str(file_name)},
        )
        return self._ensure_state(resp, "115 rename failed")

    def move_file(self, file_id: str, target_cid: str) -> dict:
        resp = self._request(
            "https://webapi.115.com/files/move",
            method="POST",
            data={"fid": str(file_id), "pid": str(target_cid)},
        )
        return self._ensure_state(resp, "115 move failed")

    def delete_file(self, file_id: str) -> dict:
        resp = self._request(
            "https://webapi.115.com/rb/delete",
            method="POST",
            data={"fid": str(file_id), "ignore_warn": 1},
        )
        return self._ensure_state(resp, "115 delete failed")
