from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable, Protocol

from .clients.p115 import P115RiskControlError
from .models import StageOutcome, StageResult, TaskSnapshot, TaskStage, TaskStatus, next_stage_after_success
from .strm_mode import effective_task_strm_mode
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
    # Waiting on the upstream task's own CMS sync; bounded so a stuck upstream
    # (or lost completion event) cannot head-of-line block the queue forever.
    TaskStage.SHARE_SYNC_SUBMITTED: 30,
}
QUALITY_REPAIR_WAIT_SECONDS = 24 * 60 * 60
QUALITY_REPAIR_METADATA_KEYS = (
    "quality_repair_queued",
    "quality_repair_action",
    "quality_repair_reason",
    "quality_run_id",
    "quality_repair_started_at",
    "quality_repair_deadline_at",
)
_QUALITY_REPAIR_WAIT_STAGES = {
    TaskStage.STRM_READY,
    TaskStage.CMS_DELETE_SETTLED,
    TaskStage.MOVED,
    TaskStage.EMBY_CONFIRMED,
}
_DEFER_METADATA_KEYS = ("_defer_stage", "_defer_message", "_defer_count")
_HEARTBEAT_INTERVAL_SECONDS = 15.0
_P115_RISK_COOLDOWN_STATE_KEY = "115:risk_cooldown_until"
_ACTIVITY_STATE_KEY = "task_runner:activity"


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _without_defer_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in metadata.items() if key not in _DEFER_METADATA_KEYS}


def _stage_timing_metadata(task: TaskSnapshot, finished_at: float) -> dict[str, float]:
    started_at = float(task.claimed_at or finished_at)
    next_run_at = float(task.next_run_at if task.next_run_at is not None else started_at)
    if next_run_at <= 0:
        # enqueue_task(next_run_at=0) means "run immediately"; a zero next run
        # time must not be read as "scheduled at epoch" or the reported wait
        # becomes tens of thousands of hours.
        next_run_at = started_at
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


class TaskWorkflow(Protocol):
    def run_stage(self, task: TaskSnapshot) -> StageResult:
        raise NotImplementedError


