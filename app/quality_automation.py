from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import Config, MoveConfig, is_relative_to, is_under_any_root, safe_resolve
from .models import TaskSnapshot, TaskStage, TaskStatus
from .quality import QualityIssue, scan_task_quality
from .task_store import TaskStore


@dataclass(frozen=True)
class QualityRepairPlan:
    task_id: int
    action: str
    reason: str
    issue_codes: tuple[str, ...] = ()
    title: str = ""
    execution_status: str = "planned"


@dataclass(frozen=True)
class QualityCleanupResult:
    status: str
    reason: str = ""


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
        repair_adapter: object | None = None,
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
        self.repair_adapter = repair_adapter

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
        if local_now < self._scheduled_on(local_now, local_now.date()):
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
            issues = scan_task_quality(
                self.store,
                limit=limit,
                allowed_roots=self.allowed_roots,
                tasks=tasks,
            )
            issues.extend(
                QualityIssue("invalid_share", "115 已明确确认自有分享失效", task_id=task.id)
                for task in tasks
                if str(
                    task.metadata.get("invalid_share_status")
                    or task.metadata.get("share_validation_status")
                    or ""
                ).strip().lower()
                == "invalid"
            )
            plans = self._plan(tasks, issues)
            if self.repair_adapter is not None:
                plans = [
                    self.execute_plan(plan, run_id) if plan.action != "skip" else plan
                    for plan in plans
                ]
            finished_local = local_now if injected_now else self._local_now(datetime.now(self._timezone))
            summary = QualityRunSummary(
                run_id=run_id,
                status="succeeded",
                started_at=started_at,
                finished_at=finished_local.isoformat(),
                issue_count=len(issues),
                planned_count=sum(plan.action != "skip" for plan in plans),
                skipped_count=sum(plan.action == "skip" for plan in plans),
                failed_count=sum(plan.execution_status == "failed" for plan in plans),
                scanned_count=len(tasks),
                plans=tuple(plans),
            )
        except Exception as exc:
            finished_local = local_now if injected_now else self._local_now(datetime.now(self._timezone))
            summary = QualityRunSummary(
                run_id=run_id,
                status="failed",
                started_at=started_at,
                finished_at=finished_local.isoformat(),
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
            if str(task.metadata.get("p115_risk_controlled") or "").lower() in {"1", "true", "yes"}:
                plans.append(self._skip_plan(task, "risk_control", task_issues))
                continue
            if "invalid_share" in issue_codes:
                invalid_status = str(
                    task.metadata.get("invalid_share_status")
                    or task.metadata.get("share_validation_status")
                    or ""
                ).strip().lower()
                if invalid_status != "invalid":
                    plans.append(self._skip_plan(task, "unknown_share_status", task_issues))
                    continue
                action = "invalid_share"
            elif any(code in {"direct_strm", "unexpected_strm"} for code in issue_codes):
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

    def execute_plan(self, plan: QualityRepairPlan, run_id: str) -> QualityRepairPlan:
        """Atomically reserve a task before handing repair work to an adapter."""
        if plan.action == "skip":
            return plan
        task = self.store.find_task(plan.task_id)
        if task is None:
            return replace(plan, execution_status="skipped", reason="task_missing")
        if task.metadata.get("quality_repair_queued"):
            return replace(plan, execution_status="skipped", reason="task_busy")
        if task.status == TaskStatus.RUNNING or task.claimed_by.strip():
            return replace(plan, execution_status="skipped", reason="task_busy")
        risk_until = 0.0
        try:
            risk_until = float(task.metadata.get("p115_risk_cooldown_until") or 0)
        except (TypeError, ValueError):
            pass
        if (
            str(task.metadata.get("p115_risk_controlled") or "").lower() in {"1", "true", "yes"}
            or risk_until > time.time()
        ):
            return replace(plan, execution_status="skipped", reason="risk_control")
        if plan.action == "invalid_share" and str(
            task.metadata.get("invalid_share_status")
            or task.metadata.get("share_validation_status")
            or ""
        ).strip().lower() != "invalid":
            return replace(plan, execution_status="skipped", reason="unknown_share_status")

        target_stage = TaskStage.EMBY_CONFIRMED if plan.action == "restore" else TaskStage.RECEIVED
        metadata = {
            "quality_run_id": str(run_id),
            "quality_repair_action": plan.action,
            "quality_repair_reason": plan.reason,
        }
        if plan.action in {"reprocess", "invalid_share"}:
            metadata["force_reprocess"] = True
        reserved = self.store.compare_and_set_transition(
            task.id,
            task.current_stage,
            {TaskStatus.PENDING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.NEEDS_ACTION},
            require_unclaimed=True,
            target_stage=target_stage,
            target_status=TaskStatus.RUNNING,
            target_event_message=f"自动巡检已排队：{plan.action}",
            metadata_patch=metadata,
            next_run_at=time.time(),
            clear_errors=True,
            claim_by=f"quality:{run_id}",
        )
        if reserved is None:
            return replace(plan, execution_status="skipped", reason="task_busy")

        handler_name = "rebuild_invalid_share" if plan.action == "invalid_share" else plan.action
        handler = getattr(self.repair_adapter, handler_name, None) if self.repair_adapter is not None else None
        if not callable(handler):
            self.store.record_event(
                reserved.id,
                target_stage,
                TaskStatus.FAILED,
                "自动巡检没有可用的修复适配器",
                error_type="quality_repair_adapter_missing",
                error_summary="repair adapter missing",
                clear_claim=True,
            )
            return replace(plan, execution_status="failed", reason="repair_adapter_missing")
        try:
            if handler(reserved, str(run_id)) is False:
                self.store.record_event(
                    reserved.id,
                    target_stage,
                    TaskStatus.FAILED,
                    "自动巡检修复适配器拒绝执行",
                    error_type="quality_repair_rejected",
                    error_summary="repair rejected",
                    clear_claim=True,
                )
                return replace(plan, execution_status="failed", reason="repair_rejected")
            self.store.record_event(
                reserved.id,
                target_stage,
                TaskStatus.PENDING,
                f"自动巡检修复已入队：{plan.action}",
                metadata_patch={"quality_repair_queued": True},
                next_run_at=time.time(),
                clear_claim=True,
            )
        except Exception as exc:
            try:
                self.store.record_event(
                    reserved.id,
                    target_stage,
                    TaskStatus.FAILED,
                    f"自动巡检修复失败：{exc}",
                    error_type="quality_repair_failed",
                    error_summary=str(exc),
                    error_detail=repr(exc),
                    clear_claim=True,
                )
            except Exception:
                pass
            return replace(plan, execution_status="failed", reason="repair_failed")
        return replace(plan, execution_status="queued")

    def cleanup_if_safe(self, task: TaskSnapshot, run_id: str) -> QualityCleanupResult:
        """Run cleanup only after the local, share, Emby, and event gates pass."""
        metadata = task.metadata
        if not metadata.get("own_share_available"):
            return QualityCleanupResult("blocked_cleanup", "own_share_not_available")
        if str(metadata.get("emby_status") or "").lower() != "confirmed" or metadata.get("emby_match_count") != 1:
            return QualityCleanupResult("blocked_cleanup", "emby_not_confirmed_unique")
        has_success_event = False
        if hasattr(self.store, "has_quality_success_event"):
            has_success_event = bool(self.store.has_quality_success_event(task.id))
        if not has_success_event:
            return QualityCleanupResult("blocked_cleanup", "success_event_missing")
        destination_text = str(metadata.get("dest_path") or "").strip()
        if not destination_text or not self._path_allowed(destination_text):
            return QualityCleanupResult("blocked_cleanup", "destination_not_allowed")
        destination = safe_resolve(Path(destination_text))
        if not destination.is_dir():
            return QualityCleanupResult("blocked_cleanup", "destination_missing")
        own_share_code = str(metadata.get("own_share_code") or "").strip()
        receive_code = str(metadata.get("own_share_receive_code") or "1212").strip() or "1212"
        marker = f"/s/{own_share_code}_{receive_code}_"
        strm_files = [
            path
            for path in destination.rglob("*")
            if path.is_file() and path.suffix.lower() == ".strm"
        ]
        if not own_share_code or not strm_files:
            return QualityCleanupResult("blocked_cleanup", "share_strm_missing")
        for path in strm_files:
            canonical = safe_resolve(path)
            if not is_relative_to(canonical, destination) or not self._path_allowed(str(canonical)):
                return QualityCleanupResult("blocked_cleanup", "share_strm_outside_allowed_root")
            try:
                content = canonical.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return QualityCleanupResult("blocked_cleanup", "share_strm_unreadable")
            if "/d/" in content or marker not in content:
                return QualityCleanupResult("blocked_cleanup", "share_strm_not_current")
        handler = getattr(self.repair_adapter, "cleanup", None) if self.repair_adapter is not None else None
        if not callable(handler):
            return QualityCleanupResult("blocked_cleanup", "cleanup_adapter_missing")
        reserved = task
        if hasattr(self.store, "claim_quality_cleanup"):
            reserved = self.store.claim_quality_cleanup(task.id, str(run_id))
            if reserved is None:
                return QualityCleanupResult("blocked_cleanup", "cleanup_busy")
        try:
            if handler(reserved, str(run_id)) is False:
                if hasattr(self.store, "record_event"):
                    self.store.record_event(
                        reserved.id,
                        reserved.current_stage,
                        reserved.status,
                        "自动巡检清理被拒绝",
                        error_type="quality_cleanup_rejected",
                        error_summary="cleanup rejected",
                        clear_claim=True,
                    )
                return QualityCleanupResult("blocked_cleanup", "cleanup_rejected")
        except Exception:
            if hasattr(self.store, "record_event"):
                self.store.record_event(
                    reserved.id,
                    reserved.current_stage,
                    TaskStatus.NEEDS_ACTION,
                    "自动巡检清理失败",
                    error_type="quality_cleanup_failed",
                    error_summary="cleanup failed",
                    clear_claim=True,
                )
            return QualityCleanupResult("blocked_cleanup", "cleanup_failed")
        if hasattr(self.store, "record_event"):
            self.store.record_event(
                reserved.id,
                reserved.current_stage,
                reserved.status,
                "自动巡检清理完成",
                metadata_patch={"quality_cleanup_completed": True},
                clear_claim=True,
            )
        return QualityCleanupResult("cleaned")

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
            execution_status="skipped",
        )
