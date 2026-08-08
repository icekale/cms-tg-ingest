from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.clients.http import HttpJson
from app.config import Config

LOG = logging.getLogger("cms-tg-ingest")

# Upper bound for paginated share_down scans. Each page is bounded (50/100
# rows), and the loop stops early on a short page or a repeated first item,
# so 20 pages keeps the legacy CMS-status polling cheap while covering
# share histories far beyond the previous single-page window.
_SHARE_DOWN_MAX_PAGES = 20


class CmsSharePlaybackUnavailableError(RuntimeError):
    pass


def normalize_strm_url(url: str) -> str:
    parts = urllib.parse.urlsplit(str(url))
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            urllib.parse.quote(parts.path, safe="/%:@"),
            urllib.parse.quote(parts.query, safe="=&/?%:@"),
            urllib.parse.quote(parts.fragment, safe="/%:@"),
        )
    )


def iter_items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "records", "data", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


class CmsClient:
    def __init__(self, config: Config, http: HttpJson | None = None):
        self.config = config
        self.http = http or HttpJson(config.http_timeout)
        self.token = ""
        self._cached_version = ""

    def login(self) -> None:
        resp = self.http.request(
            f"{self.config.cms_base_url}/api/auth/login",
            method="POST",
            payload={"username": self.config.cms_username, "password": self.config.cms_password},
        )
        token = ((resp.get("data") or {}).get("token") or "").strip()
        if resp.get("code") != 200 or not token:
            raise RuntimeError(resp.get("msg") or "CMS login failed")
        self._cached_version = str((resp.get("data") or {}).get("version") or "").strip()
        self.token = token

    @staticmethod
    def _is_unauthorized_error(exc: RuntimeError) -> bool:
        text = str(exc).lower()
        return "401" in text or "unauthorized" in text

    def _authorized_request(
        self,
        path: str,
        payload: dict | None,
        method: str,
        safe_get_attempts: int | None = None,
    ) -> dict:
        if safe_get_attempts is not None:
            return self.http.request(
                f"{self.config.cms_base_url}{path}",
                method=method,
                payload=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                safe_get_attempts=safe_get_attempts,
            )
        return self.http.request(
            f"{self.config.cms_base_url}{path}",
            method=method,
            payload=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def _authorized(
        self,
        path: str,
        payload: dict | None = None,
        method: str = "POST",
        params: dict | None = None,
        safe_get_attempts: int | None = None,
    ) -> dict:
        if not self.token:
            self.login()
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        try:
            return self._authorized_request(path, payload, method, safe_get_attempts)
        except RuntimeError as exc:
            if not self._is_unauthorized_error(exc):
                raise
            self.token = ""
            self.login()
            return self._authorized_request(path, payload, method, safe_get_attempts)

    def add_share_down(self, url: str) -> dict:
        resp = self._authorized("/api/cloud/add_share_down", payload={"url": url})
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS rejected the share link")
        return resp

    def get_hdhive_info(self) -> dict:
        return self._authorized("/api/hdhive/info", method="GET")

    def search_movie(self, keyword: str, page: int = 1, page_size: int = 8) -> dict:
        return self._authorized(
            "/api/tmdb/search_movie",
            method="GET",
            params={"keyword": keyword, "page": page, "page_size": page_size},
        )

    def search_tv(self, keyword: str, page: int = 1, page_size: int = 8) -> dict:
        return self._authorized(
            "/api/tmdb/search_tv",
            method="GET",
            params={"keyword": keyword, "page": page, "page_size": page_size},
        )

    def list_share_down(self, page: int = 1, page_size: int = 20) -> list[dict]:
        resp = self._authorized(
            "/api/share_down/list",
            method="GET",
            params={"page": max(1, int(page)), "page_size": max(1, int(page_size))},
        )
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS share_down list failed")
        return iter_items(resp.get("data"))

    def get_share_down_detail(self, task_id: str) -> dict:
        try:
            previous_first_id: str | None = None
            for page in range(1, _SHARE_DOWN_MAX_PAGES + 1):
                page_items = self.list_share_down(page=page, page_size=50)
                for item in page_items:
                    item_id = item.get("id") or item.get("task_id") or item.get("taskId")
                    if str(item_id) == str(task_id):
                        return item
                if len(page_items) < 50:
                    break
                first_id = str(
                    page_items[0].get("id") or page_items[0].get("task_id") or page_items[0].get("taskId") or ""
                )
                # A CMS that ignores the page parameter returns the same first
                # item on every page; stop instead of re-scanning identical pages.
                if previous_first_id is not None and first_id == previous_first_id:
                    break
                previous_first_id = first_id
        except Exception as exc:
            LOG.debug("CMS status probe failed error=%s", exc)
        return {"status": "unknown"}

    def get_share_down_by_key(self, key: Any) -> dict:
        matches: list[dict] = []
        previous_first_key: str | None = None
        for page in range(1, _SHARE_DOWN_MAX_PAGES + 1):
            page_items = self.list_share_down(page=page, page_size=100)
            matches.extend(
                item
                for item in page_items
                if str(item.get("share_id") or "").lower() == key.share_code
                and str(item.get("share_pwd") or "") == key.receive_code
            )
            if matches or len(page_items) < 100:
                break
            first_key = str(page_items[0].get("share_id") or "") if page_items else ""
            if previous_first_key is not None and first_key == previous_first_key:
                break
            previous_first_key = first_key
        if not matches:
            return {}
        for item in matches:
            if str(item.get("status") or "").strip().lower() not in {"2", "failed", "error"}:
                return item
        return matches[0]

    def recognize_media(self, path: str) -> dict:
        resp = self._authorized("/api/media/file_recognize", payload={"path": path})
        return resp

    def run_auto_organize(self) -> dict:
        resp = self._authorized("/api/sync/auto_organize", method="GET", safe_get_attempts=1)
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS auto organize failed")
        return resp

    def add_share115_sync_task(self, share_code: str, receive_code: str, cid: str = "0", local_path: str = "/media/share") -> dict:
        resp = self._authorized(
            "/api/sync/share115",
            payload={
                "share_code": share_code,
                "receive_code": receive_code,
                "cid": cid,
                "local_path": local_path,
            },
        )
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS share115 sync failed")
        return resp

    def probe_strm_url(self, url: str) -> bool:
        request = urllib.request.Request(
            normalize_strm_url(url),
            headers={"Range": "bytes=0-0", "User-Agent": "cms-tg-ingest/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.http_timeout) as response:
                return int(getattr(response, "status", response.getcode())) in {200, 206}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            exc.close()
            if exc.code == 500 and "获取分享直连失败" in body:
                raise CmsSharePlaybackUnavailableError("CMS 获取分享直连失败") from exc
            raise

    def auto_organize_excluded_parent_ids(self) -> set[str]:
        resp = self._authorized("/api/config/auto_organize", method="GET")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        return {
            str(data.get(key) or "").strip()
            for key in ("NEW_MEDIA_CID", "REDUNDANT_DATA_CID", "NEW_MEDIA_EXISTS_CID", "NEW_MEDIA_FAILED_CID")
            if str(data.get(key) or "").strip()
        }

    def auto_organize_existing_parent_ids(self) -> set[str]:
        resp = self._authorized("/api/config/auto_organize", method="GET")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        value = str(data.get("NEW_MEDIA_EXISTS_CID") or "").strip()
        return {value} if value else set()

    def healthcheck(self) -> bool:
        try:
            self.list_share_down(page_size=1)
        except Exception:
            return False
        return True

    def get_version(self) -> str:
        """Best-effort CMS version detection, preferring the login response."""
        try:
            self.login()
        except Exception:
            pass
        if self._cached_version:
            return self._cached_version
        for path in ("/api/version", "/api/app/version", "/api/system/version"):
            try:
                resp = self._authorized(path, method="GET", safe_get_attempts=1)
            except Exception:
                continue
            if not isinstance(resp, dict):
                continue
            data = resp.get("data")
            if isinstance(data, dict):
                candidate = data.get("version") or data.get("ver") or data.get("current_version")
            else:
                candidate = resp.get("version") or resp.get("ver")
            value = str(candidate or "").strip()
            if value:
                return value
        return ""
