from __future__ import annotations

import re
from typing import Any, Callable

from app.clients.p115 import p115_file_name, p115_is_folder, p115_item_id, p115_item_parent_id

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


INCOMPLETE = "incomplete"
CONFLICT = "conflict"


def dest_id_from_file_hits(
    *,
    file_hits: list[dict[str, Any]],
    folder_hits: list[dict[str, Any]],
    expected_ids: list[str],
) -> str:
    folders = {p115_item_id(item): item for item in folder_hits if p115_item_id(item)}
    found: dict[str, str] = {}
    for item in file_hits:
        file_id = p115_item_id(item)
        if file_id not in {str(value) for value in expected_ids}:
            continue
        parent_id = p115_item_parent_id(item)
        parent = folders.get(parent_id) or {}
        parent_name = p115_file_name(parent)
        if is_season_folder_name(parent_name):
            dest_id = str(parent.get("pid") or parent.get("parent_id") or "").strip()
        else:
            dest_id = parent_id
        if dest_id:
            found[file_id] = dest_id
    expected = [str(value) for value in expected_ids if str(value)]
    if not expected or any(file_id not in found for file_id in expected):
        return INCOMPLETE
    dests = set(found.values())
    if len(dests) != 1:
        return CONFLICT
    return dests.pop()
