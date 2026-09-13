"""助手媒体库白名单动作：只删库内多余目录、只触发 Emby 扫库。

不给 bash。路径必须落在 STRM 媒体库根之内；删库根本身一律拒绝。
Emby 密钥不进 pi 环境，本模块在需要时从 PID 1 补回。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from app.config import (
    MoveConfig,
    default_library_roots,
    is_relative_to,
    parse_library_map,
    safe_resolve,
    split_env_list,
)

LIBRARY_ACTIONS = frozenset({"delete", "emby_scan"})
_PID1_KEYS = (
    "EMBY_BASE_URL",
    "EMBY_HOST_PORT",
    "EMBY_API_KEY",
    "EMBY_USER_ID",
    "STRM_LIBRARY_MAP",
    "STRM_SOURCE_ROOTS",
)


def _result(applied: bool, reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"applied": applied, "reason": reason}
    payload.update(extra)
    return payload


def _load_pid1_env() -> None:
    # ponytail: pi 子进程剥了业务密钥；ops 只从 PID1（bridge.py）补回 Emby/STRM。
    missing = [key for key in _PID1_KEYS if not os.environ.get(key)]
    if not missing:
        return
    try:
        raw = Path("/proc/1/environ").read_bytes()
    except OSError:
        return
    wanted = set(missing)
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key_b, _, value_b = item.partition(b"=")
        try:
            key = key_b.decode("ascii")
        except UnicodeDecodeError:
            continue
        if key in wanted and key not in os.environ:
            os.environ[key] = value_b.decode("utf-8", "replace")


def load_move_config() -> MoveConfig:
    _load_pid1_env()
    library_roots = default_library_roots()
    raw_map = os.environ.get("STRM_LIBRARY_MAP", "")
    if raw_map.strip():
        library_roots.update(parse_library_map(raw_map))
    source_roots = [
        Path(part).expanduser()
        for part in split_env_list(os.environ.get("STRM_SOURCE_ROOTS", "/mnt/user/Unraid/strm/转存"))
    ]
    return MoveConfig(source_roots=source_roots, library_roots=library_roots)


def resolve_user_path(raw: str, roots: dict[str, Path]) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for name, root in roots.items():
        resolved = safe_resolve(root)
        if text in {name, resolved.name, str(resolved)}:
            return resolved
    return safe_resolve(Path(text).expanduser())


def _owning_library(path: Path, roots: dict[str, Path]) -> tuple[str, Path] | None:
    resolved = safe_resolve(path)
    best: tuple[str, Path] | None = None
    for name, root in roots.items():
        root = safe_resolve(root)
        if not is_relative_to(resolved, root):
            continue
        if best is None or len(root.parts) > len(best[1].parts):
            best = (name, root)
    return best


def _emby_client(emby: Any | None) -> Any:
    if emby is not None:
        return emby
    _load_pid1_env()
    from app.clients.emby import EmbyClient

    return EmbyClient(
        os.environ.get("EMBY_BASE_URL") or os.environ.get("EMBY_HOST_PORT") or "",
        os.environ.get("EMBY_API_KEY") or "",
        os.environ.get("EMBY_USER_ID") or "",
    )


def apply_library_action(
    action: str,
    path: str,
    *,
    automated: bool = False,
    move_config: MoveConfig | None = None,
    emby: Any | None = None,
) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in LIBRARY_ACTIONS:
        return _result(False, "不支持的媒体库操作")
    config = move_config or load_move_config()
    target = resolve_user_path(path, config.library_roots)
    if target is None:
        return _result(False, "路径为空")
    owned = _owning_library(target, config.library_roots)
    if owned is None:
        return _result(False, "路径不在媒体库根目录内", path=str(target))
    library_name, root = owned
    if action == "delete":
        if automated:
            return _result(False, "自动巡检会话不允许删除媒体库目录")
        if safe_resolve(target) == safe_resolve(root):
            return _result(False, f"不能删除媒体库根目录（{library_name}）", path=str(target), library=library_name)
        if not target.exists():
            return _result(False, "路径不存在", path=str(target), library=library_name)
        if not target.is_dir():
            return _result(False, "只能删除目录", path=str(target), library=library_name)
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return _result(False, f"删除失败：{exc}", path=str(target), library=library_name)
        return _result(True, f"已删除 {target}（位于{library_name}）", action=action, path=str(target), library=library_name)

    client = _emby_client(emby)
    if not getattr(client, "enabled", False):
        return _result(False, "Emby 未配置", path=str(target), library=library_name)
    try:
        scanned = client.refresh_library_for_path(str(target))
    except Exception as exc:
        return _result(False, f"Emby 扫描失败：{exc}", path=str(target), library=library_name)
    label = scanned or library_name
    return _result(True, f"已请求 Emby 扫描 {label}", action=action, path=str(target), library=label)
