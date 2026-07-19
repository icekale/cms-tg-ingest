from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .clients.p115 import P115RiskControlError
from .models import TaskSnapshot, TaskStage, TaskStatus, next_stage_after_success
from .task_store import TaskStore

LOG = logging.getLogger(__name__)


_GLOBAL_115_LOCK_STAGES = {
    TaskStage.RECEIVED,
    TaskStage.CLOUD_DOWNLOADING,
    TaskStage.ORGANIZING,
    TaskStage.SHARE_ALIAS_PREPARED,
    TaskStage.OWN_SHARE_CREATED,
    TaskStage.SHARE_VALIDATED,
    TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.CLEANED,
}
_DESTINATION_LOCK_STAGES = {
    TaskStage.STRM_READY,
    TaskStage.CMS_DELETE_SETTLED,
    TaskStage.MOVED,
    TaskStage.EMBY_CONFIRMED,
}
_ORGANIZING_TIMEOUT_MESSAGES = {"等待 CMS 整理完成"}
_ORGANIZING_MAX_DEFER_COUNT = 30
_STAGE_MAX_DEFER_COUNT = {
    TaskStage.ORGANIZING: 30,
    TaskStage.STRM_READY: 20,
    TaskStage.CMS_DELETE_SETTLED: 30,
    TaskStage.EMBY_CONFIRMED: 20,
}
_DEFER_METADATA_KEYS = ("_defer_stage", "_defer_message", "_defer_count")
_HEARTBEAT_INTERVAL_SECONDS = 30.0


def _without_defer_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in metadata.items() if key not in _DEFER_METADATA_KEYS}


def _stage_timing_metadata(task: TaskSnapshot, finished_at: float) -> dict[str, float]:
    started_at = float(task.claimed_at or finished_at)
    next_run_at = float(task.next_run_at if task.next_run_at is not None else started_at)
    return {
        "stage_started_at": started_at,
        "stage_finished_at": float(finished_at),
        "stage_elapsed_seconds": round(max(0.0, float(finished_at) - started_at), 3),
        "stage_wait_seconds": round(max(0.0, started_at - next_run_at), 3),
    }


def _p115_request_count(client: object | None) -> int | None:
    if client is None or not hasattr(client, "request_count"):
        return None
    try:
        return int(getattr(client, "request_count"))
    except (TypeError, ValueError):
        return None


def _p115_request_metadata(task: TaskSnapshot, before: int | None, after: int | None) -> dict[str, int]:
    if before is None or after is None:
        return {}
    stage_count = max(0, after - before)
    try:
        previous_total = int(task.metadata.get("p115_total_request_count") or 0)
    except (TypeError, ValueError):
        previous_total = 0
    return {
        "p115_stage_request_count": stage_count,
        "p115_total_request_count": max(0, previous_total) + stage_count,
        "p115_request_count_snapshot": after,
    }


