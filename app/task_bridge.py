from __future__ import annotations

from typing import Any

from .models import TaskSnapshot, TaskStage, TaskStatus
from .task_store import TaskStore


def _text(value: Any) -> str:
    return str(value or "").strip()


def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    share_code = _text(row.get("share_code"))
    receive_code = _text(row.get("receive_code"))
    url = _text(row.get("url"))
    return share_code, receive_code, url


def _metadata(row: dict[str, Any], extra: dict[str, Any]) -> dict[str, str]:
    title = _text(extra.get("title") or row.get("title") or row.get("own_share_file_name"))
    tmdb_id = _text(extra.get("tmdb_id") or row.get("tmdb_id"))
    category = _text(
        extra.get("category")
        or row.get("category_final")
        or row.get("category_choice")
        or row.get("category")
    )
    return {"title": title, "tmdb_id": tmdb_id, "category": category}


def _runtime_metadata(row: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "own_share_file_id",
        "own_share_file_name",
        "share_alias_name",
        "share_alias_level",
        "own_share_code",
        "own_share_receive_code",
        "own_share_url",
        "share_sync_status",
        "share_validation_status",
        "share_validation_error",
        "source_path",
        "dest_path",
        "category_final",
        "move_status",
        "move_error",
        "emby_status",
        "emby_item_id",
        "emby_title",
        "emby_path",
        "emby_parent",
        "cleanup_status",
        "cleanup_file_id",
        "cleanup_error",
    )
    metadata = {key: row.get(key) for key in keys if row.get(key) not in (None, "")}
    metadata.update({str(key): value for key, value in extra.items() if value not in (None, "")})
    return metadata


def _last_event_matches(store: TaskStore, task_id: int, stage: TaskStage, status: TaskStatus, message: str, error_summary: str) -> bool:
    events = store.list_events(task_id)
    if not events:
        return False
    last = events[-1]
    return (
        last.get("stage") == stage.value
        and last.get("status") == status.value
        and last.get("message") == message
        and _text(last.get("error_type")) == ""
        and error_summary == ""
    )


def ensure_task_for_link(
    task_store: TaskStore | None,
    share_code: str,
    receive_code: str,
    url: str,
) -> TaskSnapshot | None:
    if task_store is None:
        return None
    task = task_store.upsert_task(_text(share_code), _text(receive_code), _text(url))
    if not task_store.list_events(task.id):
        task = task_store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.PENDING, "收到链接")
    return task


def record_submission_event(
    task_store: TaskStore | None,
    row: dict[str, Any],
    stage: TaskStage,
    status: TaskStatus,
    message: str,
    **metadata: Any,
) -> TaskSnapshot | None:
    if task_store is None:
        return None
    share_code, receive_code, url = _row_key(row)
    if not share_code:
        return None
    task = task_store.upsert_task(share_code, receive_code, url)
    error_summary = _text(metadata.get("error_summary"))
    if _last_event_matches(task_store, task.id, stage, status, message, error_summary):
        return task_store.find_task(task.id)
    meta = _metadata(row, metadata)
    return task_store.record_event(
        task.id,
        stage,
        status,
        message,
        title=meta["title"] or None,
        tmdb_id=meta["tmdb_id"] or None,
        category=meta["category"] or None,
        error_type=_text(metadata.get("error_type")),
        error_summary=error_summary,
        error_detail=_text(metadata.get("error_detail")),
        increment_retry=bool(metadata.get("increment_retry", False)),
        submission_id=int(row["id"]) if row.get("id") not in (None, "") else None,
        metadata_patch=_runtime_metadata(row, metadata),
    )


def record_failure(
    task_store: TaskStore | None,
    row_or_key: dict[str, Any],
    stage: TaskStage,
    error_summary: str,
    error_type: str = "",
    error_detail: str = "",
) -> TaskSnapshot | None:
    return record_submission_event(
        task_store,
        row_or_key,
        stage,
        TaskStatus.FAILED,
        error_summary,
        error_type=error_type,
        error_summary=error_summary,
        error_detail=error_detail,
        increment_retry=True,
    )


def sync_task_from_submission(
    task_store: TaskStore | None,
    row: dict[str, Any],
    message: str = "同步现有任务状态",
) -> TaskSnapshot | None:
    if task_store is None:
        return None
    cleanup_status = _text(row.get("cleanup_status")).lower()
    emby_status = _text(row.get("emby_status")).lower()
    move_status = _text(row.get("move_status")).lower()
    share_sync_status = _text(row.get("share_sync_status")).lower()
    share_validation_status = _text(row.get("share_validation_status")).lower()
    share_alias_name = _text(row.get("share_alias_name"))
    own_share_code = _text(row.get("own_share_code"))
    status = _text(row.get("status")).lower()

    if cleanup_status in {"deleted", "skipped"} and move_status == "moved" and emby_status == "confirmed":
        return record_submission_event(task_store, row, TaskStage.CLEANED, TaskStatus.SUCCEEDED, message)
    if emby_status == "confirmed":
        return record_submission_event(task_store, row, TaskStage.EMBY_CONFIRMED, TaskStatus.SUCCEEDED, message)
    if emby_status == "timeout":
        return record_submission_event(
            task_store,
            row,
            TaskStage.EMBY_CONFIRMED,
            TaskStatus.FAILED,
            message,
            error_summary="Emby 确认超时",
        )
    if emby_status == "disabled":
        return record_submission_event(
            task_store,
            row,
            TaskStage.EMBY_CONFIRMED,
            TaskStatus.NEEDS_ACTION,
            message,
            error_summary="Emby 确认未启用",
        )
    if move_status == "moved":
        return record_submission_event(task_store, row, TaskStage.MOVED, TaskStatus.SUCCEEDED, message)
    if move_status in {"error", "failed"}:
        return record_submission_event(
            task_store,
            row,
            TaskStage.MOVED,
            TaskStatus.FAILED,
            message,
            error_summary=_text(row.get("move_error")) or "STRM 移动失败",
        )
    if share_sync_status in {"submitted", "restore_submitted"}:
        return record_submission_event(task_store, row, TaskStage.SHARE_SYNC_SUBMITTED, TaskStatus.RUNNING, message)
    if share_validation_status in {"valid", "warning"}:
        return record_submission_event(task_store, row, TaskStage.SHARE_VALIDATED, TaskStatus.SUCCEEDED, message)
    if own_share_code:
        return record_submission_event(task_store, row, TaskStage.OWN_SHARE_CREATED, TaskStatus.SUCCEEDED, message)
    if share_alias_name:
        return record_submission_event(task_store, row, TaskStage.SHARE_ALIAS_PREPARED, TaskStatus.SUCCEEDED, message)
    if status in {"submitted", "pending", "unknown", "done", "success", "completed"}:
        return record_submission_event(task_store, row, TaskStage.CMS_SUBMITTED, TaskStatus.RUNNING, message)
    if status in {"failed", "error"}:
        return record_submission_event(
            task_store,
            row,
            TaskStage.CMS_SUBMITTED,
            TaskStatus.FAILED,
            message,
            error_summary=_text(row.get("last_error")) or "CMS 任务失败",
        )
    return record_submission_event(task_store, row, TaskStage.RECEIVED, TaskStatus.PENDING, message)
