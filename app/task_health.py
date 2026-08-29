from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace

from .models import TaskSnapshot
from .task_diagnostics import _duration, describe_task_wait
from .logging_system import safe_telegram_text
from .telegram_ui import _is_blocked_display_value, task_display_blocked_values
from .task_engine import stage_display_name
from .task_store import TaskStore


RUNNER_HEARTBEAT_STALE_SECONDS = 90.0


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
    runner_heartbeat_at: float = 0.0
    runner_heartbeat_stale: bool = False
    runner_state: str = ""
    runner_active: bool = False
    runner_active_task_id: int = 0
    runner_active_stage: str = ""
    runner_active_since: float = 0.0
    runner_last_claim_attempt_at: float = 0.0


def _truncate(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: max(0, limit - 3)]}..."


def _format_wait_detail(task: TaskSnapshot, *, now: float) -> str:
    blocked = task_display_blocked_values(task)
    candidate = task.title or task.metadata.get("received_title")
    if not candidate or _is_blocked_display_value(candidate, blocked):
        candidate = f"任务 #{task.id}"
    title = safe_telegram_text(candidate, 40, blocked_values=blocked)
    metadata = dict(task.metadata)
    if "_defer_message" in metadata:
        metadata["_defer_message"] = safe_telegram_text(metadata.get("_defer_message"), 90, blocked_values=blocked)
    safe_task = replace(task, title=title, error_summary=safe_telegram_text(task.error_summary, 90, blocked_values=blocked), metadata=metadata)
    return safe_telegram_text(f"#{task.id} {title}: {describe_task_wait(safe_task, now=now)}", 200, blocked_values=blocked)


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
    aggregate = store.aggregate_open_task_health(limit=limit)
    wait_tasks = aggregate.wait_tasks
    cooldown_until = aggregate.p115_cooldown_until if aggregate.p115_cooldown_until > current_time else 0.0
    runner_heartbeat_at = aggregate.runner_heartbeat_at
    runner_heartbeat_stale = bool(
        enabled
        and runner_heartbeat_at > 0
        and current_time - runner_heartbeat_at > RUNNER_HEARTBEAT_STALE_SECONDS
    )
    runner_active = False
    runner_active_task_id = 0
    runner_active_stage = ""
    runner_active_since = 0.0
    runner_last_claim_attempt_at = 0.0
    activity_state = store.get_runtime_state("task_runner:activity")
    if activity_state:
        try:
            activity = json.loads(str(activity_state.get("value") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            activity = {}
        if isinstance(activity, dict):
            try:
                runner_active_task_id = max(0, int(activity.get("active_task_id") or 0))
            except (TypeError, ValueError):
                runner_active_task_id = 0
            runner_active_stage = str(activity.get("active_stage") or "")
            try:
                runner_active_since = max(0.0, float(activity.get("active_since") or 0))
            except (TypeError, ValueError):
                runner_active_since = 0.0
            try:
                runner_last_claim_attempt_at = max(0.0, float(activity.get("last_claim_attempt_at") or 0))
            except (TypeError, ValueError):
                runner_last_claim_attempt_at = 0.0
            state_updated_at = max(0.0, float(activity_state.get("updated_at") or 0))
            runner_active = bool(
                runner_active_task_id
                and current_time - state_updated_at <= RUNNER_HEARTBEAT_STALE_SECONDS
            )
    wait_details = tuple(
        _format_wait_detail(task, now=current_time)
        for task in wait_tasks[:5]
    )
    return TaskHealthSummary(
        enabled=enabled,
        recent_count=aggregate.recent_count,
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
        runner_heartbeat_at=runner_heartbeat_at,
        runner_heartbeat_stale=runner_heartbeat_stale,
        runner_state=aggregate.runner_state,
        runner_active=runner_active,
        runner_active_task_id=runner_active_task_id,
        runner_active_stage=runner_active_stage,
        runner_active_since=runner_active_since,
        runner_last_claim_attempt_at=runner_last_claim_attempt_at,
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
    if not summary.enabled:
        lines.append("TaskRunner心跳: disabled")
    elif summary.runner_state == "error":
        lines.append("TaskRunner心跳: error")
    elif summary.runner_heartbeat_stale:
        lines.append("TaskRunner心跳: stale")
    elif summary.runner_state == "stopped":
        lines.append("TaskRunner心跳: stopped")
    elif summary.runner_heartbeat_at > 0:
        lines.append("TaskRunner心跳: active")
    else:
        lines.append("TaskRunner心跳: unknown")
    if summary.runner_active and summary.runner_active_task_id:
        since = _duration(max(0.0, current_time - summary.runner_active_since))
        lines.append(
            f"Runner当前: 处理任务 #{summary.runner_active_task_id} "
            f"({safe_telegram_text(summary.runner_active_stage or '?', 60)}，已 {since})"
        )
    elif summary.runner_last_claim_attempt_at > 0:
        idle = _duration(max(0.0, current_time - summary.runner_last_claim_attempt_at))
        lines.append(f"Runner当前: idle（上次尝试 {idle} 前）")
    else:
        lines.append("Runner当前: 尚未活动")
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
        blocked = task_display_blocked_values(task)
        candidate = task.title or task.metadata.get("received_title")
        if not candidate or _is_blocked_display_value(candidate, blocked):
            candidate = f"任务 #{task.id}"
        title = safe_telegram_text(candidate, 80, blocked_values=blocked)
        reason = safe_telegram_text(task.metadata.get("_lock_reason") or "-", 100, blocked_values=blocked)
        holder = safe_telegram_text(task.metadata.get("_lock_owner_task_id") or "-", 40, blocked_values=blocked)
        lines.append(f"最近锁等待: #{task.id} {title} / {reason} / holder #{holder}")
    if summary.latest_problem:
        task = summary.latest_problem
        blocked = task_display_blocked_values(task)
        candidate = task.title or task.metadata.get("received_title")
        if not candidate or _is_blocked_display_value(candidate, blocked):
            candidate = f"任务 #{task.id}"
        title = safe_telegram_text(candidate, 80, blocked_values=blocked)
        suffix = f"，{safe_telegram_text(task.error_summary, 120, blocked_values=blocked)}" if task.error_summary else ""
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
    report = format_task_health(summary, now=current_time)
    backup_line = "数据库备份: 未执行"
    if store is not None:
        state = store.get_runtime_state("backup_last_result")
        if state:
            try:
                payload = json.loads(str(state.get("value") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                backup_line = f"数据库备份: {safe_telegram_text(payload.get('status') or 'unknown', 60)}"
                error = safe_telegram_text(payload.get("error") or "", 160).strip()
                if error:
                    backup_line += f" ({error[:160]})"
    return report + "\n" + backup_line
