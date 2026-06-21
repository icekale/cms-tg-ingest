from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .models import TaskSnapshot, TaskStatus, next_stage_after_success
from .task_store import TaskStore

LOG = logging.getLogger(__name__)


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

    def run_once(self) -> bool:
        task = self.store.claim_next_runnable(self.worker_id, now=self.now())
        if task is None:
            return False
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

    def _apply_result(self, task: TaskSnapshot, result: StageResult) -> None:
        now = self.now()
        if result.outcome == StageOutcome.COMPLETE:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.SUCCEEDED,
                result.message,
                metadata_patch=result.metadata,
                clear_claim=True,
            )
            next_stage = next_stage_after_success(task.current_stage)
            if next_stage:
                self.store.enqueue_task(task.id, next_stage, message="等待执行", next_run_at=now)
            return
        if result.outcome == StageOutcome.DEFER:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.RUNNING,
                result.message,
                metadata_patch=result.metadata,
                next_run_at=now + result.delay_seconds,
                clear_claim=True,
            )
            return
        if result.outcome == StageOutcome.NEEDS_ACTION:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.NEEDS_ACTION,
                result.message,
                metadata_patch=result.metadata,
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
            metadata_patch=result.metadata,
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
