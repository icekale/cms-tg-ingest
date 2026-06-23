from __future__ import annotations

from dataclasses import dataclass

from .models import TaskSnapshot, TaskStatus
from .task_engine import stage_display_name
from .task_store import TaskStore


@dataclass(frozen=True)
class TaskHealthSummary:
    enabled: bool
    recent_count: int
    pending_count: int
    running_count: int
    problem_count: int
    latest_problem: TaskSnapshot | None = None


def build_task_health(store: TaskStore | None, *, enabled: bool, limit: int = 100) -> TaskHealthSummary:
    if store is None:
        return TaskHealthSummary(enabled=enabled, recent_count=0, pending_count=0, running_count=0, problem_count=0)
    tasks = store.list_recent_tasks(limit=limit)
    problems = [task for task in tasks if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION}]
    return TaskHealthSummary(
        enabled=enabled,
        recent_count=len(tasks),
        pending_count=sum(1 for task in tasks if task.status == TaskStatus.PENDING),
        running_count=sum(1 for task in tasks if task.status == TaskStatus.RUNNING),
        problem_count=len(problems),
        latest_problem=problems[0] if problems else None,
    )


def format_task_health(summary: TaskHealthSummary) -> str:
    lines = [
        f"TaskEngine: {'ENABLED' if summary.enabled else 'DISABLED'}",
        f"TaskStore最近任务: {summary.recent_count}",
        f"待执行: {summary.pending_count}",
        f"运行中: {summary.running_count}",
        f"失败/需处理: {summary.problem_count}",
    ]
    if summary.latest_problem:
        task = summary.latest_problem
        title = str(task.title or task.metadata.get("received_title") or task.share_code)
        suffix = f"，{task.error_summary}" if task.error_summary else ""
        lines.append(f"最近问题: #{task.id} {title} / {stage_display_name(task.current_stage)}{suffix}")
    return "\n".join(lines)


def format_taskstore_health(store: TaskStore | None, *, enabled: bool, limit: int = 100) -> str:
    return format_task_health(build_task_health(store, enabled=enabled, limit=limit))
