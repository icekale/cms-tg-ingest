from __future__ import annotations

from dataclasses import dataclass

from .models import TaskSnapshot, TaskStage, TaskStatus
from .task_engine import stage_display_name


@dataclass(frozen=True)
class StuckTaskIssue:
    code: str = ""
    stage: TaskStage | None = None
    message: str = ""


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时"


def _defer_count(metadata: dict) -> int:
    try:
        return int(metadata.get("_defer_count") or 0)
    except (TypeError, ValueError):
        return 0


def _int_label(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _p115_call_label(metadata: dict) -> str:
    stage_count = _int_label(metadata.get("p115_stage_request_count"))
    total_count = _int_label(metadata.get("p115_total_request_count"))
    if stage_count is None and total_count is None:
        return ""
    stage_text = "-" if stage_count is None else str(max(0, stage_count))
    total_text = "-" if total_count is None else str(max(0, total_count))
    return f"115调用 本阶段{stage_text}次/累计{total_text}次"


def _seconds_label(value: object) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return "0 秒"
    if seconds < 60:
        return f"{seconds:g} 秒"
    return _duration(seconds)


def _stage_metric_summary(values: object, *, suffix: str = "") -> str:
    if not isinstance(values, dict):
        return ""
    parts: list[str] = []
    for stage_value, raw_value in values.items():
        try:
            stage = TaskStage(str(stage_value))
        except ValueError:
            continue
        if suffix:
            metric = _int_label(raw_value)
            if metric is None:
                continue
            value_label = f"{max(0, metric)}{suffix}"
        else:
            value_label = _seconds_label(raw_value)
            if not value_label:
                continue
        parts.append(f"{stage_display_name(stage)} {value_label}")
    return "，".join(parts)


def is_unscheduled_active_task(task: TaskSnapshot) -> bool:
    return (
        task.status in (TaskStatus.RUNNING, TaskStatus.PENDING)
        and float(task.next_run_at or 0) < 0
        and not str(task.claimed_by or "").strip()
    )


def is_dispatchable_active_task(task: TaskSnapshot) -> bool:
    if task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING):
        return False
    return not is_unscheduled_active_task(task)


def describe_task_wait(task: TaskSnapshot, *, now: float) -> str:
    if is_unscheduled_active_task(task):
        wait_from = task.updated_at or task.created_at or now
        return f"不在自动调度队列，需要手动重试或从头重跑，已等待 {_duration(now - wait_from)}"

    reason = str(task.metadata.get("_defer_message") or task.error_summary or "等待执行")
    wait_from = task.updated_at or task.created_at or now
    next_run_at = task.next_run_at or now
    defer_count = _defer_count(task.metadata)

    parts = [
        reason,
        f"已等待 {_duration(now - wait_from)}",
        f"下次检查 {_duration(next_run_at - now)}后",
    ]
    if defer_count:
        parts.append(f"第 {defer_count} 次")
    elapsed = _seconds_label(task.metadata.get("stage_elapsed_seconds"))
    if elapsed:
        parts.append(f"执行 {elapsed}")
    waited = _seconds_label(task.metadata.get("stage_wait_seconds"))
    if waited:
        parts.append(f"排队/等待 {waited}")
    p115_label = _p115_call_label(task.metadata)
    if p115_label:
        parts.append(p115_label)
    return "，".join(parts)


def explain_task_slowness(task: TaskSnapshot, *, now: float) -> str:
    metadata = task.metadata or {}
    if is_unscheduled_active_task(task):
        return "不在自动调度队列，需要手动重试或从头重跑"

    lock_reason = str(metadata.get("_lock_reason") or "").strip()
    if metadata.get("_lock_waiting"):
        holder = str(metadata.get("_lock_owner_task_id") or "").strip()
        return f"等资源锁释放：#{holder} {lock_reason}".strip()

    try:
        cooldown_until = float(metadata.get("p115_risk_cooldown_until"))
    except (TypeError, ValueError):
        cooldown_until = 0.0
    if cooldown_until > now:
        return f"等 115 风控冷却，剩余 {_duration(cooldown_until - now)}"

    reason = str(metadata.get("_defer_message") or task.error_summary or "").strip()
    stage = task.current_stage
    if stage == TaskStage.ORGANIZING or "CMS 整理" in reason:
        return "等 CMS 整理"
    if stage == TaskStage.STRM_READY or "STRM" in reason:
        if "稳定" in reason:
            return "等分享 STRM 文件稳定"
        return "等分享 STRM 生成"
    if stage == TaskStage.EMBY_CONFIRMED or "Emby" in reason:
        return "等 Emby 入库"
    if reason:
        return reason
    if task.status == TaskStatus.PENDING:
        return "等任务调度执行"
    if task.status == TaskStatus.RUNNING:
        return "当前阶段执行中"
    return ""


def format_task_observability(task: TaskSnapshot, *, now: float) -> list[str]:
    lines: list[str] = []
    slow_reason = explain_task_slowness(task, now=now)
    if slow_reason:
        lines.append(f"为什么慢：{slow_reason}")

    timing_parts = []
    elapsed = _seconds_label(task.metadata.get("stage_elapsed_seconds"))
    if elapsed:
        timing_parts.append(f"执行 {elapsed}")
    waited = _seconds_label(task.metadata.get("stage_wait_seconds"))
    if waited:
        timing_parts.append(f"排队/等待 {waited}")
    if timing_parts:
        lines.append("耗时：" + "，".join(timing_parts))

    p115_label = _p115_call_label(task.metadata)
    if p115_label:
        lines.append(p115_label.replace("115调用 ", "115调用："))
    return lines


def format_stage_observability(task: TaskSnapshot) -> tuple[str, str]:
    elapsed = _stage_metric_summary(task.metadata.get("stage_elapsed_seconds_by_stage"))
    p115_calls = _stage_metric_summary(task.metadata.get("p115_request_counts_by_stage"), suffix="次")
    return elapsed, p115_calls


def classify_stuck_task(
    task: TaskSnapshot,
    *,
    now: float,
    threshold_seconds: int = 1800,
) -> StuckTaskIssue:
    if task.status not in (TaskStatus.RUNNING, TaskStatus.PENDING):
        return StuckTaskIssue()

    stage_started_at = task.updated_at if task.updated_at is not None else (task.created_at or now)
    if now - stage_started_at <= threshold_seconds:
        return StuckTaskIssue()

    if is_unscheduled_active_task(task):
        return StuckTaskIssue(
            code="stuck_stage",
            stage=task.current_stage,
            message=f"不在自动调度队列，需要手动重试或从头重跑，已等待 {_duration(now - stage_started_at)}",
        )

    reason = str(task.metadata.get("_defer_message") or task.error_summary or task.current_stage.value)
    return StuckTaskIssue(
        code="stuck_stage",
        stage=task.current_stage,
        message=f"{reason}，已等待 {_duration(now - stage_started_at)}",
    )
