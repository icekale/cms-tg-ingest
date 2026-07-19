from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from app.config import MoveConfig, MovePlan, SelfShareConfig, is_relative_to, is_under_any_root, safe_resolve
from app.media.classify import (
    candidate_tokens,
    expected_task_tmdb_id,
    extract_tmdb_id_from_name,
    final_category_for_move,
    media_type_for_category,
    normalize_text,
    parse_recognition_json,
)

MISSING_SELF_SHARE_SOURCE_REASONS = {"STRM 源目录不存在", "源目录不包含 STRM 文件", "未找到 STRM 源目录"}


class UnsafeMediaPathError(ValueError):
    pass


def category_for_self_share_row(row: dict[str, Any]) -> str:
    for key in ("category_final", "category_choice"):
        category = str(row.get(key) or "").strip()
        if category:
            return category
    return final_category_for_move(row, parse_recognition_json(row))


def iter_strm_files(path: Path, allowed_roots: Iterable[str | Path] | None = None):
    roots = tuple(safe_resolve(Path(root)) for root in allowed_roots) if allowed_roots is not None else None
    if roots is not None and not is_under_any_root(path, list(roots)):
        raise UnsafeMediaPathError(f"path outside allowed roots: {path}")
    try:
        for child in path.rglob("*"):
            if roots is not None and not is_under_any_root(child, list(roots)):
                raise UnsafeMediaPathError(f"path outside allowed roots: {child}")
            if child.is_file() and child.suffix.lower() == ".strm":
                yield child
    except OSError:
        return


def has_strm_file(path: Path) -> bool:
    return any(iter_strm_files(path))


def newest_mtime(path: Path) -> float:
    newest = 0.0
    try:
        newest = path.stat().st_mtime
        for child in path.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


def is_directory_stable(path: Path, stable_seconds: int) -> bool:
    if stable_seconds <= 0:
        return True
    mtime = newest_mtime(path)
    return bool(mtime and time.time() - mtime >= stable_seconds)


def directory_stability_metadata(path: Path, stable_seconds: int) -> dict[str, float]:
    mtime = newest_mtime(path)
    age = max(0.0, time.time() - mtime) if mtime else 0.0
    remaining = max(0.0, float(stable_seconds) - age)
    return {
        "newest_mtime": mtime,
        "stable_age_seconds": round(age, 3),
        "stable_required_seconds": float(max(0, int(stable_seconds))),
        "stable_remaining_seconds": round(remaining, 3),
    }


def destination_for_category(category: str, media_dir_name: str, config: MoveConfig) -> Path | None:
    root = config.library_roots.get(category)
    if not root:
        return None
    return safe_resolve(root / media_dir_name)


def library_category_for_path(path: Path | None, config: MoveConfig) -> str:
    if not path:
        return ""
    for category, root in config.library_roots.items():
        if is_relative_to(path, root):
            return category
    return ""


def library_media_root_for_path(path: Path, config: MoveConfig) -> tuple[Path, str] | None:
    resolved = safe_resolve(path)
    for category, root in config.library_roots.items():
        root = safe_resolve(root)
        if not is_relative_to(resolved, root):
            continue
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if not rel.parts:
            return None
        return safe_resolve(root / rel.parts[0]), category
    return None


def find_strm_source_dir(config: MoveConfig, recognition: dict[str, Any], share_name: str = "") -> Path | None:
    tokens = candidate_tokens(recognition, share_name)
    if not tokens:
        return None
    matches: list[tuple[int, int, float, Path]] = []
    for root in config.source_roots:
        root = safe_resolve(root)
        if not root.exists():
            continue
        try:
            dirs = [p for p in root.rglob("*") if p.is_dir()]
        except OSError:
            continue
        for path in dirs:
            name_norm = normalize_text(path.name)
            full_norm = normalize_text(str(path))
            name_match = any(token in name_norm for token in tokens)
            full_match = any(token in full_norm for token in tokens)
            if not name_match and not full_match:
                continue
            if not has_strm_file(path):
                continue
            score = 2 if name_match else 1
            depth = -len(path.relative_to(root).parts)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            matches.append((score, depth, mtime, path))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return safe_resolve(matches[0][3])


