from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Callable


LOG = logging.getLogger("cms-tg-ingest")


@dataclass(frozen=True)
class BackgroundJobSnapshot:
    key: str
    description: str
    status: str
    queued_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobSubmission:
    outcome: str
    snapshot: BackgroundJobSnapshot | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "snapshot": self.snapshot.payload() if self.snapshot is not None else None,
        }


_SENSITIVE_VALUE = re.compile(r"(?i)(token|cookie|password|authorization|secret)\s*[=:]\s*[^\s,;]+")
_URL = re.compile(r"https?://\S+")


def _redact(value: object) -> str:
    text = _URL.sub("[redacted-url]", str(value))
    return _SENSITIVE_VALUE.sub(r"\1=[redacted]", text)


class BackgroundJobCoordinator:
    def __init__(self, max_workers: int = 1, max_in_flight: int = 8, state_store: Any | None = None):
        if max_workers < 1 or max_in_flight < 1:
            raise ValueError("max_workers and max_in_flight must be positive")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="background-jobs")
        self._capacity = threading.BoundedSemaphore(max_in_flight)
        self._lock = threading.Lock()
        self._active_keys: set[str] = set()
        self._snapshots: dict[str, BackgroundJobSnapshot] = {}
        self._closed = False
        self._state_store = state_store

    def submit(
        self,
        key: str,
        callable: Callable[[], Any],
        *,
        description: str = "",
        on_complete: Callable[[BackgroundJobSnapshot], None] | None = None,
    ) -> JobSubmission:
        normalized_key = str(key)
        with self._lock:
            if self._closed:
                return JobSubmission("closed", self._snapshots.get(normalized_key))
            existing = self._snapshots.get(normalized_key)
            if normalized_key in self._active_keys:
                return JobSubmission("already_running", existing)
            if not self._capacity.acquire(blocking=False):
                return JobSubmission("capacity_rejected", existing)
            snapshot = BackgroundJobSnapshot(
                key=normalized_key,
                description=_redact(description)[:160],
                status="queued",
                queued_at=time.time(),
            )
            self._active_keys.add(normalized_key)
            self._snapshots[normalized_key] = snapshot
            self._persist(snapshot)
            try:
                self._executor.submit(self._run, normalized_key, callable, on_complete)
            except Exception:
                self._active_keys.discard(normalized_key)
                self._capacity.release()
                raise
        return JobSubmission("accepted", snapshot)

    def snapshot(self, key: str) -> BackgroundJobSnapshot | None:
        with self._lock:
            return self._snapshots.get(str(key))

    def list_snapshots(self) -> tuple[BackgroundJobSnapshot, ...]:
        with self._lock:
            return tuple(self._snapshots.values())

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait)

    def _run(
        self,
        key: str,
        callable: Callable[[], Any],
        on_complete: Callable[[BackgroundJobSnapshot], None] | None,
    ) -> None:
        self._replace_snapshot(key, status="running", started_at=time.time())
        try:
            callable()
        except Exception as exc:
            LOG.exception("Background job failed key=%s", key)
            snapshot = self._replace_snapshot(
                key,
                status="failed",
                finished_at=time.time(),
                error=_redact(f"{type(exc).__name__}: {exc}")[:160],
            )
        else:
            snapshot = self._replace_snapshot(key, status="succeeded", finished_at=time.time())
        finally:
            with self._lock:
                self._active_keys.discard(key)
                self._capacity.release()
        if on_complete is not None:
            try:
                on_complete(snapshot)
            except Exception:
                LOG.exception("Background job completion callback failed key=%s", key)

    def _replace_snapshot(self, key: str, **changes: Any) -> BackgroundJobSnapshot:
        with self._lock:
            snapshot = BackgroundJobSnapshot(**{**self._snapshots[key].payload(), **changes})
            self._snapshots[key] = snapshot
            self._persist(snapshot)
            return snapshot

    def _persist(self, snapshot: BackgroundJobSnapshot) -> None:
        if self._state_store is None:
            return
        try:
            self._state_store.set_runtime_state(
                f"background_job:{snapshot.key}",
                json.dumps(snapshot.payload(), ensure_ascii=True, separators=(",", ":")),
            )
        except Exception:
            LOG.warning("Unable to persist background job state key=%s", snapshot.key, exc_info=True)
