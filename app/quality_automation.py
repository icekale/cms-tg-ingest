from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import DEFAULT_OWN_SHARE_RECEIVE_CODE, Config, MoveConfig, is_relative_to, is_under_any_root, safe_resolve
from .media.strm import UnsafeMediaPathError, iter_strm_files
from .models import TaskSnapshot, TaskStage, TaskStatus
from .quality import QualityIssue, ShareIdentityResolver, scan_task_quality
from .quality_rules import (
    QUALITY_RULE_VERSION,
    QualityRuleEngine,
    QualityRuleMatch,
    quality_attempt_count,
    risk_cooldown_is_active,
    rule_config,
)
from .strm_mode import effective_task_strm_mode
from .task_actions import delete_task_record_and_submission
from .task_store import (
    TaskStore,
    build_reprocess_metadata,
    reprocess_delete_keys_for,
    reprocess_stage_for,
)
from .task_runner import QUALITY_REPAIR_WAIT_SECONDS


LOG = logging.getLogger("cms-tg-ingest")

_STRM_SHARE_MARKER_RE = re.compile(r"/s/([A-Za-z0-9]+)_([A-Za-z0-9]*)_")


def _quality_plan_title(task: TaskSnapshot) -> str:
    blocked = {
        str(getattr(task, field, "") or "").strip()
        for field in ("share_code", "receive_code", "own_share_code", "own_share_receive_code")
    }
    blocked.update(
        str((task.metadata or {}).get(field) or "").strip()
        for field in ("share_code", "receive_code", "own_share_code", "own_share_receive_code")
    )
    normalized = {" ".join(value.split()).casefold() for value in blocked if value}
    for value in (task.title, task.metadata.get("received_title")):
        title = str(value or "").strip()
        if title and " ".join(title.split()).casefold() not in normalized:
            return title
    return f"任务 #{task.id}"


@dataclass(frozen=True)
class QualityRepairPlan:
    task_id: int
    action: str
    reason: str
    issue_codes: tuple[str, ...] = ()
    title: str = ""
    execution_status: str = "planned"
    rule_id: str = ""
    risk_level: str = ""
    rule_version: str = QUALITY_RULE_VERSION
    planned_updated_at: float = 0
    target_stage: str = ""


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
    queued_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    scanned_count: int = 0
    plans: tuple[QualityRepairPlan, ...] = ()
    error: str = ""
    rule_counts: dict[str, int] = field(default_factory=dict)
    manual_count: int = 0
    cooldown_count: int = 0
    budget_used: dict[str, object] = field(default_factory=dict)


_NO_NOTIFY_SKIP_REASONS = frozenset(
    {
        "terminal_task",
        "task_busy",
        "max_tasks",
        "115_check_budget",
        "cooldown",
        "risk_control",
        "p115_cooldown",
        "manual_suppressed",
        "rule_version_changed",
        "task_missing",
        "task_changed",
        "claim_lost",
        "unsupported_action",
        "archived",
    }
)
NOTIFY_STATE_KEY = "quality_auto_last_notify"


def open_manual_task_ids(summary: QualityRunSummary) -> frozenset[int]:
    """Return task ids that need attention but are not terminal or busy."""
    return frozenset(
        plan.task_id
        for plan in summary.plans
        if plan.execution_status == "skipped" and plan.reason not in _NO_NOTIFY_SKIP_REASONS
    )


def quality_notify_signature(summary: QualityRunSummary) -> str:
    """Stable signature of actionable plans so unchanged work is not re-notified."""
    actionable = sorted(
        (plan.task_id, plan.rule_id, plan.execution_status)
        for plan in summary.plans
        if plan.execution_status in {"queued", "failed"}
    )
    if not actionable and summary.error:
        # Run-level failure with no per-plan detail: dedupe by error text.
        return f"run-error:{summary.error}"
    return json.dumps(actionable, ensure_ascii=False, sort_keys=True) if actionable else ""


def should_notify_quality_run(
    summary: QualityRunSummary,
    previous_signature: str = "",
    previous_open_ids: frozenset[int] = frozenset(),
) -> tuple[bool, str]:
    """Decide whether a finished quality run needs Telegram attention."""
    signature = quality_notify_signature(summary)
    if signature and signature != previous_signature:
        return True, signature
    if summary.failed_count and not signature:
        # Run-level failure with no dedupeable plan detail: alert per run.
        return True, signature
    if open_manual_task_ids(summary) - previous_open_ids:
        return True, signature
    return False, signature


