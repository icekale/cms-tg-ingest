from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from .background_jobs import redact_background_text
from .logging_system import redact_text
from .models import TaskSnapshot
from .quality_rules import QUALITY_RULE_VERSION, QualityRuleEngine, quality_attempt_count
from .task_diagnostics import explain_task_slowness, format_stage_observability
from .task_health import build_task_health
from .quality import redact_quality_detail, scan_task_quality
from .strm_mode import effective_task_strm_mode
from .task_actions import available_lifecycle_actions, task_termination_requested
from .task_store import TaskStore


_SENSITIVE_QUERY_KEYS = {
    "password",
    "passwd",
    "pwd",
    "code",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "share_password",
    "share_pwd",
    "hdhive_token",
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "bearer_token",
    "session_token",
    "csrf_token",
    "p115_cookie",
}
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|access_token|refresh_token|cookie|share[-_]?password|share[-_]?pwd|hdhive[-_]?token|api[-_]?key|authorization|auth_token|bearer_token|session_token|csrf_token|p115_cookie)\s*([:=])\s*([^\s&;,]+)"
)
_SENSITIVE_METADATA_KEYS = {
    "authorization",
    "cookie",
    "p115_cookie",
    "access_token",
    "refresh_token",
    "hdhive_token",
    "token",
    "receive_code",
    "own_share_receive_code",
    "password",
    "share_password",
    "share_pwd",
    "api_key",
    "apikey",
    "auth_token",
    "bearer_token",
    "session_token",
    "csrf_token",
    "own_share_url",
}
_SENSITIVE_METADATA_KEY_SUFFIXES = (
    "_token",
    "_api_key",
    "_cookie",
    "_password",
    "_passwd",
    "_pwd",
    "_secret",
)
_OWN_SHARE_RECEIVE_CODE_SOURCES = {"web", "cms", "env", "default"}


def _safe_error(value: Any) -> str:
    redacted = redact_background_text(redact_text(value or ""))
    redacted = redacted.replace("[REDACTED]", "***").replace("[redacted]", "***")
    return _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2***", redacted)


