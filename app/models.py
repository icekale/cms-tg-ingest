from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TaskStage(str, Enum):
    RECEIVED = "received"
    CMS_SUBMITTED = "cms_submitted"
    ORGANIZING = "organizing"
    RECOGNIZING = "recognizing"
    ORGANIZED = "organized"
    OWN_SHARE_CREATED = "own_share_created"
    SHARE_SYNC_SUBMITTED = "share_sync_submitted"
    STRM_READY = "strm_ready"
    MOVED = "moved"
    EMBY_CONFIRMED = "emby_confirmed"
    CLEANED = "cleaned"
    NEEDS_ACTION = "needs_action"
    FAILED = "failed"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_ACTION = "needs_action"


class RetryAction(str, Enum):
    RETRY_CURRENT_STAGE = "retry_current_stage"
    MANUAL_ACTION_REQUIRED = "manual_action_required"
    NO_RETRY = "no_retry"


@dataclass(frozen=True)
class TaskError:
    error_type: str
    summary: str
    detail: str = ""


@dataclass(frozen=True)
class RetryDecision:
    action: RetryAction
    stage: TaskStage | None
    reason: str


@dataclass(frozen=True)
class TaskSnapshot:
    id: int
    share_code: str
    receive_code: str
    url: str
    title: str
    tmdb_id: str
    category: str
    current_stage: TaskStage
    status: TaskStatus
    error_type: str
    error_summary: str
    retry_count: int
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TaskSnapshot":
        return cls(
            id=int(row["id"]),
            share_code=str(row.get("share_code") or ""),
            receive_code=str(row.get("receive_code") or ""),
            url=str(row.get("url") or ""),
            title=str(row.get("title") or ""),
            tmdb_id=str(row.get("tmdb_id") or ""),
            category=str(row.get("category") or ""),
            current_stage=TaskStage(str(row.get("current_stage") or TaskStage.RECEIVED.value)),
            status=TaskStatus(str(row.get("status") or TaskStatus.PENDING.value)),
            error_type=str(row.get("error_type") or ""),
            error_summary=str(row.get("error_summary") or ""),
            retry_count=int(row.get("retry_count") or 0),
            created_at=float(row.get("created_at") or 0),
            updated_at=float(row.get("updated_at") or 0),
        )


_SUCCESS_FLOW = {
    TaskStage.RECEIVED: TaskStage.ORGANIZING,
    TaskStage.ORGANIZING: TaskStage.RECOGNIZING,
    TaskStage.RECOGNIZING: TaskStage.OWN_SHARE_CREATED,
    TaskStage.CMS_SUBMITTED: TaskStage.ORGANIZED,
    TaskStage.ORGANIZED: TaskStage.OWN_SHARE_CREATED,
    TaskStage.OWN_SHARE_CREATED: TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.SHARE_SYNC_SUBMITTED: TaskStage.STRM_READY,
    TaskStage.STRM_READY: TaskStage.MOVED,
    TaskStage.MOVED: TaskStage.EMBY_CONFIRMED,
    TaskStage.EMBY_CONFIRMED: TaskStage.CLEANED,
}


def next_stage_after_success(stage: TaskStage) -> TaskStage | None:
    return _SUCCESS_FLOW.get(stage)


def terminal_stages() -> set[TaskStage]:
    return {TaskStage.CLEANED, TaskStage.NEEDS_ACTION, TaskStage.FAILED}
