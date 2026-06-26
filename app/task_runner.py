from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .models import TaskSnapshot, TaskStage, TaskStatus, next_stage_after_success
from .task_store import TaskStore

LOG = logging.getLogger(__name__)


_GLOBAL_115_LOCK_STAGES = {
    TaskStage.RECEIVED,
    TaskStage.ORGANIZING,
    TaskStage.OWN_SHARE_CREATED,
    TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.CLEANED,
}
_DESTINATION_LOCK_STAGES = {
    TaskStage.STRM_READY,
    TaskStage.MOVED,
    TaskStage.EMBY_CONFIRMED,
}
_ORGANIZING_TIMEOUT_MESSAGES = {"等待 CMS 整理完成"}
_ORGANIZING_MAX_DEFER_COUNT = 30
_STAGE_MAX_DEFER_COUNT = {
    TaskStage.ORGANIZING: 30,
    TaskStage.STRM_READY: 20,
    TaskStage.EMBY_CONFIRMED: 20,
}
_DEFER_METADATA_KEYS = ("_defer_stage", "_defer_message", "_defer_count")


def _without_defer_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in metadata.items() if key not in _DEFER_METADATA_KEYS}


def _lock_metadata_for_task(task: TaskSnapshot) -> dict[str, object]:
    if task.current_stage in _GLOBAL_115_LOCK_STAGES:
        return {
            "_lock_key": "115:global",
            "_lock_reason": "115/CMS 全局阶段",
            "_lock_waiting": False,
            "_lock_owner_task_id": "",
        }
    if task.current_stage in _DESTINATION_LOCK_STAGES:
        dest_path = str(task.metadata.get("dest_path") or task.metadata.get("emby_path") or "").strip()
        if dest_path:
            return {
                "_lock_key": f"dest:{dest_path}",
                "_lock_reason": "媒体库目录阶段",
                "_lock_waiting": False,
                "_lock_owner_task_id": "",
            }
        tmdb_id = str(task.tmdb_id or task.metadata.get("tmdb_id") or "").strip()
        if tmdb_id:
            return {
                "_lock_key": f"tmdb:{tmdb_id}",
                "_lock_reason": "TMDB 条目阶段",
                "_lock_waiting": False,
                "_lock_owner_task_id": "",
            }
    return {}


def _defer_count(metadata: dict[str, object], stage: str, message: str) -> int:
    if metadata.get("_defer_stage") != stage or metadata.get("_defer_message") != message:
        return 1
    try:
        previous = int(metadata.get("_defer_count") or 0)
    except (TypeError, ValueError):
        previous = 0
    return max(0, previous) + 1


def _defer_delay(base_delay_seconds: float, count: int) -> float:
    if count <= 2:
        return base_delay_seconds
    if count <= 4:
        return max(base_delay_seconds, 30.0)
    if count <= 8:
        return max(base_delay_seconds, 60.0)
    return max(base_delay_seconds, 120.0)


class StageOutcome(str, Enum):
    COMPLETE = "complete"
    DEFER = "defer"
    NEEDS_ACTION = "needs_action"
    FAILED = "failed"


@dataclass(frozen=True)
class StageResult:
    outcome: StageOutcome
    message: str
    metadata: dict[str, object] = field(default_factory=dict)
    delay_seconds: float = 0
    error_type: str = ""
    error_detail: str = ""

    @classmethod
    def complete(cls, message: str, metadata: dict[str, object] | None = None) -> "StageResult":
        return cls(StageOutcome.COMPLETE, message, metadata or {})

    @classmethod
    def defer(cls, message: str, delay_seconds: float, metadata: dict[str, object] | None = None) -> "StageResult":
        return cls(StageOutcome.DEFER, message, metadata or {}, delay_seconds=max(1.0, float(delay_seconds)))

    @classmethod
    def needs_action(cls, message: str, metadata: dict[str, object] | None = None) -> "StageResult":
        return cls(StageOutcome.NEEDS_ACTION, message, metadata or {}, error_type="needs_action")

    @classmethod
    def failed(
        cls,
        message: str,
        error_type: str = "stage_failed",
        error_detail: str = "",
        metadata: dict[str, object] | None = None,
    ) -> "StageResult":
        return cls(StageOutcome.FAILED, message, metadata or {}, error_type=error_type, error_detail=error_detail)


class TaskWorkflow(Protocol):
    def run_stage(self, task: TaskSnapshot) -> StageResult:
        raise NotImplementedError


