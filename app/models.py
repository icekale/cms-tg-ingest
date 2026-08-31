from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStage(str, Enum):
    RECEIVED = "received"
    CLOUD_DOWNLOADING = "cloud_downloading"
    CMS_SUBMITTED = "cms_submitted"
    ORGANIZING = "organizing"
    RECOGNIZING = "recognizing"
    ORGANIZED = "organized"
    SHARE_ALIAS_PREPARED = "share_alias_prepared"
    OWN_SHARE_CREATED = "own_share_created"
    SHARE_VALIDATED = "share_validated"
    SHARE_SYNC_SUBMITTED = "share_sync_submitted"
    STRM_READY = "strm_ready"
    CMS_DELETE_SETTLED = "cms_delete_settled"
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
    CANCELLED = "cancelled"


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


class StageOutcome(str, Enum):
    COMPLETE = "complete"
    DEFER = "defer"
    NEEDS_ACTION = "needs_action"
    FAILED = "failed"


@dataclass(frozen=True)
class OperationCompletion:
    operation_key: str
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""


@dataclass(frozen=True)
class StageCheckpoint:
    media: dict[str, Any] = field(default_factory=dict)
    share: dict[str, Any] = field(default_factory=dict)
    move: dict[str, Any] = field(default_factory=dict)
    emby: dict[str, Any] = field(default_factory=dict)
    cleanup: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    targets: tuple[dict[str, Any], ...] = ()
    operations: tuple[OperationCompletion, ...] = ()


@dataclass(frozen=True)
class StageResult:
    outcome: StageOutcome
    message: str
    metadata: dict[str, object] = field(default_factory=dict)
    delay_seconds: float = 0
    error_type: str = ""
    error_detail: str = ""
    checkpoint: StageCheckpoint = field(default_factory=StageCheckpoint)

    @classmethod
    def complete(
        cls,
        message: str,
        metadata: dict[str, object] | None = None,
        checkpoint: StageCheckpoint | None = None,
    ) -> "StageResult":
        return cls(StageOutcome.COMPLETE, message, metadata or {}, checkpoint=checkpoint or StageCheckpoint())

    @classmethod
    def defer(
        cls,
        message: str,
        delay_seconds: float,
        metadata: dict[str, object] | None = None,
        checkpoint: StageCheckpoint | None = None,
    ) -> "StageResult":
        return cls(
            StageOutcome.DEFER,
            message,
            metadata or {},
            delay_seconds=max(1.0, float(delay_seconds)),
            checkpoint=checkpoint or StageCheckpoint(),
        )

    @classmethod
    def needs_action(
        cls,
        message: str,
        metadata: dict[str, object] | None = None,
        checkpoint: StageCheckpoint | None = None,
    ) -> "StageResult":
        return cls(
            StageOutcome.NEEDS_ACTION,
            message,
            metadata or {},
            error_type="needs_action",
            checkpoint=checkpoint or StageCheckpoint(),
        )

    @classmethod
    def failed(
        cls,
        message: str,
        error_type: str = "stage_failed",
        error_detail: str = "",
        metadata: dict[str, object] | None = None,
        checkpoint: StageCheckpoint | None = None,
    ) -> "StageResult":
        return cls(
            StageOutcome.FAILED,
            message,
            metadata or {},
            error_type=error_type,
            error_detail=error_detail,
            checkpoint=checkpoint or StageCheckpoint(),
        )


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
    source_type: str = "share"
    source_key: str = ""
    chat_id: str = ""
    submission_id: int | None = None
    next_run_at: float = 0
    claimed_by: str = ""
    claimed_at: float = 0
    claim_token: str = ""
    claim_heartbeat_at: float = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0
    updated_at: float = 0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TaskSnapshot":
        metadata_raw = str(row.get("metadata_json") or "{}").strip() or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        submission_raw = row.get("submission_id")
        submission_id = int(submission_raw) if submission_raw not in (None, "") else None
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
            source_type=str(row.get("source_type") or "share"),
            source_key=str(row.get("source_key") or ""),
            chat_id=str(row.get("chat_id") or ""),
            submission_id=submission_id,
            next_run_at=float(row.get("next_run_at") or 0),
            claimed_by=str(row.get("claimed_by") or ""),
            claimed_at=float(row.get("claimed_at") or 0),
            claim_token=str(row.get("claim_token") or ""),
            claim_heartbeat_at=float(row.get("claim_heartbeat_at") or 0),
            metadata=metadata,
            created_at=float(row.get("created_at") or 0),
            updated_at=float(row.get("updated_at") or 0),
        )


@dataclass(frozen=True)
class TaskOperation:
    id: int
    task_id: int
    operation_key: str
    operation_type: str
    status: str
    request: dict[str, Any]
    result: dict[str, Any]
    attempt_count: int
    last_error: str
    created_at: float
    started_at: float
    finished_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TaskOperation":
        def decode_object(value: Any) -> dict[str, Any]:
            try:
                decoded = json.loads(str(value or "{}").strip() or "{}")
            except Exception:
                return {}
            return decoded if isinstance(decoded, dict) else {}

        return cls(
            id=int(row["id"]),
            task_id=int(row["task_id"]),
            operation_key=str(row.get("operation_key") or ""),
            operation_type=str(row.get("operation_type") or ""),
            status=str(row.get("status") or "prepared"),
            request=decode_object(row.get("request_json")),
            result=decode_object(row.get("result_json")),
            attempt_count=int(row.get("attempt_count") or 0),
            last_error=str(row.get("last_error") or ""),
            created_at=float(row.get("created_at") or 0),
            started_at=float(row.get("started_at") or 0),
            finished_at=float(row.get("finished_at") or 0),
            updated_at=float(row.get("updated_at") or 0),
        )


_SUCCESS_FLOW = {
    TaskStage.RECEIVED: TaskStage.ORGANIZING,
    TaskStage.CLOUD_DOWNLOADING: TaskStage.ORGANIZING,
    TaskStage.ORGANIZING: TaskStage.RECOGNIZING,
    TaskStage.RECOGNIZING: TaskStage.SHARE_ALIAS_PREPARED,
    TaskStage.CMS_SUBMITTED: TaskStage.ORGANIZED,
    TaskStage.ORGANIZED: TaskStage.SHARE_ALIAS_PREPARED,
    TaskStage.SHARE_ALIAS_PREPARED: TaskStage.OWN_SHARE_CREATED,
    TaskStage.OWN_SHARE_CREATED: TaskStage.SHARE_VALIDATED,
    TaskStage.SHARE_VALIDATED: TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.SHARE_SYNC_SUBMITTED: TaskStage.STRM_READY,
    TaskStage.STRM_READY: TaskStage.CMS_DELETE_SETTLED,
    TaskStage.CMS_DELETE_SETTLED: TaskStage.MOVED,
    TaskStage.MOVED: TaskStage.EMBY_CONFIRMED,
    TaskStage.EMBY_CONFIRMED: TaskStage.CLEANED,
}


def next_stage_after_success(stage: TaskStage, strm_mode: str = "shared") -> TaskStage | None:
    from .strm_mode import next_stage_for_mode

    return next_stage_for_mode(stage, strm_mode)


def terminal_stages() -> set[TaskStage]:
    return {TaskStage.CLEANED, TaskStage.NEEDS_ACTION, TaskStage.FAILED}