def _safe_url(value: str) -> str:
    """Keep links useful to the UI without returning share passwords or tokens."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    try:
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        hostname = ""
        port = None
    if not hostname:
        return ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    safe_netloc = hostname + (f":{port}" if port is not None else "")
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _SENSITIVE_QUERY_KEYS or _is_sensitive_metadata_key(key):
            query.append((key, "***"))
        else:
            query.append((key, item))
    encoded_query = "&".join(f"{quote(key)}={quote(item, safe='*')}" for key, item in query)
    return urlunsplit((parsed.scheme, safe_netloc, parsed.path, encoded_query, ""))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def task_display_title(task: Any) -> str:
    metadata = getattr(task, "metadata", {}) or {}
    organized = metadata.get("organized_folder")
    if isinstance(organized, dict):
        folder_name = str(organized.get("file_name") or "").strip()
        if folder_name:
            return folder_name
    for key in ("own_share_file_name", "dest_path", "source_path", "emby_path"):
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue
        if key.endswith("_path"):
            name = Path(value).name
            if name:
                return name
        return value
    title = str(getattr(task, "title", "") or "").strip()
    if title and not title.startswith(("http://", "https://")):
        return title
    return str(getattr(task, "share_code", "") or title or "-")


def _safe_container_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _safe_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_safe_container_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _safe_url(value)
        return _safe_error(value)
    return value


def _is_sensitive_metadata_key(value: Any) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_METADATA_KEYS or normalized.endswith(_SENSITIVE_METADATA_KEY_SUFFIXES)


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if _is_sensitive_metadata_key(key):
            continue
        result[str(key)] = _safe_container_value(value)
    return result


def _safe_api_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key == "own_share_receive_code" and isinstance(item, dict):
                configured = bool(item.get("configured"))
                source = str(item.get("source") or "").strip().lower()
                result[key] = {
                    "configured": configured,
                    "masked": "****" if configured else "",
                    "source": source if source in _OWN_SHARE_RECEIVE_CODE_SOURCES else "",
                }
                continue
            if _is_sensitive_metadata_key(normalized_key):
                result[key] = "***"
                continue
            result[key] = _safe_api_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_api_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _safe_url(value)
        return _safe_error(value)
    return value


def serialize_task(
    task: TaskSnapshot,
    *,
    now: float | None = None,
    lifecycle_actions_enabled: bool = True,
) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    elapsed, p115_calls = format_stage_observability(task)
    termination_requested = task_termination_requested(task)
    available_actions = sorted(available_lifecycle_actions(task)) if lifecycle_actions_enabled else []
    return _safe_api_value({
        "id": task.id,
        "title": task.title or task.share_code,
        "display_title": task_display_title(task),
        "source_type": task.source_type,
        "stage": _enum_value(task.current_stage),
        "status": _enum_value(task.status),
        "strm_mode": effective_task_strm_mode(task),
        "category": task.category or task.metadata.get("category") or "",
        "tmdb_id": task.tmdb_id or task.metadata.get("tmdb_id") or "",
        "safe_url": _safe_url(task.url),
        "error": {"type": task.error_type, "summary": _safe_error(task.error_summary)},
        "retry_count": task.retry_count,
        "next_run_at": task.next_run_at,
        "claimed": bool(task.claimed_by),
        "available_actions": available_actions,
        "termination_requested": termination_requested,
        "why_slow": explain_task_slowness(task, now=current_time),
        "stage_elapsed": elapsed,
        "stage_p115_calls": p115_calls,
        "metadata": _safe_metadata(task.metadata),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    })


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    return _safe_api_value({
        "id": int(event.get("id") or 0),
        "stage": str(event.get("stage") or ""),
        "status": str(event.get("status") or ""),
        "message": _safe_error(event.get("message")),
        "error_type": str(event.get("error_type") or ""),
        "created_at": float(event.get("created_at") or 0),
    })


def serialize_health(store: TaskStore, *, enabled: bool = True, now: float | None = None) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    summary = build_task_health(store, enabled=enabled, now=current_time)
    backup = {"status": "never", "file_count": 0, "skipped_count": 0, "error": ""}
    backup_state = store.get_runtime_state("backup_last_result")
    if backup_state:
        try:
            parsed_backup = json.loads(str(backup_state.get("value") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_backup = {}
        if isinstance(parsed_backup, dict):
            backup = {
                "status": str(parsed_backup.get("status") or "unknown"),
                "file_count": len(parsed_backup.get("files") or []) if isinstance(parsed_backup.get("files"), list) else 0,
                "skipped_count": len(parsed_backup.get("skipped") or []) if isinstance(parsed_backup.get("skipped"), list) else 0,
                "error": _safe_error(str(parsed_backup.get("error") or ""))[:160],
                "finished_at": str(parsed_backup.get("finished_at") or ""),
            }
    return {
        "enabled": summary.enabled,
        "recent_count": summary.recent_count,
        "pending_count": summary.pending_count,
        "running_count": summary.running_count,
        "needs_action_count": summary.needs_action_count,
        "problem_count": summary.problem_count,
        "lock_wait_count": summary.lock_wait_count,
        "p115_cooldown_until": summary.p115_cooldown_until,
        "p115_cooldown_active": summary.p115_cooldown_until > current_time,
        "runner_heartbeat_at": summary.runner_heartbeat_at,
        "runner_heartbeat_stale": summary.runner_heartbeat_stale,
        "runner_state": summary.runner_state,
        "backup": backup,
        "wait_details": list(summary.wait_details),
        "latest_problem": (
            serialize_task(
                summary.latest_problem,
                now=current_time,
                lifecycle_actions_enabled=enabled,
            )
            if summary.latest_problem
            else None
        ),
        "latest_lock_wait": (
            serialize_task(
                summary.latest_lock_wait,
                now=current_time,
                lifecycle_actions_enabled=enabled,
            )
            if summary.latest_lock_wait
            else None
        ),
    }


def serialize_background_job(background_jobs: Any | None, *, prefix: str = "") -> dict[str, Any] | None:
    if background_jobs is None:
        return None
    snapshots = getattr(background_jobs, "list_snapshots", lambda: ())()
    matches = [snapshot for snapshot in snapshots if str(getattr(snapshot, "key", "")).startswith(prefix)]
    if not matches:
        return None
    snapshot = max(matches, key=lambda item: float(getattr(item, "queued_at", 0)))
    return {
        "description": redact_background_text(getattr(snapshot, "description", "")),
        "state": str(getattr(snapshot, "status", "")),
        "started_at": getattr(snapshot, "started_at", None),
        "finished_at": getattr(snapshot, "finished_at", None),
        "error": redact_background_text(getattr(snapshot, "error", "")),
    }


def serialize_hdhive(
    service: Any | None,
    scheduler: Any | None = None,
    background_jobs: Any | None = None,
) -> dict[str, Any]:
    if service is None:
        return {"enabled": False, "subscriptions": [], "account": None, "schedule": {}, "background_job": None}
    subscriptions = []
    for subscription in service.list():
        item_rows = []
        for item in service.store.list_items(subscription.id):
            if is_dataclass(item):
                item_row = asdict(item)
                item_row.pop("unlocked_url", None)
                item_row["last_error"] = _safe_error(item_row.get("last_error"))
                item_rows.append(item_row)
        row = asdict(subscription) if is_dataclass(subscription) else {"id": subscription.id}
        row.pop("source_url", None)
        row.pop("chat_id", None)
        row["last_error"] = _safe_error(row.get("last_error"))
        row["items"] = item_rows
        try:
            summary = json.loads(str(row.get("last_summary_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        row["last_summary"] = summary if isinstance(summary, dict) else {}
        row["completed"] = str(row.get("status") or "").lower() == "completed"
        subscriptions.append(row)
    account = None
    account_getter = getattr(getattr(service, "proxy", None), "account", None)
    if callable(account_getter):
        try:
            value = account_getter()
            account = {
                "nickname": str(getattr(value, "nickname", "")),
                "points": int(getattr(value, "points", 0)),
                "weekly_free_quota_remaining": int(getattr(value, "weekly_free_quota_remaining", 0)),
                "weekly_free_quota_unlimited": bool(getattr(value, "weekly_free_quota_unlimited", False)),
                "level": str(getattr(value, "level", "")),
                "is_blocked": bool(getattr(value, "is_blocked", False)),
                "is_forever_vip": bool(getattr(value, "is_forever_vip", False)),
            }
        except Exception as exc:
            account = {"error": _safe_error(str(exc)[:160])}
    schedule = {}
    if scheduler is not None:
        try:
            schedule = dict(scheduler.status_snapshot())
        except Exception as exc:
            schedule = {"error": _safe_error(str(exc)[:160])}
    return _safe_api_value({
        "enabled": True,
        "subscriptions": subscriptions,
        "account": account,
        "schedule": schedule,
        "background_job": serialize_background_job(background_jobs, prefix="hdhive:"),
    })


def serialize_hdhive_subscription(service: Any | None, subscription_id: int) -> dict[str, Any] | None:
    if service is None:
        return None
    payload = serialize_hdhive(service)
    return next(
        (row for row in payload.get("subscriptions", []) if int(row.get("id") or 0) == int(subscription_id)),
        None,
    )


def api_response(payload: Any, *, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(_safe_api_value(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, body


def api_tasks(
    store: TaskStore,
    *,
    limit: int = 100,
    now: float | None = None,
    lifecycle_actions_enabled: bool = True,
) -> dict[str, Any]:
    tasks = store.list_recent_tasks(limit=max(1, min(int(limit), 500)))
    return {
        "items": [
            serialize_task(task, now=now, lifecycle_actions_enabled=lifecycle_actions_enabled)
            for task in tasks
        ],
        "count": len(tasks),
    }


def api_task_detail(
    store: TaskStore,
    task_id: int,
    *,
    now: float | None = None,
    lifecycle_actions_enabled: bool = True,
) -> dict[str, Any] | None:
    task = store.find_task(task_id)
    if task is None:
        return None
    result = serialize_task(task, now=now, lifecycle_actions_enabled=lifecycle_actions_enabled)
    result["events"] = [serialize_event(event) for event in store.list_events(task.id)]
    return result


def quality_items(
    store: TaskStore,
    *,
    limit: int = 100,
    quality_automation: Any | None = None,
    now: float | None = None,
    issues: list[Any] | None = None,
) -> list[dict[str, Any]]:
    allowed_roots = getattr(quality_automation, "allowed_roots", None)
    if not isinstance(allowed_roots, (list, tuple, set, frozenset)):
        allowed_roots = None
    if issues is None:
        share_identity_resolver = getattr(quality_automation, "share_identity_resolver", None)
        scan_kwargs = {
            "limit": max(1, min(int(limit), 500)),
            "allowed_roots": allowed_roots,
        }
        if callable(share_identity_resolver):
            scan_kwargs["share_identity_resolver"] = share_identity_resolver
        issues = scan_task_quality(store, **scan_kwargs)
    grouped: dict[int, list[Any]] = {}
    for issue in issues:
        grouped.setdefault(int(issue.task_id), []).append(issue)
    engine = getattr(quality_automation, "rule_engine", None)
    if not isinstance(engine, QualityRuleEngine):
        engine = QualityRuleEngine()
    config = getattr(quality_automation, "rule_config", None)
    if not isinstance(config, dict):
        config = None
    current_time = time.time() if now is None else float(now)
    items: list[dict[str, Any]] = []
    for issue in issues:
        task = store.find_task(int(issue.task_id)) if int(issue.task_id) > 0 else None
        if task is None:
            descriptor = {
                "rule_id": "manual_required",
                "rule_reason": "task_not_found",
                "risk_level": "high",
                "issue_codes": [issue.code],
                "manual_status": "manual_required",
                "attempts": 0,
                "next_eligible_at": 0,
                "available_actions": ["view"],
                "evidence": [issue.detail] if issue.detail else [],
                "auto_allowed": False,
                "rule_version": QUALITY_RULE_VERSION,
            }
        elif quality_automation is not None and callable(getattr(quality_automation, "quality_descriptor", None)):
            candidate = quality_automation.quality_descriptor(task, grouped[int(issue.task_id)], now=current_time)
            descriptor = candidate if isinstance(candidate, dict) else None
            if descriptor is None:
                match = engine.evaluate(task, grouped[int(issue.task_id)], config=config)
                descriptor = {
                    "rule_id": match.rule_id,
                    "rule_reason": match.reason,
                    "risk_level": match.risk_level,
                    "issue_codes": list(match.issue_codes),
                    "manual_status": "manual_required",
                    "attempts": quality_attempt_count(task),
                    "next_eligible_at": 0,
                    "available_actions": ["view"],
                    "evidence": list(match.evidence),
                    "auto_allowed": False,
                    "rule_version": QUALITY_RULE_VERSION,
                }
        else:
            match = engine.evaluate(task, grouped[int(issue.task_id)], config=config)
            descriptor = {
                "rule_id": match.rule_id,
                "rule_reason": match.reason,
                "risk_level": match.risk_level,
                "issue_codes": list(match.issue_codes),
                "manual_status": "manual_required",
                "attempts": quality_attempt_count(task),
                "next_eligible_at": 0,
                "available_actions": ["view"],
                "evidence": list(match.evidence),
                "auto_allowed": False,
                "rule_version": QUALITY_RULE_VERSION,
            }
        descriptor = dict(descriptor)
        descriptor["evidence"] = [redact_quality_detail(value) for value in descriptor.get("evidence", [])]
        items.append(
            {
                "task_id": issue.task_id,
                "title": issue.title or (task.title if task is not None else ""),
                "display_title": task_display_title(task) if task is not None else issue.title,
                "code": issue.code,
                "message": issue.message,
                "detail": redact_quality_detail(issue.detail),
                **descriptor,
            }
        )
    return items


def api_quality(
    store: TaskStore,
    *,
    limit: int = 100,
    quality_automation: Any | None = None,
    background_jobs: Any | None = None,
) -> dict[str, Any]:
    items = quality_items(store, limit=limit, quality_automation=quality_automation)
    rule_counts: dict[str, int] = {}
    task_keys: set[tuple[int, str]] = set()
    manual_count = 0
    cooldown_count = 0
    now = time.time()
    for item in items:
        key = (int(item["task_id"]), str(item["rule_id"]))
        if key not in task_keys:
            task_keys.add(key)
            rule_counts[str(item["rule_id"])] = rule_counts.get(str(item["rule_id"]), 0) + 1
            if str(item["manual_status"]) == "manual_required":
                manual_count += 1
            try:
                cooldown_count += float(item["next_eligible_at"] or 0) > now
            except (TypeError, ValueError):
                pass
    payload = {
        "count": len(items),
        "items": items,
        "rule_counts": rule_counts,
        "manual_count": manual_count,
        "cooldown_count": cooldown_count,
    }
    if quality_automation is not None:
        snapshot = quality_automation.status_snapshot()
        payload["automation"] = snapshot if isinstance(snapshot, dict) else {}
    payload["background_job"] = serialize_background_job(background_jobs, prefix="quality:run")
    return payload
