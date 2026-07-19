from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import Config, MoveConfig, is_under_any_root, safe_resolve
from .models import TaskSnapshot, TaskStatus
from .quality import QualityIssue, scan_task_quality
from .task_store import TaskStore


@dataclass(frozen=True)
class QualityRepairPlan:
    task_id: int
    action: str
    reason: str
    issue_codes: tuple[str, ...] = ()
    title: str = ""


@dataclass(frozen=True)
class QualityRunSummary:
    run_id: str
    status: str
    started_at: str = ""
    finished_at: str | None = None
    issue_count: int = 0
    planned_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    scanned_count: int = 0
    plans: tuple[QualityRepairPlan, ...] = ()
    error: str = ""


class QualityAutomation:
    STALE_RUN_SECONDS = 21600
    _STATUS_KEY = "quality_auto_status"
    _SUMMARY_KEY = "quality_auto_last_summary"
    _CURRENT_RUN_KEY = "quality_auto_current_run_id"

    def __init__(
        self,
        store: TaskStore,
        config: Config,
        *,
        move_config: MoveConfig | None = None,
        allowed_roots: Iterable[str | Path] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._timezone = ZoneInfo(config.quality_auto_timezone)
        self._run_time = self._parse_run_time(config.quality_auto_time)

        if allowed_roots is None:
            move_config = move_config or MoveConfig.from_config(config)
            roots = [*move_config.source_roots, *move_config.library_roots.values()]
        else:
            roots = list(allowed_roots)
        self.allowed_roots = tuple(safe_resolve(Path(root)) for root in roots)

    @staticmethod
    def _parse_run_time(value: str) -> datetime_time:
        if re.fullmatch(r"\d{2}:\d{2}", str(value or "")) is None:
            raise ValueError("quality_auto_time must use HH:MM format")
        try:
            hour, minute = (int(part) for part in str(value).split(":", 1))
            return datetime_time(hour, minute)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("quality_auto_time must be a valid HH:MM time") from exc

    def _local_now(self, now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(self._timezone)
        if now.tzinfo is None:
            return now.replace(tzinfo=self._timezone)
        return now.astimezone(self._timezone)

    def next_run_at(self, now: datetime | None = None) -> datetime:
        local_now = self._local_now(now)
        scheduled = self._scheduled_on(local_now, local_now.date())
        if local_now >= scheduled:
            scheduled = self._scheduled_on(local_now, local_now.date() + timedelta(days=1))
        return scheduled

    def _scheduled_on(self, reference: datetime, run_date) -> datetime:
        candidate = reference.replace(
            year=run_date.year,
            month=run_date.month,
            day=run_date.day,
            hour=self._run_time.hour,
            minute=self._run_time.minute,
            second=0,
            microsecond=0,
        )
        return candidate.astimezone(timezone.utc).astimezone(self._timezone)

    def run_if_due(self, now: datetime | None = None) -> QualityRunSummary | None:
        if not self.config.quality_auto_enabled:
            return None
        local_now = self._local_now(now)
        current_time = (local_now.hour, local_now.minute)
        configured_time = (self._run_time.hour, self._run_time.minute)
        if current_time < configured_time:
            return None

        run_date = local_now.date().isoformat()
        run_id = f"quality-{run_date}-{time.monotonic_ns():x}"
        if not self.store.claim_quality_run_execution(
            run_id,
            local_now.timestamp(),
            run_date=run_date,
            stale_after_seconds=self.STALE_RUN_SECONDS,
        ):
            return None
        return self._run_once_owned(run_id, local_now, injected_now=now is not None)

    def run_once(self, run_id: str, now: datetime | None = None) -> QualityRunSummary:
        local_now = self._local_now(now)
        run_id = str(run_id)
        started_at = local_now.isoformat()
        if not self.store.claim_quality_run_execution(
            run_id,
            local_now.timestamp(),
            stale_after_seconds=self.STALE_RUN_SECONDS,
        ):
            return QualityRunSummary(
                run_id=run_id,
                status="conflict",
                started_at=started_at,
                error="quality run lease is owned by another run",
            )
        return self._run_once_owned(run_id, local_now, injected_now=now is not None)

    def _run_once_owned(
        self,
        run_id: str,
        local_now: datetime,
        *,
        injected_now: bool,
    ) -> QualityRunSummary:
        started_at = local_now.isoformat()
        running = QualityRunSummary(run_id=run_id, status="running", started_at=started_at)
        if not self._persist_summary(running, local_now.timestamp()):
            return replace(running, status="superseded", error="quality run lease was superseded")
        try:
            limit = max(1, int(self.config.quality_auto_max_tasks))
            tasks = self.store.list_recent_tasks(limit=limit)
            issues = scan_task_quality(self.store, limit=limit, allowed_roots=self.allowed_roots)
            plans = self._plan(tasks, issues)
            finished_local = local_now if injected_now else self._local_now(datetime.now(self._timezone))
            summary = QualityRunSummary(
                run_id=run_id,
                status="succeeded",
                started_at=started_at,
                finished_at=finished_local.isoformat(),
                issue_count=len(issues),
                planned_count=sum(plan.action != "skip" for plan in plans),
                skipped_count=sum(plan.action == "skip" for plan in plans),
                scanned_count=len(tasks),
                plans=tuple(plans),
            )
        except Exception as exc:
            summary = QualityRunSummary(
                run_id=run_id,
                status="failed",
                started_at=started_at,
                finished_at=self._local_now(datetime.now(self._timezone)).isoformat(),
                failed_count=1,
                error=f"{type(exc).__name__}: {exc}",
            )
        finished_timestamp = (
            datetime.fromisoformat(summary.finished_at).timestamp()
            if summary.finished_at
            else local_now.timestamp()
        )
        if not self._persist_summary(summary, finished_timestamp):
            return replace(summary, status="superseded", error="quality run lease was superseded")
        return summary

    def run_now(self) -> bool:
        """Run synchronously; return False only when another run is marked running."""
        run_id = f"quality-manual-{time.monotonic_ns():x}"
        if not self.store.claim_quality_run_execution(
            run_id,
            time.time(),
            stale_after_seconds=self.STALE_RUN_SECONDS,
        ):
            return False
        self._run_once_owned(run_id, self._local_now(None), injected_now=False)
        return True

    def _persist_summary(self, summary: QualityRunSummary, updated_at: float) -> bool:
        return self.store.update_quality_run_state_if_owner(
            summary.run_id,
            summary.status,
            json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True),
            updated_at,
        )

    def _plan(self, tasks: list[TaskSnapshot], issues: list[QualityIssue]) -> list[QualityRepairPlan]:
        grouped: dict[int, list[QualityIssue]] = {}
        for issue in issues:
            grouped.setdefault(int(issue.task_id), []).append(issue)

        plans: list[QualityRepairPlan] = []
        planned_count = 0
        tasks_by_id = {task.id: task for task in tasks}
        for task in tasks:
            task_issues = grouped.get(task.id)
            metadata_safe = self._safe_metadata(task)
            candidate = bool(task_issues) or not metadata_safe
            if not candidate:
                continue

            if task.status == TaskStatus.RUNNING or task.claimed_by.strip():
                plans.append(self._skip_plan(task, "task_busy", task_issues))
                continue
            if not metadata_safe:
                plans.append(self._skip_plan(task, "unsafe_metadata", task_issues))
                continue
            if planned_count >= max(1, int(self.config.quality_auto_max_tasks)):
                plans.append(self._skip_plan(task, "max_tasks", task_issues))
                continue

            issue_codes = tuple(sorted({issue.code for issue in task_issues}))
            if any(code in {"direct_strm", "unexpected_strm"} for code in issue_codes):
                action = "reprocess"
            elif any(code in {"missing_dest", "missing_strm"} for code in issue_codes):
                action = "restore"
            else:
                plans.append(self._skip_plan(task, "unsupported_issue", task_issues))
                continue
            plans.append(
                QualityRepairPlan(
                    task_id=task.id,
                    action=action,
                    reason=issue_codes[0] if issue_codes else "quality_issue",
                    issue_codes=issue_codes,
                    title=task.title,
                )
            )
            planned_count += 1

        for task_id, task_issues in grouped.items():
            if task_id not in tasks_by_id:
                plans.append(
                    QualityRepairPlan(
                        task_id=task_id,
                        action="skip",
                        reason="unsafe_metadata",
                        issue_codes=tuple(sorted({issue.code for issue in task_issues})),
                    )
                )
        return plans

    def _safe_metadata(self, task: TaskSnapshot) -> bool:
        dest_path = str(task.metadata.get("dest_path") or "").strip()
        if not dest_path or not self._path_allowed(dest_path):
            return False
        for key in ("source_path", "strm_path"):
            value = str(task.metadata.get(key) or "").strip()
            if value and not self._path_allowed(value):
                return False
        return True

    def _path_allowed(self, value: str) -> bool:
        try:
            return is_under_any_root(Path(value), list(self.allowed_roots))
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    @staticmethod
    def _skip_plan(
        task: TaskSnapshot,
        reason: str,
        issues: list[QualityIssue] | None = None,
    ) -> QualityRepairPlan:
        return QualityRepairPlan(
            task_id=task.id,
            action="skip",
            reason=reason,
            issue_codes=tuple(sorted({issue.code for issue in issues or []})),
            title=task.title,
        )
