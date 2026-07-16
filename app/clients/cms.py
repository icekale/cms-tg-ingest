from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.clients.http import HttpJson
from app.config import Config

LOG = logging.getLogger("cms-tg-ingest")


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

    def login(self) -> None:
        resp = self.http.request(
            f"{self.config.cms_base_url}/api/auth/login",
            method="POST",
            payload={"username": self.config.cms_username, "password": self.config.cms_password},
        )
        token = ((resp.get("data") or {}).get("token") or "").strip()
        if resp.get("code") != 200 or not token:
            raise RuntimeError(resp.get("msg") or "CMS login failed")
        self.token = token

    @staticmethod
    def _is_unauthorized_error(exc: RuntimeError) -> bool:
        text = str(exc).lower()
        return "401" in text or "unauthorized" in text

    def _authorized_request(self, path: str, payload: dict | None, method: str) -> dict:
        return self.http.request(
            f"{self.config.cms_base_url}{path}",
            method=method,
            payload=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def _authorized(self, path: str, payload: dict | None = None, method: str = "POST", params: dict | None = None) -> dict:
        if not self.token:
            self.login()
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        try:
            return self._authorized_request(path, payload, method)
        except RuntimeError as exc:
            if not self._is_unauthorized_error(exc):
                raise
            self.token = ""
            self.login()
            return self._authorized_request(path, payload, method)

    def add_share_down(self, url: str) -> dict:
        resp = self._authorized("/api/cloud/add_share_down", payload={"url": url})
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS rejected the share link")
        return resp

    def list_share_down(self, page_size: int = 20) -> list[dict]:
        resp = self._authorized("/api/share_down/list", method="GET", params={"page": 1, "page_size": page_size})
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS share_down list failed")
        return iter_items(resp.get("data"))

    def get_share_down_detail(self, task_id: str) -> dict:
        try:
            for item in self.list_share_down(page_size=50):
                item_id = item.get("id") or item.get("task_id") or item.get("taskId")
                if str(item_id) == str(task_id):
                    return item
        except Exception as exc:
            LOG.debug("CMS status probe failed error=%s", exc)
        return {"status": "unknown"}

    def get_share_down_by_key(self, key: Any) -> dict:
        for item in self.list_share_down(page_size=100):
            if str(item.get("share_id") or "").lower() == key.share_code and str(item.get("share_pwd") or "") == key.receive_code:
                return item
        return {}

    def recognize_media(self, path: str) -> dict:
        resp = self._authorized("/api/media/file_recognize", payload={"path": path})
        return resp

    def run_auto_organize(self) -> dict:
        resp = self._authorized("/api/sync/auto_organize", method="GET")
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
