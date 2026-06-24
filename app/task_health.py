from __future__ import annotations

import time
from dataclasses import dataclass

from .models import TaskSnapshot, TaskStatus
from .task_diagnostics import describe_task_wait
from .task_engine import stage_display_name
from .task_store import TaskStore


@dataclass(frozen=True)
class TaskHealthSummary:
    enabled: bool
    recent_count: int
    pending_count: int
    running_count: int
    needs_action_count: int
    problem_count: int
    lock_wait_count: int
    latest_problem: TaskSnapshot | None = None
    latest_lock_wait: TaskSnapshot | None = None
    wait_details: tuple[str, ...] = ()


def build_task_health(store: TaskStore | None, *, enabled: bool, limit: int = 100) -> TaskHealthSummary:
    if store is None:
        return TaskHealthSummary(
            enabled=enabled,
            recent_count=0,
            pending_count=0,
            running_count=0,
            needs_action_count=0,
            problem_count=0,
            lock_wait_count=0,
        )
    queue = store.queue_summary(limit=limit)
    tasks = store.list_recent_tasks(limit=limit)
    problems = [task for task in tasks if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION}]
    now = time.time()
    wait_details = tuple(
        f"#{task.id} {task.title or task.metadata.get('received_title') or task.share_code}: {describe_task_wait(task, now=now)}"
        for task in tasks
        if task.status in {TaskStatus.RUNNING, TaskStatus.PENDING}
    )
    return TaskHealthSummary(
        enabled=enabled,
        recent_count=queue.recent_count,
        pending_count=queue.pending_count,
        running_count=queue.running_count,
        needs_action_count=queue.needs_action_count,
        problem_count=queue.failed_count + queue.needs_action_count,
        lock_wait_count=queue.lock_wait_count,
        latest_problem=problems[0] if problems else None,
        latest_lock_wait=queue.latest_lock_wait,
        wait_details=wait_details,
    )


def format_task_health(summary: TaskHealthSummary) -> str:
    lines = [
        f"TaskEngine: {'ENABLED' if summary.enabled else 'DISABLED'}",
        f"TaskStore最近任务: {summary.recent_count}",
        f"待执行: {summary.pending_count}",
        f"运行中: {summary.running_count}",
        f"需人工: {summary.needs_action_count}",
        f"锁等待: {summary.lock_wait_count}",
        f"失败/需处理: {summary.problem_count}",
    ]
    for detail in summary.wait_details:
        lines.append(f"等待详情: {detail}")
    if summary.latest_lock_wait:
        task = summary.latest_lock_wait
        title = str(task.title or task.metadata.get("received_title") or task.share_code)
        reason = str(task.metadata.get("_lock_reason") or "-")
        holder = str(task.metadata.get("_lock_owner_task_id") or "-")
        lines.append(f"最近锁等待: #{task.id} {title} / {reason} / holder #{holder}")
    if summary.latest_problem:
        task = summary.latest_problem
        title = str(task.title or task.metadata.get("received_title") or task.share_code)
        suffix = f"，{task.error_summary}" if task.error_summary else ""
        lines.append(f"最近问题: #{task.id} {title} / {stage_display_name(task.current_stage)}{suffix}")
    return "\n".join(lines)


def format_taskstore_health(store: TaskStore | None, *, enabled: bool, limit: int = 100) -> str:
    return format_task_health(build_task_health(store, enabled=enabled, limit=limit))
