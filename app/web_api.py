from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from .models import TaskSnapshot
from .task_diagnostics import explain_task_slowness, format_stage_observability
from .task_health import build_task_health
from .task_store import TaskStore


def _safe_url(value: str) -> str:
    """Keep links useful to the UI without returning share passwords or tokens."""
    parsed = urlsplit(str(value or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"password", "passwd", "pwd", "code", "token", "access_token", "cookie"}:
            query.append((key, "***"))
        else:
            query.append((key, item))
    encoded_query = "&".join(f"{quote(key)}={quote(item, safe='*')}" for key, item in query)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded_query, ""))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    sensitive_keys = {"cookie", "p115_cookie", "access_token", "token", "receive_code", "password", "own_share_url"}
    for key, value in metadata.items():
        if str(key).lower() in sensitive_keys:
            continue
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            result[str(key)] = _safe_url(value)
        elif isinstance(value, dict):
            result[str(key)] = _safe_metadata(value)
        else:
            result[str(key)] = value
    return result


def serialize_task(task: TaskSnapshot, *, now: float | None = None) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    elapsed, p115_calls = format_stage_observability(task)
    return {
        "id": task.id,
        "title": task.title or task.share_code,
        "source_type": task.source_type,
        "stage": _enum_value(task.current_stage),
        "status": _enum_value(task.status),
        "strm_mode": str(task.metadata.get("strm_mode") or "shared"),
        "category": task.category or task.metadata.get("category") or "",
        "tmdb_id": task.tmdb_id or task.metadata.get("tmdb_id") or "",
        "safe_url": _safe_url(task.url),
        "error": {"type": task.error_type, "summary": task.error_summary},
        "retry_count": task.retry_count,
        "next_run_at": task.next_run_at,
        "claimed": bool(task.claimed_by),
        "why_slow": explain_task_slowness(task, now=current_time),
        "stage_elapsed": elapsed,
        "stage_p115_calls": p115_calls,
        "metadata": _safe_metadata(task.metadata),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(event.get("id") or 0),
        "stage": str(event.get("stage") or ""),
        "status": str(event.get("status") or ""),
        "message": str(event.get("message") or ""),
        "error_type": str(event.get("error_type") or ""),
        "created_at": float(event.get("created_at") or 0),
    }


def serialize_health(store: TaskStore, *, enabled: bool = True, now: float | None = None) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    summary = build_task_health(store, enabled=enabled, now=current_time)
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
        "wait_details": list(summary.wait_details),
        "latest_problem": serialize_task(summary.latest_problem, now=current_time) if summary.latest_problem else None,
        "latest_lock_wait": serialize_task(summary.latest_lock_wait, now=current_time) if summary.latest_lock_wait else None,
    }


def serialize_hdhive(service: Any | None) -> dict[str, Any]:
    if service is None:
        return {"enabled": False, "subscriptions": []}
    subscriptions = []
    for subscription in service.list():
        item_rows = []
        for item in service.store.list_items(subscription.id):
            if is_dataclass(item):
                item_rows.append(asdict(item))
        row = asdict(subscription) if is_dataclass(subscription) else {"id": subscription.id}
        row["items"] = item_rows
        subscriptions.append(row)
    return {"enabled": True, "subscriptions": subscriptions}


def api_response(payload: Any, *, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, body


def api_tasks(store: TaskStore, *, limit: int = 100, now: float | None = None) -> dict[str, Any]:
    tasks = store.list_recent_tasks(limit=max(1, min(int(limit), 500)))
    return {"items": [serialize_task(task, now=now) for task in tasks], "count": len(tasks)}


def api_task_detail(store: TaskStore, task_id: int, *, now: float | None = None) -> dict[str, Any] | None:
    task = store.find_task(task_id)
    if task is None:
        return None
    result = serialize_task(task, now=now)
    result["events"] = [serialize_event(event) for event in store.list_events(task.id)]
    return result