def find_recent_library_strm_source_dir(
    config: MoveConfig,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str = "",
) -> tuple[Path, str] | None:
    try:
        since = float(row.get("created_at") or row.get("updated_at") or 0) - 60
    except (TypeError, ValueError):
        since = 0
    tokens = candidate_tokens(recognition, share_name)
    candidates: dict[Path, tuple[str, float, bool]] = {}
    for root in config.library_roots.values():
        root = safe_resolve(root)
        if not root.exists():
            continue
        try:
            dirs = [p for p in root.rglob("*") if p.is_dir()]
        except OSError:
            continue
        for path in dirs:
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            media = library_media_root_for_path(path, config)
            if not media:
                continue
            media_root, category = media
            if not has_strm_file(media_root):
                continue
            mtime = newest_mtime(media_root)
            name_norm = normalize_text(str(media_root))
            token_match = bool(tokens and any(token in name_norm for token in tokens))
            old = candidates.get(media_root)
            if not old or mtime > old[1] or token_match:
                candidates[media_root] = (category, mtime, token_match)
    if not candidates:
        return None
    token_matches = [(path, data) for path, data in candidates.items() if data[2]]
    if len(token_matches) != 1:
        return None
    path, (category, _mtime, _token_match) = token_matches[0]
    return safe_resolve(path), category


def category_from_existing_library_match(
    config: MoveConfig,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str = "",
) -> str:
    found = find_recent_library_strm_source_dir(config, row, recognition, share_name=share_name)
    if not found:
        return ""
    source_dir, category = found
    expected_tmdb = expected_task_tmdb_id(recognition, row)
    source_tmdb = extract_tmdb_id_from_name(str(source_dir))
    if expected_tmdb and source_tmdb and expected_tmdb != source_tmdb:
        return ""
    return category


def plan_strm_move(
    source_path: Path | None,
    category: str,
    config: MoveConfig,
    destination_name: str = "",
) -> MovePlan:
    if not source_path:
        return MovePlan(status="skipped", reason="未找到 STRM 源目录", category=category)
    source = safe_resolve(source_path)
    if not source.exists() or not source.is_dir():
        return MovePlan(status="skipped", reason="STRM 源目录不存在", source_path=source, category=category)
    if not has_strm_file(source):
        return MovePlan(status="skipped", reason="源目录不包含 STRM 文件", source_path=source, category=category)
    if not is_directory_stable(source, config.stable_seconds):
        return MovePlan(
            status="skipped",
            reason="STRM 源目录仍在更新",
            source_path=source,
            category=category,
            metadata=directory_stability_metadata(source, config.stable_seconds),
        )
    dest = destination_for_category(category, destination_name or source.name, config)
    if not dest:
        return MovePlan(status="skipped", reason=f"分类未映射到媒体库：{category}", source_path=source, category=category)
    if not is_under_any_root(dest, list(config.library_roots.values())):
        return MovePlan(status="error", reason="目标目录不在媒体库白名单内", source_path=source, dest_path=dest, category=category)
    library_root = safe_resolve(config.library_roots[category])
    if is_relative_to(source, library_root):
        return MovePlan(status="skipped", reason="已在目标媒体库，无需移动", source_path=source, dest_path=source, category=category)
    if is_under_any_root(source, list(config.library_roots.values())):
        return MovePlan(status="skipped", reason="已在其他媒体库，跳过跨库移动", source_path=source, dest_path=dest, category=category)
    if not is_under_any_root(source, config.source_roots):
        return MovePlan(status="error", reason="源目录不在允许范围内", source_path=source, category=category)
    if source == library_root:
        return MovePlan(status="error", reason="源目录不能是媒体库根目录", source_path=source, dest_path=dest, category=category)
    if dest.exists():
        return MovePlan(status="conflict", reason="目标目录已存在，按策略跳过", source_path=source, dest_path=dest, category=category)
    return MovePlan(status="pending", reason="ready", source_path=source, dest_path=dest, category=category)