def load_quality_notify_state(store: TaskStore) -> tuple[str, frozenset[int]]:
    row = store.get_runtime_state(NOTIFY_STATE_KEY)
    if not row:
        return "", frozenset()
    try:
        payload = json.loads(str(row["value"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "", frozenset()
    signature = str(payload.get("signature") or "")
    raw_ids = payload.get("open_ids") or []
    open_ids = frozenset(int(item) for item in raw_ids if str(item).isdigit())
    return signature, open_ids


def save_quality_notify_state(
    store: TaskStore,
    signature: str,
    open_ids: frozenset[int],
) -> None:
    store.set_runtime_state(
        NOTIFY_STATE_KEY,
        json.dumps(
            {"signature": signature, "open_ids": sorted(int(item) for item in open_ids)},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


class QualityAutomation:
    STALE_RUN_SECONDS = 21600
    MAX_TASKS = 1000
    SCAN_TASK_MULTIPLIER = 10
    MAX_115_CHECK_LIMIT = 100
    AUTO_RECHECK_COOLDOWN_SECONDS = 6 * 60 * 60
    MAX_AUTO_RECHECK_ATTEMPTS = 3
    AUTO_RECHECK_RECOVERABLE_STAGES = frozenset(
        {
            TaskStage.STRM_READY,
            TaskStage.CMS_DELETE_SETTLED,
            TaskStage.MOVED,
            TaskStage.EMBY_CONFIRMED,
            TaskStage.CLEANED,
        }
    )
    AUTO_RECHECK_ERROR_TYPES = frozenset({"stage_wait_timeout", "organizing_timeout"})
    AUTO_RECHECK_ISSUE_CODES = frozenset({"missing_dest", "missing_strm", "unexpected_strm"})
    SHARE_REVALIDATE_COOLDOWN_SECONDS = 6 * 60 * 60
    MAX_SHARE_REVALIDATE_ATTEMPTS = 3
    MAX_STRM_CLEANUP_PATHS = 500
    MAX_SHARE_CHECKS = 20
    ARCHIVE_TERMINAL_RULES = frozenset({"unsafe_path", "terminal_invalid_share"})
    ARCHIVE_ISSUE_CODES = frozenset(
        {"unsafe_metadata", "unsafe_path", "invalid_share", "invalid_share_cleaned", "source_deleted"}
    )
    SCAN_CACHE_PREFIX = "quality_dir_fp:live:"
    SCAN_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
    _STATUS_KEY = "quality_auto_status"
    _SUMMARY_KEY = "quality_auto_last_summary"
    _CURRENT_RUN_KEY = "quality_auto_current_run_id"
    _OVERRIDES_KEY = "quality_auto_overrides"
    _RULE_CONFIG_KEY = "quality_rule_config"
    DEFAULT_RULE_CONFIG = {
        "allow_auto_reprocess": False,
        "max_attempts": 2,
        "cooldown_seconds": 86400,
    }
    MAX_COOLDOWN_SECONDS = 7 * 24 * 60 * 60
    MAX_TASK_ID = 2**63 - 1

    def __init__(
        self,
        store: TaskStore,
        config: Config,
        *,
        move_config: MoveConfig | None = None,
        allowed_roots: Iterable[str | Path] | None = None,
        repair_adapter: object | None = None,
        submission_store: object | None = None,
        share_inspector: object | None = None,
        on_enabled_changed: object | None = None,
        rule_engine: QualityRuleEngine | None = None,
        share_identity_resolver: ShareIdentityResolver | None = None,
        strm_url_probe: object | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._env_defaults = {
            "quality_auto_enabled": bool(config.quality_auto_enabled),
            "quality_auto_time": str(config.quality_auto_time),
            "quality_auto_timezone": str(config.quality_auto_timezone),
            "quality_auto_max_tasks": int(config.quality_auto_max_tasks),
            "quality_auto_115_check_limit": int(config.quality_auto_115_check_limit),
        }
        self._load_runtime_overrides()
        if not 1 <= int(config.quality_auto_max_tasks) <= self.MAX_TASKS:
            raise ValueError(f"quality_auto_max_tasks must be between 1 and {self.MAX_TASKS}")
        if not 1 <= int(config.quality_auto_115_check_limit) <= self.MAX_115_CHECK_LIMIT:
            raise ValueError(f"quality_auto_115_check_limit must be between 1 and {self.MAX_115_CHECK_LIMIT}")
        self._timezone = ZoneInfo(config.quality_auto_timezone)
        self._run_time = self._parse_run_time(config.quality_auto_time)
        self.rule_engine = rule_engine or QualityRuleEngine()
        self.rule_config = self._load_rule_config()
        self.strm_cleanup_enabled = bool(config.quality_strm_cleanup_enabled)
        self.archive_after_seconds = max(1, int(config.quality_archive_after_seconds))
        self.unfixable_retention_seconds = (
            max(1, int(config.quality_unfixable_retention_days)) * 24 * 3600
            if int(config.quality_unfixable_retention_days) > 0
            else 0
        )

        if allowed_roots is None:
            move_config = move_config or MoveConfig.from_config(config)
            roots = [*move_config.source_roots, *move_config.library_roots.values()]
        else:
            roots = list(allowed_roots)
        self.allowed_roots = tuple(safe_resolve(Path(root)) for root in roots)
        self.repair_adapter = repair_adapter
        self.submission_store = submission_store
        self.share_inspector = share_inspector if callable(share_inspector) else None
        self.on_enabled_changed = on_enabled_changed
        self.share_identity_resolver = share_identity_resolver if callable(share_identity_resolver) else None
        self.strm_url_probe = strm_url_probe if callable(strm_url_probe) else None

    def _repair_enabled(self) -> bool:
        return bool(getattr(self.config, "quality_auto_repair_enabled", False))

    def _load_rule_config(self) -> dict[str, bool | int]:
        values: dict[str, object] = dict(self.DEFAULT_RULE_CONFIG)
        state = self.store.get_runtime_state(self._RULE_CONFIG_KEY)
        if state:
            try:
                override = json.loads(state["value"])
            except (TypeError, ValueError, KeyError):
                override = None
            if isinstance(override, dict):
                values.update({key: override[key] for key in values if key in override})
        controls = rule_config(values)
        controls["cooldown_seconds"] = min(int(controls["cooldown_seconds"]), self.MAX_COOLDOWN_SECONDS)
        return controls

    def _load_runtime_overrides(self) -> None:
        state = self.store.get_runtime_state(self._OVERRIDES_KEY)
        if not state:
            return
        try:
            values = json.loads(state["value"])
        except (TypeError, ValueError, KeyError):
            return
        if not isinstance(values, dict):
            return
        defaults = self._env_defaults
        enabled = self._runtime_bool(values.get("quality_auto_enabled"), bool(defaults["quality_auto_enabled"]))
        raw_time = values.get("quality_auto_time", defaults["quality_auto_time"])
        try:
            parsed_time = self._parse_run_time(str(raw_time))
        except (TypeError, ValueError):
            parsed_time = self._parse_run_time(str(defaults["quality_auto_time"]))
        raw_timezone = values.get("quality_auto_timezone", defaults["quality_auto_timezone"])
        try:
            parsed_timezone = ZoneInfo(str(raw_timezone))
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            parsed_timezone = ZoneInfo(str(defaults["quality_auto_timezone"]))

        def valid_limit(name: str, maximum: int) -> int:
            raw = values.get(name, defaults[name])
            try:
                if isinstance(raw, bool):
                    raise ValueError
                parsed = int(raw)
            except (OverflowError, TypeError, ValueError):
                return int(defaults[name])
            return parsed if 1 <= parsed <= maximum else int(defaults[name])

        self.config.quality_auto_enabled = enabled
        self.config.quality_auto_time = parsed_time.strftime("%H:%M")
        self.config.quality_auto_timezone = str(parsed_timezone)
        self.config.quality_auto_max_tasks = valid_limit("quality_auto_max_tasks", self.MAX_TASKS)
        self.config.quality_auto_115_check_limit = valid_limit(
            "quality_auto_115_check_limit", self.MAX_115_CHECK_LIMIT
        )

    @staticmethod
    def _runtime_bool(value: object, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
        return default

    def update_settings(
        self,
        *,
        enabled: bool,
        run_time: str,
        timezone_name: str,
        max_tasks: int,
        check_limit: int,
    ) -> dict[str, object]:
        parsed_time = self._parse_run_time(run_time)
        try:
            timezone = ZoneInfo(str(timezone_name))
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("quality automation timezone must be a valid IANA timezone") from exc
        max_tasks = int(max_tasks)
        check_limit = int(check_limit)
        if not 1 <= max_tasks <= self.MAX_TASKS:
            raise ValueError(f"quality_auto_max_tasks must be between 1 and {self.MAX_TASKS}")
        if not 1 <= check_limit <= self.MAX_115_CHECK_LIMIT:
            raise ValueError(f"quality_auto_115_check_limit must be between 1 and {self.MAX_115_CHECK_LIMIT}")
        values: dict[str, object] = {
            "quality_auto_enabled": bool(enabled),
            "quality_auto_time": parsed_time.strftime("%H:%M"),
            "quality_auto_timezone": str(timezone_name),
            "quality_auto_max_tasks": max_tasks,
            "quality_auto_115_check_limit": check_limit,
        }
        previous_enabled = bool(self.config.quality_auto_enabled)
        for name, value in values.items():
            setattr(self.config, name, value)
        self._timezone = timezone
        self._run_time = parsed_time
        self.store.set_runtime_state(self._OVERRIDES_KEY, json.dumps(values, ensure_ascii=False, sort_keys=True))
        if previous_enabled != bool(enabled) and callable(self.on_enabled_changed):
            self.on_enabled_changed(bool(enabled))
        return values

    def reset_settings(self) -> dict[str, object]:
        previous_enabled = bool(self.config.quality_auto_enabled)
        self.store.delete_runtime_state(self._OVERRIDES_KEY)
        for name, value in self._env_defaults.items():
            setattr(self.config, name, value)
        self._timezone = ZoneInfo(str(self.config.quality_auto_timezone))
        self._run_time = self._parse_run_time(str(self.config.quality_auto_time))
        if previous_enabled != bool(self.config.quality_auto_enabled) and callable(self.on_enabled_changed):
            self.on_enabled_changed(bool(self.config.quality_auto_enabled))
        return dict(self._env_defaults)

    def status_snapshot(self, now: datetime | None = None) -> dict[str, object]:
        summary: dict[str, object] = {}
        state = self.store.get_runtime_state(self._SUMMARY_KEY)
        if state:
            try:
                parsed = json.loads(state["value"])
                if isinstance(parsed, dict):
                    summary = parsed
            except (TypeError, ValueError, KeyError):
                summary = {}
        current = self.store.get_runtime_state(self._CURRENT_RUN_KEY)
        status = self.store.get_runtime_state(self._STATUS_KEY)
        local_now = self._local_now(now)
        return {
            "enabled": bool(self.config.quality_auto_enabled),
            "repair_enabled": self._repair_enabled(),
            "time": str(self.config.quality_auto_time),
            "timezone": str(self.config.quality_auto_timezone),
            "max_tasks": int(self.config.quality_auto_max_tasks),
            "check_limit": int(self.config.quality_auto_115_check_limit),
            "status": str(status["value"] if status else "idle"),
            "current_run_id": str(current["value"] if current else ""),
            "last_summary": summary,
            "next_run_at": self.next_run_at(local_now).isoformat(),
        }

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
            scan_limit = min(self.MAX_TASKS, max(limit, limit * self.SCAN_TASK_MULTIPLIER))
            tasks = self.store.list_recent_tasks(limit=scan_limit)
            current_time = local_now.timestamp()
            retired_ids: set[int] = set()
            repair_enabled = self._repair_enabled()
            for task in tasks:
                try:
                    self._ensure_terminal_marker(task, current_time)
                    self._archive_terminal_task(task, current_time, run_id)
                    if repair_enabled:
                        revalidated = self._auto_revalidate_share(task, current_time)
                        if not revalidated and self._retire_unfixable_task(task, current_time, run_id):
                            retired_ids.add(task.id)
                except Exception:
                    LOG.debug("Quality cleanup failed task_id=%s", task.id, exc_info=True)
            tasks = [task for task in tasks if task.id not in retired_ids]
            scan_tasks = [
                task for task in tasks if self._scan_skip_reason(task, current_time) is None
            ]
            cached_issues: list[QualityIssue] = []
            fresh_tasks: list[TaskSnapshot] = []
            for task in scan_tasks:
                fingerprint = self._directory_fingerprint(str(task.metadata.get("dest_path") or ""))
                cache = self._load_scan_cache(task) if fingerprint is not None else None
                if cache is not None and cache.get("fingerprint") == list(fingerprint):
                    for item in cache.get("issues") or []:
                        cached_issues.append(
                            QualityIssue(
                                str(item.get("code") or ""),
                                str(item.get("message") or ""),
                                str(item.get("detail") or ""),
                                task.id,
                                task.title,
                            )
                        )
                    continue
                fresh_tasks.append(task)
            issues = scan_task_quality(
                self.store,
                limit=scan_limit,
                allowed_roots=self.allowed_roots,
                tasks=fresh_tasks,
                share_identity_resolver=self.share_identity_resolver,
            )
            issues.extend(cached_issues)
            for task in fresh_tasks:
                fingerprint = self._directory_fingerprint(str(task.metadata.get("dest_path") or ""))
                if fingerprint is not None:
                    self._store_scan_cache(
                        task,
                        fingerprint,
                        [issue for issue in issues if issue.task_id == task.id],
                    )
            if repair_enabled and self.strm_url_probe is not None:
                scan_ids = {task.id for task in scan_tasks}
                probed: list[QualityIssue] = []
                for task in scan_tasks:
                    task_issues = [issue for issue in issues if issue.task_id == task.id]
                    if any(issue.code == "direct_strm" for issue in task_issues):
                        task_issues = self._probe_direct_strm_issues(task, task_issues)
                    probed.extend(task_issues)
                issues = probed + [issue for issue in issues if issue.task_id not in scan_ids]
            issues.extend(
                QualityIssue("invalid_share", "115 已明确确认自有分享失效", task_id=task.id)
                for task in scan_tasks
                if str(
                    task.metadata.get("invalid_share_status")
                    or task.metadata.get("share_validation_status")
                    or ""
                ).strip().lower()
                == "invalid"
            )
            plans = self._plan(scan_tasks, issues, run_id=run_id, now=current_time)
            plans, budget = self._apply_budgets(
                plans,
                max_tasks=limit,
                check_limit=max(1, int(self.config.quality_auto_115_check_limit)),
            )
            if repair_enabled and self.repair_adapter is not None:
                plans = [self.execute_plan(plan, run_id) if plan.action != "skip" else plan for plan in plans]
            elif not repair_enabled:
                plans = [
                    replace(plan, execution_status="skipped", reason="scan_only")
                    if plan.action != "skip" and plan.execution_status == "planned"
                    else plan
                    for plan in plans
                ]
            finished_local = local_now if injected_now else self._local_now(datetime.now(self._timezone))
            failed_count = sum(plan.execution_status == "failed" for plan in plans)
            rule_counts: dict[str, int] = {}
            for plan in plans:
                if plan.rule_id:
                    rule_counts[plan.rule_id] = rule_counts.get(plan.rule_id, 0) + 1
            manual_count = sum(
                plan.execution_status == "skipped"
                and plan.reason not in {"task_busy", "max_tasks", "115_check_budget"}
                for plan in plans
            )
            cooldown_count = sum(plan.reason in {"cooldown", "risk_control", "p115_cooldown"} for plan in plans)
            summary = QualityRunSummary(
                run_id=run_id,
                status="failed" if failed_count else "succeeded",
                started_at=started_at,
                finished_at=finished_local.isoformat(),
                issue_count=len(issues),
                planned_count=sum(plan.action != "skip" for plan in plans),
                queued_count=sum(plan.execution_status == "queued" for plan in plans),
                skipped_count=sum(plan.action == "skip" or plan.execution_status == "skipped" for plan in plans),
                failed_count=failed_count,
                scanned_count=len(scan_tasks),
                plans=tuple(plans),
                rule_counts=rule_counts,
                manual_count=manual_count,
                cooldown_count=cooldown_count,
                budget_used=budget,
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
        self.store.record_quality_run(
            summary.run_id,
            local_now.date().isoformat(),
            summary.status,
            datetime.fromisoformat(summary.started_at).timestamp(),
            finished_timestamp,
            scanned_count=summary.scanned_count,
            issue_count=summary.issue_count,
            planned_count=summary.planned_count,
            queued_count=summary.queued_count,
            failed_count=summary.failed_count,
            skipped_count=summary.skipped_count,
            manual_count=summary.manual_count,
            cooldown_count=summary.cooldown_count,
            rule_counts=summary.rule_counts,
            budget_used=summary.budget_used,
        )
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

    def quality_descriptor(
        self,
        task: TaskSnapshot,
        issues: Iterable[QualityIssue] | None = None,
        *,
        now: float | None = None,
    ) -> dict[str, object]:
        """Describe one current rule decision for presentation and manual actions."""
        current_time = time.time() if now is None else float(now)
        issue_list = tuple(issues) if issues is not None else tuple(
            scan_task_quality(
                self.store,
                tasks=[task],
                allowed_roots=self.allowed_roots,
                share_identity_resolver=self.share_identity_resolver,
            )
        )
        # Surface confirmed-dead direct links from stored probe results without
        # any external call (probe happens during scheduled runs only).
        dead_map = self._dead_direct_paths(task)
        if dead_map:
            upgraded: list[QualityIssue] = []
            for issue in issue_list:
                if issue.code == "direct_strm" and str(issue.detail).strip() in dead_map:
                    upgraded.append(
                        QualityIssue("dead_direct_link", "直链 STRM 已确认失效", issue.detail, issue.task_id, issue.title)
                    )
                else:
                    upgraded.append(issue)
            issue_list = tuple(upgraded)
        match = self.rule_engine.evaluate(task, issue_list, config=self.rule_config)
        state = self.store.quality_state(task.id, now=current_time)
        manual_status = str(state.get("quality_manual_status") or "open").strip().lower()
        next_eligible = float(state.get("quality_next_eligible_at") or 0)
        busy = task.status == TaskStatus.RUNNING or bool(task.claimed_by.strip())
        terminal = task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {
            TaskStage.FAILED,
            TaskStage.NEEDS_ACTION,
        }
        archived = self._is_archived(task)
        safe = self._safe_metadata(task)
        source_evidence = self._has_source_evidence(task)
        risk_controlled = self._risk_controlled(task, current_time)
        queued = bool(state.get("quality_repair_queued"))
        repairable = bool(
            match.auto_action == "reprocess"
            and safe
            and source_evidence
            and not risk_controlled
            and not busy
            and not terminal
            and not queued
            and manual_status == "open"
            and next_eligible <= current_time
        )
        auto_allowed = bool(repairable and match.auto_allowed)
        rule_actions = {
            str(value).strip().lower()
            for value in match.manual_actions
            if str(value).strip().lower() in {"view", "snooze", "ignore", "resume"}
        }
        actions = ["view"]
        if archived:
            actions.append("resume")
        elif manual_status in {"snoozed", "ignored", "manual_required"}:
            actions.append("resume")
            # Human takeover must not be a dead end: allow dismissing the issue
            # (snooze/ignore) whenever the rule exposes those actions.
            if manual_status == "manual_required":
                actions.extend(action for action in ("snooze", "ignore") if action in rule_actions)
        elif not queued and not busy and not terminal:
            actions.extend(action for action in ("snooze", "ignore") if action in rule_actions)
            if auto_allowed or repairable:
                actions.extend(action for action in ("execute", "reprocess") if action not in actions)
            elif match.rule_id in {"missing_destination", "missing_strm"} and safe:
                actions.append("reprocess")
        reason = str(state.get("quality_rule_reason") or "").strip()
        # Parenthesized explicitly: fall back to the rule's reason when none is
        # stored, or when the stored reason is the generic manual_required
        # marker for anything but a repeated_failure rule.
        if not reason or (reason == "manual_required" and match.rule_id != "repeated_failure"):
            reason = match.reason
        return {
            "rule_id": match.rule_id,
            "rule_reason": reason,
            "risk_level": match.risk_level,
            "issue_codes": list(match.issue_codes),
            "manual_status": manual_status,
            "archived": archived,
            "attempts": quality_attempt_count(task),
            "next_eligible_at": next_eligible,
            "available_actions": actions,
            "evidence": list(match.evidence),
            "auto_allowed": auto_allowed,
            "rule_version": QUALITY_RULE_VERSION,
        }

    def manual_action(
        self,
        task_id: int,
        rule_id: str,
        action: str,
        actor: str,
        until: float | None = None,
        *,
        rule_version: str | None = None,
    ) -> dict[str, object]:
        """Apply one validated human quality action without touching external services."""
        try:
            if isinstance(task_id, bool):
                raise ValueError
            normalized_task_id = int(task_id)
        except (TypeError, ValueError, OverflowError):
            return {"status": "invalid", "task": None, "action": str(action or ""), "reason": "invalid_task_id"}
        if not 1 <= normalized_task_id <= self.MAX_TASK_ID:
            return {"status": "invalid", "task": None, "action": str(action or ""), "reason": "invalid_task_id"}
        task = self.store.find_task(normalized_task_id)
        if task is None:
            return {"status": "not_found", "task": None, "action": str(action or ""), "reason": "task_not_found"}
        normalized_action = str(action or "").strip().lower()
        stored_rule = str(task.metadata.get("quality_rule_id") or "").strip()
        if stored_rule and str(rule_id or "").strip() != stored_rule:
            return {"status": "rejected", "task": task, "action": normalized_action, "reason": "rule_mismatch"}
        if normalized_action in {"execute", "reprocess"} and bool(task.metadata.get("quality_repair_queued")):
            return {"status": "conflict", "task": task, "action": normalized_action, "reason": "already_queued"}
        descriptor = self.quality_descriptor(task)
        archived = bool(descriptor.get("archived"))
        if not archived and str(rule_id or "").strip() != str(descriptor["rule_id"]):
            return {"status": "rejected", "task": task, "action": normalized_action, "reason": "rule_mismatch"}
        stored_version = str(task.metadata.get("quality_rule_version") or "").strip()
        if rule_version is not None and str(rule_version).strip() != QUALITY_RULE_VERSION:
            return {"status": "rejected", "task": task, "action": normalized_action, "reason": "rule_version_changed"}
        if stored_version and stored_version != QUALITY_RULE_VERSION:
            return {"status": "rejected", "task": task, "action": normalized_action, "reason": "rule_version_changed"}
        if normalized_action not in set(descriptor["available_actions"]):
            return {"status": "rejected", "task": task, "action": normalized_action, "reason": "action_not_allowed"}
        if normalized_action == "view":
            return {"status": "viewed", "task": task, "action": normalized_action, "reason": "read_only"}
        if task.status == TaskStatus.RUNNING or task.claimed_by.strip():
            return {"status": "conflict", "task": task, "action": normalized_action, "reason": "task_busy"}
        actor = str(actor or "web").strip()[:128] or "web"
        if normalized_action == "snooze":
            try:
                current_time = time.time()
                target_until = current_time + 24 * 60 * 60 if until is None else float(until)
                if (
                    not math.isfinite(target_until)
                    or target_until <= current_time
                    or target_until > current_time + self.MAX_COOLDOWN_SECONDS
                ):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return {"status": "rejected", "task": task, "action": normalized_action, "reason": "invalid_until"}
            updated = self.store.mark_quality_snoozed(
                task.id,
                target_until,
                actor,
                rule_id=str(descriptor["rule_id"]),
                expected_updated_at=task.updated_at,
            )
            if updated is None:
                latest = self.store.find_task(task.id)
                return {"status": "conflict", "task": latest or task, "action": normalized_action, "reason": "task_changed"}
            return {"status": "snoozed", "task": updated, "action": normalized_action, "reason": "manual_snooze"}
        if normalized_action == "ignore":
            updated = self.store.mark_quality_ignored(
                task.id,
                actor,
                rule_id=str(descriptor["rule_id"]),
                expected_updated_at=task.updated_at,
            )
            if updated is None:
                latest = self.store.find_task(task.id)
                return {"status": "conflict", "task": latest or task, "action": normalized_action, "reason": "task_changed"}
            return {"status": "ignored", "task": updated, "action": normalized_action, "reason": "manual_ignore"}
        if normalized_action == "resume":
            updated = self.store.resume_quality(
                task.id,
                actor,
                rule_id=str(descriptor["rule_id"]),
                expected_updated_at=task.updated_at,
            )
            if updated is None:
                latest = self.store.find_task(task.id)
                return {"status": "conflict", "task": latest or task, "action": normalized_action, "reason": "task_changed"}
            return {"status": "resumed", "task": updated, "action": normalized_action, "reason": "manual_resume"}

        attempts = quality_attempt_count(task)
        started_at = time.time()
        metadata = build_reprocess_metadata(
            task,
            {
                "quality_manual_status": "open",
                "quality_repair_queued": True,
                "quality_repair_action": normalized_action,
                "quality_repair_reason": str(descriptor["rule_id"]),
                "quality_rule_id": str(descriptor["rule_id"]),
                "quality_rule_version": QUALITY_RULE_VERSION,
                "quality_last_actor": actor,
                "quality_last_attempt_at": started_at,
                "quality_repair_attempts": attempts + 1,
                "quality_repair_started_at": started_at,
                "quality_next_eligible_at": started_at + int(self.rule_config["cooldown_seconds"]),
            },
        )
        target_stage = reprocess_stage_for(task)
        updated = self.store.compare_and_set_transition(
            task.id,
            task.current_stage,
            {TaskStatus.PENDING, TaskStatus.SUCCEEDED},
            require_unclaimed=True,
            target_stage=target_stage,
            target_status=TaskStatus.PENDING,
            target_event_message=(
                f"人工质量操作已入队：{normalized_action}"
                f"（rule={descriptor['rule_id']}; action={normalized_action}; actor={actor}）"
            ),
            metadata_patch=metadata,
            metadata_delete_keys=tuple(
                key for key in reprocess_delete_keys_for(task) if key != "quality_repair_queued"
            ),
            next_run_at=0,
            clear_errors=True,
            expected_updated_at=task.updated_at,
        )
        if updated is None:
            latest = self.store.find_task(task.id)
            return {"status": "conflict", "task": latest or task, "action": normalized_action, "reason": "task_changed"}
        return {"status": "queued", "task": updated, "action": normalized_action, "reason": "manual_reprocess"}

    def _persist_summary(self, summary: QualityRunSummary, updated_at: float) -> bool:
        return self.store.update_quality_run_state_if_owner(
            summary.run_id,
            summary.status,
            json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True),
            updated_at,
        )

    def _plan(
        self,
        tasks: list[TaskSnapshot],
        issues: list[QualityIssue],
        *,
        run_id: str = "",
        now: float | None = None,
    ) -> list[QualityRepairPlan]:
        grouped: dict[int, list[QualityIssue]] = {}
        for issue in issues:
            grouped.setdefault(int(issue.task_id), []).append(issue)

        plans: list[QualityRepairPlan] = []
        tasks_by_id = {task.id: task for task in tasks}
        current_time = time.time() if now is None else float(now)
        for task in tasks:
            task_issues = grouped.get(task.id)
            if not task_issues:
                if self._safe_metadata(task):
                    match = self.rule_engine.evaluate(task, [], config=self.rule_config)
                    if match.rule_id == "no_issue":
                        recheck = self._auto_recheck_plan(task, now=current_time)
                        if recheck is not None:
                            plans.append(recheck)
                        continue
                    current = task
                    if run_id:
                        current = self._persist_rule_match(task, match, run_id) or task
                    plans.append(self._plan_match(current, match, current_time))
                    continue
                task_issues = [QualityIssue("unsafe_metadata", "任务质量元数据不安全", task_id=task.id, title=task.title)]
            match = self.rule_engine.evaluate(task, task_issues, config=self.rule_config)
            current = task
            if run_id:
                current = self._persist_rule_match(task, match, run_id) or task
            plans.append(self._plan_match(current, match, current_time))

        for task_id, task_issues in grouped.items():
            if task_id not in tasks_by_id:
                plans.append(
                    QualityRepairPlan(
                        task_id=task_id,
                        action="skip",
                        reason="unsafe_metadata",
                        issue_codes=tuple(sorted({issue.code for issue in task_issues})),
                        rule_id="unsafe_path",
                        rule_version=QUALITY_RULE_VERSION,
                        execution_status="skipped",
                    )
                )
        return plans

    def _auto_recheck_plan(self, task: TaskSnapshot, now: float) -> QualityRepairPlan | None:
        """Requeue a waiting task only after its recoverable STRM issue is gone."""
        if not self._recheck_candidate(task, now):
            return None
        retry_stage = TaskStage(str(task.metadata.get("retry_stage") or ""))
        return QualityRepairPlan(
            task_id=task.id,
            action="requeue",
            reason="auto_recheck_recovered",
            title=_quality_plan_title(task),
            rule_id="auto_recheck",
            risk_level="medium",
            planned_updated_at=task.updated_at,
            target_stage=retry_stage.value,
        )

    @staticmethod
    def _is_archived(task: TaskSnapshot) -> bool:
        try:
            return float(task.metadata.get("quality_archived_at") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _archive_terminal_task(
        self,
        task: TaskSnapshot,
        now: float,
        run_id: str,
    ) -> bool:
        """Archive old terminal tasks that can never be auto-repaired."""
        if self._is_archived(task):
            return False
        terminal = task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {
            TaskStage.FAILED,
            TaskStage.NEEDS_ACTION,
        }
        if not terminal:
            return False
        try:
            terminal_since = float(task.metadata.get("quality_terminal_since") or 0)
        except (TypeError, ValueError):
            terminal_since = 0
        if terminal_since <= 0:
            try:
                terminal_since = float(task.updated_at or task.created_at or 0)
            except (TypeError, ValueError):
                terminal_since = 0
        if now - terminal_since < self.archive_after_seconds:
            return False
        rule_id = str(task.metadata.get("quality_rule_id") or "").strip()
        reason = str(task.metadata.get("quality_rule_reason") or rule_id or "").strip()[:200]
        raw_codes = task.metadata.get("quality_issue_codes") or []
        if isinstance(raw_codes, str):
            raw_codes = [part.strip() for part in raw_codes.split(",") if part.strip()]
        issue_codes = {str(code) for code in raw_codes if str(code).strip()}
        if rule_id not in self.ARCHIVE_TERMINAL_RULES and not (issue_codes & self.ARCHIVE_ISSUE_CODES):
            return False
        updated = self.store.update_quality_state(
            task.id,
            task.updated_at,
            {
                "quality_archived_at": now,
                "quality_archived_reason": reason or "legacy_terminal",
            },
            "质量巡检已归档历史遗留问题",
            "quality-auto",
            rule_id=rule_id or "manual_required",
            action="archive",
        )
        return updated is not None

    def _ensure_terminal_marker(self, task: TaskSnapshot, now: float) -> bool:
        """Record the first observed terminal time so archiving is deterministic."""
        if self._is_archived(task):
            return False
        terminal = task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {
            TaskStage.FAILED,
            TaskStage.NEEDS_ACTION,
        }
        if not terminal:
            return False
        try:
            existing = float(task.metadata.get("quality_terminal_since") or 0)
        except (TypeError, ValueError):
            existing = 0
        if existing > 0:
            return False
        updated = self.store.update_quality_state(
            task.id,
            task.updated_at,
            {"quality_terminal_since": now},
            "质量巡检记录终态时间",
            "quality-auto",
        )
        return updated is not None

    @staticmethod
    def _extract_strm_share_codes(text: str) -> tuple[tuple[str, str], ...]:
        """All /s/{code}_{receive}_ share references in one strm file.

        Returns (share_code, receive_code) pairs so liveness probes use the
        password the file actually embeds instead of falling back to the
        current task's own receive code.
        """
        pairs: list[tuple[str, str]] = []
        for match in _STRM_SHARE_MARKER_RE.finditer(text):
            code = match.group(1)
            receive = match.group(2)
            if code and (code, receive) not in pairs:
                pairs.append((code, receive))
        return tuple(pairs)

    _DEAD_DIRECT_PROBE_COOLDOWN_SECONDS = 24 * 60 * 60

    def _dead_direct_paths(self, task: TaskSnapshot) -> dict[str, float]:
        """Confirmed-dead direct-link strm paths (path -> probed_at) from metadata."""
        raw = task.metadata.get("quality_dead_direct") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
        if not isinstance(raw, dict):
            return {}
        result: dict[str, float] = {}
        for path, probed_at in raw.items():
            try:
                result[str(path)] = float(probed_at)
            except (TypeError, ValueError):
                continue
        return result

    def _probe_direct_strm_issues(
        self,
        task: TaskSnapshot,
        issues: list[QualityIssue],
        *,
        now: float | None = None,
    ) -> list[QualityIssue]:
        """Confirm /d/ direct links against the CMS playback probe.

        direct_strm issues whose file URL responds 200/206 stay as-is; files
        that fail the probe (or raise) are re-tagged as dead_direct_link and
        remembered in task metadata (24h cooldown) so the web descriptor does
        not need external calls. Never raises; probe failures degrade to
        "unknown" (issue unchanged).
        """
        if self.strm_url_probe is None:
            return issues
        current_time = time.time() if now is None else float(now)
        direct = [issue for issue in issues if issue.code == "direct_strm" and str(issue.detail).strip()]
        if not direct:
            return issues
        dead_map = self._dead_direct_paths(task)
        changed = False
        output: list[QualityIssue] = []
        dead_paths: dict[str, float] = {}
        for issue in issues:
            if issue.code != "direct_strm":
                output.append(issue)
                continue
            path = str(issue.detail).strip()
            if not path:
                output.append(issue)
                continue
            if path in dead_map and current_time - dead_map[path] <= self._DEAD_DIRECT_PROBE_COOLDOWN_SECONDS:
                output.append(
                    QualityIssue("dead_direct_link", "直链 STRM 已确认失效", issue.detail, issue.task_id, issue.title)
                )
                continue
            if path in dead_map and current_time - dead_map[path] > self._DEAD_DIRECT_PROBE_COOLDOWN_SECONDS:
                # Stale probe result: re-check below.
                pass
            try:
                url = Path(path).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                output.append(issue)
                continue
            if not url.startswith("http"):
                output.append(issue)
                continue
            try:
                alive = bool(self.strm_url_probe(url))
            except Exception:
                LOG.debug("Direct strm probe failed path=%s", path, exc_info=True)
                alive = True  # unknown -> keep the plain direct_strm marker
            if alive:
                # A live direct link is NOT dead; do not record it in the
                # confirmed-dead map, otherwise the 24h cooldown would re-tag
                # the alive file as dead and allow its strm to be deleted.
                output.append(issue)
            else:
                changed = True
                dead_paths[path] = current_time
                output.append(
                    QualityIssue("dead_direct_link", "直链 STRM 已确认失效", issue.detail, issue.task_id, issue.title)
                )
        if changed:
            merged = dict(dead_map)
            merged.update(dead_paths)
            try:
                self.store.update_quality_state(
                    task.id,
                    task.updated_at,
                    {"quality_dead_direct": json.dumps(merged, ensure_ascii=False, sort_keys=True)},
                    "直链 STRM 探测完成",
                    "quality-auto",
                    rule_id=str(task.metadata.get("quality_rule_id") or "") or None,
                )
            except Exception:
                LOG.debug("Failed to persist direct strm probe state task_id=%s", task.id, exc_info=True)
        return output

    def _share_alive_state(self, share_code: str, receive_code: str) -> str:
        """Best-effort 115 share liveness probe: valid / invalid / unknown.

        unknown when no inspector is configured, the probe fails, or the
        response is unparseable. Never raises.
        """
        if self.share_inspector is None:
            return "unknown"
        try:
            state = self.share_inspector(str(share_code), str(receive_code))
        except Exception:
            LOG.debug("Share liveness probe failed code=%s", share_code, exc_info=True)
            return "unknown"
        if not isinstance(state, dict):
            return "unknown"
        share_state = str(state.get("share_state") or "").strip().lower()
        have_vio = str(state.get("have_vio_file") or "").strip().lower() in {"1", "true", "yes"}
        if have_vio:
            return "invalid"
        # 115 treats share_state "0"/"1"/"true" all as usable; only empty or
        # other values mean unavailable (see P115WebClient.share_snap and
        # workflows/self_share.py). Keeping "0" -> valid matches the rest of
        # the codebase so an alive share is never deleted or marked invalid.
        if share_state in {"0", "1", "true"}:
            return "valid"
        return "unknown"

    def stale_strm_candidates(
        self,
        task: TaskSnapshot,
        *,
        check_shares: bool = False,
    ) -> list[dict[str, object]]:
        """Preview which strm files in the task's dest folder reference dead shares.

        A file is a candidate only when it references at least one own-share code
        that is NOT the current task's and NOT referenced by any alive task, is
        not a direct link (/d/), and lives inside allowed roots. Never deletes.
        """
        if not self.strm_cleanup_enabled:
            return []
        dest_value = str(task.metadata.get("dest_path") or "").strip()
        if not dest_value:
            return []
        dest = safe_resolve(Path(dest_value))
        if not is_under_any_root(dest, self.allowed_roots) or not dest.is_dir():
            return []
        own_code = str(task.metadata.get("own_share_code") or "").strip()
        receive_code = str(task.metadata.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        live_codes = self.store.list_live_share_codes()
        dead_direct = self._dead_direct_paths(task)
        candidates: list[dict[str, object]] = []
        share_states: dict[str, str] = {}
        checked_codes: list[str] = []
        try:
            strm_files = sorted(iter_strm_files(dest, allowed_roots=self.allowed_roots, skip_outside_links=True))
        except UnsafeMediaPathError:
            return []
        for path in strm_files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "/d/" in text:
                # Direct links are cleaned only when the playback probe already
                # confirmed them dead (recorded during scheduled runs).
                if str(path) in dead_direct:
                    candidates.append(
                        {
                            "path": str(path),
                            "share_code": "",
                            "reason": "dead_direct_link_confirmed",
                            "share_state": "invalid",
                        }
                    )
                continue
            codes = [code for code, _receive in self._extract_strm_share_codes(text) if code and code != own_code]
            if not codes:
                continue
            dead = [code for code in codes if code not in live_codes]
            if not dead:
                continue
            candidate: dict[str, object] = {
                "path": str(path),
                "share_code": dead[0],
                "reason": "no_live_task_references_share",
                "share_state": "unknown",
            }
            if check_shares:
                # Probe each dead share with the receive code embedded in this
                # strm file (its own password), not the current task's.
                code_receive = {
                    code: receive
                    for code, receive in self._extract_strm_share_codes(text)
                    if code and code != own_code
                }
                code = dead[0]
                if code not in share_states:
                    if len(checked_codes) < self.MAX_SHARE_CHECKS:
                        share_states[code] = self._share_alive_state(
                            code, code_receive.get(code) or receive_code
                        )
                        checked_codes.append(code)
                    else:
                        share_states[code] = "unknown"
                candidate["share_state"] = share_states[code]
            candidates.append(candidate)
        return candidates

    def cleanup_stale_strm(
        self,
        task_id: int,
        paths: Iterable[str],
        *,
        actor: str = "web",
        allow_alive: bool = False,
    ) -> dict[str, object]:
        """Delete confirmed stale strm files with a fresh per-file re-check.

        Every requested path must still be a candidate from a fresh scan at
        execution time (file exists, still references a dead share, not a
        direct link, inside allowed roots). One task_operations row is written
        per file. When the task's issues are fully cleared and it was waiting on
        manual review, evaluation is automatically resumed.
        """
        if not self.strm_cleanup_enabled:
            return {"status": "disabled", "removed": [], "skipped": [], "resumed": False}
        requested = [str(item).strip() for item in paths if str(item).strip()]
        if not requested:
            return {"status": "empty", "removed": [], "skipped": [], "resumed": False}
        if len(requested) > self.MAX_STRM_CLEANUP_PATHS:
            return {"status": "too_many", "removed": [], "skipped": [], "resumed": False}
        task = self.store.find_task(int(task_id))
        if task is None:
            return {"status": "not_found", "removed": [], "skipped": [], "resumed": False}
        candidates = {str(item["path"]) for item in self.stale_strm_candidates(task)}
        live_codes = self.store.list_live_share_codes()
        own_code = str(task.metadata.get("own_share_code") or "").strip()
        receive_code = str(task.metadata.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        dead_direct = self._dead_direct_paths(task)
        removed: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        checked_states: dict[str, str] = {}
        for raw_path in requested:
            try:
                path = safe_resolve(Path(raw_path))
            except (TypeError, ValueError, OSError):
                skipped.append({"path": raw_path, "reason": "invalid_path"})
                continue
            if str(path) not in candidates:
                skipped.append({"path": str(path), "reason": "not_candidate"})
                continue
            if not is_under_any_root(path, self.allowed_roots) or not path.is_file():
                skipped.append({"path": str(path), "reason": "path_changed"})
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped.append({"path": str(path), "reason": "unreadable"})
                continue
            is_dead_direct = str(path) in dead_direct and "/d/" in text
            if "/d/" in text and not is_dead_direct:
                skipped.append({"path": str(path), "reason": "became_direct"})
                continue
            codes = [code for code, _receive in self._extract_strm_share_codes(text) if code and code != own_code]
            if not is_dead_direct and (not codes or all(code in live_codes for code in codes)):
                skipped.append({"path": str(path), "reason": "share_became_live"})
                continue
            # A share that is still alive on 115 is protected by default: deleting
            # its strm breaks playback (the #368 lesson). The caller must opt in
            # with allow_alive to remove such files. Confirmed-dead direct links
            # are never protected (they are already verified dead).
            if (
                not allow_alive
                and not is_dead_direct
                and self.share_inspector is not None
            ):
                # Probe each share with the receive code embedded in this strm
                # file, not the current task's own receive code.
                code_receive = {
                    code: receive
                    for code, receive in self._extract_strm_share_codes(text)
                    if code and code != own_code
                }
                alive_code = None
                for code in codes:
                    if code in live_codes:
                        continue
                    if code not in checked_states:
                        checked_states[code] = self._share_alive_state(
                            code, code_receive.get(code) or receive_code
                        )
                    if checked_states[code] == "valid":
                        alive_code = code
                        break
                if alive_code is not None:
                    skipped.append(
                        {
                            "path": str(path),
                            "reason": "share_still_alive",
                            "share_code": alive_code,
                            "share_state": "valid",
                        }
                    )
                    continue
            digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
            operation_key = f"quality-strm-cleanup-{digest}-{time.monotonic_ns():x}"
            request: dict[str, object] = {
                "path": str(path),
                "share_code": codes[0] if codes else "",
                "dry_run": False,
                # Content snapshot so a deletion can be rolled back exactly, even
                # if the referenced share is gone later. strm files are plain
                # text (~300 bytes); the task_operations table is cleared with
                # the task history, so this stays bounded.
                "content": text,
            }
            operation = self.store.prepare_operation(task.id, operation_key, "quality_strm_cleanup", request)
            started = self.store.start_operation(task.id, operation_key) if operation is not None else None
            try:
                path.unlink()
            except OSError as exc:
                if started is not None:
                    self.store.mark_operation_failed(task.id, operation_key, error=str(exc))
                skipped.append({"path": str(path), "reason": "unlink_failed", "error": str(exc)[:160]})
                continue
            if started is not None:
                self.store.complete_operation(task.id, operation_key, result={"removed": True})
            removed.append({"path": str(path), "share_code": codes[0] if codes else ""})
        resumed = False
        if removed:
            try:
                issues = scan_task_quality(
                    self.store,
                    tasks=[task],
                    allowed_roots=self.allowed_roots,
                    share_identity_resolver=self.share_identity_resolver,
                )
            except Exception:
                LOG.debug("Stale-STRM cleanup rescan failed task_id=%s", task.id, exc_info=True)
                issues = []
            state = self.store.quality_state(task.id)
            manual_status = str(state.get("quality_manual_status") or "").strip().lower()
            rule_id = str(state.get("quality_rule_id") or "").strip() or None
            if not issues and manual_status == "manual_required":
                try:
                    self.store.resume_quality(
                        task.id,
                        actor,
                        rule_id=rule_id,
                        expected_updated_at=task.updated_at,
                    )
                    resumed = True
                except Exception:
                    LOG.debug("Failed to auto-resume quality after stale strm cleanup task_id=%s", task.id, exc_info=True)
        return {
            "status": "ok",
            "removed": removed,
            "skipped": skipped,
            "resumed": resumed,
        }

    def _auto_revalidate_share(self, task: TaskSnapshot, now: float) -> bool:
        """Requeue an invalid-share task so TaskRunner re-inspects the live share."""
        if self._is_archived(task):
            return False
        if task.status == TaskStatus.SUCCEEDED and task.current_stage in {
            TaskStage.MOVED,
            TaskStage.EMBY_CONFIRMED,
            TaskStage.CLEANED,
        }:
            return self._revalidate_stale_share_marker(task, now)
        if task.status != TaskStatus.NEEDS_ACTION or task.current_stage != TaskStage.SHARE_VALIDATED:
            return False
        if str(task.claimed_by or "").strip() or str(task.claim_token or "").strip():
            return False
        metadata = task.metadata
        if str(metadata.get("share_validation_status") or "").strip().lower() != "invalid":
            return False
        if str(metadata.get("invalid_share_status") or "").strip().lower() in {
            "invalid_share_cleaned",
            "source_deleted",
        }:
            return False
        if not str(metadata.get("own_share_code") or "").strip():
            return False
        if self._risk_controlled(task, now):
            return False
        try:
            attempts = int(metadata.get("quality_share_recheck_count") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= self.MAX_SHARE_REVALIDATE_ATTEMPTS:
            return False
        try:
            next_at = float(metadata.get("quality_share_recheck_next_at") or 0)
        except (TypeError, ValueError):
            next_at = 0
        if next_at > now:
            return False
        timestamp = time.time()
        updated = self.store.record_event(
            task.id,
            TaskStage.SHARE_VALIDATED,
            TaskStatus.PENDING,
            "自动复验：清除失效标记并重新验证分享",
            error_type="",
            error_summary="",
            error_detail="",
            metadata_patch={
                "share_validation_status": "",
                "share_validation_error": "",
                "invalid_share_status": "",
                "share_review_status": "pending",
                "quality_share_recheck_count": attempts + 1,
                "quality_share_recheck_last_at": timestamp,
                "quality_share_recheck_next_at": timestamp + self.SHARE_REVALIDATE_COOLDOWN_SECONDS,
            },
            next_run_at=0,
            clear_claim=True,
            expected_stage=TaskStage.SHARE_VALIDATED,
            expected_status=TaskStatus.NEEDS_ACTION,
            expected_updated_at=task.updated_at,
        )
        return updated is not None

    def _revalidate_stale_share_marker(self, task: TaskSnapshot, now: float) -> bool:
        """Clear a stale invalid share marker on a completed task when the live share is valid."""
        if self.share_inspector is None or self.submission_store is None:
            return False
        submission_id = task.submission_id or task.metadata.get("submission_id")
        if submission_id in (None, ""):
            return False
        try:
            row = self.submission_store.find_by_id(int(submission_id))
        except (TypeError, ValueError, AttributeError):
            return False
        if not isinstance(row, dict):
            return False
        if str(row.get("share_validation_status") or "").strip().lower() != "invalid":
            return False
        own_code = str(
            task.metadata.get("own_share_code") or row.get("own_share_code") or ""
        ).strip()
        own_pwd = str(
            task.metadata.get("own_share_receive_code")
            or row.get("own_share_receive_code")
            or DEFAULT_OWN_SHARE_RECEIVE_CODE
        ).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        if not own_code:
            return False
        if self._risk_controlled(task, now):
            return False
        try:
            attempts = int(task.metadata.get("quality_share_recheck_count") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= self.MAX_SHARE_REVALIDATE_ATTEMPTS:
            return False
        try:
            next_at = float(task.metadata.get("quality_share_recheck_next_at") or 0)
        except (TypeError, ValueError):
            next_at = 0
        if next_at > now:
            return False
        try:
            state = self.share_inspector(own_code, own_pwd)
        except Exception:
            return False
        if not isinstance(state, dict):
            return False
        share_state = str(state.get("share_state") or "").strip().lower()
        have_vio_file = str(state.get("have_vio_file") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if share_state not in {"0", "1", "true"} or have_vio_file:
            return False
        timestamp = time.time()
        try:
            self.submission_store.update_self_share(
                int(submission_id),
                share_validation_status="valid",
                share_validation_error="",
            )
        except (TypeError, ValueError, AttributeError):
            return False
        self.store.record_event(
            task.id,
            task.current_stage,
            task.status,
            "自动复验：自有分享恢复可用，已清除失效标记",
            metadata_patch={
                "quality_share_recheck_count": attempts + 1,
                "quality_share_recheck_last_at": timestamp,
                "quality_share_recheck_next_at": timestamp + self.SHARE_REVALIDATE_COOLDOWN_SECONDS,
            },
        )
        return True

    def _retire_unfixable_task(
        self,
        task: TaskSnapshot,
        now: float,
        run_id: str,
    ) -> bool:
        """Delete old terminal tasks that can never be repaired (retention policy)."""
        if self.unfixable_retention_seconds <= 0:
            return False
        terminal = task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {
            TaskStage.FAILED,
            TaskStage.NEEDS_ACTION,
        }
        if not terminal:
            return False
        if str(task.claimed_by or "").strip() or str(task.claim_token or "").strip():
            return False
        if self._recheck_candidate(task, now):
            return False
        metadata = task.metadata
        rule_id = str(metadata.get("quality_rule_id") or "").strip()
        raw_codes = metadata.get("quality_issue_codes") or []
        if isinstance(raw_codes, str):
            raw_codes = [part.strip() for part in raw_codes.split(",") if part.strip()]
        issue_codes = {str(code) for code in raw_codes if str(code).strip()}
        submission_id = task.submission_id or metadata.get("submission_id")
        own_code = str(metadata.get("own_share_code") or "").strip()
        orphan = submission_id in (None, "") and not own_code and task.current_stage in {
            TaskStage.RECEIVED,
            TaskStage.NEEDS_ACTION,
        }
        if (
            rule_id not in self.ARCHIVE_TERMINAL_RULES
            and not (issue_codes & self.ARCHIVE_ISSUE_CODES)
            and not orphan
        ):
            return False
        try:
            base = float(
                metadata.get("quality_terminal_since")
                or metadata.get("quality_archived_at")
                or 0
            )
        except (TypeError, ValueError):
            base = 0
        if base <= 0:
            try:
                base = float(task.updated_at or task.created_at or 0)
            except (TypeError, ValueError):
                base = 0
        if now - base < self.unfixable_retention_seconds:
            return False
        result = delete_task_record_and_submission(self.store, self.submission_store, task.id)
        if result.applied:
            LOG.info("Retired unfixable terminal task task_id=%s run_id=%s", task.id, run_id)
        return result.applied

    def _recheck_candidate(self, task: TaskSnapshot, now: float) -> bool:
        """True when a waiting task is eligible for an automatic recovery recheck."""
        if self._is_archived(task):
            return False
        if task.status != TaskStatus.NEEDS_ACTION or task.current_stage != TaskStage.NEEDS_ACTION:
            return False
        if str(task.claimed_by or "").strip() or str(task.claim_token or "").strip():
            return False
        try:
            if float(task.metadata.get("termination_requested_at") or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
        try:
            retry_stage = TaskStage(str(task.metadata.get("retry_stage") or ""))
        except ValueError:
            return False
        if retry_stage not in self.AUTO_RECHECK_RECOVERABLE_STAGES:
            return False
        error_type = str(task.error_type or "").strip()
        raw_codes = task.metadata.get("quality_issue_codes") or []
        if isinstance(raw_codes, str):
            raw_codes = [part.strip() for part in raw_codes.split(",") if part.strip()]
        issue_codes = {str(code) for code in raw_codes if str(code).strip()}
        recoverable_marker = (
            error_type in self.AUTO_RECHECK_ERROR_TYPES
            or bool(issue_codes & self.AUTO_RECHECK_ISSUE_CODES)
            or "等待超时" in str(task.error_summary or "")
        )
        if not recoverable_marker:
            return False
        if not self._safe_metadata(task):
            return False
        if self._risk_controlled(task, now):
            return False
        try:
            attempts = int(task.metadata.get("quality_auto_recheck_count") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= self.MAX_AUTO_RECHECK_ATTEMPTS:
            return False
        try:
            next_at = float(task.metadata.get("quality_auto_recheck_next_at") or 0)
        except (TypeError, ValueError):
            next_at = 0
        return next_at <= now

    def _scan_skip_reason(self, task: TaskSnapshot, now: float) -> str | None:
        if self._is_archived(task):
            return "archived"
        if str(task.claimed_by or "").strip() or str(task.claim_token or "").strip():
            return "busy"
        terminal = task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {
            TaskStage.FAILED,
            TaskStage.NEEDS_ACTION,
        }
        if terminal:
            return None if self._recheck_candidate(task, now) else "terminal"
        try:
            next_eligible = float(task.metadata.get("quality_next_eligible_at") or 0)
        except (TypeError, ValueError):
            next_eligible = 0
        if next_eligible > now:
            return "cooldown"
        return None

    @staticmethod
    def _directory_fingerprint(path: str | Path) -> tuple[int, int, float] | None:
        try:
            destination = Path(path)
        except (TypeError, ValueError):
            return None
        if not destination.is_dir():
            return None
        file_count = 0
        total_bytes = 0
        newest_mtime = 0.0
        try:
            # os.walk(followlinks=False): a directory symlink must not pull in
            # files from outside the destination when computing the fingerprint.
            for base, _dirnames, filenames in os.walk(destination, followlinks=False):
                base_path = Path(base)
                for name in filenames:
                    if not name.lower().endswith(".strm"):
                        continue
                    child = base_path / name
                    if not child.is_file():
                        continue
                    file_count += 1
                    try:
                        stat = child.stat()
                    except OSError:
                        continue
                    total_bytes += int(stat.st_size or 0)
                    newest_mtime = max(newest_mtime, float(stat.st_mtime or 0))
        except OSError:
            return None
        return (file_count, total_bytes, newest_mtime)

    def _scan_cache_key(self, task: TaskSnapshot) -> str:
        dest_path = str(task.metadata.get("dest_path") or "").strip()
        try:
            mode = effective_task_strm_mode(task)
        except ValueError:
            mode = "shared"
        own_code = str(task.metadata.get("own_share_code") or "").strip()
        receive_code = str(task.metadata.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        return f"{self.SCAN_CACHE_PREFIX}{dest_path}|{mode}|{own_code}|{receive_code}"

    def _load_scan_cache(self, task: TaskSnapshot) -> dict[str, Any] | None:
        row = self.store.get_runtime_state(self._scan_cache_key(task))
        if not row:
            return None
        try:
            payload = json.loads(str(row["value"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            cached_at = float(payload.get("cached_at") or 0)
        except (TypeError, ValueError):
            cached_at = 0
        if time.time() - cached_at > self.SCAN_CACHE_TTL_SECONDS:
            return None
        return payload

    def _store_scan_cache(
        self,
        task: TaskSnapshot,
        fingerprint: tuple[int, int, float],
        issues: list[QualityIssue],
    ) -> None:
        try:
            mode = effective_task_strm_mode(task)
        except ValueError:
            mode = "shared"
        own_code = str(task.metadata.get("own_share_code") or "").strip()
        receive_code = str(task.metadata.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        self.store.set_runtime_state(
            self._scan_cache_key(task),
            json.dumps(
                {
                    "fingerprint": list(fingerprint),
                    "mode": mode,
                    "own_share_code": own_code,
                    "receive_code": receive_code,
                    "issues": [
                        {"code": issue.code, "message": issue.message, "detail": issue.detail}
                        for issue in issues
                    ],
                    "cached_at": time.time(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    def _persist_rule_match(
        self,
        task: TaskSnapshot,
        match: QualityRuleMatch,
        run_id: str,
    ) -> TaskSnapshot | None:
        updated = self.store.update_quality_state(
            task.id,
            task.updated_at,
            {
                "quality_rule_id": match.rule_id,
                "quality_rule_reason": match.reason,
                "quality_rule_risk_level": match.risk_level,
                "quality_issue_codes": list(match.issue_codes),
                "quality_last_run_id": str(run_id),
                "quality_rule_version": QUALITY_RULE_VERSION,
            },
            f"质量规则评估：{match.rule_id}",
            "quality-auto",
        )
        return updated if isinstance(updated, TaskSnapshot) else self.store.find_task(task.id)

    def _plan_match(self, task: TaskSnapshot, match: QualityRuleMatch, now: float) -> QualityRepairPlan:
        base = {
            "task_id": task.id,
            "issue_codes": match.issue_codes,
            "title": _quality_plan_title(task),
            "rule_id": match.rule_id,
            "risk_level": match.risk_level,
            "rule_version": QUALITY_RULE_VERSION,
            "planned_updated_at": task.updated_at,
        }
        if task.status == TaskStatus.RUNNING or task.claimed_by.strip():
            return QualityRepairPlan(action="skip", reason="task_busy", execution_status="skipped", **base)
        if self._is_archived(task):
            return QualityRepairPlan(action="skip", reason="archived", execution_status="skipped", **base)
        if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {
            TaskStage.FAILED,
            TaskStage.NEEDS_ACTION,
        }:
            return QualityRepairPlan(action="skip", reason="terminal_task", execution_status="skipped", **base)
        if not self._safe_metadata(task):
            return QualityRepairPlan(action="skip", reason="unsafe_metadata", execution_status="skipped", **base)
        state = self.store.quality_state(task.id, now=now)
        manual_status = str(state.get("quality_manual_status") or "open").strip().lower()
        if manual_status in {"snoozed", "ignored"}:
            return QualityRepairPlan(action="skip", reason=f"manual_{manual_status}", execution_status="skipped", **base)
        if manual_status == "manual_required":
            return QualityRepairPlan(
                action="skip",
                reason=str(state.get("quality_rule_reason") or "manual_required"),
                execution_status="skipped",
                **base,
            )
        try:
            next_eligible = float(state.get("quality_next_eligible_at") or 0)
        except (TypeError, ValueError):
            next_eligible = 0
        if next_eligible > now:
            return QualityRepairPlan(action="skip", reason="cooldown", execution_status="skipped", **base)
        if match.rule_id in {"missing_destination", "missing_strm"}:
            if self._restore_eligible(task, now):
                return QualityRepairPlan(
                    action="restore",
                    reason=match.rule_id,
                    target_stage=TaskStage.MOVED.value,
                    **base,
                )
            if quality_attempt_count(task) >= int(self.rule_config["max_attempts"]):
                current = self._mark_manual_required(task)
                if current.updated_at != task.updated_at:
                    base["planned_updated_at"] = current.updated_at
                return QualityRepairPlan(action="skip", reason="manual_required", execution_status="skipped", **base)
            return QualityRepairPlan(action="skip", reason=match.rule_id, execution_status="skipped", **base)
        if not match.auto_allowed or match.auto_action != "reprocess":
            if match.rule_id == "repeated_failure":
                current = self._mark_manual_required(task)
                if current.updated_at != task.updated_at:
                    base["planned_updated_at"] = current.updated_at
                return QualityRepairPlan(action="skip", reason="manual_required", execution_status="skipped", **base)
            return QualityRepairPlan(action="skip", reason=match.rule_id, execution_status="skipped", **base)
        attempts = quality_attempt_count(task)
        if attempts >= int(self.rule_config["max_attempts"]):
            current = self._mark_manual_required(task)
            if current.updated_at != task.updated_at:
                base["planned_updated_at"] = current.updated_at
            return QualityRepairPlan(action="skip", reason="manual_required", execution_status="skipped", **base)
        if not self._has_source_evidence(task):
            return QualityRepairPlan(action="skip", reason="missing_source_evidence", execution_status="skipped", **base)
        if self._risk_controlled(task, now):
            return QualityRepairPlan(action="skip", reason="risk_control", execution_status="skipped", **base)
        return QualityRepairPlan(action="reprocess", reason=match.rule_id, **base)

    def _restore_eligible(self, task: TaskSnapshot, now: float) -> bool:
        if task.current_stage not in {TaskStage.MOVED, TaskStage.EMBY_CONFIRMED, TaskStage.CLEANED}:
            return False
        if task.status != TaskStatus.SUCCEEDED:
            return False
        if self._task_move_status(task) != "moved":
            return False
        if bool(task.metadata.get("quality_repair_queued")):
            return False
        if not self._safe_metadata(task) or self._risk_controlled(task, now):
            return False
        if not self._has_source_evidence(task):
            return False
        if not str(task.metadata.get("own_share_file_id") or "").strip():
            return False
        invalid = str(
            task.metadata.get("share_validation_status") or task.metadata.get("invalid_share_status") or ""
        ).strip().lower()
        if invalid in {"invalid", "invalid_share_cleaned", "source_deleted"}:
            return False
        return quality_attempt_count(task) < int(self.rule_config["max_attempts"])

    def _task_move_status(self, task: TaskSnapshot) -> str:
        value = str(task.metadata.get("move_status") or "").strip().lower()
        if value:
            return value
        if self.submission_store is None:
            return ""
        submission_id = task.submission_id or task.metadata.get("submission_id")
        if submission_id in (None, ""):
            return ""
        try:
            row = self.submission_store.find_by_id(int(submission_id))
        except (TypeError, ValueError, AttributeError):
            return ""
        if not isinstance(row, dict):
            return ""
        return str(row.get("move_status") or "").strip().lower()

    def _apply_budgets(
        self,
        plans: list[QualityRepairPlan],
        *,
        max_tasks: int,
        check_limit: int,
    ) -> tuple[list[QualityRepairPlan], dict[str, object]]:
        action_count = 0
        check_count = 0
        output: list[QualityRepairPlan] = []
        for plan in plans:
            if plan.action == "skip":
                output.append(plan)
                continue
            if action_count >= max_tasks:
                output.append(replace(plan, action="skip", reason="max_tasks", execution_status="skipped"))
                continue
            if plan.action == "reprocess" and check_count >= check_limit:
                output.append(replace(plan, action="skip", reason="115_check_budget", execution_status="skipped"))
                continue
            action_count += 1
            if plan.action == "reprocess":
                check_count += 1
            output.append(plan)
        return output, {
            "max_tasks": int(max_tasks),
            "115_check_limit": int(check_limit),
            "used": {"max_tasks": action_count, "115_checks": check_count},
            "used_actions": action_count,
            "used_115_checks": check_count,
        }

    @staticmethod
    def _has_source_evidence(task: TaskSnapshot) -> bool:
        raw_submission = task.submission_id or task.metadata.get("submission_id")
        try:
            if raw_submission not in (None, "") and int(raw_submission) > 0:
                return True
        except (TypeError, ValueError):
            pass
        own_code = str(task.metadata.get("own_share_code") or "").strip()
        receive_code = str(task.metadata.get("own_share_receive_code") or "").strip()
        source = str(task.url or task.source_key or task.metadata.get("source_url") or "").strip()
        return bool(own_code and receive_code and source)

    @staticmethod
    def _risk_controlled(task: TaskSnapshot, now: float) -> bool:
        value = str(task.metadata.get("p115_risk_controlled") or "").strip().lower()
        if value in {"1", "true", "yes", "on", "enabled"}:
            return True
        return risk_cooldown_is_active(task.metadata.get("p115_risk_cooldown_until"), float(now))

    def _mark_manual_required(self, task: TaskSnapshot) -> TaskSnapshot:
        state = self.store.quality_state(task.id)
        if (
            str(state.get("quality_manual_status") or "").strip().lower() == "manual_required"
            and str(state.get("quality_rule_reason") or "").strip().lower() == "manual_required"
        ):
            return task
        updated = self.store.update_quality_state(
            task.id,
            task.updated_at,
            {
                "quality_manual_status": "manual_required",
                "quality_rule_reason": "manual_required",
            },
            "质量自动修复已达到尝试上限",
            "quality-auto",
        )
        return updated or self.store.find_task(task.id) or task

    def execute_plan(self, plan: QualityRepairPlan, run_id: str) -> QualityRepairPlan:
        """Atomically reserve a task before handing repair work to an adapter."""
        if plan.action == "skip":
            return plan
        task = self.store.find_task(plan.task_id)
        if task is None:
            return replace(plan, execution_status="skipped", reason="task_missing")
        if plan.planned_updated_at and task.updated_at != plan.planned_updated_at:
            return replace(plan, execution_status="skipped", reason="task_changed")
        if plan.action == "requeue":
            return self._execute_auto_recheck(plan, task, run_id)
        if plan.action == "restore":
            return self._execute_auto_restore(plan, task, run_id)
        if plan.action != "reprocess":
            return replace(plan, execution_status="skipped", reason="unsupported_action")
        if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {
            TaskStage.FAILED,
            TaskStage.NEEDS_ACTION,
        }:
            return replace(plan, execution_status="skipped", reason="terminal_task")
        invalid_status = str(
            task.metadata.get("invalid_share_status") or task.metadata.get("share_validation_status") or ""
        ).strip().lower()
        if invalid_status in {"invalid", "invalid_share_cleaned", "source_deleted"} or any(
            str(task.metadata.get(key) or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}
            for key in ("invalid_share_cleaned", "source_deleted")
        ):
            return replace(plan, execution_status="skipped", reason="terminal_invalid_share")
        if task.status == TaskStatus.RUNNING or task.claimed_by.strip():
            return replace(plan, execution_status="skipped", reason="task_busy")
        if not self._safe_metadata(task):
            return replace(plan, execution_status="skipped", reason="unsafe_metadata")
        if not self._has_source_evidence(task):
            return replace(plan, execution_status="skipped", reason="missing_source_evidence")
        if self._risk_controlled(task, time.time()):
            return replace(plan, execution_status="skipped", reason="risk_control")
        state = self.store.quality_state(task.id)
        current_manual_status = str(state.get("quality_manual_status") or "open").strip().lower()
        if current_manual_status in {"snoozed", "ignored"}:
            return replace(plan, execution_status="skipped", reason="manual_suppressed")
        if current_manual_status == "manual_required":
            return replace(
                plan,
                execution_status="skipped",
                reason=str(state.get("quality_rule_reason") or "manual_required"),
            )
        try:
            if float(state.get("quality_next_eligible_at") or 0) > time.time():
                return replace(plan, execution_status="skipped", reason="cooldown")
            attempts = quality_attempt_count(task)
        except (TypeError, ValueError):
            attempts = max(0, int(task.retry_count or 0))
        if attempts >= int(self.rule_config["max_attempts"]):
            self._mark_manual_required(task)
            return replace(plan, execution_status="skipped", reason="manual_required")
        stored_version = str(state.get("quality_rule_version") or "").strip()
        if stored_version and stored_version != str(plan.rule_version or QUALITY_RULE_VERSION):
            return replace(plan, execution_status="skipped", reason="rule_version_changed")

        target_stage = reprocess_stage_for(task)
        metadata = {
            "quality_run_id": str(run_id),
            "quality_repair_action": plan.action,
            "quality_repair_reason": plan.reason,
            "quality_rule_id": plan.rule_id,
            "quality_rule_version": QUALITY_RULE_VERSION,
            "quality_last_run_id": str(run_id),
            "quality_last_attempt_at": time.time(),
            "quality_repair_attempts": attempts + 1,
        }
        repair_started_at = time.time()
        next_eligible_at = repair_started_at + min(
            int(self.rule_config["cooldown_seconds"]) * (2**attempts),
            self.MAX_COOLDOWN_SECONDS,
        )
        metadata.update(
            {
                "quality_repair_started_at": repair_started_at,
                "quality_repair_deadline_at": repair_started_at + QUALITY_REPAIR_WAIT_SECONDS,
                "quality_next_eligible_at": next_eligible_at,
            }
        )
        metadata = build_reprocess_metadata(task, metadata)
        metadata_delete_keys = reprocess_delete_keys_for(task)
        reserved = self.store.compare_and_set_transition(
            task.id,
            task.current_stage,
            {TaskStatus.PENDING, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.NEEDS_ACTION},
            require_unclaimed=True,
            target_stage=target_stage,
            target_status=TaskStatus.RUNNING,
            target_event_message=f"自动巡检已排队：{plan.action}",
            metadata_patch=metadata,
            metadata_delete_keys=metadata_delete_keys,
            next_run_at=time.time(),
            clear_errors=True,
            claim_by=f"quality:{run_id}",
            expected_updated_at=task.updated_at,
        )
        if reserved is None:
            return replace(plan, execution_status="skipped", reason="task_busy")

        handler_name = plan.action
        handler = getattr(self.repair_adapter, handler_name, None) if self.repair_adapter is not None else None
        if not callable(handler):
            if self._record_owned_repair_event(
                reserved.id,
                target_stage,
                TaskStatus.FAILED,
                "自动巡检没有可用的修复适配器",
                owner_run_id=run_id,
                claimed_at=reserved.claimed_at,
                claim_token=reserved.claim_token,
                reserved_updated_at=reserved.updated_at,
                error_type="quality_repair_adapter_missing",
                error_summary="repair adapter missing",
                clear_claim=True,
                metadata_patch=self._failure_quality_patch(attempts + 1),
            ) is None:
                return replace(plan, execution_status="skipped", reason="claim_lost")
            return replace(plan, execution_status="failed", reason="repair_adapter_missing")
        try:
            if handler(reserved, str(run_id)) is False:
                if self._record_owned_repair_event(
                    reserved.id,
                    target_stage,
                    TaskStatus.FAILED,
                    "自动巡检修复适配器拒绝执行",
                    owner_run_id=run_id,
                    claimed_at=reserved.claimed_at,
                    claim_token=reserved.claim_token,
                    reserved_updated_at=reserved.updated_at,
                    error_type="quality_repair_rejected",
                    error_summary="repair rejected",
                    clear_claim=True,
                    metadata_patch=self._failure_quality_patch(attempts + 1),
                ) is None:
                    return replace(plan, execution_status="skipped", reason="claim_lost")
                return replace(plan, execution_status="failed", reason="repair_rejected")
            if self._record_owned_repair_event(
                reserved.id,
                target_stage,
                TaskStatus.PENDING,
                f"自动巡检修复已入队：{plan.action}",
                owner_run_id=run_id,
                claimed_at=reserved.claimed_at,
                claim_token=reserved.claim_token,
                reserved_updated_at=reserved.updated_at,
                metadata_patch={"quality_repair_queued": True, "quality_last_actor": "quality-auto"},
                next_run_at=time.time(),
                clear_claim=True,
            ) is None:
                return replace(plan, execution_status="skipped", reason="claim_lost")
        except Exception as exc:
            try:
                if self._record_owned_repair_event(
                    reserved.id,
                    target_stage,
                    TaskStatus.FAILED,
                    f"自动巡检修复失败：{exc}",
                    owner_run_id=run_id,
                    claimed_at=reserved.claimed_at,
                    claim_token=reserved.claim_token,
                    reserved_updated_at=reserved.updated_at,
                    error_type="quality_repair_failed",
                    error_summary=str(exc),
                    error_detail=repr(exc),
                    clear_claim=True,
                    metadata_patch=self._failure_quality_patch(attempts + 1),
                ) is None:
                    return replace(plan, execution_status="skipped", reason="claim_lost")
            except Exception:
                pass
            return replace(plan, execution_status="failed", reason="repair_failed")
        return replace(plan, execution_status="queued")

    def _execute_auto_recheck(
        self,
        plan: QualityRepairPlan,
        task: TaskSnapshot,
        run_id: str,
    ) -> QualityRepairPlan:
        try:
            target_stage = TaskStage(str(plan.target_stage or ""))
        except ValueError:
            return replace(plan, execution_status="skipped", reason="invalid_target_stage")
        if target_stage not in self.AUTO_RECHECK_RECOVERABLE_STAGES:
            return replace(plan, execution_status="skipped", reason="invalid_target_stage")
        if task.status != TaskStatus.NEEDS_ACTION or task.current_stage != TaskStage.NEEDS_ACTION:
            return replace(plan, execution_status="skipped", reason="task_changed")
        if task.claimed_by.strip() or task.claim_token.strip():
            return replace(plan, execution_status="skipped", reason="task_busy")
        if not self._safe_metadata(task) or self._risk_controlled(task, time.time()):
            return replace(plan, execution_status="skipped", reason="risk_control")
        try:
            attempts = int(task.metadata.get("quality_auto_recheck_count") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts >= self.MAX_AUTO_RECHECK_ATTEMPTS:
            return replace(plan, execution_status="skipped", reason="manual_required")
        now = time.time()
        next_at = now + self.AUTO_RECHECK_COOLDOWN_SECONDS
        updated = self.store.compare_and_set_transition(
            task.id,
            TaskStage.NEEDS_ACTION,
            {TaskStatus.NEEDS_ACTION},
            require_unclaimed=True,
            target_stage=target_stage,
            target_status=TaskStatus.PENDING,
            target_event_message="自动复检：目标 STRM/媒体目录已恢复，重新入队",
            metadata_patch={
                "quality_auto_recheck_count": attempts + 1,
                "quality_auto_recheck_last_at": now,
                "quality_auto_recheck_next_at": next_at,
                "quality_auto_recheck_target_stage": target_stage.value,
                "quality_repair_action": "requeue",
                "quality_rule_id": "auto_recheck",
                "quality_last_run_id": str(run_id),
            },
            next_run_at=0,
            clear_errors=True,
            clear_claim=True,
            expected_updated_at=task.updated_at,
        )
        if updated is None:
            return replace(plan, execution_status="skipped", reason="task_changed")
        return replace(plan, execution_status="queued")

    def _execute_auto_restore(
        self,
        plan: QualityRepairPlan,
        task: TaskSnapshot,
        run_id: str,
    ) -> QualityRepairPlan:
        if task.current_stage not in {TaskStage.MOVED, TaskStage.EMBY_CONFIRMED, TaskStage.CLEANED}:
            return replace(plan, execution_status="skipped", reason="task_changed")
        if task.status != TaskStatus.SUCCEEDED:
            return replace(plan, execution_status="skipped", reason="task_changed")
        if task.claimed_by.strip() or task.claim_token.strip():
            return replace(plan, execution_status="skipped", reason="task_busy")
        if not self._restore_eligible(task, time.time()):
            return replace(plan, execution_status="skipped", reason="missing_source_evidence")
        now = time.time()
        attempts = quality_attempt_count(task)
        next_eligible_at = now + min(
            int(self.rule_config["cooldown_seconds"]) * (2**attempts),
            self.MAX_COOLDOWN_SECONDS,
        )
        metadata = {
            "retry_from_stage": task.current_stage.value,
            "retry_stage": TaskStage.MOVED.value,
            "quality_repair_action": "restore",
            "quality_repair_reason": plan.reason,
            "quality_rule_id": plan.rule_id,
            "quality_rule_version": QUALITY_RULE_VERSION,
            "quality_last_run_id": str(run_id),
            "quality_last_attempt_at": now,
            "quality_repair_attempts": attempts + 1,
            "quality_repair_started_at": now,
            "quality_repair_deadline_at": now + QUALITY_REPAIR_WAIT_SECONDS,
            "quality_next_eligible_at": next_eligible_at,
            "quality_repair_queued": True,
        }
        updated = self.store.compare_and_set_transition(
            task.id,
            task.current_stage,
            {TaskStatus.SUCCEEDED},
            require_unclaimed=True,
            target_stage=TaskStage.MOVED,
            target_status=TaskStatus.PENDING,
            target_event_message="自动巡检已排队：restore",
            metadata_patch=metadata,
            next_run_at=0,
            clear_errors=True,
            clear_claim=True,
            expected_updated_at=task.updated_at,
        )
        if updated is None:
            return replace(plan, execution_status="skipped", reason="task_changed")
        return replace(plan, execution_status="queued")

    def _record_owned_repair_event(
        self,
        task_id: int,
        target_stage: TaskStage,
        status: TaskStatus,
        message: str,
        *,
        owner_run_id: str,
        claimed_at: float,
        claim_token: str,
        reserved_updated_at: float,
        **kwargs: object,
    ) -> TaskSnapshot | None:
        return self.store.record_event(
            task_id,
            target_stage,
            status,
            message,
            expected_stage=target_stage,
            expected_status=TaskStatus.RUNNING,
            expected_claimed_by=f"quality:{owner_run_id}",
            expected_claimed_at=claimed_at,
            expected_claim_token=claim_token,
            expected_updated_at=reserved_updated_at,
            **kwargs,
        )

    def _failure_quality_patch(self, attempts: int) -> dict[str, object]:
        patch: dict[str, object] = {
            "quality_repair_attempts": int(attempts),
            "quality_last_attempt_at": time.time(),
        }
        if int(attempts) >= int(self.rule_config["max_attempts"]):
            patch["quality_manual_status"] = "manual_required"
            patch["quality_rule_reason"] = "manual_required"
        return patch

    def cleanup_if_safe(self, task: TaskSnapshot, run_id: str) -> QualityCleanupResult:
        """Run cleanup only after the local, share, Emby, and event gates pass."""
        task = self.store.find_task(task.id) or task
        metadata = task.metadata
        if metadata.get("quality_cleanup_completed"):
            return QualityCleanupResult("already_cleaned")
        if not metadata.get("own_share_available"):
            return QualityCleanupResult("blocked_cleanup", "own_share_not_available")
        if (
            task.status != TaskStatus.SUCCEEDED
            or task.current_stage not in {TaskStage.EMBY_CONFIRMED, TaskStage.CLEANED}
            or str(metadata.get("emby_status") or "").lower() != "confirmed"
            or metadata.get("emby_match_count") != 1
        ):
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
        receive_code = str(metadata.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        marker = f"/s/{own_share_code}_{receive_code}_"
        strm_files = [
            base_path / name
            for base, _dirnames, filenames in os.walk(destination, followlinks=False)
            for base_path in [Path(base)]
            for name in filenames
            if name.lower().endswith(".strm") and (base_path / name).is_file()
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
        if str(metadata.get("share_review_status") or "").strip().lower() != "passed":
            return QualityCleanupResult("blocked_cleanup", "share_review_not_passed")
        handler = getattr(self.repair_adapter, "cleanup", None) if self.repair_adapter is not None else None
        if not callable(handler):
            return QualityCleanupResult("blocked_cleanup", "cleanup_adapter_missing")
        reserved = task
        if hasattr(self.store, "claim_quality_cleanup"):
            reserved = self.store.claim_quality_cleanup(
                task.id,
                str(run_id),
                expected_updated_at=task.updated_at,
            )
            if reserved is None:
                return QualityCleanupResult("blocked_cleanup", "cleanup_busy")
        try:
            if handler(reserved, str(run_id)) is False:
                finish = getattr(self.store, "record_quality_cleanup_event", None)
                if callable(finish):
                    finish(
                        reserved.id,
                        str(run_id),
                        reserved.status,
                        "自动巡检清理被拒绝",
                        expected_claimed_at=reserved.claimed_at,
                        expected_claim_token=reserved.claim_token,
                        expected_updated_at=reserved.updated_at,
                        error_type="quality_cleanup_rejected",
                        error_summary="cleanup rejected",
                    )
                return QualityCleanupResult("blocked_cleanup", "cleanup_rejected")
        except Exception:
            finish = getattr(self.store, "record_quality_cleanup_event", None)
            if callable(finish):
                finish(
                    reserved.id,
                    str(run_id),
                    TaskStatus.NEEDS_ACTION,
                    "自动巡检清理失败",
                    expected_claimed_at=reserved.claimed_at,
                    expected_claim_token=reserved.claim_token,
                    expected_updated_at=reserved.updated_at,
                    error_type="quality_cleanup_failed",
                    error_summary="cleanup failed",
                )
            return QualityCleanupResult("blocked_cleanup", "cleanup_failed")
        try:
            finish = getattr(self.store, "record_quality_cleanup_event", None)
            if not callable(finish) or not finish(
                reserved.id,
                str(run_id),
                reserved.status,
                "自动巡检清理完成",
                metadata_patch={"quality_cleanup_completed": True},
                expected_claimed_at=reserved.claimed_at,
                expected_claim_token=reserved.claim_token,
                expected_updated_at=reserved.updated_at,
            ):
                raise RuntimeError("cleanup owner changed before completion was persisted")
        except Exception:
            finalize = getattr(self.store, "finalize_quality_cleanup", None)
            if not callable(finalize) or not finalize(
                reserved.id,
                str(run_id),
                expected_claimed_at=reserved.claimed_at,
                expected_claim_token=reserved.claim_token,
                expected_updated_at=reserved.updated_at,
            ):
                return QualityCleanupResult("blocked_cleanup", "cleanup_completion_persist_failed")
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
            title=_quality_plan_title(task),
            execution_status="skipped",
        )
