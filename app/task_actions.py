"""Guarded task actions shared by Web and Telegram controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import RetryAction, TaskSnapshot, TaskStage, TaskStatus
from .task_diagnostics import is_unscheduled_active_task
from .task_engine import decide_retry
from .task_store import TaskStore, command_key, reprocess_stage_for


TASK_ACTIONS = frozenset({"retry", "emby", "restore", "reprocess", "resume_organizing", "terminate"})
_ACTION_COMMANDS = {
    "retry": "retry",
    "reprocess": "reprocess",
    "emby": "emby_check",
    "restore": "restore",
    "resume_organizing": "resume_organizing",
}
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
    if float(getattr(task, "archived_at", 0) or 0) > 0:
        return frozenset()
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


def can_resume_organizing(task: TaskSnapshot, store: TaskStore) -> bool:
    if task.status != TaskStatus.NEEDS_ACTION or str(task.claimed_by or "").strip():
        return False
    if task.current_stage not in {TaskStage.NEEDS_ACTION, TaskStage.ORGANIZING}:
        return False
    metadata = task.metadata or {}
    defer_stage = str(metadata.get("_defer_stage") or "").strip()
    # Older conflict tasks kept organizing as the current stage.
    direct_organizing_resume = (
        task.current_stage == TaskStage.ORGANIZING
        and str(task.error_type or "").strip() == "needs_action"
    )
    legacy_organizing_resume = (
        not defer_stage
        and str(metadata.get("retry_from_stage") or "").strip() == TaskStage.ORGANIZING.value
        and str(metadata.get("retry_stage") or "").strip() == TaskStage.ORGANIZING.value
    )
    if not direct_organizing_resume and defer_stage != TaskStage.ORGANIZING.value and not legacy_organizing_resume:
        return False
    identity = metadata.get("intake_identity")
    root_ids = identity.get("root_ids") if isinstance(identity, dict) else None
    files = identity.get("files") if isinstance(identity, dict) else None
    if (
        not isinstance(identity, dict)
        or not isinstance(root_ids, list)
        or not root_ids
        or any(not isinstance(value, str) or not value.strip() for value in root_ids)
        or not isinstance(files, list)
        or not files
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item.get("id", "").strip()
            for item in files
        )
    ):
        return False
    if str(task.metadata.get("own_share_code") or "").strip():
        return False
    operations = store.list_operations(task.id)
    if any(operation.operation_type == "create_share" for operation in operations):
        return False
    return any(
        operation.operation_type == "receive_share" and operation.status == "succeeded"
        for operation in operations
    )


def available_task_actions(
    task: TaskSnapshot,
    max_retries: int,
    *,
    store: TaskStore | None = None,
) -> frozenset[str]:
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
    if store is not None and can_resume_organizing(task, store):
        actions.add("resume_organizing")
    return frozenset(actions)


def _messages(actor: str, action: str) -> tuple[str | None, str]:
    actor = str(actor or "manual").strip() or "manual"
    if actor == "TG":
        labels = {
            "retry": ("TG 按钮触发重试", "TG 按钮重试已入队"),
            "emby": (None, "TG 按钮触发 Emby 检查"),
            "restore": ("TG 按钮触发 STRM 恢复", "TG 按钮 STRM 恢复已入队"),
            "reprocess": (None, "TG 按钮触发从头重跑"),
            "resume_organizing": ("TG 按钮触发继续整理", "继续整理已入队"),
        }
    elif actor == "Web":
        labels = {
            "retry": ("手动触发重试", "手动重试已入队"),
            "emby": (None, "Web 触发 Emby 检查"),
            "restore": ("Web 触发 STRM 恢复", "Web STRM 恢复已入队"),
            "reprocess": (None, "Web 触发从头重跑"),
            "resume_organizing": ("手动触发继续整理", "继续整理已入队"),
        }
    else:
        labels = {
            "retry": (f"{actor} 触发重试", f"{actor} 重试已入队"),
            "emby": (None, f"{actor} 触发 Emby 检查"),
            "restore": (f"{actor} 触发 STRM 恢复", f"{actor} STRM 恢复已入队"),
            "reprocess": (None, f"{actor} 触发从头重跑"),
            "resume_organizing": (f"{actor} 触发继续整理", "继续整理已入队"),
        }
    return labels[action]


def _failed_result(task: TaskSnapshot, reason: str) -> TaskActionResult:
    return TaskActionResult(False, task, reason)


def delete_task_record(store: TaskStore, task_id: int) -> TaskActionResult:
    """Delete one terminal, unclaimed task using its current snapshot."""
    task = store.find_task(task_id)
    if task is None or float(getattr(task, "archived_at", 0) or 0) > 0:
        return TaskActionResult(False, None, "任务不存在或已过期")
    if "delete" not in available_lifecycle_actions(task):
        if str(task.claimed_by or "").strip():
            return _failed_result(task, "任务正在执行，请稍后再试")
        return _failed_result(task, "任务尚未结束，无法删除")
    if not store.archive_task(task.id, actor="manual", reason="user_delete", expected_updated_at=task.updated_at):
        return _failed_result(task, "任务状态已变化，请刷新后重试")
    return TaskActionResult(True, task, "任务已归档")


def delete_task_record_and_submission(
    task_store: TaskStore,
    submission_store: Any,
    task_id: int,
) -> TaskActionResult:
    """Delete one terminal task and its linked submission record together."""
    task = task_store.find_task(task_id)
    if task is None:
        return TaskActionResult(False, None, "任务不存在或已过期")
    submission_id = task.submission_id or task.metadata.get("submission_id")
    result = delete_task_record(task_store, task_id)
    if not result.applied:
        return result
    submission_removed = True
    if submission_id not in (None, "") and submission_store is not None:
        try:
            submission_removed = bool(submission_store.delete_submission(int(submission_id)))
        except (AttributeError, TypeError, ValueError):
            submission_removed = False
    reason = result.reason
    if submission_id not in (None, "") and not submission_removed:
        reason = f"{result.reason}；提交记录未能删除"
    return TaskActionResult(True, result.task, reason)


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
    actions = available_task_actions(task, max_retries, store=store)
    if action not in actions:
        if str(task.claimed_by or "").strip():
            reason = "任务正在执行，请稍后再试"
        elif action == "retry":
            reason = decide_retry(task, max_retries=max_retries).reason
        elif action == "reprocess":
            reason = "当前任务不支持从头重跑"
        elif action == "resume_organizing":
            reason = "当前任务不满足继续整理条件"
        else:
            reason = "当前任务不支持该操作"
        return TaskActionResult(False, task, reason)

    _, target_message = _messages(actor, action)
    target_stage = task.current_stage
    if action == "retry":
        target_stage = decide_retry(task, max_retries=max_retries).stage
    elif action in {"emby", "restore"}:
        target_stage = TaskStage.EMBY_CONFIRMED
    elif action == "resume_organizing":
        target_stage = TaskStage.ORGANIZING
    elif action == "reprocess":
        target_stage = reprocess_stage_for(task)

    command_type = _ACTION_COMMANDS[action]
    store.enqueue_command(
        task.id,
        command_type,
        {"target_stage": target_stage.value, "message": target_message},
        idempotency_key=command_key(
            command_type,
            task.id,
            task.current_stage.value,
            task.status.value,
            f"{float(task.updated_at):.6f}",
        ),
        actor=actor,
        source="task-action",
    )
    return TaskActionResult(True, task, target_message)
