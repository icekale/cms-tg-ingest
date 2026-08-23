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
        children = list_files(file_id, limit=500)
        for item in children:
            child_id = p115_item_id(item)
            child_name = p115_file_name(item)
            if p115_is_folder(item) and is_season_folder_name(child_name):
                episodes = list_files(child_id, limit=500)
                for episode in episodes:
                    add(p115_item_id(episode), p115_file_name(episode))
                continue
            add(child_id, child_name)
    return files


INCOMPLETE = "incomplete"
CONFLICT = "conflict"


def dest_file_ids_from_hits(
    *,
    file_hits: list[dict[str, Any]],
    folder_hits: list[dict[str, Any]],
    expected_ids: list[str],
) -> dict[str, list[str]] | None:
    expected = {
        str(value).strip()
        for value in expected_ids
        if value is not None and str(value).strip()
    }
    folders: dict[str, dict[str, Any]] = {}
    folder_identities: dict[str, tuple[str, str]] = {}
    for item in folder_hits:
        folder_id = p115_item_id(item)
        if not folder_id:
            continue
        identity = (p115_item_parent_id(item), p115_file_name(item))
        if folder_id in folder_identities and folder_identities[folder_id] != identity:
            return None
        folder_identities[folder_id] = identity
        folders.setdefault(folder_id, item)
    by_file: dict[str, set[str]] = {}
    for item in file_hits:
        file_id = p115_item_id(item)
        if file_id not in expected:
            continue
        parent_id = p115_item_parent_id(item)
        parent = folders.get(parent_id) or {}
        if is_season_folder_name(p115_file_name(parent)):
            dest_id = p115_item_parent_id(parent)
        else:
            dest_id = parent_id
        if dest_id:
            by_file.setdefault(file_id, set()).add(dest_id)
    if set(by_file) != expected:
        return {}
    if any(len(destinations) != 1 for destinations in by_file.values()):
        return None
    grouped: dict[str, list[str]] = {}
    for file_id, destinations in by_file.items():
        dest_id = next(iter(destinations))
        grouped.setdefault(dest_id, []).append(file_id)
    return {
        dest_id: sorted(file_ids)
        for dest_id, file_ids in sorted(grouped.items())
    }


def dest_id_from_file_hits(
    *,
    file_hits: list[dict[str, Any]],
    folder_hits: list[dict[str, Any]],
    expected_ids: list[str],
) -> str:
    grouped = dest_file_ids_from_hits(
        file_hits=file_hits,
        folder_hits=folder_hits,
        expected_ids=expected_ids,
    )
    if grouped is None:
        return CONFLICT
    if not grouped:
        return INCOMPLETE
    if len(grouped) != 1:
        return CONFLICT
    return next(iter(grouped))


def collect_file_ids_under_dest(dest_id: str, list_files: ListFiles) -> set[str]:
    dest_id = str(dest_id or "").strip()
    found: set[str] = set()
    if not dest_id:
        return found
    children = list_files(dest_id, limit=500)
    for item in children:
        item_id = p115_item_id(item)
        if item_id:
            found.add(item_id)
        if p115_is_folder(item) and is_season_folder_name(p115_file_name(item)):
            for episode in list_files(item_id, limit=500):
                episode_id = p115_item_id(episode)
                if episode_id:
                    found.add(episode_id)
    return found


def cleanup_root_action(
    *,
    root_id: str,
    parent_id: str,
    dest_id: str,
    cleanup_parents: set[str],
) -> str:
    root_id = str(root_id or "").strip()
    parent_id = str(parent_id or "").strip()
    dest_id = str(dest_id or "").strip()
    if not root_id or root_id == dest_id or not parent_id:
        return "skip"
    if parent_id in {str(value) for value in cleanup_parents if str(value)}:
        return "delete"
    return "needs_action"