class TaskRunner:
    def __init__(
        self,
        store: TaskStore,
        workflow: TaskWorkflow,
        *,
        worker_id: str | None = None,
        interval_seconds: float = 5,
        risk_cooldown_seconds: float = 900,
        p115_client: object | None = None,
        now: Callable[[], float] | None = None,
        claim_stale_after_seconds: int = 300,
    ):
        self.store = store
        self.workflow = workflow
        self.worker_id = str(worker_id or new_worker_id())
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.risk_cooldown_seconds = max(1.0, float(risk_cooldown_seconds))
        self.p115_client = p115_client
        self.claim_stale_after_seconds = max(30, int(claim_stale_after_seconds))
        self.now = now or time.time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._active_claim_lock = threading.Lock()
        self._active_claim: TaskSnapshot | None = None
        self._active_claim_renewal_failed = False
        self._claim_release_pending = False
        self._p115_risk_cooldown_until = self._load_p115_risk_cooldown()
        self._last_heartbeat_at = 0.0
        self._last_claim_attempt_at = 0.0
        self._runner_lease_token = ""

    def _load_p115_risk_cooldown(self) -> float:
        try:
            state = self.store.get_runtime_state(_P115_RISK_COOLDOWN_STATE_KEY)
            return max(0.0, float(state["value"])) if state else 0.0
        except (KeyError, TypeError, ValueError):
            LOG.warning("Invalid persisted 115 risk cooldown state; ignoring it")
            return 0.0

    def _refresh_p115_risk_cooldown(self) -> None:
        persisted_until = self._load_p115_risk_cooldown()
        if persisted_until > self._p115_risk_cooldown_until:
            self._p115_risk_cooldown_until = persisted_until

    def _safe_runtime_state(self, key: str, value: str, *, updated_at: float | None = None) -> bool:
        try:
            self.store.set_runtime_state(key, value, updated_at=updated_at)
            return True
        except Exception:
            LOG.debug("Failed to update TaskRunner runtime state key=%s", key, exc_info=True)
            return False

    def _record_heartbeat(self) -> None:
        now = self.now()
        if now - self._last_heartbeat_at < _HEARTBEAT_INTERVAL_SECONDS:
            return
        try:
            if self.store.refresh_runtime_state_timestamp("task_runner", updated_at=now):
                self._last_heartbeat_at = now
        except Exception:
            LOG.debug("Failed to record TaskRunner heartbeat", exc_info=True)

    def _record_activity(self, now: float | None = None) -> None:
        """Persist a snapshot of what the runner is currently working on.

        Written on the same 15s cadence as the heartbeat so health/doctor can
        show "runner is processing task #N (stage X)" or that the runner has
        been idle — the evidence needed before ever reconsidering multi-worker.
        Never raises.
        """
        current_time = self.now() if now is None else float(now)
        with self._active_claim_lock:
            active = self._active_claim
        try:
            self.store.set_runtime_state(
                _ACTIVITY_STATE_KEY,
                json.dumps(
                    {
                        "active_task_id": int(active.id) if active is not None else 0,
                        "active_stage": active.current_stage.value if active is not None else "",
                        "active_since": float(active.claimed_at or 0) if active is not None else 0.0,
                        "last_claim_attempt_at": self._last_claim_attempt_at,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                updated_at=current_time,
            )
        except Exception:
            LOG.debug("Failed to record TaskRunner activity state", exc_info=True)

    def _run_heartbeat(self) -> None:
        while not self._stop.is_set():
            if self._thread is None or not self._thread.is_alive():
                self._safe_runtime_state("task_runner", "error")
                return
            self._record_heartbeat()
            self._record_activity()
            try:
                self._renew_active_claim()
                if self._runner_lease_token:
                    self.store.renew_runner_lease(self.worker_id, self._runner_lease_token)
            except Exception:
                LOG.warning("Failed to renew active task claim; will retry", exc_info=True)
            self._stop.wait(_HEARTBEAT_INTERVAL_SECONDS)

    def _renew_active_claim(self) -> None:
        with self._active_claim_lock:
            task = self._active_claim
        if task is None:
            return
        renewed = self.store.renew_claim(
            task.id,
            self.worker_id,
            task.claim_token,
            now=self.now(),
        )
        if renewed:
            return
        with self._active_claim_lock:
            if self._active_claim is None or self._active_claim.claim_token != task.claim_token:
                return
            if self._active_claim_renewal_failed:
                return
            self._active_claim_renewal_failed = True
        LOG.warning(
            "Failed to renew active task claim task_id=%s stage=%s worker_id=%s",
            task.id,
            task.current_stage.value,
            self.worker_id,
        )
        # Renewal is dying: the stage may already have external side effects, so
        # flag the task for human review instead of letting it be re-claimed and
        # re-run (which would replay 115/CMS operations). flag_claim_lost is a
        # no-op when another worker now owns the claim.
        try:
            flagged = self.store.flag_claim_lost(
                task.id,
                self.worker_id,
                task.claim_token,
                now=self.now(),
            )
        except Exception:
            LOG.debug("Failed to flag lost claim task_id=%s", task.id, exc_info=True)
            flagged = None
        if flagged is not None:
            LOG.warning(
                "Claim lost for task_id=%s stage=%s: marked needs-action, auto-replay suppressed",
                task.id,
                task.current_stage.value,
            )

    def _set_active_claim(self, task: TaskSnapshot) -> None:
        with self._active_claim_lock:
            self._active_claim = task
            self._active_claim_renewal_failed = False

    def _clear_active_claim(self, task: TaskSnapshot) -> None:
        with self._active_claim_lock:
            if self._active_claim is not None and self._active_claim.claim_token == task.claim_token:
                self._active_claim = None
                self._active_claim_renewal_failed = False

    def run_once(self) -> bool:
        self._last_claim_attempt_at = self.now()
        claim_command = getattr(self.store, "claim_next_command", None)
        if callable(claim_command):
            command = claim_command(self.worker_id, now=self.now())
            if isinstance(command, dict):
                return self._run_command(command)
        if self._claim_release_pending:
            self.store.clear_worker_claims(self.worker_id, now=self.now())
            self._claim_release_pending = False
        task = self.store.claim_next_runnable(
            self.worker_id,
            now=self.now(),
            stale_after_seconds=self.claim_stale_after_seconds,
        )
        if task is None:
            return False
        try:
            return self._run_claimed_task(task)
        except Exception:
            self._release_claims_after_runner_failure()
            raise
        finally:
            self._clear_active_claim(task)

    def _release_claims_after_runner_failure(self) -> None:
        self._claim_release_pending = True
        try:
            self.store.clear_worker_claims(self.worker_id, now=self.now())
        except Exception:
            LOG.exception("Failed to release task claim after runner failure")
        else:
            self._claim_release_pending = False

    def _run_claimed_task(self, task: TaskSnapshot) -> bool:
        if self._settle_requested_termination(task):
            return True
        if self._defer_for_p115_risk_cooldown(task):
            return True
        task = self._prepare_lock(task)
        if task is None:
            return True
        if self._settle_requested_termination(task):
            return True
        p115_before = _p115_request_count(self.p115_client)
        self._set_active_claim(task)
        try:
            result = self.workflow.run_stage(task)
        except P115RiskControlError as exc:
            retry_after_seconds = max(0.0, float(getattr(exc, "retry_after_seconds", 0) or 0))
            self._p115_risk_cooldown_until = self.now() + max(self.risk_cooldown_seconds, retry_after_seconds)
            self.store.set_runtime_state(
                _P115_RISK_COOLDOWN_STATE_KEY,
                str(self._p115_risk_cooldown_until),
                updated_at=self.now(),
            )
            message = "115 风控/频率限制，已暂停自动重试；请稍后在 TG/Web 手动重试。"
            if self._settle_requested_termination(
                task,
                error_type="p115_risk_control",
                error_summary=message,
                error_detail=str(exc),
            ):
                return True
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
            self._record_claimed_event(
                task,
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
            error_summary = str(exc) or exc.__class__.__name__
            if self._settle_requested_termination(
                task,
                error_type="stage_exception",
                error_summary=error_summary,
                error_detail=repr(exc),
            ):
                return True
            p115_metadata = _p115_request_metadata(task, p115_before, _p115_request_count(self.p115_client))
            observability_metadata = {}
            if "p115_stage_request_count" in p115_metadata:
                observability_metadata["p115_request_counts_by_stage"] = _metric_by_stage(
                    task.metadata.get("p115_request_counts_by_stage"),
                    task.current_stage,
                    p115_metadata["p115_stage_request_count"],
                )
            self._record_claimed_event(
                task,
                task.current_stage,
                TaskStatus.FAILED,
                error_summary,
                metadata_patch=p115_metadata | observability_metadata,
                error_type="stage_exception",
                error_summary=error_summary,
                error_detail=repr(exc),
                clear_claim=True,
            )
            return True
        if self._settle_requested_termination(task):
            return True
        self._apply_result(task, result, p115_before=p115_before, p115_after=_p115_request_count(self.p115_client))
        return True

    def _settle_requested_termination(
        self,
        task: TaskSnapshot,
        *,
        error_type: str = "",
        error_summary: str = "",
        error_detail: str = "",
    ) -> bool:
        settled = self.store.settle_requested_termination(
            task.id,
            self.worker_id,
            task.claim_token,
            error_type=error_type,
            error_summary=error_summary,
            error_detail=error_detail,
            now=self.now(),
        )
        return settled is not None

    def _finish_released_termination(self, task: TaskSnapshot | None) -> None:
        if task is None or not task.metadata.get("termination_requested_at"):
            return
        actor = str(task.metadata.get("termination_requested_by") or "Web")
        self.store.request_task_termination(task.id, actor, now=self.now())

    def _defer_for_p115_risk_cooldown(self, task: TaskSnapshot) -> bool:
        self._refresh_p115_risk_cooldown()
        now = self.now()
        if task.current_stage not in _GLOBAL_115_LOCK_STAGES or now >= self._p115_risk_cooldown_until:
            return False
        message = "115 风控冷却中，暂停 115/CMS 阶段自动执行"
        released = self._record_claimed_event(
            task,
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
        self._finish_released_termination(released)
        return True

    def _record_claimed_event(
        self,
        task: TaskSnapshot,
        stage: TaskStage,
        status: TaskStatus,
        message: str,
        **kwargs,
    ) -> TaskSnapshot | None:
        recorded = self.store.record_event(
            task.id,
            stage,
            status,
            message,
            expected_stage=task.current_stage,
            expected_status=TaskStatus.RUNNING,
            expected_claimed_by=self.worker_id,
            expected_claimed_at=task.claimed_at,
            expected_claim_token=task.claim_token,
            expected_updated_at=task.updated_at,
            **kwargs,
        )
        if recorded is None:
            LOG.warning(
                "Discarded stale task result task_id=%s stage=%s worker_id=%s",
                task.id,
                task.current_stage.value,
                self.worker_id,
            )
            self._flag_claim_lost_if_renewal_failed(task)
        return recorded

    def _flag_claim_lost_if_renewal_failed(self, task: TaskSnapshot) -> None:
        """Backstop: when a stage result was discarded and renewal had failed,
        make sure the task is flagged needs-action so it is not auto-reclaimed
        and re-run (external side effects may already have happened). Idempotent
        and safe against foreign claims."""
        with self._active_claim_lock:
            renewal_failed = self._active_claim_renewal_failed
        if not renewal_failed:
            return
        try:
            flagged = self.store.flag_claim_lost(
                task.id,
                self.worker_id,
                task.claim_token,
                now=self.now(),
            )
        except Exception:
            LOG.debug("Failed to flag lost claim task_id=%s", task.id, exc_info=True)
            return
        if flagged is not None:
            LOG.warning(
                "Task claim lost for task_id=%s stage=%s: marked needs-action after discarded result",
                task.id,
                task.current_stage.value,
            )

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
            expected_stage=task.current_stage,
            expected_claimed_by=self.worker_id,
            expected_claimed_at=task.claimed_at,
            expected_claim_token=task.claim_token,
            expected_updated_at=task.updated_at,
            wait_message=wait_message,
            next_run_at=self.now() + self.interval_seconds,
            now=self.now(),
        )
        if result.stale:
            LOG.warning(
                "Discarded stale task lock preparation task_id=%s stage=%s worker_id=%s",
                task.id,
                task.current_stage.value,
                self.worker_id,
            )
            return None
        if result.holder:
            self._finish_released_termination(result.task)
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
            metadata_delete_keys = _DEFER_METADATA_KEYS
            if task.current_stage == TaskStage.CLEANED and task.metadata.get("quality_repair_queued"):
                metadata_delete_keys += QUALITY_REPAIR_METADATA_KEYS
            committed = self.store.commit_claimed_result(
                task,
                self.worker_id,
                replace(
                    result,
                    metadata=_without_defer_metadata(
                        result.metadata | timing_metadata | p115_metadata | observability_metadata
                    ),
                ),
                next_stage=next_stage_after_success(
                    task.current_stage,
                    effective_task_strm_mode(task),
                ),
                next_run_at=now,
                metadata_delete_keys=metadata_delete_keys,
            )
            if committed is None:
                LOG.warning("Discarded stale task result task_id=%s", task.id)
                self._flag_claim_lost_if_renewal_failed(task)
                return
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
            quality_repair_deadline = None
            if task.metadata.get("quality_repair_queued") and task.current_stage in _QUALITY_REPAIR_WAIT_STAGES:
                try:
                    quality_repair_deadline = float(task.metadata.get("quality_repair_deadline_at") or 0)
                except (TypeError, ValueError):
                    quality_repair_deadline = 0
                if quality_repair_deadline <= 0:
                    quality_repair_deadline = now + QUALITY_REPAIR_WAIT_SECONDS
                    metadata_patch["quality_repair_deadline_at"] = quality_repair_deadline
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
                self._record_claimed_event(
                    task,
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
            quality_repair_timed_out = (
                quality_repair_deadline is not None
                and quality_repair_deadline > 0
                and now >= quality_repair_deadline
            )
            normal_wait_timed_out = (
                quality_repair_deadline is None
                and max_defer_count is not None
                and defer_count >= max_defer_count
            )
            if quality_repair_timed_out or normal_wait_timed_out:
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
                self._record_claimed_event(
                    task,
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
            self._record_claimed_event(
                task,
                task.current_stage,
                TaskStatus.RUNNING,
                result.message,
                metadata_patch=metadata_patch,
                next_run_at=now + _defer_delay(result.delay_seconds, defer_count),
                clear_claim=True,
            )
            return
        if result.outcome == StageOutcome.NEEDS_ACTION:
            metadata_patch = _without_defer_metadata(
                result.metadata | timing_metadata | p115_metadata | observability_metadata
            )
            needs_action_stage = task.current_stage
            if task.current_stage == TaskStage.ORGANIZING:
                metadata_patch = {
                    **metadata_patch,
                    "retry_from_stage": task.current_stage.value,
                    "retry_stage": task.current_stage.value,
                }
                needs_action_stage = TaskStage.NEEDS_ACTION
            self._record_claimed_event(
                task,
                needs_action_stage,
                TaskStatus.NEEDS_ACTION,
                result.message,
                metadata_patch=metadata_patch,
                metadata_delete_keys=_DEFER_METADATA_KEYS,
                error_type=result.error_type or "needs_action",
                error_summary=result.message,
                error_detail=result.error_detail,
                clear_claim=True,
            )
            return
        self._record_claimed_event(
            task,
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

    def _run_command(self, command: dict) -> bool:
        command_id = int(command["id"])
        token = str(command.get("claim_token") or "")
        task_id = int(command.get("task_id") or 0)
        command_type = str(command.get("command_type") or "")
        try:
            if command_type == "retry":
                self.store.enqueue_task(task_id)
            elif command_type == "reprocess":
                self.store.reprocess_task(task_id)
            elif command_type == "terminate":
                self.store.request_task_termination(task_id, str(command.get("actor") or "command"))
            elif command_type in {"emby_check", "restore", "resume_organizing"}:
                stage = {
                    "emby_check": TaskStage.EMBY_CONFIRMED,
                    "restore": TaskStage.EMBY_CONFIRMED,
                    "resume_organizing": TaskStage.ORGANIZING,
                }[command_type]
                self.store.enqueue_task(task_id, stage)
            elif command_type in {"repair_move", "invalidate_share", "quality_repair"}:
                pass
            else:
                self.store.fail_command(command_id, token, "unsupported command")
                return True
            self.store.complete_command(command_id, token, result={"applied": True})
        except Exception as exc:
            fail = getattr(self.store, "fail_command", None)
            if callable(fail):
                fail(command_id, token, str(exc))
            else:
                raise
        return True

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        acquire = getattr(self.store, "acquire_runner_lease", None)
        if callable(acquire):
            token = acquire(self.worker_id)
            if not token:
                raise RuntimeError("another TaskRunner already holds the runner lease")
            self._runner_lease_token = str(token)
        self._stop.clear()
        self._last_heartbeat_at = 0.0
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()
        self._heartbeat_thread = threading.Thread(target=self._run_heartbeat, daemon=True)
        self._heartbeat_thread.start()
        return self._thread

    def stop(self, join_timeout: float = 5) -> None:
        self._stop.set()
        if self._runner_lease_token:
            release = getattr(self.store, "release_runner_lease", None)
            if callable(release):
                release(self.worker_id, self._runner_lease_token)
            self._runner_lease_token = ""
        deadline = time.monotonic() + max(0.0, float(join_timeout))
        for thread in (self._thread, self._heartbeat_thread):
            if thread and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))

    def run_forever(self) -> None:
        failure_count = 0
        while not self._stop.is_set():
            try:
                did_work = self.run_once()
            except Exception as exc:
                failure_count += 1
                LOG.exception("Task runner infrastructure failure")
                self._safe_runtime_state("task_runner", "error")
                self._safe_runtime_state("task_runner_last_error", str(exc)[:300])
                self._stop.wait(min(30.0, self.interval_seconds * (2 ** min(failure_count, 5))))
                continue
            failure_count = 0
            self._safe_runtime_state("task_runner", "running")
            if not did_work:
                self._stop.wait(self.interval_seconds)
        self._safe_runtime_state("task_runner", "stopped")