def execute_strm_move(plan: MovePlan, store: Any, row: dict[str, Any]) -> dict[str, Any]:
    if plan.status != "pending":
        return store.update_move(
            int(row["id"]),
            plan.status,
            source_path=str(plan.source_path) if plan.source_path else None,
            dest_path=str(plan.dest_path) if plan.dest_path else None,
            category_final=plan.category,
            error=plan.reason,
        ) or row
    assert plan.source_path is not None and plan.dest_path is not None
    plan.dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(plan.source_path), str(plan.dest_path))
    except Exception as exc:
        return store.update_move(
            int(row["id"]),
            "error",
            source_path=str(plan.source_path),
            dest_path=str(plan.dest_path),
            category_final=plan.category,
            error=str(exc),
        ) or row
    return store.update_move(
        int(row["id"]),
        "moved",
        source_path=str(plan.source_path),
        dest_path=str(plan.dest_path),
        category_final=plan.category,
    ) or row


def validate_self_share_strm_source(source: Path, row: dict[str, Any]) -> str:
    if str(row.get("workflow_mode") or "") != "self_share_sync":
        return ""
    if not source.exists() or not source.is_dir():
        return ""
    expected_tmdb = expected_task_tmdb_id(parse_recognition_json(row), row)
    folder_tmdb = extract_tmdb_id_from_name(str(source))
    if expected_tmdb and folder_tmdb and expected_tmdb != folder_tmdb:
        return f"任务 TMDB {expected_tmdb} 与文件夹 TMDB {folder_tmdb} 不一致，阻止移动 STRM"
    own_share_code = str(row.get("own_share_code") or "").strip()
    if not own_share_code:
        return "等待自有分享码，暂不移动 STRM"
    receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
    expected_marker = f"/s/{own_share_code}_{receive_code}_"
    for path in sorted(iter_strm_files(source)):
        issue = validate_self_share_strm_file(path, expected_marker)
        if issue:
            return issue
    return ""


def validate_self_share_strm_file(path: Path, expected_marker: str) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return f"无法读取 STRM：{path}"
    if "/d/" in text:
        return f"发现直链 STRM：{path}"
    if expected_marker not in text:
        return f"STRM 不是预期的分享链接：{path}"
    return ""