class TaskRunner:
    def __init__(
        self,
        store: TaskStore,
        workflow: TaskWorkflow,
        *,
        worker_id: str = "task-runner",
        interval_seconds: float = 5,
        now: Callable[[], float] | None = None,
    ):
        self.store = store
        self.workflow = workflow
        self.worker_id = worker_id
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.now = now or time.time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_claims_cleared = False

    def run_once(self) -> bool:
        self._clear_startup_claims_once()
        task = self.store.claim_next_runnable(self.worker_id, now=self.now())
        if task is None:
            return False
        task = self._prepare_lock(task)
        if task is None:
            return True
        try:
            result = self.workflow.run_stage(task)
        except Exception as exc:
            LOG.exception("Task stage failed task_id=%s stage=%s", task.id, task.current_stage.value)
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.FAILED,
                str(exc) or exc.__class__.__name__,
                error_type="stage_exception",
                error_summary=str(exc) or exc.__class__.__name__,
                error_detail=repr(exc),
                increment_retry=True,
                clear_claim=True,
            )
            return True
        self._apply_result(task, result)
        return True

    def _clear_startup_claims_once(self) -> None:
        if self._startup_claims_cleared:
            return
        self._startup_claims_cleared = True
        released = self.store.clear_worker_claims(self.worker_id, now=self.now())
        if released:
            LOG.warning("Released %s stale task claims for worker_id=%s", released, self.worker_id)

    def _prepare_lock(self, task: TaskSnapshot) -> TaskSnapshot | None:
        lock_metadata = _lock_metadata_for_task(task)
        if not lock_metadata:
            return task
        lock_key = str(lock_metadata.get("_lock_key") or "")
        wait_message = f"等待资源锁: {lock_metadata.get('_lock_reason', '')}"

        def conflicts_with_holder(holder: TaskSnapshot) -> bool:
            if str(holder.metadata.get("_lock_key") or "") == lock_key:
                return True
            return str(_lock_metadata_for_task(holder).get("_lock_key") or "") == lock_key

        result = self.store.claim_task_lock(
            task.id,
            lock_metadata,
            conflicts_with_holder,
            wait_message=wait_message,
            next_run_at=self.now() + self.interval_seconds,
            now=self.now(),
        )
        if result.holder:
            return None
        return result.task

    def _apply_result(self, task: TaskSnapshot, result: StageResult) -> None:
        current = self.store.find_task(task.id)
        if (
            current is None
            or current.current_stage != task.current_stage
            or current.claimed_by != self.worker_id
        ):
            LOG.warning(
                "Discarded stale task result task_id=%s stage=%s worker_id=%s",
                task.id,
                task.current_stage.value,
                self.worker_id,
            )
            return
        now = self.now()
        if result.outcome == StageOutcome.COMPLETE:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.SUCCEEDED,
                result.message,
                metadata_patch=_without_defer_metadata(result.metadata),
                metadata_delete_keys=_DEFER_METADATA_KEYS,
                clear_claim=True,
            )
            next_stage = next_stage_after_success(task.current_stage)
            if next_stage:
                self.store.enqueue_task(task.id, next_stage, message="等待执行", next_run_at=now)
            return
        if result.outcome == StageOutcome.DEFER:
            defer_count = _defer_count(task.metadata, task.current_stage.value, result.message)
            metadata_patch = {
                **result.metadata,
                "_defer_stage": task.current_stage.value,
                "_defer_message": result.message,
                "_defer_count": defer_count,
            }
            max_defer_count = _STAGE_MAX_DEFER_COUNT.get(task.current_stage)
            if (
                task.current_stage == TaskStage.ORGANIZING
                and result.message in _ORGANIZING_TIMEOUT_MESSAGES
                and defer_count >= _ORGANIZING_MAX_DEFER_COUNT
            ):
                metadata_patch.update(
                    {
                        "retry_from_stage": task.current_stage.value,
                        "retry_stage": TaskStage.ORGANIZING.value,
                        "_lock_key": "",
                        "_lock_waiting": False,
                        "_lock_owner_task_id": "",
                    }
                )
                self.store.record_event(
                    task.id,
                    TaskStage.NEEDS_ACTION,
                    TaskStatus.NEEDS_ACTION,
                    "CMS 整理等待超时，请人工检查分享内容或稍后重试",
                    metadata_patch=_without_defer_metadata(metadata_patch),
                    metadata_delete_keys=_DEFER_METADATA_KEYS,
                    error_type="organizing_timeout",
                    error_summary="CMS 整理等待超时，请人工检查分享内容或稍后重试",
                    clear_claim=True,
                )
                return
            if max_defer_count is not None and defer_count >= max_defer_count:
                error_summary = f"{result.message} 等待超时，请人工检查后重试"
                metadata_patch.update(
                    {
                        "retry_from_stage": task.current_stage.value,
                        "retry_stage": task.current_stage.value,
                        "_lock_key": "",
                        "_lock_waiting": False,
                        "_lock_owner_task_id": "",
                    }
                )
                self.store.record_event(
                    task.id,
                    TaskStage.NEEDS_ACTION,
                    TaskStatus.NEEDS_ACTION,
                    error_summary,
                    metadata_patch=_without_defer_metadata(metadata_patch),
                    metadata_delete_keys=_DEFER_METADATA_KEYS,
                    error_type="stage_wait_timeout",
                    error_summary=error_summary,
                    clear_claim=True,
                )
                return
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.RUNNING,
                result.message,
                metadata_patch=metadata_patch,
                next_run_at=now + _defer_delay(result.delay_seconds, defer_count),
                clear_claim=True,
            )
            return
        if result.outcome == StageOutcome.NEEDS_ACTION:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.NEEDS_ACTION,
                result.message,
                metadata_patch=_without_defer_metadata(result.metadata),
                metadata_delete_keys=_DEFER_METADATA_KEYS,
                error_type=result.error_type or "needs_action",
                error_summary=result.message,
                error_detail=result.error_detail,
                clear_claim=True,
            )
            return
        self.store.record_event(
            task.id,
            task.current_stage,
            TaskStatus.FAILED,
            result.message,
            metadata_patch=_without_defer_metadata(result.metadata),
            metadata_delete_keys=_DEFER_METADATA_KEYS,
            error_type=result.error_type or "stage_failed",
            error_summary=result.message,
            error_detail=result.error_detail,
            increment_retry=True,
            clear_claim=True,
        )

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            did_work = self.run_once()
            if not did_work:
                self._stop.wait(self.interval_seconds)
