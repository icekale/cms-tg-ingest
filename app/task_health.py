from __future__ import annotations

import time
from dataclasses import dataclass, replace

from .models import TaskSnapshot
from .task_diagnostics import _duration, describe_task_wait
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
    wait_overflow_count: int = 0
    p115_cooldown_until: float = 0.0


def _truncate(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


def _format_wait_detail(task: TaskSnapshot, *, now: float) -> str:
    title = _truncate(task.title or task.metadata.get("received_title") or task.share_code, 40)
    metadata = dict(task.metadata)
    if "_defer_message" in metadata:
        metadata["_defer_message"] = _truncate(metadata.get("_defer_message"), 90)
    safe_task = replace(task, title=title, error_summary=_truncate(task.error_summary, 90), metadata=metadata)
    return _truncate(f"#{task.id} {title}: {describe_task_wait(safe_task, now=now)}", 200)


def build_task_health(
    store: TaskStore | None,
    *,
    enabled: bool,
    limit: int = 100,
    now: float | None = None,
) -> TaskHealthSummary:
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
    current_time = time.time() if now is None else float(now)
    recent_tasks = store.list_recent_tasks(limit=limit)
    aggregate = store.aggregate_open_task_health(limit=5)
    wait_tasks = aggregate.wait_tasks
    cooldown_until = aggregate.p115_cooldown_until if aggregate.p115_cooldown_until > current_time else 0.0
    wait_details = tuple(
        _format_wait_detail(task, now=current_time)
        for task in wait_tasks[:5]
    )
    return TaskHealthSummary(
        enabled=enabled,
        recent_count=len(recent_tasks),
        pending_count=aggregate.pending_count,
        running_count=aggregate.running_count,
        needs_action_count=aggregate.needs_action_count,
        problem_count=aggregate.problem_count,
        lock_wait_count=aggregate.lock_wait_count,
        latest_problem=aggregate.latest_problem,
        latest_lock_wait=aggregate.latest_lock_wait,
        wait_details=wait_details,
        wait_overflow_count=max(0, aggregate.pending_count + aggregate.running_count - len(wait_details)),
        p115_cooldown_until=cooldown_until,
    )


def format_task_health(summary: TaskHealthSummary, *, now: float | None = None) -> str:
    current_time = time.time() if now is None else float(now)
    lines = [
        f"TaskEngine: {'ENABLED' if summary.enabled else 'DISABLED'}",
        f"TaskStore最近任务: {summary.recent_count}",
        f"待执行: {summary.pending_count}",
        f"运行中: {summary.running_count}",
        f"需人工: {summary.needs_action_count}",
        f"锁等待: {summary.lock_wait_count}",
        f"失败/需处理: {summary.problem_count}",
    ]
    if summary.p115_cooldown_until > current_time:
        remaining = _duration(summary.p115_cooldown_until - current_time)
        lines.append(f"115风控冷却: ACTIVE，剩余 {remaining}")
    else:
        lines.append("115风控冷却: inactive")
    for detail in summary.wait_details:
        lines.append(f"等待详情: {detail}")
    if summary.wait_overflow_count:
        lines.append(f"等待详情: 另有 {summary.wait_overflow_count} 个任务等待中")
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


def format_taskstore_health(
    store: TaskStore | None,
    *,
    enabled: bool,
    limit: int = 100,
    now: float | None = None,
) -> str:
    current_time = time.time() if now is None else float(now)
    summary = build_task_health(store, enabled=enabled, limit=limit, now=current_time)
    return format_task_health(summary, now=current_time)
