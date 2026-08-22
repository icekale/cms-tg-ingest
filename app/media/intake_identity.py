from __future__ import annotations

import re
from typing import Any, Callable

from app.clients.p115 import p115_file_name, p115_is_folder, p115_item_id

VIDEO_SUFFIXES = (".mkv", ".mp4", ".ts", ".iso", ".avi", ".mov", ".wmv", ".m2ts")
_SEASON_NAME = re.compile(r"(?i)^(season\s*\d+|第.+季)$")

ListFiles = Callable[..., list[dict[str, Any]]]


def is_video_name(name: str) -> bool:
    return str(name or "").strip().lower().endswith(VIDEO_SUFFIXES)


def is_season_folder_name(name: str) -> bool:
    return bool(_SEASON_NAME.match(str(name or "").strip()))


def snapshot_files(roots: list[dict[str, Any]], list_files: ListFiles) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(file_id: str, name: str) -> None:
        file_id = str(file_id or "").strip()
        name = str(name or "").strip()
        if not file_id or not is_video_name(name) or file_id in seen:
            return
        seen.add(file_id)
        files.append({"id": file_id, "name": name})

    for root in roots:
        file_id = str(root.get("file_id") or "").strip()
        name = str(root.get("file_name") or "").strip()
        if not file_id:
            continue
        if not root.get("is_folder"):
            add(file_id, name)
            continue
        try:
            children = list_files(file_id, limit=500)
        except Exception:
            continue
        for item in children:
            child_id = p115_item_id(item)
            child_name = p115_file_name(item)
            if p115_is_folder(item) and is_season_folder_name(child_name):
                try:
                    episodes = list_files(child_id, limit=500)
                except Exception:
                    continue
                for episode in episodes:
                    add(p115_item_id(episode), p115_file_name(episode))
                continue
            add(child_id, child_name)
    return files
