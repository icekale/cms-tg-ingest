from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

from app.clients.http import HttpJson
from app.config import is_relative_to, safe_resolve


def extract_tmdb_id_from_name(value: str) -> str:
    match = re.search(r"tmdb(?:id)?[=_\-](\d+)", str(value or ""), re.I)
    return match.group(1) if match else ""


def item_tmdb_id(item: dict[str, Any]) -> str:
    provider_ids = item.get("ProviderIds") or item.get("ProviderIDs") or {}
    tmdb_id = str(provider_ids.get("Tmdb") or provider_ids.get("TMDB") or "").strip()
    if tmdb_id:
        return tmdb_id
    return extract_tmdb_id_from_name(" ".join(str(item.get(k) or "") for k in ("Path", "Name", "OriginalTitle")))


class EmbyClient:
    def __init__(self, base_url: str, api_key: str, user_id: str = "", http: HttpJson | None = None, timeout: int = 60):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.user_id = user_id or ""
        self.http = http or HttpJson(timeout)
        self._library_roots: list[tuple[Path, str]] | None = None
        self._library_entries_cache: list[dict[str, Any]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        return self._request("GET", path, params=params)

    def _post(self, path: str, params: dict | None = None, payload: dict | None = None) -> dict | list:
        return self._request("POST", path, params=params, payload=payload)

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        payload: dict | None = None,
    ) -> dict | list:
        if not self.enabled:
            raise RuntimeError("Emby confirmation is disabled")
        params = dict(params or {})
        params["api_key"] = self.api_key
        url = self.base_url + path + "?" + urllib.parse.urlencode(params)
        return self.http.request(url, method=method, payload=payload)

    def get_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        users = self._get("/Users")
        if isinstance(users, list) and users:
            self.user_id = str(users[0].get("Id") or "")
        if not self.user_id:
            raise RuntimeError("Cannot determine Emby user id")
        return self.user_id

    def recent_items(self, limit: int = 20) -> list[dict]:
        user_id = self.get_user_id()
        resp = self._get(
            f"/Users/{user_id}/Items",
            {
                "Recursive": "true",
                "Limit": str(limit),
                "Fields": "Path,ProviderIds,DateCreated,MediaSources,ParentId,Overview",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
            },
        )
        if isinstance(resp, dict):
            return [item for item in resp.get("Items") or [] if isinstance(item, dict)]
        return []

    def find_item_by_tmdb(self, tmdb_id: str) -> dict | None:
        tmdb_id = str(tmdb_id or "").strip()
        if not tmdb_id:
            return None
        user_id = self.get_user_id()
        resp = self._get(
            f"/Users/{user_id}/Items",
            {
                "Recursive": "true",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "Path,ProviderIds,ParentId,MediaSources",
                "Limit": "10",
            },
        )
        items = [item for item in (resp.get("Items") if isinstance(resp, dict) else []) or [] if isinstance(item, dict)]
        for item in items:
            if item_tmdb_id(item) == tmdb_id:
                return item
        return None

    def library_roots(self) -> list[tuple[Path, str]]:
        if self._library_roots is not None:
            return self._library_roots
        entries = self._library_entries()
        self._library_roots = [(entry["path"], entry["name"]) for entry in entries]
        return self._library_roots

    def _library_entries(self) -> list[dict[str, Any]]:
        if self._library_entries_cache is not None:
            return self._library_entries_cache
        if self._library_roots is not None:
            return [{"path": root, "name": name, "item_id": ""} for root, name in self._library_roots]
        resp = self._get("/Library/VirtualFolders/Query")
        items = resp.get("Items") if isinstance(resp, dict) else resp
        entries: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("Name") or "").strip()
                if not name:
                    continue
                item_id = str(item.get("ItemId") or item.get("Id") or "").strip()
                raw_paths: list[str] = []
                for value in item.get("Locations") or []:
                    if value:
                        raw_paths.append(str(value))
                path_infos = (item.get("LibraryOptions") or {}).get("PathInfos") or []
                for info in path_infos:
                    if isinstance(info, dict) and info.get("Path"):
                        raw_paths.append(str(info.get("Path")))
                for raw_path in raw_paths:
                    entries.append({"path": safe_resolve(Path(raw_path)), "name": name, "item_id": item_id})
        entries.sort(key=lambda entry: len(entry["path"].parts), reverse=True)
        self._library_entries_cache = entries
        self._library_roots = [(entry["path"], entry["name"]) for entry in entries]
        return entries

    def _library_entry_for_path(self, item_path: str | Path) -> dict[str, Any] | None:
        raw_path = str(item_path or "").strip()
        if not raw_path:
            return None
        resolved = safe_resolve(Path(raw_path))
        for entry in self._library_entries():
            if is_relative_to(resolved, entry["path"]):
                return entry
        return None

    def refresh_library_for_path(self, item_path: str | Path) -> str | None:
        entry = self._library_entry_for_path(item_path)
        if entry and entry.get("item_id"):
            item_id = urllib.parse.quote(str(entry["item_id"]), safe="")
            self._post(
                f"/Items/{item_id}/Refresh",
                {
                    "Recursive": "true",
                    "ImageRefreshMode": "Default",
                    "MetadataRefreshMode": "Default",
                },
            )
            return str(entry.get("name") or "") or None
        self._post("/Library/Refresh")
        return str((entry or {}).get("name") or "") or None

    def library_name_for_item(self, item: dict) -> str | None:
        raw_path = str(item.get("Path") or "").strip()
        if not raw_path:
            return None
        item_path = safe_resolve(Path(raw_path))
        for root, name in self.library_roots():
            if is_relative_to(item_path, root):
                return name
        return None
