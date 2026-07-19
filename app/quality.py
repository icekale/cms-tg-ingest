from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .config import is_under_any_root
from .media.strm import UnsafeMediaPathError, iter_strm_files
from .models import TaskSnapshot
from .task_store import TaskStore


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    detail: str = ""
    task_id: int = 0
    title: str = ""


def inspect_task_files(
    task: TaskSnapshot,
    *,
    dest_path: str | Path,
    own_share_code: str = "",
    own_share_receive_code: str = "1212",
    allowed_roots: Iterable[str | Path] | None = None,
) -> list[QualityIssue]:
    del task
    dest = Path(dest_path)
    if allowed_roots is not None and not is_under_any_root(dest, list(allowed_roots)):
        return [QualityIssue("unsafe_metadata", "目标路径不在允许根目录", str(dest))]
    if not dest.exists():
        return [QualityIssue("missing_dest", "目标目录不存在", str(dest))]
    try:
        files = sorted(iter_strm_files(dest, allowed_roots=allowed_roots))
    except UnsafeMediaPathError:
        return [QualityIssue("unsafe_metadata", "目标路径不在允许根目录", str(dest))]
    if not files:
        return [QualityIssue("missing_strm", "目标目录没有 STRM 文件", str(dest))]
    issues: list[QualityIssue] = []
    receive_code = str(own_share_receive_code or "1212").strip() or "1212"
    expected_marker = f"/s/{own_share_code}_{receive_code}_" if own_share_code else "/s/"
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if "/d/" in text:
            issues.append(QualityIssue("direct_strm", "发现直链 STRM", str(path)))
        elif expected_marker not in text:
            issues.append(QualityIssue("unexpected_strm", "STRM 不是预期的分享链接", str(path)))
    return issues


def scan_task_quality(
    store: TaskStore,
    limit: int = 100,
    allowed_roots: Iterable[str | Path] | None = None,
    tasks: Iterable[TaskSnapshot] | None = None,
) -> list[QualityIssue]:
    allowed_roots = tuple(allowed_roots) if allowed_roots is not None else None
    issues: list[QualityIssue] = []
    task_rows = list(tasks) if tasks is not None else store.list_recent_tasks(limit=limit)
    for task in task_rows:
        dest_path = str(task.metadata.get("dest_path") or "").strip()
        if not dest_path:
            continue
        own_share_code = str(task.metadata.get("own_share_code") or "").strip()
        own_share_receive_code = str(task.metadata.get("own_share_receive_code") or "1212").strip() or "1212"
        title = task.title or str(task.metadata.get("received_title") or "") or task.share_code
        for issue in inspect_task_files(
            task,
            dest_path=dest_path,
            own_share_code=own_share_code,
            own_share_receive_code=own_share_receive_code,
            allowed_roots=allowed_roots,
        ):
            issues.append(replace(issue, task_id=task.id, title=title))
    return issues


def format_task_quality_report(issues: list[QualityIssue]) -> str:
    if not issues:
        return "TaskStore 轻量巡检：未发现本地 STRM 问题。"
    lines = ["TaskStore 轻量巡检：发现本地 STRM 问题"]
    for idx, issue in enumerate(issues, 1):
        title = issue.title or f"任务 #{issue.task_id}"
        task_label = f"#{issue.task_id} {title}" if issue.task_id else title
        detail = f"：{issue.detail}" if issue.detail else ""
        lines.append(f"{idx}. {task_label} - {issue.message}{detail}")
    return "\n".join(lines)
