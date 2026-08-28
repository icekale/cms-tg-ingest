from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import Iterable

from .config import DEFAULT_OWN_SHARE_RECEIVE_CODE
from .logging_system import safe_telegram_text
from .media.strm import UnsafeMediaPathError, iter_strm_files
from .models import TaskSnapshot
from .quality_rules import is_path_within_allowed_roots
from .strm_mode import effective_task_strm_mode, normalize_strm_mode
from .task_store import TaskStore
from .telegram_rich import RichDocument, document, heading, paragraph, table


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    detail: str = ""
    task_id: int = 0
    title: str = ""


_StrmDirectoryScan = tuple[tuple[tuple[Path, str], ...], tuple[QualityIssue, ...]]
ShareIdentity = tuple[str, str]
ShareIdentityResolver = Callable[[TaskSnapshot], ShareIdentity | Sequence[ShareIdentity] | None]


def normalize_share_identities(value: object) -> tuple[ShareIdentity, ...]:
    """Accept one (code, receive) pair or a sequence of pairs."""
    if value is None:
        return ()
    if isinstance(value, (tuple, list)):
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and not isinstance(value[1], (tuple, list))
        ):
            return _share_identity_pair(value[0], value[1])
        pairs: list[ShareIdentity] = []
        seen: set[ShareIdentity] = set()
        for item in value:
            if not isinstance(item, (tuple, list)) or not item:
                continue
            receive = item[1] if len(item) > 1 else ""
            for identity in _share_identity_pair(item[0], receive):
                if identity not in seen:
                    seen.add(identity)
                    pairs.append(identity)
        return tuple(pairs)
    return ()


