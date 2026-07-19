from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.clients.http import FormHttp, load_cookie_value
from app.config import default_library_roots
from app.media.classify import candidate_tokens, extract_tmdb_id_from_name, extract_year_from_name, normalize_text

LOG = logging.getLogger("cms-tg-ingest")
CMS_PARENT_CID_CATEGORY_MAP: dict[str, str] = {}
DEFAULT_ORGANIZED_SCAN_MAX_LIST_CALLS = 80


class P115RiskControlError(RuntimeError):
    """Raised when 115 asks callers to slow down or stops automated actions."""


class P115ShareUnavailableError(RuntimeError):
    """Raised when 115 confirms that a share no longer exists or is invalid."""


def is_p115_risk_control_message(value: str) -> bool:
    text = str(value or "")
    return any(
        token in text
        for token in (
            "限制接收",
            "被限制接收",
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
        for key in ("list", "items", "records", "data", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def p115_file_id(item: dict[str, Any]) -> str:
    return str(item.get("cid") or item.get("fid") or item.get("file_id") or "").strip()


def p115_parent_id(item: dict[str, Any]) -> str:
    return str(item.get("pid") or item.get("parent_id") or "").strip()


def p115_residue_file_id(item: dict[str, Any]) -> str:
    return str(item.get("fid") or item.get("file_id") or item.get("cid") or "").strip()


def p115_residue_parent_id(item: dict[str, Any]) -> str:
    return str(item.get("cid") or item.get("pid") or item.get("parent_id") or "").strip()


def normalize_cloud_status(item: dict[str, Any]) -> str:
    raw = item.get("status", item.get("stat", item.get("state", "")))
    value = str(raw).strip().lower()
    if value in {"11", "completed", "complete", "success", "succeeded", "done"}:
        return "completed"
    if value in {"12", "running", "downloading", "queued", "pending", "wait"}:
        return "running"
    if value in {"9", "failed", "failure", "error", "cancelled", "canceled"}:
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
        clock: Any | None = None,
        sleeper: Any | None = None,
    ):
        self.cookie = load_cookie_value(cookie)
        self.http = http or FormHttp(timeout)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds or 0.0))
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self._last_request_at: float | None = None
        self._request_lock = threading.Lock()
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

    def _request(self, url: str, method: str = "GET", data: dict | None = None, params: dict | None = None) -> dict:
        with self._request_lock:
            self._rate_limit()
            self.request_count += 1
            return self.http.request(url, method=method, data=data, params=params, headers=self._headers())

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

    def share_snap(self, share_code: str, receive_code: str, cid: str = "0", limit: int = 100) -> dict[str, Any]:
        resp = self._request(
            "https://webapi.115.com/share/snap",
            params={
                "share_code": share_code,
                "receive_code": receive_code,
                "cid": cid,
                "offset": 0,
                "limit": limit,
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

    def receive_share_to_cid(self, share_code: str, receive_code: str, target_cid: str) -> dict[str, Any]:
        snap = self.share_snap(share_code, receive_code, cid="0", limit=100)
        data = snap.get("data") if isinstance(snap.get("data"), dict) else {}
        items = iter_items(data.get("list") or data)
        file_ids = [str(item.get("fid") or item.get("cid") or item.get("file_id") or "").strip() for item in items]
        file_ids = [file_id for file_id in file_ids if file_id]
        if not file_ids:
            raise RuntimeError("115 share snap did not return file ids")
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
        info = data.get("shareinfo") if isinstance(data.get("shareinfo"), dict) else {}
        receive_data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        title = str(receive_data.get("receive_title") or info.get("share_title") or (items[0].get("n") if items else "") or "").strip()
        return {"title": title, "file_ids": file_ids, "response": resp}

    def cloud_download_add(self, url: str, target_cid: str) -> dict[str, str]:
        resp = self._request(
            "https://clouddownload.115.com/lixianssp/?ac=add_task_url",
            method="POST",
            data={"url": str(url), "wp_path_id": str(target_cid), "savepath": ""},
        )
        self._ensure_state(resp, "115 cloud download submit failed")
        item = _cloud_task_item(resp)
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
        info_hash = str(identity.get("info_hash") or "").strip()
        if info_hash:
            resp = self._request(
                "https://clouddownload.115.com/?ac=get_user_task",
                params={"info_hash": info_hash},
            )
        else:
            resp = self._request(
                "https://clouddownload.115.com/?ac=task_lists",
                params={"page": 1, "page_size": 30},
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

    def cloud_download_output(self, identity: dict[str, Any], target_cid: str) -> dict[str, str]:
        status = self.cloud_download_status(identity)
        if status["status"] != "completed":
            raise RuntimeError(f"115 cloud download is not completed: {status['status']}")
        return validate_cloud_output(status, target_cid)

    def list_files(self, parent_id: str, limit: int = 100) -> list[dict[str, Any]]:
        resp = self._request(
            "https://webapi.115.com/files",
            params={"cid": str(parent_id), "limit": limit, "offset": 0, "show_dir": 1, "fc_mix": 1},
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
    ) -> list[dict[str, Any]]:
        root_parent_ids = {str(parent_id) for parent_id in parent_ids if str(parent_id)}
        queue: list[tuple[str, list[str], int]] = [(parent_id, [], 0) for parent_id in root_parent_ids]
        seen: set[str] = set()
        folders: list[dict[str, Any]] = []
        list_calls = 0
        while queue:
            batch: list[tuple[str, list[str], int]] = []
            while queue:
                parent_id, parts, depth = queue.pop(0)
                if parent_id in seen or depth >= max_depth:
                    continue
                seen.add(parent_id)
                batch.append((parent_id, parts, depth))
            if not batch:
                break
            level_folders: list[dict[str, Any]] = []
            for parent_id, parts, depth in batch:
                if max_list_calls > 0 and list_calls >= max_list_calls:
                    folders.extend(level_folders)
                    return folders
                list_calls += 1
                for item in self.list_files(parent_id, limit=limit):
                    if not p115_is_folder(item):
                        continue
                    name = p115_file_name(item)
                    file_id = p115_file_id(item)
                    child_parts = parts + [name]
                    folder = dict(item)
                    folder["_category"] = infer_category_from_115_path(child_parts, category_names)
                    level_folders.append(folder)
                    queue.append((file_id, child_parts, depth + 1))
            folders.extend(level_folders)
            if recognition is not None:
                selected = select_organized_115_folder(
                    level_folders,
                    recognition,
                    share_name,
                    excluded_parent_ids=excluded_parent_ids,
                    allowed_parent_ids=allowed_parent_ids or root_parent_ids,
                )
                if selected:
                    return level_folders
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
    ) -> dict[str, str] | None:
        search_values = candidate_tokens(recognition, share_name)
        tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
        if tmdb_id:
            search_values.insert(0, tmdb_id)
        seen = set()
        items: list[dict[str, Any]] = []
        for value in search_values:
            value = str(value or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            items.extend(self.search_files(value, limit=20))
            selected = select_organized_115_folder(items, recognition, share_name, excluded_parent_ids=excluded_parent_ids)
            if selected:
                return selected
        if scan_parent_ids:
            try:
                scanned = self.scan_organized_folders(
                    scan_parent_ids,
                    category_names=category_names,
                    recognition=recognition,
                    share_name=share_name,
                    excluded_parent_ids=excluded_parent_ids,
                    allowed_parent_ids=scan_parent_ids,
                )
            except Exception:
                LOG.debug("115 organized folder scan failed; falling back to search", exc_info=True)
            else:
                selected = select_organized_115_folder(
                    scanned,
                    recognition,
                    share_name,
                    excluded_parent_ids=excluded_parent_ids,
                    allowed_parent_ids=scan_parent_ids,
                )
                if selected:
                    return selected
        # If CMS/TMDB already identified the item, do not guess by year; wait for the exact TMDB folder.
        if tmdb_id:
            return None
        year = extract_year_from_name(share_name)
        if year:
            fallback_items: list[dict[str, Any]] = []
            for value in (f"{year} tmdb", year):
                if value in seen:
                    continue
                seen.add(value)
                fallback_items.extend(self.search_files(value, limit=20))
            return select_recent_tmdb_115_folder(fallback_items, year, excluded_parent_ids=excluded_parent_ids, min_update_time=min_update_time)
        return None

    def create_long_share(self, file_id: str) -> dict[str, str]:
        resp = self._request(
            "https://webapi.115.com/share/send",
            method="POST",
            data={"file_ids": str(file_id), "ignore_warn": 1},
        )
        self._ensure_state(resp, "115 create share failed")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        share_code = str(resp.get("share_code") or data.get("share_code") or "").strip()
        receive_code = str(resp.get("receive_code") or data.get("receive_code") or "").strip()
        share_url = str(resp.get("share_url") or data.get("share_url") or "").strip()
        if not share_code:
            raise RuntimeError("115 create share did not return share_code")
        update = self._request(
            "https://webapi.115.com/share/updateshare",
            method="POST",
            data={
                "share_code": share_code,
                "receive_code": receive_code or "1212",
                "share_duration": -1,
                "auto_fill_recvcode": 1,
            },
        )
        self._ensure_state(update, "115 update share failed")
        return {"share_code": share_code, "receive_code": receive_code or "1212", "share_url": share_url}

    def rename_file(self, file_id: str, file_name: str) -> dict:
        resp = self._request(
            "https://webapi.115.com/files/edit",
            method="POST",
            data={"fid": str(file_id), "file_name": str(file_name)},
        )
        return self._ensure_state(resp, "115 rename failed")

    def delete_file(self, file_id: str) -> dict:
        resp = self._request(
            "https://webapi.115.com/rb/delete",
            method="POST",
            data={"fid": str(file_id), "ignore_warn": 1},
        )
        return self._ensure_state(resp, "115 delete failed")
