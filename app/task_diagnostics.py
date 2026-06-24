from __future__ import annotations

from dataclasses import dataclass

from .models import TaskSnapshot, TaskStage, TaskStatus


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


def describe_task_wait(task: TaskSnapshot, *, now: float) -> str:
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
    return "，".join(parts)


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

    reason = str(task.metadata.get("_defer_message") or task.error_summary or task.current_stage.value)
    return StuckTaskIssue(
        code="stuck_stage",
        stage=task.current_stage,
        message=f"{reason}，已等待 {_duration(now - stage_started_at)}",
    )