def _share_identity_pair(code: object, receive: object) -> tuple[ShareIdentity, ...]:
    share_code = str(code or "").strip()
    if not share_code:
        return ()
    receive_code = str(receive or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
    return ((share_code, receive_code),)


def share_markers_for_identities(
    own_share_code: str = "",
    own_share_receive_code: str = DEFAULT_OWN_SHARE_RECEIVE_CODE,
    accepted_identities: Iterable[ShareIdentity] | None = None,
) -> tuple[str, ...]:
    markers: list[str] = []
    seen: set[str] = set()
    pairs = list(accepted_identities or ())
    if str(own_share_code or "").strip():
        pairs.insert(0, (str(own_share_code).strip(), own_share_receive_code))
    for identity in normalize_share_identities(pairs):
        marker = f"/s/{identity[0]}_{identity[1]}_"
        if marker not in seen:
            seen.add(marker)
            markers.append(marker)
    return tuple(markers) if markers else ("/s/",)


def redact_quality_detail(value: object) -> str:
    """Keep quality evidence useful without exposing absolute host paths."""
    text = str(value or "")
    if not text:
        return ""
    try:
        posix_path = Path(text)
        windows_path = PureWindowsPath(text)
        if posix_path.is_absolute() or windows_path.is_absolute():
            name = posix_path.name or windows_path.name
            return f"本地路径已隐藏（名称：{name or '未知'}）"
    except (OSError, RuntimeError, ValueError):
        return "本地路径已隐藏"
    return text


def inspect_task_files(
    task: TaskSnapshot,
    *,
    dest_path: str | Path,
    expected_mode: str = "shared",
    own_share_code: str = "",
    own_share_receive_code: str = DEFAULT_OWN_SHARE_RECEIVE_CODE,
    accepted_identities: Iterable[ShareIdentity] | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    _scan_cache: dict[Path, _StrmDirectoryScan] | None = None,
) -> list[QualityIssue]:
    del task
    allowed_roots = tuple(allowed_roots) if allowed_roots is not None else None
    expected_mode = normalize_strm_mode(expected_mode)
    dest = Path(dest_path)
    cache_key = dest.absolute()
    scan = _scan_cache.get(cache_key) if _scan_cache is not None else None
    if scan is None:
        base_issues: list[QualityIssue] = []
        entries: list[tuple[Path, str]] = []
        if not is_path_within_allowed_roots(dest, allowed_roots):
            base_issues.append(QualityIssue("unsafe_metadata", "目标路径不在允许根目录", str(dest)))
        elif not dest.exists():
            base_issues.append(QualityIssue("missing_dest", "目标目录不存在", str(dest)))
        else:
            try:
                files = sorted(iter_strm_files(dest, allowed_roots=allowed_roots))
            except UnsafeMediaPathError:
                base_issues.append(QualityIssue("unsafe_metadata", "目标路径不在允许根目录", str(dest)))
            else:
                if not files:
                    base_issues.append(QualityIssue("missing_strm", "目标目录没有 STRM 文件", str(dest)))
                for path in files:
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError as exc:
                        base_issues.append(
                            QualityIssue("unreadable_strm", "STRM 文件无法读取", f"{path}: {exc}")
                        )
                        continue
                    entries.append((path, text))
        scan = (tuple(entries), tuple(base_issues))
        if _scan_cache is not None:
            _scan_cache[cache_key] = scan
    entries, base_issues = scan
    issues = list(base_issues)
    markers = share_markers_for_identities(
        own_share_code,
        own_share_receive_code,
        accepted_identities,
    )
    for path, text in entries:
        if "/d/" in text:
            if expected_mode != "direct":
                issues.append(QualityIssue("direct_strm", "发现直链 STRM", str(path)))
        elif expected_mode == "source_shared":
            if "/s/" not in text:
                issues.append(QualityIssue("unexpected_strm", "STRM 不是预期的分享链接", str(path)))
        elif expected_mode == "direct" or not any(marker in text for marker in markers):
            issues.append(QualityIssue("unexpected_strm", "STRM 不是预期的分享链接", str(path)))
    return issues


def scan_task_quality(
    store: TaskStore,
    limit: int = 100,
    allowed_roots: Iterable[str | Path] | None = None,
    tasks: Iterable[TaskSnapshot] | None = None,
    share_identity_resolver: ShareIdentityResolver | None = None,
) -> list[QualityIssue]:
    allowed_roots = tuple(allowed_roots) if allowed_roots is not None else None
    issues: list[QualityIssue] = []
    scan_cache: dict[Path, _StrmDirectoryScan] = {}
    identity_cache: dict[tuple[str, str], tuple[ShareIdentity, ...]] = {}
    task_rows = list(tasks) if tasks is not None else store.list_recent_tasks(limit=limit)
    for task in task_rows:
        candidate = task.title or task.metadata.get("received_title")
        if not candidate or str(candidate).strip() == str(task.share_code or "").strip():
            candidate = f"任务 #{task.id}"
        title = safe_telegram_text(candidate, 120)
        try:
            expected_mode = effective_task_strm_mode(task)
        except ValueError as exc:
            raw_mode = str(task.metadata.get("strm_mode") or "").strip()
            issues.append(QualityIssue("invalid_strm_mode", "任务 STRM 模式无效", raw_mode or str(exc), task.id, title))
            continue
        dest_path = str(task.metadata.get("dest_path") or "").strip()
        if not dest_path:
            continue
        own_share_code = str(task.metadata.get("own_share_code") or "").strip()
        own_share_receive_code = str(task.metadata.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        accepted_identities: tuple[ShareIdentity, ...] = ()
        if expected_mode == "shared" and callable(share_identity_resolver):
            identity_key = (dest_path, str(task.metadata.get("tmdb_id") or task.tmdb_id or "").strip())
            if identity_key not in identity_cache:
                try:
                    identity_cache[identity_key] = normalize_share_identities(share_identity_resolver(task))
                except Exception:
                    identity_cache[identity_key] = ()
            accepted_identities = identity_cache[identity_key]
        for issue in inspect_task_files(
            task,
            dest_path=dest_path,
            expected_mode=expected_mode,
            own_share_code=own_share_code,
            own_share_receive_code=own_share_receive_code,
            accepted_identities=accepted_identities,
            allowed_roots=allowed_roots,
            _scan_cache=scan_cache,
        ):
            issues.append(replace(issue, task_id=task.id, title=title))
    return issues


def format_task_quality_report(issues: list[QualityIssue]) -> RichDocument:
    if not issues:
        return document(paragraph("TaskStore 轻量巡检：未发现本地 STRM 问题。"))
    rows = []
    for issue in issues:
        title = safe_telegram_text(issue.title or f"任务 #{issue.task_id}", 120)
        message = safe_telegram_text(issue.message, 180)
        task_label = f"#{issue.task_id} {title}" if issue.task_id else title
        detail = f"：{safe_telegram_text(redact_quality_detail(issue.detail), 180)}" if issue.detail else ""
        rows.append((task_label, f"{message}{detail}"))
    return document(heading("TaskStore 轻量巡检"), table(("# / 任务", "问题"), rows))