def validate_self_share_strm_destination(
    destination: Path,
    row: dict[str, Any],
    required_relative_path: str = "",
) -> str:
    if str(row.get("workflow_mode") or "") != "self_share_sync":
        return ""
    destination = safe_resolve(destination)
    if not destination.exists() or not destination.is_dir():
        return "目标 STRM 目录不存在"
    own_share_code = str(row.get("own_share_code") or "").strip()
    if not own_share_code:
        return "等待自有分享码，暂不确认目标 STRM"
    receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
    expected_marker = f"/s/{own_share_code}_{receive_code}_"
    if required_relative_path:
        relative = Path(required_relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            return "单集 STRM 相对路径无效"
        target = safe_resolve(destination / relative)
        if not is_relative_to(target, destination) or not target.is_file():
            return f"目标自有分享 STRM 不存在：{relative}"
        return validate_self_share_strm_file(target, expected_marker)
    if not has_strm_file(destination):
        return "目标目录不包含 STRM 文件"
    return validate_self_share_strm_source(destination, row)


def remove_direct_strm_files(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    removed = 0
    for strm_path in sorted(iter_strm_files(path)):
        try:
            text = strm_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "/d/" not in text:
            continue
        try:
            strm_path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def _strm_has_direct_link(path: Path) -> bool:
    try:
        return "/d/" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def find_recent_direct_library_strm_source_dir(
    config: MoveConfig,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str = "",
) -> tuple[Path, str] | None:
    try:
        since = float(row.get("created_at") or row.get("updated_at") or 0) - 60
    except (TypeError, ValueError):
        since = 0
    explicit_tmdb = ""
    for value in (row.get("title"), share_name, row.get("url")):
        explicit_tmdb = extract_tmdb_id_from_name(str(value or ""))
        if explicit_tmdb:
            break
    expected_tmdb = expected_task_tmdb_id(recognition, row)
    tokens = candidate_tokens(recognition, share_name)
    candidates: dict[Path, tuple[str, float, bool, bool]] = {}
    for category, root in config.library_roots.items():
        root = safe_resolve(root)
        if not root.exists():
            continue
        try:
            media_roots = [child for child in root.iterdir() if child.is_dir()]
        except OSError:
            continue
        for media_root in media_roots:
            media_root = safe_resolve(media_root)
            try:
                media_mtime = media_root.stat().st_mtime
            except OSError:
                continue
            folder_tmdb = extract_tmdb_id_from_name(str(media_root))
            exact_tmdb_folder = bool(expected_tmdb and folder_tmdb and expected_tmdb == folder_tmdb)
            if since and media_mtime < since and not exact_tmdb_folder:
                continue
            for strm_path in iter_strm_files(media_root):
                try:
                    mtime = strm_path.stat().st_mtime
                except OSError:
                    continue
                if since and mtime < since:
                    continue
                if not _strm_has_direct_link(strm_path):
                    continue
                if explicit_tmdb and folder_tmdb and explicit_tmdb != folder_tmdb:
                    continue
                try:
                    text = strm_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                haystack = normalize_text(f"{media_root} {text}")
                token_match = bool(tokens and any(token in haystack for token in tokens))
                tmdb_match = bool(expected_tmdb and folder_tmdb and expected_tmdb == folder_tmdb)
                old = candidates.get(media_root)
                if not old or mtime > old[1] or token_match or tmdb_match:
                    candidates[media_root] = (category, mtime, token_match, tmdb_match)
    if not candidates and not since:
        for category, root in config.library_roots.items():
            root = safe_resolve(root)
            if not root.exists():
                continue
            for strm_path in iter_strm_files(root):
                try:
                    mtime = strm_path.stat().st_mtime
                except OSError:
                    continue
                if not _strm_has_direct_link(strm_path):
                    continue
                media = library_media_root_for_path(strm_path.parent, config)
                if not media:
                    continue
                media_root, category = media
                folder_tmdb = extract_tmdb_id_from_name(str(media_root))
                if explicit_tmdb and folder_tmdb and explicit_tmdb != folder_tmdb:
                    continue
                try:
                    text = strm_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                haystack = normalize_text(f"{media_root} {text}")
                token_match = bool(tokens and any(token in haystack for token in tokens))
                tmdb_match = bool(expected_tmdb and folder_tmdb and expected_tmdb == folder_tmdb)
                old = candidates.get(media_root)
                if not old or mtime > old[1] or token_match or tmdb_match:
                    candidates[media_root] = (category, mtime, token_match, tmdb_match)
    if not candidates:
        return None
    exact_matches = [(path, data) for path, data in candidates.items() if data[3]]
    if len(exact_matches) == 1:
        path, (category, _mtime, _token_match, _tmdb_match) = exact_matches[0]
        return safe_resolve(path), category
    token_matches = [(path, data) for path, data in candidates.items() if data[2]]
    if len(token_matches) == 1:
        path, (category, _mtime, _token_match, _tmdb_match) = token_matches[0]
        return safe_resolve(path), category
    if len(candidates) == 1:
        path, (category, _mtime, _token_match, _tmdb_match) = next(iter(candidates.items()))
        return safe_resolve(path), category
    return None


def category_from_existing_library_folder(config: MoveConfig, folder: dict[str, Any]) -> str:
    folder_name = str(folder.get("file_name") or "").strip()
    if not folder_name:
        return ""
    matches: list[tuple[int, float, str]] = []
    for category, root in config.library_roots.items():
        path = safe_resolve(root / folder_name)
        if not path.exists() or not path.is_dir():
            continue
        matches.append((1 if has_strm_file(path) else 0, newest_mtime(path), category))
    if not matches:
        return ""
    matches.sort(reverse=True)
    return matches[0][2]


def cleanup_direct_strm_for_organized_folder(config: MoveConfig, folder: dict[str, Any]) -> int:
    folder_name = str(folder.get("file_name") or "").strip()
    if not folder_name:
        return 0
    removed = 0
    for root in config.library_roots.values():
        removed += remove_direct_strm_files(safe_resolve(root / folder_name))
    return removed


def cleanup_direct_strm_for_task_identity(
    config: MoveConfig,
    row: dict[str, Any],
    recognition: dict[str, Any] | None = None,
) -> int:
    dest_path = str(row.get("dest_path") or "").strip()
    if not dest_path:
        return 0
    reference = safe_resolve(Path(dest_path))
    relative_paths = self_share_strm_relative_paths(reference, row)
    if not relative_paths:
        return 0
    recognition = recognition or parse_recognition_json(row)
    tmdb_id = expected_task_tmdb_id(recognition, row)
    if not tmdb_id:
        return remove_direct_strm_relative_paths(reference, relative_paths)
    removed = 0
    for root in config.library_roots.values():
        root = safe_resolve(root)
        if not root.exists():
            continue
        try:
            media_roots = [child for child in root.iterdir() if child.is_dir()]
        except OSError:
            continue
        for media_root in media_roots:
            if extract_tmdb_id_from_name(media_root.name) == tmdb_id:
                removed += remove_direct_strm_relative_paths(safe_resolve(media_root), relative_paths)
    return removed


def self_share_strm_relative_paths(path: Path, row: dict[str, Any]) -> set[Path]:
    own_share_code = str(row.get("own_share_code") or "").strip()
    if not own_share_code or not path.exists() or not path.is_dir():
        return set()
    receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
    marker = f"/s/{own_share_code}_{receive_code}_"
    relative_paths: set[Path] = set()
    for strm_path in iter_strm_files(path):
        try:
            text = strm_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if marker in text:
            relative_paths.add(strm_path.relative_to(path))
    return relative_paths


def remove_direct_strm_relative_paths(path: Path, relative_paths: set[Path]) -> int:
    removed = 0
    for relative_path in relative_paths:
        target = safe_resolve(path / relative_path)
        if not is_relative_to(target, path) or not target.is_file() or not _strm_has_direct_link(target):
            continue
        try:
            target.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def merge_self_share_strm_folder(plan: MovePlan, store: Any, row: dict[str, Any]) -> dict[str, Any]:
    if plan.status in {"pending", "conflict"} and plan.source_path and plan.dest_path:
        source = safe_resolve(plan.source_path)
        issue = validate_self_share_strm_source(source, row)
        if issue:
            return store.update_move(
                int(row["id"]),
                "error",
                source_path=str(source),
                dest_path=str(safe_resolve(plan.dest_path)),
                category_final=plan.category,
                error=issue,
            ) or row
    if plan.status != "conflict" or not plan.source_path or not plan.dest_path:
        return execute_strm_move(plan, store, row)
    source = safe_resolve(plan.source_path)
    dest = safe_resolve(plan.dest_path)
    if not source.exists() or not source.is_dir():
        return execute_strm_move(MovePlan("skipped", "STRM 源目录不存在", source, dest, plan.category), store, row)
    if not dest.exists() or not dest.is_dir():
        return execute_strm_move(MovePlan("pending", "ready", source, dest, plan.category), store, row)
    try:
        for child in source.rglob("*"):
            if not child.is_file():
                continue
            relative = child.relative_to(source)
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)
        shutil.rmtree(source)
    except Exception as exc:
        return store.update_move(
            int(row["id"]),
            "error",
            source_path=str(source),
            dest_path=str(dest),
            category_final=plan.category,
            error=str(exc),
        ) or row
    return store.update_move(
        int(row["id"]),
        "moved",
        source_path=str(source),
        dest_path=str(dest),
        category_final=plan.category,
        error="目标目录已存在，已合并并覆盖同名 STRM",
    ) or row


def find_self_share_strm_source_dir(
    config: SelfShareConfig,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str,
) -> Path | None:
    move_config = MoveConfig(source_roots=[config.strm_root], library_roots={}, stable_seconds=0)
    folder_name = str(row.get("share_alias_name") or row.get("own_share_file_name") or "").strip()
    if folder_name:
        candidate = safe_resolve(config.strm_root / folder_name)
        if candidate.exists() and has_strm_file(candidate):
            return candidate
        return None
    return find_strm_source_dir(move_config, recognition, share_name=share_name)


def restore_canonical_strm_paths(source: Path, row: dict[str, Any]) -> int:
    try:
        manifest = json.loads(row.get("canonical_manifest_json") or "{}")
    except (TypeError, ValueError):
        return 0
    if not isinstance(manifest, dict):
        return 0
    restored = 0
    source = safe_resolve(source)
    for entry in manifest.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        alias_path = Path(str(entry.get("alias_path") or ""))
        canonical_path = Path(str(entry.get("canonical_path") or ""))
        if alias_path.is_absolute() or canonical_path.is_absolute() or ".." in alias_path.parts or ".." in canonical_path.parts:
            continue
        alias_strm = safe_resolve(source / alias_path.with_suffix(".strm"))
        canonical_strm = safe_resolve(source / canonical_path.with_suffix(".strm"))
        if not is_relative_to(alias_strm, source) or not is_relative_to(canonical_strm, source) or not alias_strm.is_file():
            continue
        canonical_strm.parent.mkdir(parents=True, exist_ok=True)
        alias_strm.replace(canonical_strm)
        restored += 1
    return restored


def canonical_self_share_root_name(row: dict[str, Any]) -> str:
    try:
        manifest = json.loads(row.get("canonical_manifest_json") or "{}")
    except (TypeError, ValueError):
        manifest = {}
    if isinstance(manifest, dict):
        root_name = str(manifest.get("root_name") or "").strip()
        if root_name:
            return root_name
    return str(row.get("own_share_file_name") or "").strip()


def select_move_source_for_workflow(
    existing_source: Path | None,
    prepared_self_share_source: Path | None,
    self_share_enabled: bool = False,
) -> Path | None:
    if self_share_enabled:
        return prepared_self_share_source
    return existing_source or prepared_self_share_source


def move_config_for_workflow_source(
    move_config: MoveConfig,
    source_dir: Path | None,
    self_share_config: SelfShareConfig | None = None,
) -> MoveConfig:
    if self_share_config and source_dir and is_relative_to(source_dir, self_share_config.strm_root):
        return MoveConfig(
            source_roots=[self_share_config.strm_root],
            library_roots=move_config.library_roots,
            conflict_policy=move_config.conflict_policy,
            stable_seconds=min(max(0, int(move_config.stable_seconds)), 5),
        )
    return move_config


def prepare_self_share_move_inputs(
    current_row: dict[str, Any],
    recognition: dict[str, Any],
    title: str,
    self_share_workflow: Any,
    existing_source: Path | None = None,
) -> tuple[dict[str, Any], Path | None, str]:
    prepared_row, prepared_source = self_share_workflow.prepare(current_row, recognition, title)
    source_dir = select_move_source_for_workflow(existing_source, prepared_source, self_share_enabled=True)
    return prepared_row, source_dir, final_category_for_move(prepared_row, recognition)


def _cleanup_own_share_source(store: Any, row: dict[str, Any], cleanup_client: Any | None) -> tuple[dict[str, Any], str]:
    if not cleanup_client:
        return row, ""
    if row.get("cleanup_status") == "deleted":
        return row, "115转存源此前已删除。"
    file_id = str(row.get("own_share_file_id") or "").strip()
    if not file_id:
        return row, ""
    share_code = str(row.get("own_share_code") or "").strip()
    if not share_code:
        updated = store.update_cleanup(int(row["id"]), "pending", file_id=file_id, error="等待自有分享创建完成") or row
        return updated, "等待自有分享创建完成后再删除 115 转存源。"
    try:
        cleanup_client.delete_file(file_id)
    except Exception as exc:
        updated = store.update_cleanup(int(row["id"]), "error", file_id=file_id, error=str(exc)) or row
        return updated, f"115转存源删除失败：{exc}"
    updated = store.update_cleanup(int(row["id"]), "deleted", file_id=file_id) or row
    return updated, "115转存源已删除；自有分享保留。"


def repair_stranded_self_share_moves(store: Any, move_config: MoveConfig, limit: int = 50) -> int:
    repaired = 0
    for row in store.stranded_self_share_move_candidates(limit=max(1, int(limit))):
        category = category_for_self_share_row(row)
        source_name = str(row.get("share_alias_name") or row.get("own_share_file_name") or "").strip()
        canonical_name = canonical_self_share_root_name(row)
        if not category or not source_name or not canonical_name:
            continue
        for source_root in move_config.source_roots:
            source = safe_resolve(Path(source_root) / source_name)
            restore_canonical_strm_paths(source, row)
            plan = plan_strm_move(source, category, move_config, destination_name=canonical_name)
            if plan.status in {"pending", "conflict"}:
                updated = merge_self_share_strm_folder(plan, store, row)
                if updated.get("move_status") == "moved":
                    repaired += 1
                break
            if plan.status != "skipped":
                execute_strm_move(plan, store, row)
                break
    return repaired


def restore_missing_self_share_library_folder(
    store: Any,
    cms: Any,
    row: dict[str, Any],
    self_share_config: SelfShareConfig,
    move_config: MoveConfig,
    required_relative_path: str = "",
) -> tuple[str, dict[str, Any]]:
    metadata = {
        "submission_id": int(row["id"]),
        "dest_path": str(row.get("dest_path") or ""),
        "category": category_for_self_share_row(row),
    }
    dest = safe_resolve(Path(str(row.get("dest_path") or "")))
    destination_issue = validate_self_share_strm_destination(dest, row, required_relative_path)
    if not destination_issue:
        metadata["direct_strm_removed"] = cleanup_direct_strm_for_task_identity(
            move_config,
            row,
            parse_recognition_json(row),
        )
        return "ready", metadata
    metadata["destination_validation_error"] = destination_issue
    category = category_for_self_share_row(row)
    canonical_name = canonical_self_share_root_name(row)
    source_name = str(row.get("share_alias_name") or row.get("own_share_file_name") or "").strip()
    if not category or not canonical_name or not source_name:
        return "skipped", metadata
    source = safe_resolve(self_share_config.strm_root / source_name)
    restore_canonical_strm_paths(source, row)
    restore_move_config = MoveConfig(
        source_roots=[self_share_config.strm_root],
        library_roots=move_config.library_roots,
        conflict_policy=move_config.conflict_policy,
        stable_seconds=move_config.stable_seconds,
    )
    plan = plan_strm_move(source, category, restore_move_config, destination_name=canonical_name)
    metadata.update(
        {
            "source_path": str(plan.source_path or source),
            "dest_path": str(plan.dest_path or dest),
            "category": category,
            "restore_reason": plan.reason,
        }
    )
    if plan.status in {"pending", "conflict"}:
        updated = merge_self_share_strm_folder(plan, store, row)
        metadata.update(
            {
                "source_path": str(updated.get("source_path") or metadata["source_path"]),
                "dest_path": str(updated.get("dest_path") or metadata["dest_path"]),
                "category": str(updated.get("category_final") or category),
            }
        )
        if str(updated.get("move_status") or "").lower() == "moved":
            metadata["direct_strm_removed"] = cleanup_direct_strm_for_task_identity(
                move_config,
                updated,
                parse_recognition_json(updated),
            )
            return "restored", metadata
        return "move_failed", metadata
    if plan.status == "skipped" and plan.reason in MISSING_SELF_SHARE_SOURCE_REASONS:
        share_code = str(row.get("own_share_code") or "").strip()
        receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
        if not share_code:
            return "skipped", metadata
        if str(row.get("workflow_phase") or "") != "restore_share_sync_submitted":
            cms.add_share115_sync_task(
                share_code,
                receive_code,
                cid=self_share_config.cms_cid,
                local_path=self_share_config.cms_local_path,
            )
            if hasattr(store, "update_self_share"):
                store.update_self_share(
                    int(row["id"]),
                    workflow_phase="restore_share_sync_submitted",
                    share_sync_status="restore_submitted",
                )
            return "restore_submitted", metadata
        return "waiting_source", metadata
    return "skipped", metadata


def restore_missing_self_share_library_folders(
    store: Any,
    cms: Any,
    self_share_config: SelfShareConfig,
    move_config: MoveConfig,
    limit: int = 50,
    recent_seconds: int = 3600,
) -> int:
    restored = 0
    if not hasattr(store, "missing_self_share_library_candidates"):
        return restored
    cutoff = time.time() - max(1, int(recent_seconds)) if recent_seconds > 0 else 0
    for row in store.missing_self_share_library_candidates(limit=max(1, int(limit))):
        if cutoff and float(row.get("updated_at") or 0) < cutoff:
            continue
        status, _metadata = restore_missing_self_share_library_folder(store, cms, row, self_share_config, move_config)
        if status == "restored":
            restored += 1
    return restored


def cleanup_pending_self_share_sources(store: Any, cleanup_client: Any | None, limit: int = 50) -> int:
    if not cleanup_client or not hasattr(store, "pending_self_share_cleanup_candidates"):
        return 0
    cleaned = 0
    for row in store.pending_self_share_cleanup_candidates(limit=max(1, int(limit))):
        updated, _line = _cleanup_own_share_source(store, row, cleanup_client)
        if str(updated.get("cleanup_status") or "").lower() == "deleted":
            cleaned += 1
    return cleaned
