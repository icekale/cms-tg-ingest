from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from .models import TaskSnapshot
from .task_store import TaskStore


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    detail: str = ""
    task_id: int = 0
    title: str = ""


def inspect_task_files(task: TaskSnapshot, *, dest_path: str | Path, own_share_code: str = "") -> list[QualityIssue]:
    del task
    dest = Path(dest_path)
    if not dest.exists():
        return [QualityIssue("missing_dest", "目标目录不存在", str(dest))]
    files = sorted(path for path in dest.rglob("*.strm") if path.is_file())
    if not files:
        return [QualityIssue("missing_strm", "目标目录没有 STRM 文件", str(dest))]
    issues: list[QualityIssue] = []
    expected_marker = f"/s/{own_share_code}_1212_" if own_share_code else "/s/"
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if "/d/" in text:
            issues.append(QualityIssue("direct_strm", "发现直链 STRM", str(path)))
        elif expected_marker not in text:
            issues.append(QualityIssue("unexpected_strm", "STRM 不是预期的分享链接", str(path)))
    return issues


def scan_task_quality(store: TaskStore, limit: int = 100) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for task in store.list_recent_tasks(limit=limit):
        dest_path = str(task.metadata.get("dest_path") or "").strip()
        if not dest_path:
            continue
        own_share_code = str(task.metadata.get("own_share_code") or "").strip()
        title = task.title or str(task.metadata.get("received_title") or "") or task.share_code
        for issue in inspect_task_files(task, dest_path=dest_path, own_share_code=own_share_code):
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
