"""Guarded task actions shared by Web and Telegram controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import RetryAction, TaskSnapshot, TaskStage, TaskStatus
from .task_diagnostics import is_unscheduled_active_task
from .task_engine import decide_retry
from .task_store import (
    TaskStore,
    build_reprocess_metadata,
    reprocess_delete_keys_for,
    reprocess_stage_for,
)


TASK_ACTIONS = frozenset({"retry", "emby", "restore", "reprocess", "terminate"})
_DOWNSTREAM_RECOVERY_STAGES = frozenset({TaskStage.MOVED, TaskStage.EMBY_CONFIRMED, TaskStage.CLEANED})
_TERMINAL_ACTION_STATUSES = frozenset({TaskStatus.FAILED, TaskStatus.NEEDS_ACTION, TaskStatus.SUCCEEDED})


@dataclass(frozen=True)
class TaskActionResult:
    applied: bool
    task: TaskSnapshot | None
    reason: str


def task_termination_requested(task: TaskSnapshot) -> bool:
    value = task.metadata.get("termination_requested_at")
    if isinstance(value, bool):
        return value
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def available_lifecycle_actions(task: TaskSnapshot) -> frozenset[str]:
    requested = task_termination_requested(task)
    if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        return frozenset() if requested else frozenset({"terminate"})
    if task.status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.NEEDS_ACTION,
        TaskStatus.CANCELLED,
    } and not str(task.claimed_by or "").strip():
        return frozenset({"delete"})
    return frozenset()


def available_task_actions(task: TaskSnapshot, max_retries: int) -> frozenset[str]:
    """Return actions valid for this snapshot without mutating the task."""
    actions: set[str] = set()
    if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING} and not task_termination_requested(task):
        actions.add("terminate")
    if str(task.claimed_by or "").strip():
        return frozenset(actions)

    decision = decide_retry(task, max_retries=max_retries)
    if task.status == TaskStatus.FAILED and decision.action == RetryAction.RETRY_CURRENT_STAGE:
        actions.add("retry")
    if task.current_stage in _DOWNSTREAM_RECOVERY_STAGES and task.status in _TERMINAL_ACTION_STATUSES:
        actions.update({"emby", "restore"})
    if task.status in _TERMINAL_ACTION_STATUSES or (
        task.current_stage != TaskStage.RECEIVED and is_unscheduled_active_task(task)
    ):
        actions.add("reprocess")
    return frozenset(actions)


def _messages(actor: str, action: str) -> tuple[str | None, str]:
    actor = str(actor or "manual").strip() or "manual"
    if actor == "TG":
        labels = {
            "retry": ("TG 按钮触发重试", "TG 按钮重试已入队"),
            "emby": (None, "TG 按钮触发 Emby 检查"),
            "restore": ("TG 按钮触发 STRM 恢复", "TG 按钮 STRM 恢复已入队"),
            "reprocess": (None, "TG 按钮触发从头重跑"),
        }
    elif actor == "Web":
        labels = {
            "retry": ("手动触发重试", "手动重试已入队"),
            "emby": (None, "Web 触发 Emby 检查"),
            "restore": ("Web 触发 STRM 恢复", "Web STRM 恢复已入队"),
            "reprocess": (None, "Web 触发从头重跑"),
        }
    else:
        labels = {
            "retry": (f"{actor} 触发重试", f"{actor} 重试已入队"),
            "emby": (None, f"{actor} 触发 Emby 检查"),
            "restore": (f"{actor} 触发 STRM 恢复", f"{actor} STRM 恢复已入队"),
            "reprocess": (None, f"{actor} 触发从头重跑"),
        }
    return labels[action]


def _failed_result(task: TaskSnapshot, reason: str) -> TaskActionResult:
    return TaskActionResult(False, task, reason)


def apply_task_action(
    store: TaskStore,
    task_id: int,
    action: str,
    *,
    max_retries: int,
    actor: str,
) -> TaskActionResult:
    """Apply one action only if the loaded snapshot is still eligible."""
    action = str(action or "").strip().lower()
    task = store.find_task(task_id)
    if task is None:
        return TaskActionResult(False, None, "任务不存在或已过期")
    if action not in TASK_ACTIONS:
        return TaskActionResult(False, task, "不支持的任务操作")
    if action == "terminate":
        if task.status == TaskStatus.CANCELLED:
            return TaskActionResult(True, task, "任务已终止")
        if task.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return _failed_result(task, "任务已经结束，无需终止")
        updated = store.request_task_termination(task.id, actor)
        if updated is None:
            return _failed_result(task, "操作未执行，任务状态已变化")
        return TaskActionResult(True, updated, "任务终止已请求")
    actions = available_task_actions(task, max_retries)
    if action not in actions:
        if str(task.claimed_by or "").strip():
            reason = "任务正在执行，请稍后再试"
        elif action == "retry":
            reason = decide_retry(task, max_retries=max_retries).reason
        elif action == "reprocess":
            reason = "当前任务不支持从头重跑"
        else:
            reason = "当前任务不支持该操作"
        return TaskActionResult(False, task, reason)

    initial_message, target_message = _messages(actor, action)
    target_stage = task.current_stage
    metadata_patch: dict[str, Any] | None = None
    metadata_delete_keys: tuple[str, ...] | None = None
    increment_retry = False
    if action == "retry":
        decision = decide_retry(task, max_retries=max_retries)
        target_stage = decision.stage  # available_task_actions guarantees this is present.
        metadata_patch = {
            "retry_from_stage": task.current_stage.value,
            "retry_stage": target_stage.value,
        }
    elif action == "emby":
        target_stage = TaskStage.EMBY_CONFIRMED
    elif action == "restore":
        target_stage = TaskStage.EMBY_CONFIRMED
        metadata_patch = {
            "retry_from_stage": task.current_stage.value,
            "retry_stage": TaskStage.EMBY_CONFIRMED.value,
        }
    elif action == "reprocess":
        target_stage = reprocess_stage_for(task)
        metadata_patch = build_reprocess_metadata(task)
        metadata_delete_keys = reprocess_delete_keys_for(task)

    updated = store.compare_and_set_transition(
        task.id,
        task.current_stage,
        {task.status},
        require_unclaimed=True,
        target_stage=target_stage,
        target_status=TaskStatus.PENDING,
        target_event_message=target_message,
        initial_event_message=initial_message,
        initial_event_stage=TaskStage.EMBY_CONFIRMED if action == "restore" else None,
        increment_retry=increment_retry,
        metadata_patch=metadata_patch,
        metadata_delete_keys=metadata_delete_keys,
        next_run_at=0,
        clear_errors=True,
        clear_claim=True,
        expected_updated_at=task.updated_at,
    )
    if updated is None:
        return _failed_result(task, "操作未执行，任务状态已变化")
    return TaskActionResult(True, updated, target_message)
