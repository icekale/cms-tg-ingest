from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import TaskStage, _SUCCESS_FLOW


STRM_MODES = frozenset({"shared", "direct"})
STRM_MODE_LABELS = {"shared": "共享 STRM", "direct": "直链 STRM"}

_LOCKED_STAGES = frozenset(
    {
        TaskStage.SHARE_ALIAS_PREPARED,
        TaskStage.OWN_SHARE_CREATED,
        TaskStage.SHARE_VALIDATED,
        TaskStage.SHARE_SYNC_SUBMITTED,
        TaskStage.STRM_READY,
        TaskStage.CMS_DELETE_SETTLED,
        TaskStage.MOVED,
        TaskStage.EMBY_CONFIRMED,
        TaskStage.CLEANED,
    }
)

_DIRECT_SUCCESS_FLOW = {
    TaskStage.RECEIVED: TaskStage.ORGANIZING,
    TaskStage.CLOUD_DOWNLOADING: TaskStage.ORGANIZING,
    TaskStage.ORGANIZING: TaskStage.RECOGNIZING,
    TaskStage.RECOGNIZING: TaskStage.STRM_READY,
    TaskStage.STRM_READY: TaskStage.MOVED,
    TaskStage.MOVED: TaskStage.EMBY_CONFIRMED,
}


def normalize_strm_mode(value: str | None, default: str = "shared") -> str:
    fallback = str(default).strip().lower()
    if fallback not in STRM_MODES:
        raise ValueError(f"invalid STRM mode: {default!r}")
    normalized = fallback if value is None or not str(value).strip() else str(value).strip().lower()
    if normalized not in STRM_MODES:
        raise ValueError(f"invalid STRM mode: {value!r}")
    return normalized


def _task_metadata(task: Any) -> Mapping[str, Any]:
    if isinstance(task, Mapping):
        metadata = task.get("metadata")
    else:
        metadata = getattr(task, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def effective_task_strm_mode(
    task: Any,
    default_mode: str = "shared",
    legacy_workflow_mode: str = "",
) -> str:
    metadata = _task_metadata(task)
    metadata_mode = metadata.get("strm_mode")
    if metadata_mode is not None and str(metadata_mode).strip():
        return normalize_strm_mode(metadata_mode)

    legacy_mode = str(legacy_workflow_mode or "").strip().lower()
    if not legacy_mode:
        legacy_mode = str(metadata.get("workflow_mode") or "").strip().lower()
    if legacy_mode == "self_share_sync":
        return "shared"
    if legacy_mode == "direct":
        return "direct"
    return normalize_strm_mode(default_mode)


def is_strm_mode_locked(stage: TaskStage | str) -> bool:
    return TaskStage(stage) in _LOCKED_STAGES


def next_stage_for_mode(stage: TaskStage | str, strm_mode: str = "shared") -> TaskStage | None:
    target_stage = TaskStage(stage)
    normalized_mode = normalize_strm_mode(strm_mode)
    if normalized_mode == "direct":
        return _DIRECT_SUCCESS_FLOW.get(target_stage)
    return _SUCCESS_FLOW.get(target_stage)