def _metric_by_stage(existing: object, stage: TaskStage, value: float | int) -> dict[str, float | int]:
    result = dict(existing) if isinstance(existing, dict) else {}
    result[stage.value] = value
    return result


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
        risk_cooldown_seconds: float = 900,
        p115_client: object | None = None,
        now: Callable[[], float] | None = None,
    ):
        self.store = store
        self.workflow = workflow
        self.worker_id = worker_id
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.risk_cooldown_seconds = max(1.0, float(risk_cooldown_seconds))
        self.p115_client = p115_client
        self.now = now or time.time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._startup_claims_cleared = False
        self._p115_risk_cooldown_until = 0.0
        self._last_heartbeat_at = 0.0

    def _record_heartbeat(self) -> None:
        now = self.now()
        if now - self._last_heartbeat_at < _HEARTBEAT_INTERVAL_SECONDS:
            return
        try:
            self.store.set_runtime_state("task_runner", "running", updated_at=now)
            self._last_heartbeat_at = now
        except Exception:
            LOG.debug("Failed to record TaskRunner heartbeat", exc_info=True)

    def _run_heartbeat(self) -> None:
        while not self._stop.is_set():
            self._record_heartbeat()
            self._stop.wait(_HEARTBEAT_INTERVAL_SECONDS)

    def run_once(self) -> bool:
        self._clear_startup_claims_once()
        task = self.store.claim_next_runnable(self.worker_id, now=self.now())
        if task is None:
            return False
        if self._defer_for_p115_risk_cooldown(task):
            return True
        task = self._prepare_lock(task)
        if task is None:
            return True
        p115_before = _p115_request_count(self.p115_client)
        try:
            result = self.workflow.run_stage(task)
        except P115RiskControlError as exc:
            self._p115_risk_cooldown_until = self.now() + self.risk_cooldown_seconds
            message = "115 风控/频率限制，已暂停自动重试；请稍后在 TG/Web 手动重试。"
            p115_metadata = _p115_request_metadata(task, p115_before, _p115_request_count(self.p115_client))
            observability_metadata = {}
            if "p115_stage_request_count" in p115_metadata:
                observability_metadata["p115_request_counts_by_stage"] = _metric_by_stage(
                    task.metadata.get("p115_request_counts_by_stage"),
                    task.current_stage,
                    p115_metadata["p115_stage_request_count"],
                )
            metadata_patch = {
                **p115_metadata,
                **observability_metadata,
                "retry_from_stage": task.current_stage.value,
                "retry_stage": task.current_stage.value,
                "p115_risk_cooldown_until": self._p115_risk_cooldown_until,
                "_lock_key": "",
                "_lock_waiting": False,
                "_lock_owner_task_id": "",
            }
            self.store.record_event(
                task.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                message,
                metadata_patch=metadata_patch,
                error_type="p115_risk_control",
                error_summary=message,
                error_detail=str(exc),
                clear_claim=True,
            )
            return True
        except Exception as exc:
            LOG.exception("Task stage failed task_id=%s stage=%s", task.id, task.current_stage.value)
            p115_metadata = _p115_request_metadata(task, p115_before, _p115_request_count(self.p115_client))
            observability_metadata = {}
            if "p115_stage_request_count" in p115_metadata:
                observability_metadata["p115_request_counts_by_stage"] = _metric_by_stage(
                    task.metadata.get("p115_request_counts_by_stage"),
                    task.current_stage,
                    p115_metadata["p115_stage_request_count"],
                )
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.FAILED,
                str(exc) or exc.__class__.__name__,
                metadata_patch=p115_metadata | observability_metadata,
                error_type="stage_exception",
                error_summary=str(exc) or exc.__class__.__name__,
                error_detail=repr(exc),
                increment_retry=True,
                clear_claim=True,
            )
            return True
        self._apply_result(task, result, p115_before=p115_before, p115_after=_p115_request_count(self.p115_client))
        return True

    def _defer_for_p115_risk_cooldown(self, task: TaskSnapshot) -> bool:
        now = self.now()
        if task.current_stage not in _GLOBAL_115_LOCK_STAGES or now >= self._p115_risk_cooldown_until:
            return False
        message = "115 风控冷却中，暂停 115/CMS 阶段自动执行"
        self.store.record_event(
            task.id,
            task.current_stage,
            TaskStatus.RUNNING,
            message,
            metadata_patch={
                "p115_risk_cooldown_until": self._p115_risk_cooldown_until,
                "_lock_key": "",
                "_lock_waiting": False,
                "_lock_owner_task_id": "",
            },
            next_run_at=self._p115_risk_cooldown_until,
            error_type="p115_risk_cooldown",
            error_summary=message,
            clear_claim=True,
        )
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

    def _apply_result(
        self,
        task: TaskSnapshot,
        result: StageResult,
        *,
        p115_before: int | None = None,
        p115_after: int | None = None,
    ) -> None:
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
        timing_metadata = _stage_timing_metadata(task, now)
        p115_metadata = _p115_request_metadata(task, p115_before, p115_after)
        observability_metadata = {
            "stage_elapsed_seconds_by_stage": _metric_by_stage(
                task.metadata.get("stage_elapsed_seconds_by_stage"),
                task.current_stage,
                timing_metadata["stage_elapsed_seconds"],
            )
        }
        if "p115_stage_request_count" in p115_metadata:
            observability_metadata["p115_request_counts_by_stage"] = _metric_by_stage(
                task.metadata.get("p115_request_counts_by_stage"),
                task.current_stage,
                p115_metadata["p115_stage_request_count"],
            )
        if result.outcome == StageOutcome.COMPLETE:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.SUCCEEDED,
                result.message,
                metadata_patch=_without_defer_metadata(result.metadata | timing_metadata | p115_metadata | observability_metadata),
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
                **timing_metadata,
                **p115_metadata,
                **observability_metadata,
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
                metadata_patch=_without_defer_metadata(result.metadata | timing_metadata | p115_metadata | observability_metadata),
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
            metadata_patch=_without_defer_metadata(result.metadata | timing_metadata | p115_metadata | observability_metadata),
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
        self._last_heartbeat_at = 0.0
        self._heartbeat_thread = threading.Thread(target=self._run_heartbeat, daemon=True)
        self._heartbeat_thread.start()
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, join_timeout: float = 5) -> None:
        self._stop.set()
        deadline = time.monotonic() + max(0.0, float(join_timeout))
        for thread in (self._thread, self._heartbeat_thread):
            if thread and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self._record_heartbeat()
            did_work = self.run_once()
            if not did_work:
                self._stop.wait(self.interval_seconds)
