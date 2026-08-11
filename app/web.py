from __future__ import annotations

import errno
import hashlib
import hmac
import html
import secrets
from hmac import compare_digest
def _constant_time_equals(provided: object, expected: object) -> bool:
    """Constant-time string comparison that tolerates non-ASCII input.

    ``hmac.compare_digest`` raises TypeError for non-ASCII str arguments, so
    encode both sides as UTF-8 bytes first. Non-encodable inputs simply fail
    closed.
    """
    try:
        provided_bytes = str(provided or "").encode("utf-8")
        expected_bytes = str(expected or "").encode("utf-8")
    except (TypeError, UnicodeEncodeError):
        return False
    return compare_digest(provided_bytes, expected_bytes)
import json
import mimetypes
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import __version__
from .background_jobs import BackgroundJobCoordinator, JobSubmission, redact_background_text
from .config import SelfShareConfig
from .logging_system import LogFilter, LogHub, parse_log_filter
from .media.classify import enrich_task_media_metadata
from .models import TaskStage, TaskStatus
from .quality import QualityIssue, format_task_quality_report, scan_task_quality
from .quality_automation import QualityAutomation
from .task_bridge import sync_task_from_submission
from .task_diagnostics import (
    _duration,
    format_stage_observability,
    format_task_observability,
    is_dispatchable_active_task,
    is_unscheduled_active_task,
)
from .task_actions import TASK_ACTIONS, apply_task_action, available_task_actions, delete_task_record
from .task_actions import available_lifecycle_actions, delete_task_record_and_submission
from .config import normalize_task_max_retries
from .task_engine import decide_retry, stage_display_name
from .task_health import build_task_health, format_task_health
from .task_store import TaskStore
from .self_share_settings import (
    resolve_own_share_receive_code,
    resolve_self_share_receive_cid,
    resolve_self_share_review_policy,
)
from .strm_mode import STRM_MODE_LABELS
from .web_api import (
    api_response,
    api_task_detail,
    api_log_analysis,
    api_quality,
    api_quality_runs,
    api_cms_version,
    api_tasks,
    api_emby_dashboard,
    check_cms_strm_guard,
    check_cms_direct_strm_guard,
    check_cms_os_strm_guard,
    CMS_STRM_GUARD_MARKER,
    CMS_DIRECT_STRM_GUARD_MARKER,
    CMS_OS_STRM_GUARD_MARKER,
    quality_items,
    serialize_health,
    serialize_hdhive,
    serialize_hdhive_subscription,
    task_display_title,
    _safe_url,
)


MAX_REQUEST_BODY_BYTES = 64 * 1024
REQUEST_BODY_READ_TIMEOUT_SECONDS = 2
SSE_HEARTBEAT_SECONDS = 15.0
SSE_CLIENT_QUEUE_SIZE = 256
SSE_MAX_CLIENTS = 8
SSE_WRITE_TIMEOUT_SECONDS = 5.0
LEGACY_LIFECYCLE_REASON = "旧版任务引擎模式不支持终止或删除任务"
TASK_NOT_FOUND_MESSAGE = "任务不存在或已过期"

# Username/password session authentication.
_SESSION_COOKIE_NAME = "cms_web_session"
_SESSION_TTL_SECONDS = 7 * 24 * 3600
_LOGIN_PATH = "/login"
_LOGOUT_PATH = "/logout"
_LOG_STREAM_PATH = "/api/v1/logs/stream"
_LOG_ANALYZE_PATH = "/api/v1/logs/analyze"
_LOG_QUERY_KEYS = frozenset({"filter_type", "lines", "keyword", "logger", "token"})
_LOG_ANALYZE_QUERY_KEYS = frozenset({"lines", "since_seconds", "logger", "keyword", "level", "token"})


class _WebThreadingHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, log_hub: LogHub | None):
        self._cms_log_hub = log_hub
        self._cms_shutdown_event = Event()
        super().__init__(server_address, handler_class)

    def shutdown(self) -> None:
        self._cms_shutdown_event.set()
        close_streams = getattr(self._cms_log_hub, "close_streams", None)
        if callable(close_streams):
            close_streams()
        super().shutdown()


def _parse_request_target(path: str):
    parsed = urlparse(path)
    if parsed.scheme and (not parsed.netloc or not parsed.hostname):
        raise ValueError("absolute request target requires an authority")
    _ = parsed.port
    return parsed


class RequestBodyTooLarge(ValueError):
    pass


class RequestBodyDisconnected(Exception):
    pass


def encode_sse_event(event: str, payload: dict[str, object], event_id: int | None = None) -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n\n").encode("utf-8", "backslashreplace")


def parse_content_length(value: str | None, limit: int = MAX_REQUEST_BODY_BYTES) -> int:
    if value in (None, ""):
        return 0
    if not value.isascii() or not value.isdecimal():
        raise ValueError("invalid content length")
    length = int(value, 10)
    if length > limit:
        raise RequestBodyTooLarge("content length exceeds request body limit")
    return length


_NAV_ITEMS = (
    ("overview", "/", "运行概览"),
    ("quality", "/quality", "质量巡检"),
    ("health", "/health", "本地健康"),
    ("hdhive", "/hdhive", "HDHive 订阅"),
)

_TASK_PHASES = (
    ("115 云下载", {TaskStage.CLOUD_DOWNLOADING}),
    ("接收", {TaskStage.RECEIVED, TaskStage.CMS_SUBMITTED}),
    ("CMS 整理", {TaskStage.ORGANIZING, TaskStage.ORGANIZED}),
    ("分类识别", {TaskStage.RECOGNIZING}),
    ("建分享", {TaskStage.SHARE_ALIAS_PREPARED, TaskStage.OWN_SHARE_CREATED, TaskStage.SHARE_VALIDATED}),
    ("分享 STRM", {TaskStage.SHARE_SYNC_SUBMITTED, TaskStage.STRM_READY, TaskStage.CMS_DELETE_SETTLED}),
    ("移动入库", {TaskStage.MOVED}),
    ("Emby 确认", {TaskStage.EMBY_CONFIRMED}),
    ("清理完成", {TaskStage.CLEANED}),
)


def _background_job_status_markup(background_jobs: BackgroundJobCoordinator | None, prefix: str) -> str:
    if background_jobs is None:
        return ""
    snapshots = [snapshot for snapshot in background_jobs.list_snapshots() if snapshot.key.startswith(prefix)]
    if not snapshots:
        return ""
    snapshot = max(snapshots, key=lambda item: item.queued_at)
    values = [
        html.escape(snapshot.description or snapshot.key),
        html.escape(snapshot.status),
    ]
    if snapshot.started_at:
        values.append(html.escape(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.started_at))))
    if snapshot.finished_at:
        values.append(html.escape(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.finished_at))))
    if snapshot.error:
        values.append(html.escape(redact_background_text(snapshot.error)))
    return f'<p class="task-message">后台任务：{" · ".join(values)}</p>'

def _navigation(active: str) -> str:
    links = []
    for key, href, label in _NAV_ITEMS:
        current = ' aria-current="page"' if key == active else ""
        links.append(f'<a href="{href}"{current}>{html.escape(label)}</a>')
    return (
        '<header class="app-header"><div class="app-header-inner">'
        '<a class="app-brand" href="/">CMS 入库助手</a>'
        f'<nav class="app-nav" aria-label="主导航">{"".join(links)}</nav>'
        "</div></header>"
    )


def _event_stage(value: object) -> TaskStage | None:
    if isinstance(value, TaskStage):
        return value
    try:
        return TaskStage(str(value))
    except ValueError:
        return None


def _task_phase_index(task: Any, events: list[dict[str, Any]]) -> int | None:
    candidates = [task.current_stage]
    candidates.extend(
        stage
        for stage in (_event_stage(event.get("stage")) for event in reversed(events))
        if stage is not None
    )
    for stage in candidates:
        for index, (_label, stages) in enumerate(_TASK_PHASES):
            if stage in stages:
                return index
    return None


def _render_phase_track(task: Any, events: list[dict[str, Any]]) -> str:
    current = _task_phase_index(task, events)
    steps = []
    for index, (label, _stages) in enumerate(_TASK_PHASES):
        state = ""
        attributes = ['role="listitem"']
        if current is not None and index < current:
            state = " is-done"
            attributes.append(f'aria-label="{html.escape(label)}，已完成"')
        elif current is not None and index == current:
            attributes.append('aria-current="step"')
            state = " is-done" if task.status == TaskStatus.SUCCEEDED else " is-current"
            if task.status == TaskStatus.SUCCEEDED:
                attributes.append(f'aria-label="{html.escape(label)}，已完成"')
        steps.append(
            f'<div class="phase-step{state}" {" ".join(attributes)}><i></i><span>{html.escape(label)}</span></div>'
        )
    return f'<div class="phase-track" aria-label="任务处理进度" role="list">{"".join(steps)}</div>'


def _page(title: str, body: str, *, active: str = "") -> str:
    navigation = _navigation(active)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f4f5f6;
  --surface: #ffffff;
  --surface-muted: #f8f9fa;
  --border: #d7dadd;
  --border-soft: #e7e9eb;
  --border-hover: #aeb3b8;
  --text: #202124;
  --muted: #6a6f75;
  --muted-strong: #4f555b;
  --primary: #1f5f99;
  --primary-dark: #174b7a;
  --success-bg: #e8f4ec;
  --success-text: #24643b;
  --success-border: #b9d8c3;
  --warning-bg: #fff4d6;
  --warning-text: #805d10;
  --warning-border: #e1cf9d;
  --danger-bg: #fbe9e9;
  --danger-text: #9b2c2c;
  --danger-border: #e2b8b8;
  --info-bg: #e8f1f8;
  --info-text: #245b85;
  --code-bg: #eef2f7;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --bg: #10121a;
    --surface: #171a24;
    --surface-muted: #1d2130;
    --border: #343a4e;
    --border-soft: #262b3a;
    --border-hover: #4a5268;
    --text: #e6e8f0;
    --muted: #9aa3b5;
    --muted-strong: #b8c0d0;
    --primary: #8b93ff;
    --primary-dark: #a5abff;
    --success-bg: #143324;
    --success-text: #4bc47e;
    --success-border: #2d5a3e;
    --warning-bg: #33290f;
    --warning-text: #e0a93c;
    --warning-border: #6d5526;
    --danger-bg: #3a1a24;
    --danger-text: #f26d82;
    --danger-border: #7a3a4a;
    --info-bg: #16263a;
    --info-text: #6fa8f0;
    --code-bg: #1d2130;
  }}
}}
* {{ box-sizing: border-box; letter-spacing: 0; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  line-height: 1.5;
}}
a {{ color: var(--primary); text-decoration: none; }}
a:hover {{ color: var(--primary-dark); text-decoration: underline; }}
.app-header {{ background: var(--surface); border-bottom: 1px solid var(--border); }}
.app-header-inner {{ width: min(1180px, calc(100% - 32px)); min-height: 60px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 24px; }}
.app-brand {{ color: var(--text); font-size: 17px; font-weight: 700; white-space: nowrap; }}
.app-brand:hover {{ color: var(--text); text-decoration: none; }}
.app-nav {{ align-self: stretch; display: flex; align-items: stretch; gap: 20px; }}
.app-nav a {{ display: flex; align-items: center; border-bottom: 2px solid transparent; color: var(--muted-strong); font-size: 14px; }}
.app-nav a:hover {{ color: var(--text); text-decoration: none; }}
.app-nav a[aria-current="page"] {{ border-bottom-color: var(--primary); color: var(--primary); font-weight: 650; }}
.shell {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 40px; }}
.page-heading, .topbar {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }}
.topbar > div {{ min-width: 0; }}
.eyebrow {{ color: var(--muted); font-size: 13px; margin: 0 0 4px; }}
h1 {{ font-size: 28px; line-height: 1.2; margin: 0; }}
h2 {{ font-size: 18px; margin: 0; }}
p {{ margin: 0; }}
.subtle {{ color: var(--muted); }}
.status-strip {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 20px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }}
.status-summary, .status-facts {{ display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }}
.status-facts {{ color: var(--muted-strong); font-size: 13px; }}
.metrics-grid, .stats-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }}
.metric, .stat-card {{ min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 16px; }}
.stat-label {{ color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
.stat-value {{ font-size: 28px; line-height: 1; font-weight: 700; }}
.workspace-grid, .overview-grid {{ display: grid; grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.25fr); gap: 14px; align-items: start; }}
.workspace-grid > .panel {{ margin: 0; }}
.panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px; margin: 14px 0; }}
.panel-heading, .panel-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }}
.badge {{ display: inline-flex; align-items: center; border: 1px solid transparent; border-radius: 6px; padding: 3px 8px; font-size: 12px; font-weight: 650; white-space: nowrap; }}
.status-succeeded, .status-healthy {{ background: var(--success-bg); color: var(--success-text); }}
.status-running, .status-pending, .status-busy {{ background: var(--info-bg); color: var(--info-text); }}
.status-needs_action, .status-attention {{ background: var(--warning-bg); color: var(--warning-text); }}
.status-failed {{ background: var(--danger-bg); color: var(--danger-text); }}
.task-list {{ display: grid; gap: 10px; }}
.task-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 14px; border: 1px solid var(--border-soft); border-radius: 6px; background: var(--surface-muted); }}
.task-row > div {{ min-width: 0; }}
.task-title {{ font-weight: 650; margin-bottom: 4px; overflow-wrap: anywhere; }}
.task-meta {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 13px; }}
.task-message {{ margin-top: 6px; color: var(--muted-strong); font-size: 13px; overflow-wrap: anywhere; }}
.task-message.error {{ color: var(--danger-text); }}
.overflow-tasks {{ margin-top: 10px; border-top: 1px solid var(--border-soft); }}
.overflow-tasks > summary {{ padding: 12px 2px 2px; color: var(--primary); cursor: pointer; font-weight: 650; }}
.overflow-tasks > .task-list {{ margin-top: 10px; }}
.maintenance-panel {{ margin-top: 14px; }}
.maintenance-actions {{ display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }}
.phase-track {{ display: grid; grid-template-columns: repeat(8, minmax(72px, 1fr)); gap: 0; margin: 18px 0; overflow-x: auto; }}
.task-row > .phase-track {{ grid-column: 1 / -1; width: 100%; min-width: 0; margin-bottom: 0; }}
.phase-step {{ position: relative; min-width: 72px; padding: 0 6px; color: var(--muted); text-align: center; font-size: 12px; }}
.phase-step::before {{ content: ""; position: absolute; top: 7px; right: 50%; left: -50%; height: 2px; background: var(--border); }}
.phase-step:first-child::before {{ display: none; }}
.phase-step i {{ position: relative; z-index: 1; display: block; width: 16px; height: 16px; margin: 0 auto 7px; border: 2px solid var(--border); border-radius: 50%; background: var(--surface); }}
.phase-step.is-done {{ color: var(--success-text); }}
.phase-step.is-done::before, .phase-step.is-done i {{ border-color: var(--success-text); background: var(--success-text); }}
.phase-step.is-current {{ color: var(--info-text); font-weight: 650; }}
.phase-step.is-current::before {{ background: var(--success-text); }}
.phase-step.is-current i {{ border-color: var(--info-text); background: var(--info-bg); }}
.summary-grid, .detail-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.breadcrumb {{ margin-bottom: 6px; color: var(--muted); font-size: 13px; }}
.incident-strip {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }}
.incident-strip[data-status="failed"] {{ border-color: var(--danger-border); background: var(--danger-bg); }}
.incident-strip[data-status="needs_action"], .incident-strip[data-status="attention"] {{ border-color: var(--warning-border); background: var(--warning-bg); }}
.incident-strip.is-neutral {{ border-color: var(--border); background: var(--surface); }}
.incident-copy {{ min-width: 0; }}
.incident-strip > .actions {{ flex-shrink: 0; max-width: 50%; }}
.incident-summary {{ font-weight: 700; overflow-wrap: anywhere; }}
.incident-recommendation {{ margin-top: 4px; color: var(--muted-strong); font-size: 13px; overflow-wrap: anywhere; }}
.task-detail-title {{ max-width: 100%; overflow-wrap: anywhere; }}
.summary-item {{ min-width: 0; padding: 10px 0; border-bottom: 1px solid var(--border-soft); }}
.summary-label {{ color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
.summary-value {{ overflow-wrap: anywhere; }}
.timeline {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
.timeline li {{ padding: 12px; border: 1px solid var(--border-soft); border-radius: 6px; background: var(--surface-muted); }}
.timeline-time {{ color: var(--muted); }}
.older-events {{ margin-top: 12px; border-top: 1px solid var(--border-soft); }}
.older-events > summary {{ padding: 12px 2px 0; color: var(--primary); cursor: pointer; font-weight: 650; }}
.older-events > .timeline {{ margin-top: 12px; }}
.diagnostic-details {{ margin-top: 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }}
.diagnostic-details > summary {{ padding: 12px 14px; cursor: pointer; font-weight: 650; }}
.details-content {{ padding: 0 14px 14px; }}
.danger-zone {{ margin-top: 20px; border: 1px solid var(--danger-border); border-radius: 6px; background: var(--danger-bg); }}
.danger-zone > summary {{ padding: 12px 14px; color: var(--danger-text); cursor: pointer; font-weight: 650; }}
.danger-zone .details-content {{ color: var(--muted-strong); }}
.danger-zone .actions {{ margin-top: 12px; }}
.danger-zone + p {{ margin-top: 14px; }}
.quality-summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }}
.quality-list {{ display: grid; }}
.quality-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 14px 0; border-bottom: 1px solid var(--border-soft); }}
.quality-row:last-child {{ border-bottom: 0; }}
.quality-task {{ min-width: 0; }}
.quality-issue-counts {{ display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 6px; }}
.quality-count {{ display: inline-flex; gap: 6px; color: var(--muted-strong); font-size: 13px; }}
.quality-count strong {{ color: var(--text); font-variant-numeric: tabular-nums; }}
.quality-row-action {{ display: grid; justify-items: end; gap: 6px; }}
.quality-total {{ color: var(--muted); font-size: 13px; }}
.health-status {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px; border: 1px solid var(--border); border-radius: 6px; }}
.health-status.is-healthy, .empty-state.is-healthy {{ border-color: var(--success-border); background: var(--success-bg); color: var(--success-text); }}
.health-status.is-warning {{ border-color: var(--warning-border); background: var(--warning-bg); color: var(--warning-text); }}
.health-status p {{ margin-top: 3px; }}
.health-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }}
.health-item {{ min-width: 0; padding: 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }}
.health-value {{ margin-top: 4px; font-size: 24px; line-height: 1; font-weight: 700; font-variant-numeric: tabular-nums; }}
.health-notice {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border-soft); }}
.health-notice:last-child {{ border-bottom: 0; }}
.empty-state {{ padding: 24px; text-align: center; color: var(--muted); background: var(--surface-muted); border: 1px dashed var(--border); border-radius: 8px; }}
.actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.actions form {{ display: inline-block; margin: 0; }}
.button, button {{ display: inline-flex; align-items: center; justify-content: center; max-width: 100%; min-height: 36px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); font: inherit; font-weight: 650; white-space: normal; overflow-wrap: anywhere; text-align: center; cursor: pointer; }}
.button:hover, button:hover {{ border-color: var(--border-hover); text-decoration: none; }}
.button-primary {{ border-color: var(--primary); background: var(--primary); color: white; }}
.button-secondary {{ border-color: var(--border); background: var(--surface); color: var(--text); }}
.button-danger {{ border-color: var(--danger-border); background: var(--danger-bg); color: var(--danger-text); }}
:focus-visible {{ outline: 3px solid var(--primary-dark); outline-offset: 2px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
th, td {{ border-bottom: 1px solid var(--border-soft); padding: 11px 10px; text-align: left; vertical-align: top; }}
th {{ color: var(--muted); font-size: 12px; font-weight: 700; }}
code {{ background: var(--code-bg); padding: 2px 5px; border-radius: 6px; }}
.diagnostic {{ margin: 0; padding: 16px; border: 1px solid #30363d; border-radius: 6px; background: #202428; color: #f1f3f4; overflow: auto; font-size: 13px; line-height: 1.6; }}
.detail-item {{ background: var(--surface-muted); border: 1px solid var(--border-soft); border-radius: 6px; padding: 12px; }}
.detail-label {{ color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
.detail-value {{ overflow-wrap: anywhere; }}
@media (max-width: 760px) {{
  .app-header-inner {{ width: min(100% - 20px, 1180px); min-height: auto; padding-top: 12px; display: grid; gap: 8px; }}
  .app-nav {{ min-height: 42px; gap: 16px; overflow-x: auto; }}
  .app-nav a {{ white-space: nowrap; }}
  .shell {{ width: min(100% - 20px, 1180px); padding-top: 18px; }}
  .page-heading, .topbar {{ display: grid; }}
  .status-strip {{ align-items: flex-start; }}
  .metrics-grid, .stats-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .workspace-grid, .overview-grid, .health-grid {{ grid-template-columns: 1fr; }}
  .quality-summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .task-row, .health-notice {{ grid-template-columns: 1fr; }}
  .quality-row {{ grid-template-columns: 1fr; }}
  .quality-row-action {{ justify-items: start; }}
  .incident-strip {{ align-items: flex-start; flex-direction: column; }}
  .incident-strip > .actions {{ max-width: 100%; }}
  .summary-grid, .detail-grid {{ grid-template-columns: 1fr; }}
}}
@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{ animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; transition-duration: 0.01ms !important; }}
}}
</style>
</head>
<body>
{navigation}
<main class="shell">
{body}
</main>
<footer class="shell subtle">cms-tg-ingest {html.escape(__version__)}</footer>
</body>
</html>"""

def _task_lock_label(task: Any) -> str:
    if not task.metadata.get("_lock_waiting"):
        return "-"
    reason = str(task.metadata.get("_lock_reason") or "").strip()
    if not reason:
        return "-"
    owner = str(task.metadata.get("_lock_owner_task_id") or "").strip()
    return f"等待资源锁: #{owner} {reason}" if owner else f"等待资源锁: {reason}"


def parse_task_id_from_path(path: str) -> int | None:
    parts = str(path or "").split("/")
    if len(parts) != 3 or parts[0] or parts[1] != "task" or not parts[2]:
        return None
    try:
        return int(parts[2])
    except (TypeError, ValueError):
        return None


def parse_task_action_path(path: str) -> tuple[int, str] | None:
    parts = str(path or "").split("/")
    if len(parts) != 4 or parts[0] or parts[1] != "task" or parts[3] not in TASK_ACTIONS:
        return None
    try:
        return int(parts[2]), parts[3]
    except (TypeError, ValueError):
        return None


def _task_is_unscheduled_legacy(task: Any) -> bool:
    return task.current_stage != TaskStage.RECEIVED and is_unscheduled_active_task(task)


def _status_class(status: TaskStatus | str) -> str:
    value = status.value if isinstance(status, TaskStatus) else str(status)
    return "status-" + value.lower().replace(".", "_").replace("-", "_")


def _badge(label: str, class_name: str = "") -> str:
    classes = "badge" + (f" {class_name}" if class_name else "")
    return f'<span class="{classes}">{html.escape(label)}</span>'


def _task_wait_message(task: Any) -> str:
    lock_label = _task_lock_label(task)
    if lock_label != "-":
        return lock_label
    message = str(task.metadata.get("_defer_message") or "").strip()
    if message:
        count = task.metadata.get("_defer_count")
        suffix = f"（第 {count} 次）" if count else ""
        return message + suffix
    return ""


def _task_issue_message(task: Any) -> str:
    error = str(getattr(task, "error_summary", "") or "").strip()
    if error:
        return error
    return _task_wait_message(task)


def _task_observability_lines(task: Any, *, now: float | None = None) -> list[str]:
    return format_task_observability(task, now=time.time() if now is None else now)


def _task_counts(tasks: list[Any]) -> dict[str, int]:
    return {
        "active": sum(1 for task in tasks if is_dispatchable_active_task(task)),
        "problem": sum(1 for task in tasks if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or is_unscheduled_active_task(task)),
        "waiting": sum(1 for task in tasks if is_dispatchable_active_task(task) and _task_wait_message(task)),
        "completed": sum(1 for task in tasks if task.status == TaskStatus.SUCCEEDED),
    }


def _is_attention_task(task: Any) -> bool:
    return task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or is_unscheduled_active_task(task)


def _is_queue_task(task: Any) -> bool:
    return is_dispatchable_active_task(task) and not _is_attention_task(task)


def _overall_status(counts: dict[str, int]) -> tuple[str, str]:
    if counts["problem"]:
        return "需要关注", "status-attention"
    if counts["active"] or counts["waiting"]:
        return "正在处理", "status-busy"
    return "运行正常", "status-healthy"


def _render_task_row(task: Any, *, compact: bool = False, phase_html: str = "", now: float | None = None) -> str:
    title = task_display_title(task)
    stage = stage_display_name(task.current_stage)
    status_label = "需处理" if is_unscheduled_active_task(task) else task.status.value
    status_class = "status-attention" if is_unscheduled_active_task(task) else _status_class(task.status)
    message = _task_issue_message(task)
    message_class = " error" if task.status == TaskStatus.FAILED else ""
    message_html = f'<div class="task-message{message_class}">{html.escape(message)}</div>' if message else ""
    observability_html = "".join(
        f'<div class="task-message">{html.escape(line)}</div>'
        for line in _task_observability_lines(task, now=now)[:3]
    )
    detail_label = "查看详情" if compact else f"查看详情 #{task.id}"
    return (
        '<div class="task-row">'
        '<div>'
        f'<div class="task-title">{html.escape(title)}</div>'
        '<div class="task-meta">'
        f'<span>#{task.id}</span>'
        f'<span>{html.escape(stage)}</span>'
        f'{_badge(status_label, status_class)}'
        '</div>'
        f'{message_html}'
        f'{observability_html}'
        '</div>'
        f'<a class="button" href="/task/{task.id}">{detail_label}</a>'
        f'{phase_html}'
        '</div>'
    )


def render_task_list(store: TaskStore, *, task_engine_enabled: bool = True) -> str:
    tasks = store.list_recent_tasks(limit=100)
    now = time.time()
    attention_tasks = [task for task in tasks if _is_attention_task(task)]
    queue_tasks = [task for task in tasks if _is_queue_task(task)]
    counts = _task_counts(tasks)
    health = build_task_health(store, enabled=task_engine_enabled, limit=100)
    overall_label, overall_class = _overall_status(counts)

    attention_html = "".join(_render_task_row(task, compact=True, now=now) for task in attention_tasks[:8])
    overflow_tasks = attention_tasks[8:]
    if overflow_tasks:
        overflow_rows = "".join(_render_task_row(task, compact=True, now=now) for task in overflow_tasks)
        attention_html += (
            '<details class="overflow-tasks">'
            f'<summary>查看其余 {len(overflow_tasks)} 项</summary>'
            f'<div class="task-list">{overflow_rows}</div>'
            '</details>'
        )
    if not attention_html:
        attention_html = '<div class="empty-state">暂无需要处理的任务</div>'

    queue_rows = "".join(
        _render_task_row(
            task,
            phase_html=_render_phase_track(task, []),
            now=now,
        )
        for task in queue_tasks[:25]
    )
    if not queue_rows:
        queue_rows = '<div class="empty-state">当前没有活跃任务</div>'

    engine_label = "任务引擎正常" if health.enabled else "任务引擎已停用"
    cooldown_label = (
        f"115 风控冷却中，剩余 {_duration(health.p115_cooldown_until - now)}"
        if health.p115_cooldown_until > now
        else "115 未冷却"
    )
    updated_label = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

    body = f"""
<div class="page-heading">
  <div>
    <p class="eyebrow">Telegram 115 自分享 STRM 工作流</p>
    <h1>运行概览</h1>
  </div>
  <p class="subtle">本地更新：{updated_label}</p>
</div>

<section class="status-strip" aria-label="运行状态">
  <div class="status-summary">
    {_badge(overall_label, overall_class)}
    <span>{len(queue_tasks)} 个活跃任务，{len(attention_tasks)} 个需关注</span>
  </div>
  <div class="status-facts"><span>{engine_label}</span><span>{cooldown_label}</span></div>
</section>

<section class="metrics-grid" aria-label="任务概览">
  <div class="metric"><div class="stat-label">运行中</div><div class="stat-value">{counts['active']}</div></div>
  <div class="metric"><div class="stat-label">需处理/失败</div><div class="stat-value">{counts['problem']}</div></div>
  <div class="metric"><div class="stat-label">等待资源</div><div class="stat-value">{counts['waiting']}</div></div>
  <div class="metric"><div class="stat-label">已完成历史</div><div class="stat-value">{counts['completed']}</div></div>
</section>

<div class="workspace-grid">
  <section class="panel" data-section="attention">
    <div class="panel-header">
      <div><h2>需要关注</h2><p class="subtle">失败、需人工处理或不在自动调度队列的任务。</p></div>
    </div>
    <div class="task-list">{attention_html}</div>
  </section>

  <section class="panel" data-section="queue">
    <div class="panel-header">
      <div><h2>当前队列</h2><p class="subtle">可调度的待处理和运行中任务，最多显示 25 项。</p></div>
    </div>
    <div class="task-list">{queue_rows}</div>
  </section>
</div>

<section class="panel maintenance-panel" data-section="maintenance">
  <div class="panel-header"><div><h2>本地维护</h2><p class="subtle">页面操作只读取或清理本地任务记录。</p></div></div>
  <div class="maintenance-actions">
    <a href="/">重新载入页面</a>
    <form method="post" action="/history/clear" onsubmit="return confirm('只清除已结束任务记录，不删除文件。确定继续？')">
      <button class="button-danger" type="submit">清理已结束记录</button>
    </form>
  </div>
</section>
"""
    return _page("运行概览", body, active="overview")

def render_task_detail(
    store: TaskStore,
    task_id: int,
    submission_store: Any | None = None,
    max_retries: int = 3,
) -> str:
    task = store.find_task(task_id)
    if not task and submission_store is not None and hasattr(submission_store, "find_by_id"):
        row = submission_store.find_by_id(task_id)
        if row:
            task = sync_task_from_submission(store, row, message="打开详情页时懒回填旧记录")
    if not task:
        return _page("任务不存在", '<section class="empty-state"><h1>任务不存在</h1></section>')

    events = store.list_events(task.id)

    def render_event(event: dict[str, Any]) -> str:
        created_at = float(event.get("created_at") or 0)
        time_label = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at)) if created_at else ""
        time_html = f'<span class="timeline-time">{html.escape(time_label)}</span>' if time_label else ""
        return (
            "<li>"
            f'<div class="task-meta"><code>{html.escape(str(event.get("stage") or ""))}</code>'
            f'{_badge(str(event.get("status") or ""), "")}{time_html}</div>'
            f'<div class="task-message">{html.escape(str(event.get("message") or ""))}</div>'
            "</li>"
        )

    display_events = list(reversed(events))
    recent_event_items = "".join(render_event(event) for event in display_events[:8])
    recent_events = (
        f'<ul class="timeline recent-timeline">{recent_event_items}</ul>'
        if recent_event_items
        else '<div class="empty-state">暂无处理事件</div>'
    )
    older_event_items = "".join(render_event(event) for event in display_events[8:])
    older_events = ""
    if older_event_items:
        older_events = (
            '<details class="older-events"><summary>查看更早事件</summary>'
            f'<ul class="timeline">{older_event_items}</ul></details>'
        )
    decision = decide_retry(task, max_retries=max_retries)
    actions = available_task_actions(task, max_retries=max_retries)
    retry_eligible = "retry" in actions
    downstream_actions_eligible = "emby" in actions or "restore" in actions
    reprocess_eligible = "reprocess" in actions
    retry_form = ""
    if retry_eligible:
        retry_form = f'<form method="post" action="/task/{task.id}/retry"><button class="button button-primary" type="submit">重试当前阶段</button></form>'
    secondary_actions = ""
    if downstream_actions_eligible:
        emby_form = f'<form method="post" action="/task/{task.id}/emby"><button class="button button-secondary" type="submit">查 Emby</button></form>'
        restore_form = f'<form method="post" action="/task/{task.id}/restore"><button class="button button-secondary" type="submit">恢复 STRM</button></form>'
        secondary_actions = f"""
<section class="panel">
  <div class="panel-header"><h2>其他操作</h2></div>
  <div class="actions">{emby_form}{restore_form}</div>
</section>
"""
    danger_zone = ""
    if reprocess_eligible:
        reprocess_form = (
            f'<form method="post" action="/task/{task.id}/reprocess" '
            "onsubmit=\"return confirm('将从接收阶段重新执行该任务。确定继续？')\">"
            '<button class="button button-danger" type="submit">从头重跑</button></form>'
        )
        danger_zone = f"""
<details class="danger-zone">
  <summary>高风险操作</summary>
  <div class="details-content">
    <p>从头重跑可能再次调用 115/CMS，并重新执行整个入库流程。</p>
    <div class="actions">{reprocess_form}</div>
  </div>
</details>
"""
    media_library = str(task.metadata.get("emby_parent") or task.metadata.get("emby_refresh_library") or "-")
    dest_path = str(task.metadata.get("dest_path") or task.metadata.get("emby_path") or "-")
    error_summary = str(task.error_summary or "").strip()
    wait_label = _task_wait_message(task)
    unscheduled = _task_is_unscheduled_legacy(task)
    normal_active = task.status in {TaskStatus.PENDING, TaskStatus.RUNNING} and not error_summary and not wait_label and not unscheduled
    if error_summary:
        incident_summary = error_summary
        recommendation = decision.reason if retry_eligible or task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION, TaskStatus.SUCCEEDED} else "请关注当前任务状态"
        incident_tone = "failed" if task.status == TaskStatus.FAILED else "attention"
    elif wait_label:
        incident_summary = wait_label
        recommendation = "任务正在按计划处理" if not unscheduled else "可从头重跑该遗留任务"
        incident_tone = "attention"
    elif unscheduled:
        incident_summary = "任务不在自动调度队列"
        recommendation = "可从头重跑该遗留任务"
        incident_tone = "attention"
    elif normal_active:
        incident_summary = "等待任务引擎执行" if task.status == TaskStatus.PENDING else "任务正在按计划处理"
        recommendation = "任务正在按计划处理" if task.status == TaskStatus.PENDING else "当前无需手动操作"
        incident_tone = "neutral"
    elif task.status == TaskStatus.FAILED:
        incident_summary = "任务执行失败"
        recommendation = decision.reason
        incident_tone = "failed"
    elif task.status == TaskStatus.NEEDS_ACTION:
        incident_summary = "任务需要人工处理"
        recommendation = decision.reason
        incident_tone = "attention"
    else:
        incident_summary = "任务已完成"
        recommendation = "可按需检查 Emby 或恢复 STRM" if downstream_actions_eligible else "当前无需手动操作"
        incident_tone = "neutral"
    observability = _task_observability_lines(task)
    slow_label = next((line.split("：", 1)[1] for line in observability if line.startswith("为什么慢：")), "-")
    if normal_active:
        slow_label = "等待任务引擎执行" if task.status == TaskStatus.PENDING else "任务正在按计划处理"
    timing_label = next((line.split("：", 1)[1] for line in observability if line.startswith("耗时：")), "-")
    p115_label = next((line.split("：", 1)[1] for line in observability if line.startswith("115调用：")), "-")
    stage_elapsed_summary, stage_p115_summary = format_stage_observability(task)
    stage_elapsed_summary = stage_elapsed_summary or "-"
    stage_p115_summary = stage_p115_summary or "-"
    incident_classes = "incident-strip is-neutral" if incident_tone == "neutral" else "incident-strip"
    body = f"""
<div class="topbar">
  <div>
    <p class="breadcrumb"><a href="/">运行概览</a> / 任务 #{task.id}</p>
    <h1 class="task-detail-title">{html.escape(task_display_title(task))}</h1>
  </div>
  {_badge("需处理" if unscheduled else task.status.value, "status-attention" if unscheduled else _status_class(task.status))}
</div>

<div class="{incident_classes}" data-status="{html.escape(incident_tone)}">
  <div class="incident-copy">
    <p class="incident-summary">{html.escape(incident_summary)}</p>
    <p class="incident-recommendation">{html.escape(recommendation)}</p>
  </div>
  <div class="actions">{retry_form}</div>
</div>

{_render_phase_track(task, events)}

<section class="panel">
  <div class="panel-header"><h2>任务摘要</h2></div>
  <div class="summary-grid">
    <div class="summary-item"><div class="summary-label">当前阶段</div><div class="summary-value">{html.escape(stage_display_name(task.current_stage))}</div></div>
    <div class="summary-item"><div class="summary-label">目标媒体库</div><div class="summary-value">{html.escape(media_library)}</div></div>
    <div class="summary-item"><div class="summary-label">为什么慢</div><div class="summary-value">{html.escape(slow_label)}</div></div>
    <div class="summary-item"><div class="summary-label">执行耗时</div><div class="summary-value">{html.escape(timing_label)}</div></div>
    <div class="summary-item"><div class="summary-label">115 调用</div><div class="summary-value">{html.escape(p115_label)}</div></div>
    <div class="summary-item"><div class="summary-label">推荐操作</div><div class="summary-value">{html.escape(recommendation)}</div></div>
  </div>
  <details class="diagnostic-details">
    <summary>技术详情与文件路径</summary>
    <div class="details-content detail-grid">
      <div class="detail-item"><div class="detail-label">目标文件路径</div><div class="detail-value">{html.escape(dest_path)}</div></div>
      <div class="detail-item"><div class="detail-label">错误摘要</div><div class="detail-value">{html.escape(error_summary or "-")}</div></div>
      <div class="detail-item"><div class="detail-label">各阶段耗时</div><div class="detail-value">{html.escape(stage_elapsed_summary)}</div></div>
      <div class="detail-item"><div class="detail-label">各阶段 115 调用</div><div class="detail-value">{html.escape(stage_p115_summary)}</div></div>
    </div>
  </details>
</section>

{secondary_actions}

<section class="panel">
  <div class="panel-header"><h2>处理时间线</h2></div>
  {recent_events}
  {older_events}
</section>

{danger_zone}

<p><a class="button" href="/">返回运行概览</a></p>
"""
    return _page("任务详情", body)

def _group_quality_issues(issues: list[QualityIssue]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for issue in issues:
        row = grouped.setdefault(
            issue.task_id,
            {"task_id": issue.task_id, "title": issue.title, "codes": {}, "total": 0},
        )
        row["title"] = row["title"] or issue.title
        row["codes"][issue.code] = row["codes"].get(issue.code, 0) + 1
        row["total"] += 1
    return list(grouped.values())


def _quality_repair_action(issue: QualityIssue, task: Any | None, max_retries: int = 3) -> str | None:
    if task is None:
        return None
    actions = available_task_actions(task, max_retries=max_retries)
    if issue.code in {"missing_dest", "missing_strm"} and "restore" in actions:
        return "restore"
    if issue.code in {"direct_strm", "unexpected_strm"} and "reprocess" in actions:
        return "reprocess"
    return None


def render_quality_page(
    store: TaskStore,
    quality_automation: QualityAutomation | None = None,
    max_retries: int = 3,
    background_jobs: BackgroundJobCoordinator | None = None,
) -> str:
    allowed_roots = quality_automation.allowed_roots if quality_automation is not None else ()
    share_identity_resolver = getattr(quality_automation, "share_identity_resolver", None)
    scan_kwargs = {"allowed_roots": allowed_roots}
    if callable(share_identity_resolver):
        scan_kwargs["share_identity_resolver"] = share_identity_resolver
    issues = scan_task_quality(store, **scan_kwargs)
    report = format_task_quality_report(issues)
    quality_rows = quality_items(store, quality_automation=quality_automation, issues=issues)
    grouped: dict[int, dict[str, Any]] = {}
    for item in quality_rows:
        task_id = int(item["task_id"])
        row = grouped.setdefault(
            task_id,
            {
                "task_id": task_id,
                "title": item.get("title") or "",
                "codes": {},
                "total": 0,
                "items": [],
            },
        )
        row["title"] = row["title"] or item.get("title") or ""
        row["codes"][item["code"]] = row["codes"].get(item["code"], 0) + 1
        row["total"] += 1
        row["items"].append(item)
    grouped = list(grouped.values())
    tasks: dict[int, Any | None] = {}
    actionable_task_ids: set[int] = set()
    for row in grouped:
        task_id = int(row["task_id"])
        tasks[task_id] = store.find_task(task_id)
        if any(item.get("auto_allowed") for item in row["items"]):
            actionable_task_ids.add(task_id)
        elif quality_automation is None:
            descriptor_for_row = row["items"][0] if row["items"] else None
            if (
                descriptor_for_row
                and descriptor_for_row.get("rule_id") in {"strm_mode_mismatch", "unexpected_strm"}
                and tasks[task_id] is not None
                and "reprocess" in available_task_actions(tasks[task_id], max_retries)
            ):
                actionable_task_ids.add(task_id)
    category_counts = {
        "目标目录缺失": sum(issue.code == "missing_dest" for issue in issues),
        "STRM 缺失": sum(issue.code == "missing_strm" for issue in issues),
        "直链 STRM": sum(issue.code == "direct_strm" for issue in issues),
        "异常分享": sum(issue.code == "unexpected_strm" for issue in issues),
    }
    summary_values = (
        ("问题总数", len(issues)),
        ("受影响任务", len(grouped)),
        *category_counts.items(),
    )
    summary_markup = "".join(
        f'<div class="stat-card"><div class="stat-label">{label}</div><div class="stat-value">{count}</div></div>'
        for label, count in summary_values
    )
    rows = []
    for row in grouped:
        codes = row["codes"]
        type_counts = (
            ("目标目录缺失", codes.get("missing_dest", 0)),
            ("STRM 缺失", codes.get("missing_strm", 0)),
            ("直链 STRM", codes.get("direct_strm", 0)),
            ("异常分享", codes.get("unexpected_strm", 0)),
        )
        counts_markup = "".join(
            f'<span class="quality-count"><span>{label}</span><strong>{count}</strong></span>'
            for label, count in type_counts
            if count
        )
        task_id = int(row["task_id"])
        title = html.escape(str(row["title"] or f"任务 #{task_id}"))
        task_link = f'<a href="/task/{task_id}">#{task_id} {title}</a>' if tasks.get(task_id) is not None else f'<span>#{task_id} {title}</span>'
        descriptor = row["items"][0] if row["items"] else {}
        evidence = "；".join(str(value) for value in descriptor.get("evidence", []) if value) or "-"
        next_time = descriptor.get("next_eligible_at") or 0
        next_label = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(next_time))) if next_time else "-"
        actions = descriptor.get("available_actions", [])
        action_forms = []
        for action_name, label, css, confirm in (
            ("execute", "执行重跑", "button-primary", "将该任务从头重跑并入队，确定继续？"),
            ("reprocess", "人工重跑", "button-primary", "将该任务从头重跑并入队，确定继续？"),
            ("snooze", "暂缓 24 小时", "button-secondary", "暂缓该质量问题 24 小时，确定继续？"),
            ("ignore", "忽略", "button-danger", "忽略该质量问题，确定继续？"),
            ("resume", "恢复评估", "button-secondary", "恢复该质量问题的规则评估，确定继续？"),
        ):
            if action_name not in actions:
                continue
            until = f'<input type="hidden" name="until" value="{time.time() + 24 * 60 * 60}">' if action_name == "snooze" else ""
            action_forms.append(
                f'<form method="post" action="/quality/action/{action_name}" onsubmit="return confirm(\'{html.escape(confirm, quote=True)}\')">'
                f'<input type="hidden" name="task_id" value="{task_id}">'
                f'<input type="hidden" name="rule_id" value="{html.escape(str(descriptor.get("rule_id") or ""), quote=True)}">'
                f'<input type="hidden" name="rule_version" value="{html.escape(str(descriptor.get("rule_version") or "1"), quote=True)}">'
                f'<input type="hidden" name="action" value="{action_name}">{until}'
                f'<button class="{css}" type="submit">{label}</button></form>'
            )
        actions_markup = "".join(action_forms)
        if descriptor.get("auto_allowed"):
            status_label = "auto eligible"
        elif str(descriptor.get("manual_status") or "open") == "open":
            status_label = "manual required"
        else:
            status_label = str(descriptor.get("manual_status") or "manual required").replace("_", " ")
        rows.append(
            f"""<article class="quality-row" data-quality-group="{html.escape(status_label.replace(' ', '-'), quote=True)}">
  <div class="quality-task">
    <div class="subtle">{html.escape(status_label)}</div>
  <div class="task-title">{task_link}</div>
    <div class="quality-issue-counts">{counts_markup}</div>
    <div class="quality-issue-counts"><span class="quality-count"><span>规则</span><strong>{html.escape(str(descriptor.get("rule_id") or "manual_required"))}</strong></span><span class="quality-count"><span>风险</span><strong>{html.escape(str(descriptor.get("risk_level") or "-"))}</strong></span><span class="quality-count"><span>状态</span><strong>{html.escape(status_label)}</strong></span><span class="quality-count"><span>尝试次数</span><strong>{html.escape(str(descriptor.get("attempts", 0)))}</strong></span><span class="quality-count"><span>下次时间</span><strong>{html.escape(next_label)}</strong></span><span class="quality-count"><span>证据</span><strong>{html.escape(evidence)}</strong></span></div>
  </div>
  <div class="quality-row-action"><span class="quality-total">共 {row['total']} 条</span>{f'<a class="button" href="/task/{task_id}">查看任务</a>' if tasks.get(task_id) is not None else '<span class="subtle">任务已不存在</span>'}<div class="actions">{actions_markup}</div></div>
</article>"""
        )
    results_markup = (
        f'<div class="quality-list">{"".join(rows)}</div>'
        if rows
        else '<div class="empty-state is-healthy"><strong>未发现本地 STRM 问题</strong><p>当前本地文件巡检结果健康。</p></div>'
    )
    fix_action = ""
    if actionable_task_ids:
        fix_action = f"""<form method="post" action="/quality/fix" onsubmit="return confirm('将按当前允许的质量动作入队重跑，不会恢复缺失目录。确定继续？')">
        <button class="button-primary" type="submit">修复 {len(actionable_task_ids)} 个可处理任务</button>
      </form>"""
    automation_markup = ""
    if quality_automation is not None:
        snapshot = quality_automation.status_snapshot()
        summary = snapshot.get("last_summary") if isinstance(snapshot.get("last_summary"), dict) else {}
        status_label = str(snapshot.get("status") or "idle")
        current_run = str(snapshot.get("current_run_id") or "")
        run_button = "" if status_label == "running" else '<form method="post" action="/quality/run"><button class="button-primary" type="submit">立即巡检</button></form>'
        automation_markup = f"""
<section class="panel" data-section="quality-automation">
  <div class="panel-header"><div><h2>自动巡检</h2><p class="subtle">每天按本地时间运行一次；正常修复不发送 Telegram。</p></div><span class="badge status-{html.escape(status_label)}">{html.escape(status_label)}</span></div>
  <div class="summary-grid">
    <div class="summary-item"><div class="summary-label">自动运行</div><div class="summary-value">{'已启用' if snapshot.get('enabled') else '已停用'}</div></div>
    <div class="summary-item"><div class="summary-label">执行时间</div><div class="summary-value">{html.escape(str(snapshot.get('time') or '-'))} · {html.escape(str(snapshot.get('timezone') or '-'))}</div></div>
    <div class="summary-item"><div class="summary-label">下次运行</div><div class="summary-value">{html.escape(str(snapshot.get('next_run_at') or '-'))}</div></div>
    <div class="summary-item"><div class="summary-label">最近结果</div><div class="summary-value">扫描 {html.escape(str(summary.get('scanned_count', 0)))}，问题 {html.escape(str(summary.get('issue_count', 0)))}，排队 {html.escape(str(summary.get('queued_count', 0)))}，失败 {html.escape(str(summary.get('failed_count', 0)))}</div></div>
  </div>
  <form method="post" action="/quality/settings" class="actions">
    <label>启用 <input type="checkbox" name="enabled" value="true" {'checked' if snapshot.get('enabled') else ''}></label>
    <label>时间 <input name="time" value="{html.escape(str(snapshot.get('time') or '02:50'))}" size="5"></label>
    <label>时区 <input name="timezone" value="{html.escape(str(snapshot.get('timezone') or 'Asia/Shanghai'))}" size="18"></label>
    <label>任务上限 <input name="max_tasks" value="{html.escape(str(snapshot.get('max_tasks') or 50))}" type="number" min="1" max="1000"></label>
    <label>115检查上限 <input name="check_limit" value="{html.escape(str(snapshot.get('check_limit') or 3))}" type="number" min="1" max="100"></label>
    <button class="button-primary" type="submit">保存设置</button>
  </form>
  <div class="actions">{run_button}<form method="post" action="/quality/settings/reset"><button class="button-secondary" type="submit">恢复环境默认</button></form></div>
  {f'<p class="subtle">当前运行：{html.escape(current_run)}</p>' if current_run else ''}
  {_background_job_status_markup(background_jobs, "quality:run")}
</section>
"""
    body = f"""
<div class="topbar">
  <div>
    <p class="eyebrow">本地质量巡检</p>
    <h1>TaskStore 本地轻量巡检</h1>
    <p class="subtle">只读取本地 TaskStore 和 STRM 文件路径，不会扫描 115。</p>
  </div>
  <div class="actions"><a class="button" href="/quality">重新巡检</a><a class="button" href="/">返回运行概览</a></div>
</div>
<div class="quality-summary" role="group" aria-label="巡检摘要">{summary_markup}</div>
{automation_markup}
<section class="panel">
  <div class="panel-header">
    <div><h2>巡检结果</h2><p class="subtle">发现缺失目录或直链 STRM 时，可以入队执行安全修复。</p></div>
    <div class="actions">{fix_action}</div>
  </div>
  {results_markup}
</section>
<details class="diagnostic-details">
  <summary>查看完整原始报告（{len(issues)} 条）</summary>
  <div class="details-content"><pre class="diagnostic">{html.escape(report)}</pre></div>
</details>
"""
    return _page("质量巡检", body, active="quality")

def fix_quality_issues(store: TaskStore, quality_automation: QualityAutomation | None = None) -> int:
    if quality_automation is None:
        return 0
    fixed_task_ids: set[int] = set()
    share_identity_resolver = getattr(quality_automation, "share_identity_resolver", None)
    scan_kwargs = {"allowed_roots": quality_automation.allowed_roots}
    if callable(share_identity_resolver):
        scan_kwargs["share_identity_resolver"] = share_identity_resolver
    issues = scan_task_quality(store, **scan_kwargs)
    for item in quality_items(store, quality_automation=quality_automation, issues=issues):
        task_id = int(item["task_id"])
        if task_id in fixed_task_ids:
            continue
        if not item.get("auto_allowed") or "execute" not in item.get("available_actions", []):
            continue
        result = quality_automation.manual_action(
            task_id,
            str(item.get("rule_id") or ""),
            "execute",
            "legacy-web",
            rule_version=str(item.get("rule_version") or "1"),
        )
        if result.get("status") == "queued":
            fixed_task_ids.add(task_id)
    return len(fixed_task_ids)


def _quality_task_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        task_id = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return task_id if 1 <= task_id <= QualityAutomation.MAX_TASK_ID else None


def _quality_action_result(result: dict[str, object]) -> dict[str, object]:
    payload = dict(result)
    task = payload.get("task")
    if task is not None:
        payload["task"] = {
            "id": task.id,
            "title": task.title,
            "stage": task.current_stage.value,
            "status": task.status.value,
            "updated_at": task.updated_at,
        }
    return payload


def _run_quality_action(
    quality_automation: QualityAutomation | None,
    values: dict[str, object],
    route_action: str,
) -> tuple[int, dict[str, object]]:
    if quality_automation is None:
        return 409, {"error": "quality_unavailable"}
    task_id = _quality_task_id(values.get("task_id"))
    if task_id is None:
        return 400, {"error": "invalid_task_id"}
    rule_id = str(values.get("rule_id") or "").strip()
    rule_version = str(values.get("rule_version") or values.get("version") or "").strip()
    action = str(values.get("action") or route_action).strip().lower()
    if not rule_id:
        return 400, {"error": "quality_rule_required"}
    if not rule_version:
        return 409, {"error": "quality_rule_version_required"}
    if route_action == "execute":
        if action not in {"execute", "reprocess"}:
            return 409, {"error": "quality_action_not_allowed"}
    elif action != route_action:
        return 409, {"error": "quality_action_not_allowed"}
    result = quality_automation.manual_action(
        task_id,
        rule_id,
        action,
        str(values.get("actor") or "web"),
        values.get("until"),
        rule_version=rule_version,
    )
    status = str(result.get("status") or "")
    if status == "not_found":
        return 404, {"error": "quality_task_not_found", "reason": result.get("reason", "")}
    if status == "invalid":
        return 400, {"error": "invalid_quality_action", "reason": result.get("reason", "")}
    if status == "conflict":
        return 409, {"error": "quality_action_conflict", "reason": result.get("reason", "")}
    if status == "rejected":
        reason = str(result.get("reason") or "quality_action_rejected")
        error = {
            "rule_mismatch": "quality_rule_mismatch",
            "rule_version_changed": "quality_rule_version_changed",
            "action_not_allowed": "quality_action_not_allowed",
            "invalid_until": "invalid_until",
        }.get(reason, "quality_action_rejected")
        return 409, {"error": error, "reason": reason}
    return 200, _quality_action_result(result)


def _render_health_notice(label: str, task: Any, detail: str) -> str:
    title = task.title or task.metadata.get("received_title") or task.share_code or f"任务 #{task.id}"
    return f"""<article class="health-notice">
  <div><div class="summary-label">{html.escape(label)}</div><div class="task-title">#{task.id} {html.escape(str(title))}</div><p class="task-message">{html.escape(detail)}</p></div>
  <a class="button" href="/task/{task.id}">查看任务</a>
</article>"""


def render_health_page(store: TaskStore, *, task_engine_enabled: bool = True) -> str:
    recent_limit = 100
    now = time.time()
    summary = build_task_health(store, enabled=task_engine_enabled, limit=recent_limit, now=now)
    report = format_task_health(summary, now=now)
    cooldown_active = summary.p115_cooldown_until > now
    runner_error = summary.runner_state == "error"
    warning = not summary.enabled or runner_error or cooldown_active or summary.problem_count > 0 or summary.runner_heartbeat_stale
    health_class = "is-warning" if warning else "is-healthy"
    if not summary.enabled:
        health_title = "任务引擎已停用"
    elif runner_error:
        health_title = "任务引擎状态异常"
    elif summary.runner_heartbeat_stale:
        health_title = "任务引擎心跳异常"
    else:
        health_title = "任务引擎运行正常"
    recent_count_label = f"{summary.recent_count}+" if summary.recent_count >= recent_limit else str(summary.recent_count)
    cooldown_text = (
        f"115 风控冷却中，剩余 {_duration(summary.p115_cooldown_until - now)}"
        if cooldown_active
        else "115 未处于风控冷却"
    )
    health_values = (
        ("待执行", summary.pending_count),
        ("运行中", summary.running_count),
        ("需人工", summary.needs_action_count),
        ("锁等待", summary.lock_wait_count),
    )
    health_grid = "".join(
        f'<div class="health-item"><div class="summary-label">{label}</div><div class="health-value">{count}</div></div>'
        for label, count in health_values
    )
    notices = []
    if summary.latest_problem:
        problem = summary.latest_problem
        detail = (
            "不在自动调度队列，需要人工恢复"
            if is_unscheduled_active_task(problem)
            else stage_display_name(problem.current_stage)
        )
        if problem.error_summary:
            detail = f"{detail}，{problem.error_summary}"
        notices.append(_render_health_notice("最近问题", problem, detail))
    if summary.latest_lock_wait:
        waiting = summary.latest_lock_wait
        reason = str(waiting.metadata.get("_lock_reason") or "等待资源锁")
        holder = str(waiting.metadata.get("_lock_owner_task_id") or "-")
        notices.append(_render_health_notice("最近锁等待", waiting, f"{reason}，占用任务 #{holder}"))
    attention_panel = ""
    if notices:
        attention_panel = f"""<section class="panel">
  <div class="panel-header"><h2>需要关注</h2></div>
  <div>{''.join(notices)}</div>
</section>"""
    body = f"""
<div class="topbar">
  <div>
    <p class="eyebrow">本地队列健康</p>
    <h1>TaskStore 本地健康</h1>
    <p class="subtle">只展示本地 TaskStore 状态，不会向 115、CMS 或 Emby 发起请求。</p>
  </div>
  <a class="button" href="/">返回运行概览</a>
</div>
<section class="health-status {health_class}">
  <div><strong>{health_title}</strong><p>{cooldown_text}</p></div>
  <span>最近任务 {recent_count_label} 个</span>
</section>
<div class="health-grid" role="group" aria-label="本地任务状态">{health_grid}</div>
{attention_panel}
<details class="diagnostic-details">
  <summary>查看完整健康报告</summary>
  <div class="details-content"><pre class="diagnostic">{html.escape(report)}</pre></div>
</details>
"""
    return _page("本地健康", body, active="health")


def _hdhive_time_label(value: float) -> str:
    if not value:
        return "从未检查"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _hdhive_account_markup(service: Any | None) -> str:
    if service is None:
        return '<div class="empty-state">HDHive 功能未启用。</div>'
    account_getter = getattr(getattr(service, "proxy", None), "account", None)
    if not callable(account_getter):
        return '<div class="empty-state">未配置 HDHive 授权。</div>'
    try:
        account = account_getter()
    except Exception as exc:
        return f'<div class="health-status is-warning"><div><strong>HDHive 授权不可用</strong><p>{html.escape(str(exc)[:180])}</p></div></div>'
    quota = "无限制" if getattr(account, "weekly_free_quota_unlimited", False) else str(getattr(account, "weekly_free_quota_remaining", 0))
    values = (
        ("账号", getattr(account, "nickname", "-")),
        ("等级", getattr(account, "level", "-")),
        ("积分", getattr(account, "points", "-")),
        ("本周免费次数", quota),
    )
    return '<div class="status-strip"><div class="status-summary"><span class="badge status-succeeded">已授权</span><span>HDHive 账号可用</span></div></div><div class="summary-grid">' + "".join(
        f'<div class="summary-item"><div class="summary-label">{html.escape(label)}</div><div class="summary-value">{html.escape(str(value))}</div></div>'
        for label, value in values
    ) + "</div>"


def render_hdhive_page(
    service: Any | None = None,
    scheduler: Any | None = None,
    background_jobs: BackgroundJobCoordinator | None = None,
) -> str:
    subscriptions = []
    if service is not None:
        try:
            subscriptions = service.list()
        except Exception as exc:
            subscriptions = []
            subscription_error = f'<div class="health-status is-warning"><div><strong>订阅读取失败</strong><p>{html.escape(str(exc)[:180])}</p></div></div>'
        else:
            subscription_error = ""
    else:
        subscription_error = ""

    snapshot = scheduler.status_snapshot() if scheduler is not None else {}
    schedule_markup = (
        f'<div class="summary-grid">'
        f'<div class="summary-item"><div class="summary-label">自动检查</div><div class="summary-value">{"已启用" if snapshot.get("enabled") else "已停用"}</div></div>'
        f'<div class="summary-item"><div class="summary-label">执行时间</div><div class="summary-value">{html.escape(str(snapshot.get("time") or "-"))} · {html.escape(str(snapshot.get("timezone") or "-"))}</div></div>'
        f'<div class="summary-item"><div class="summary-label">下次检查</div><div class="summary-value">{html.escape(str(snapshot.get("next_run_at") or "-"))}</div></div>'
        f'<div class="summary-item"><div class="summary-label">最近状态</div><div class="summary-value">{html.escape(str(snapshot.get("status") or "idle"))}</div></div>'
        f'</div>'
        if scheduler is not None
        else '<div class="empty-state">未配置自动检查。</div>'
    )

    rows = []
    pending_rows = []
    unlocked_rows = []
    for subscription in subscriptions:
        title = str(subscription.title or subscription.tmdb_id or subscription.source_value)
        status_label = {"active": "运行中", "paused": "已暂停", "error": "异常", "completed": "已完结"}.get(subscription.status, subscription.status)
        status_class = {"active": "status-succeeded", "paused": "status-pending", "error": "status-failed", "completed": "status-succeeded"}.get(subscription.status, "status-pending")
        source = _safe_url(subscription.source_url) or f"TMDB:{subscription.tmdb_id}"
        items = service.store.list_items(subscription.id) if service is not None else []
        item_counts = {
            "discovered": len(items),
            "enqueued": sum(item.status == "enqueued" for item in items),
            "pending_confirmation": sum(item.status == "pending_confirmation" for item in items),
            "failed": sum(item.status == "failed" for item in items),
        }
        actions = []
        if subscription.status == "active":
            actions.append(f'<form method="post" action="/hdhive/subscriptions/{subscription.id}/pause"><button class="button-secondary" type="submit">暂停</button></form>')
        else:
            actions.append(f'<form method="post" action="/hdhive/subscriptions/{subscription.id}/resume"><button class="button-secondary" type="submit">恢复</button></form>')
        actions.extend(
            (
                f'<form method="post" action="/hdhive/subscriptions/{subscription.id}/check"><button class="button-primary" type="submit">立即检查</button></form>',
                f'<form method="post" action="/hdhive/subscriptions/{subscription.id}/delete" onsubmit="return confirm(\'确认删除此订阅？\')"><button class="button-danger" type="submit">删除</button></form>',
            )
        )
        rows.append(
            f'''<article class="task-row">
  <div>
    <div class="task-title">#{subscription.id} {html.escape(title)}</div>
    <div class="task-meta"><span class="badge {status_class}">{html.escape(status_label)}</span><span>{html.escape(source)}</span><span>TMDB：{html.escape(subscription.tmdb_id)}</span><span>最近检查：{html.escape(_hdhive_time_label(subscription.last_checked_at))}</span></div>
    <div class="task-meta"><span>发现 {item_counts["discovered"]}</span><span>已解锁/入队 {item_counts["enqueued"]}</span><span>待确认 {item_counts["pending_confirmation"]}</span><span>失败 {item_counts["failed"]}</span></div>
    <form method="post" action="/hdhive/subscriptions/{subscription.id}/episode-filter" class="actions"><label>集数过滤 <input name="episode_filter" value="{html.escape(str(subscription.episode_filter or ""))}" placeholder="S01E01-S01E10,S02"></label><button class="button-secondary" type="submit">保存过滤</button></form>
    {f'<div class="task-message error">{html.escape(subscription.last_error)}</div>' if subscription.last_error else ''}
  </div>
  <div class="actions">{"".join(actions)}</div>
</article>'''
        )
        if service is not None:
            for item in items:
                if item.status == "enqueued":
                    points = item.unlock_points_spent if item.unlock_points_spent is not None else item.unlock_points
                    source_label = {"actual": "实际", "estimated": "估算"}.get(item.unlock_points_source, "")
                    unlocked_rows.append(
                        f'<tr><td>{html.escape(title)}</td><td>{html.escape(item.episode_key)}</td><td>{html.escape(item.title or item.resource_slug)}</td><td>{html.escape(str(points) if points is not None else "未知")} {html.escape(source_label)}</td><td>{html.escape(_hdhive_time_label(item.unlocked_at or 0))}</td><td>{html.escape(str(item.task_id or "-"))}</td></tr>'
                    )
                if item.status == "pending_confirmation":
                    pending_rows.append(
                        f'''<tr><td>{html.escape(title)}</td><td>{html.escape(item.episode_key)}</td><td>{html.escape(item.title or item.resource_slug)}</td><td>{html.escape(str(item.unlock_points if item.unlock_points is not None else "未知"))}</td><td><form method="post" action="/hdhive/item/{item.id}/confirm"><button class="button-primary" type="submit">确认解锁</button></form></td></tr>'''
                    )

    subscriptions_markup = "".join(rows) or '<div class="empty-state">暂无 HDHive 剧集订阅。可在 Telegram 发送 HDHive 的剧集页面链接创建订阅。</div>'
    pending_markup = (
        '<div class="table-wrap"><table><thead><tr><th>剧集</th><th>集数</th><th>资源</th><th>积分</th><th>操作</th></tr></thead><tbody>'
        + "".join(pending_rows)
        + "</tbody></table></div>"
        if pending_rows
        else '<div class="empty-state">暂无待确认资源。</div>'
    )
    unlocked_markup = (
        '<div class="table-wrap"><table><thead><tr><th>剧集</th><th>集数</th><th>资源</th><th>积分</th><th>解锁时间</th><th>任务号</th></tr></thead><tbody>'
        + "".join(unlocked_rows)
        + "</tbody></table></div>"
        if unlocked_rows
        else '<div class="empty-state">暂无解锁记录。</div>'
    )
    settings_markup = ""
    if scheduler is not None:
        settings_markup = f'''<form method="post" action="/hdhive/settings" class="actions">
  <label>启用 <input type="checkbox" name="enabled" value="true" {'checked' if snapshot.get("enabled") else ''}></label>
  <label>时间 <input name="time" value="{html.escape(str(snapshot.get("time") or "01:30"))}" size="5"></label>
  <label>时区 <input name="timezone" value="{html.escape(str(snapshot.get("timezone") or "Asia/Shanghai"))}" size="18"></label>
  <button class="button-primary" type="submit">保存设置</button>
</form>
<div class="actions"><form method="post" action="/hdhive/run"><button class="button-secondary" type="submit">立即检查全部订阅</button></form></div>'''
    body = f'''
<div class="topbar">
  <div><p class="eyebrow">HDHive 自动追剧</p><h1>HDHive 订阅</h1><p class="subtle">只处理剧集的 115 资源，按有效性、分辨率和费用选择最佳资源。</p></div>
  <a class="button" href="/">返回运行概览</a>
</div>
<section class="panel"><div class="panel-header"><h2>账号状态</h2></div>{_hdhive_account_markup(service)}</section>
<section class="panel"><div class="panel-header"><h2>自动检查</h2></div>{schedule_markup}{settings_markup}{_background_job_status_markup(background_jobs, "hdhive:")}</section>
<section class="panel"><div class="panel-header"><h2>当前订阅</h2></div>{subscription_error}{subscriptions_markup}</section>
<section class="panel"><div class="panel-header"><h2>待确认资源</h2><span class="subtle">费用超过自动解锁阈值时需要确认</span></div>{pending_markup}</section>
<section class="panel"><div class="panel-header"><h2>解锁记录</h2><span class="subtle">显示实际/估算积分、解锁时间和关联任务号</span></div>{unlocked_markup}</section>
'''
    return _page("HDHive 订阅", body, active="hdhive")

class WebApp:
    def __init__(
        self,
        store: TaskStore,
        web_token: str = "",
        web_username: str = "",
        web_password: str = "",
        submission_store: Any | None = None,
        task_engine_enabled: bool = True,
        quality_automation: QualityAutomation | None = None,
        hdhive_service: Any | None = None,
        hdhive_scheduler: Any | None = None,
        self_share_config: SelfShareConfig | None = None,
        frontend_dist_path: str | Path = "/app/frontend/dist",
        max_retries: int = 3,
        background_jobs: BackgroundJobCoordinator | None = None,
        log_hub: LogHub | None = None,
        cms_version_checker: Any | None = None,
        cms_guard_container: str = "cloud-media-sync",
        cms_guard_docker_socket: str = "",
        cms_guard_marker: str = "",
        cms_guard_workflow_mode: str = "",
        cms_direct_guard_marker: str = "",
        cms_os_guard_marker: str = "",
        media_enricher: Any | None = None,
        emby_client: Any | None = None,
    ):
        self.store = store
        self.web_token = web_token
        self.web_username = str(web_username or "").strip()
        self.web_password = str(web_password or "")
        # HMAC key for session cookies, rotated per process start so an
        # old cookie cannot be replayed after a restart.
        self._session_key = secrets.token_hex(32)
        self._username_authentication = bool(self.web_username and self.web_password)
        self.submission_store = submission_store
        self.task_engine_enabled = task_engine_enabled
        self.quality_automation = quality_automation
        self.hdhive_service = hdhive_service
        self.hdhive_scheduler = hdhive_scheduler
        self.self_share_config = self_share_config or SelfShareConfig()
        self.frontend_dist_path = Path(frontend_dist_path)
        self.max_retries = normalize_task_max_retries(max_retries)
        self._owns_background_jobs = background_jobs is None
        self.background_jobs = background_jobs or BackgroundJobCoordinator()
        self.log_hub = log_hub
        self.cms_version_checker = cms_version_checker
        self.media_enricher = media_enricher
        self.emby_client = emby_client
        self.cms_guard_container = str(cms_guard_container or "cloud-media-sync").strip()
        self.cms_guard_docker_socket = str(cms_guard_docker_socket or "").strip()
        self.cms_guard_marker = str(cms_guard_marker or "").strip()
        self.cms_guard_workflow_mode = str(cms_guard_workflow_mode or "").strip()
        self.cms_direct_guard_marker = str(cms_direct_guard_marker or "").strip()
        self.cms_os_guard_marker = str(cms_os_guard_marker or "").strip()

    def _cms_strm_guard(self) -> dict[str, Any] | None:
        """Resolve CMS STRM guard status for the health payload (never raises)."""
        workflow_mode = self.cms_guard_workflow_mode or str(getattr(self.self_share_config, "workflow_mode", "") or "").strip()
        if (workflow_mode or "direct") != "self_share_sync" and not self.cms_guard_marker:
            return None
        try:
            return check_cms_strm_guard(
                workflow_mode=workflow_mode,
                container=self.cms_guard_container,
                docker_socket=self.cms_guard_docker_socket or "/var/run/docker.sock",
                marker=self.cms_guard_marker or CMS_STRM_GUARD_MARKER,
            )
        except Exception:
            return {"ok": True, "status": "unknown", "message": "守卫状态检查异常"}

    def _cms_direct_strm_guard(self) -> dict[str, Any] | None:
        """Resolve CMS direct-STRM suppression guard status (never raises)."""
        workflow_mode = self.cms_guard_workflow_mode or str(getattr(self.self_share_config, "workflow_mode", "") or "").strip()
        if (workflow_mode or "direct") != "self_share_sync" and not self.cms_direct_guard_marker:
            return None
        try:
            return check_cms_direct_strm_guard(
                workflow_mode=workflow_mode,
                container=self.cms_guard_container,
                docker_socket=self.cms_guard_docker_socket or "/var/run/docker.sock",
                marker=self.cms_direct_guard_marker or CMS_DIRECT_STRM_GUARD_MARKER,
            )
        except Exception:
            return {"ok": True, "status": "unknown", "message": "守卫状态检查异常"}

    def _cms_os_strm_guard(self) -> dict[str, Any] | None:
        """Resolve CMS os-level STRM delete-protect guard status (never raises)."""
        workflow_mode = self.cms_guard_workflow_mode or str(getattr(self.self_share_config, "workflow_mode", "") or "").strip()
        if (workflow_mode or "direct") != "self_share_sync" and not self.cms_os_guard_marker:
            return None
        try:
            return check_cms_os_strm_guard(
                workflow_mode=workflow_mode,
                container=self.cms_guard_container,
                docker_socket=self.cms_guard_docker_socket or "/var/run/docker.sock",
                marker=self.cms_os_guard_marker or CMS_OS_STRM_GUARD_MARKER,
            )
        except Exception:
            return {"ok": True, "status": "unknown", "message": "守卫状态检查异常"}

    def _submit_background(self, key: str, callable: Any, *, description: str) -> JobSubmission:
        return self.background_jobs.submit(key, callable, description=description)

    @staticmethod
    def _job_response(submission: JobSubmission) -> tuple[int, dict[str, Any]]:
        status = {
            "accepted": 202,
            "already_running": 409,
            "capacity_rejected": 429,
            "closed": 503,
        }[submission.outcome]
        return status, {"job": submission.payload(), "started": submission.outcome == "accepted"}

    def _own_share_receive_code_payload(self) -> dict[str, Any]:
        resolved = resolve_own_share_receive_code(self.store, self.self_share_config)
        return {"configured": True, "masked": resolved.masked, "source": resolved.source}

    def _self_share_receive_cid_payload(self) -> dict[str, Any]:
        resolved = resolve_self_share_receive_cid(self.store, self.self_share_config)
        return {
            "configured": resolved.configured,
            "masked": resolved.masked,
            "source": resolved.source,
        }

    def _self_share_review_payload(self) -> dict[str, Any]:
        resolved = resolve_self_share_review_policy(self.store, self.self_share_config)
        return {"mode": resolved.mode, "seconds": resolved.seconds, "source": resolved.source}

    def _session_token(self, now: float) -> str:
        """Signed session value: <expires>:<username>:<hmac-sha256>."""
        payload = f"{int(now + _SESSION_TTL_SECONDS)}:{self.web_username}"
        signature = hmac.new(self._session_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}:{signature}"

    def _session_valid(self, value: str) -> bool:
        try:
            expires_text, username, signature = str(value or "").rsplit(":", 2)
        except ValueError:
            return False
        payload = f"{expires_text}:{username}"
        expected = hmac.new(self._session_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not _constant_time_equals(signature, expected):
            return False
        if not _constant_time_equals(username, self.web_username):
            return False
        try:
            expires = float(expires_text)
        except (TypeError, ValueError):
            return False
        return time.time() < expires

    def _authorization_source(self, path: str, headers: dict[str, str]) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self._header_value(headers, "Cookie"))
        except CookieError:
            cookie = SimpleCookie()
        if self._username_authentication:
            # Session-cookie authentication: only a valid signed session is
            # accepted; query/header tokens are disabled in this mode.
            session_cookie = cookie.get(_SESSION_COOKIE_NAME)
            if session_cookie and self._session_valid(unquote(session_cookie.value)):
                return "session"
            return ""
        if not self.web_token:
            return "anonymous"
        try:
            query = parse_qs(_parse_request_target(path).query)
        except (TypeError, ValueError):
            return ""
        if _constant_time_equals(query.get("token", [""])[0], self.web_token):
            return "query"
        if _constant_time_equals(self._header_value(headers, "X-Web-Token"), self.web_token):
            return "header"
        if cookie.get("cms_web_token") and _constant_time_equals(
            unquote(cookie["cms_web_token"].value), self.web_token
        ):
            return "cookie"
        return ""

    @staticmethod
    def _header_value(headers: dict[str, str], name: str) -> str:
        normalized_name = name.casefold()
        for header_name, value in headers.items():
            if header_name.casefold() == normalized_name:
                return value
        return ""

    def _authorized(self, path: str, headers: dict[str, str]) -> bool:
        return bool(self._authorization_source(path, headers))

    def _web_token_cookie(self) -> str:
        return f"cms_web_token={quote(self.web_token, safe='')}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800"

    def _session_cookie(self, now: float) -> str:
        value = quote(self._session_token(now), safe="")
        return (
            f"{_SESSION_COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={int(_SESSION_TTL_SECONDS)}"
        )

    @staticmethod
    def _clear_session_cookie() -> str:
        return f"{_SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    def _login_page(self, error: str = "") -> str:
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · cms-tg-ingest</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f5f6fb;
    --surface: #ffffff;
    --text: #1f2937;
    --text-strong: #141829;
    --muted: #6b7280;
    --border: #d7dae6;
    --primary: #4c5fd5;
    --primary-hover: #3e4ec4;
    --primary-invert: #ffffff;
    --danger: #c0392b;
    --glow: rgba(76, 95, 213, 0.10);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      color-scheme: dark;
      --bg: #10121a;
      --surface: #171a24;
      --text: #e6e8f0;
      --text-strong: #f4f5fa;
      --muted: #9aa3b5;
      --border: #343a4e;
      --primary: #8b93ff;
      --primary-hover: #a5abff;
      --primary-invert: #0e0f1a;
      --danger: #f26d82;
      --glow: rgba(139, 147, 255, 0.10);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    background-image: radial-gradient(1100px 560px at 50% -12%, var(--glow), transparent 70%);
    color: var(--text);
  }}
  .card {{
    width: 340px;
    padding: 36px 36px 30px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 1px 2px rgba(20, 24, 41, 0.04), 0 8px 24px rgba(20, 24, 41, 0.06);
  }}
  .brand {{ display: flex; flex-direction: column; align-items: center; gap: 10px; margin-bottom: 26px; }}
  .brand-logo {{ width: 44px; height: 44px; }}
  .brand-title {{ font-size: 19px; font-weight: 700; color: var(--text-strong); letter-spacing: -0.01em; }}
  .brand-sub {{ font-size: 12px; color: var(--muted); }}
  h1 {{ display: none; }}
  label {{ display: block; font-size: 13px; color: var(--muted); margin: 14px 0 5px; }}
  input {{
    width: 100%;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    font-size: 14px;
    background: var(--surface);
    color: var(--text);
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }}
  input:focus {{
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 18%, transparent);
  }}
  button {{
    width: 100%;
    margin-top: 22px;
    padding: 11px 12px;
    border: 0;
    border-radius: 8px;
    background: var(--primary);
    color: var(--primary-invert);
    font-size: 14px;
    font-weight: 650;
    cursor: pointer;
    transition: background-color 0.15s ease;
  }}
  button:hover {{ background: var(--primary-hover); }}
  button:focus-visible {{ outline: 2px solid var(--primary); outline-offset: 2px; }}
  .error {{
    margin: 14px 0 0;
    padding: 9px 12px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--danger) 10%, transparent);
    color: var(--danger);
    font-size: 13px;
  }}
  @media (prefers-reduced-motion: reduce) {{
    input, button {{ transition: none; }}
  }}
</style>
</head>
<body>
<form class="card" method="post" action="{_LOGIN_PATH}">
  <div class="brand">
    <svg class="brand-logo" viewBox="0 0 64 64" role="img" aria-label="媒体仓" xmlns="http://www.w3.org/2000/svg">
      <rect x="4" y="4" width="56" height="56" rx="16" fill="#1D4ED8"/>
      <rect x="16" y="17" width="32" height="30" rx="7" fill="none" stroke="#FFFFFF" stroke-width="4"/>
      <path d="M16 26h32M25 17v9M39 17v9" fill="none" stroke="#FFFFFF" stroke-width="4" stroke-linecap="round"/>
      <path d="M27 32.5 40 38 27 43.5Z" fill="#FFFFFF"/>
    </svg>
    <span class="brand-title">入库助手</span>
    <span class="brand-sub">cms-tg-ingest 管理台 · 115 · CMS · Emby 工作流</span>
  </div>
  <h1>cms-tg-ingest 管理台</h1>
  <label for="username">用户名</label>
  <input id="username" name="username" autocomplete="username" required autofocus>
  <label for="password">密码</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">登录</button>
  {error_html}
</form>
</body>
</html>
"""

    def _handle_login(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        try:
            values = {
                key: items[0] if items else ""
                for key, items in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()
            }
        except UnicodeDecodeError:
            return 400, {"Content-Type": "text/html; charset=utf-8"}, self._login_page("请求格式错误").encode("utf-8")
        username = str(values.get("username") or "").strip()
        password = str(values.get("password") or "")
        if not (
            _constant_time_equals(username, self.web_username)
            and _constant_time_equals(password, self.web_password)
        ):
            return 200, {"Content-Type": "text/html; charset=utf-8"}, self._login_page("用户名或密码错误").encode("utf-8")
        return 303, {"Location": "/app/", "Set-Cookie": self._session_cookie(time.time())}, b""

    def prepare_log_stream(
        self,
        path: str,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes, LogFilter | None]:
        try:
            parsed = _parse_request_target(path)
        except (TypeError, ValueError):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request", None
        if parsed.path != _LOG_STREAM_PATH:
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not Found", None

        authorization_source = self._authorization_source(path, headers)
        if not authorization_source:
            return 403, {"Content-Type": "text/plain; charset=utf-8"}, b"Forbidden", None

        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
        except (TypeError, ValueError):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request", None
        if set(query) - _LOG_QUERY_KEYS or any(len(values) != 1 for values in query.values()):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request", None

        if authorization_source == "query":
            query.pop("token", None)
            location = parsed.path
            if encoded_query := urlencode(query, doseq=True):
                location += f"?{encoded_query}"
            return 303, {"Location": location, "Set-Cookie": self._web_token_cookie()}, b"", None

        if self.log_hub is None:
            return 503, {"Content-Type": "text/plain; charset=utf-8"}, b"Service Unavailable", None

        try:
            spec = parse_log_filter(
                query.get("filter_type", ["main"])[0],
                query.get("lines", [1000])[0],
                query.get("keyword", [""])[0],
                query.get("logger", [""])[0],
            )
        except ValueError:
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request", None

        auth_headers = {"Set-Cookie": self._web_token_cookie()} if authorization_source == "header" else {}
        return 200, auth_headers, b"", spec

    def handle_log_analysis(
        self,
        path: str,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            parsed = _parse_request_target(path)
        except (TypeError, ValueError):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request"
        if parsed.path != _LOG_ANALYZE_PATH:
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not Found"
        if not self._authorization_source(path, headers):
            return 403, {"Content-Type": "text/plain; charset=utf-8"}, b"Forbidden"
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
        except (TypeError, ValueError):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request"
        if set(query) - _LOG_ANALYZE_QUERY_KEYS or any(len(values) != 1 for values in query.values()):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request"
        if self.log_hub is None:
            return 503, {"Content-Type": "text/plain; charset=utf-8"}, b"Service Unavailable"
        try:
            lines = int(query.get("lines", ["500"])[0])
            since_seconds = int(query.get("since_seconds", ["0"])[0])
        except (TypeError, ValueError):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request"
        payload = api_log_analysis(
            self.log_hub,
            lines=lines,
            since_seconds=since_seconds,
            logger=query.get("logger", [""])[0],
            keyword=query.get("keyword", [""])[0],
            level=query.get("level", ["main"])[0],
        )
        return api_response(payload)

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        if len(body) > MAX_REQUEST_BODY_BYTES:
            return 413, {"Content-Type": "text/plain; charset=utf-8"}, b"Payload Too Large"
        try:
            parsed = _parse_request_target(path)
        except (TypeError, ValueError):
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, b"Bad Request"

        # Username/password mode: login/logout are unauthenticated entry
        # points; everything else requires a valid session cookie.
        if self._username_authentication:
            if method == "POST" and parsed.path == _LOGIN_PATH:
                return self._handle_login(body)
            if method == "GET" and parsed.path == _LOGIN_PATH:
                return 200, {"Content-Type": "text/html; charset=utf-8"}, self._login_page().encode("utf-8")
            if method == "POST" and parsed.path == _LOGOUT_PATH:
                # Rotate the signing key so every previously issued session
                # cookie is invalidated server-side (a stateless signature
                # alone cannot distinguish "logged out" from "cookie copy").
                self._session_key = secrets.token_hex(32)
                return 303, {"Location": "/login", "Set-Cookie": self._clear_session_cookie()}, b""
            if not self._authorized(parsed.path, headers):
                return 303, {"Location": _LOGIN_PATH}, b""
            return self._serve_remaining_routes(method, parsed.path, headers, body, {})

        authorization_source = self._authorization_source(path, headers)
        if not authorization_source:
            return 403, {"Content-Type": "text/plain; charset=utf-8"}, b"Forbidden"
        if method == "GET" and authorization_source == "query":
            return 303, {"Location": parsed.path or "/", "Set-Cookie": self._web_token_cookie()}, b""
        auth_headers = {"Set-Cookie": self._web_token_cookie()} if authorization_source in {"query", "header"} else {}
        return self._serve_remaining_routes(method, parsed.path, headers, body, auth_headers)

    def _serve_remaining_routes(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        auth_headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        if path == "/app" or path == "/app/" or path.startswith("/app/"):
            return self._serve_frontend(path, auth_headers)
        if path.startswith("/api/v1/"):
            return self._handle_api(method, path, headers, body, auth_headers)
        if method == "GET" and path == "/":
            return 302, {"Location": "/app/", **auth_headers}, b""
        if method == "GET" and path == "/legacy":
            page = render_task_list(self.store, task_engine_enabled=self.task_engine_enabled)
            return 200, {"Content-Type": "text/html; charset=utf-8", **auth_headers}, page.encode("utf-8")
        if method == "GET" and path == "/quality":
            return 200, {"Content-Type": "text/html; charset=utf-8", **auth_headers}, render_quality_page(
                self.store, self.quality_automation, self.max_retries, self.background_jobs
            ).encode("utf-8")
        if method == "POST" and path in {
            "/quality/action/execute",
            "/quality/action/reprocess",
            "/quality/action/snooze",
            "/quality/action/ignore",
            "/quality/action/resume",
        }:
            route_action = path.rsplit("/", 1)[-1]
            try:
                values = {key: items[0] if items else "" for key, items in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
            except UnicodeDecodeError:
                return 400, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"invalid quality request"
            status, result = _run_quality_action(self.quality_automation, values, route_action)
            if status != 200:
                return status, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, str(result.get("error") or "quality action rejected").encode("utf-8")
            return 303, {"Location": "/quality", **auth_headers}, b""
        if method == "POST" and path == "/quality/fix":
            fix_quality_issues(self.store, self.quality_automation)
            return 303, {"Location": "/quality", **auth_headers}, b""
        if method == "POST" and path == "/quality/run":
            if self.quality_automation is None:
                return 409, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"quality automation unavailable"
            self._submit_background("quality:run", self.quality_automation.run_now, description="质量巡检")
            return 303, {"Location": "/quality", **auth_headers}, b""
        if method == "POST" and path == "/quality/settings/reset":
            if self.quality_automation is None:
                return 409, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"quality automation unavailable"
            self.quality_automation.reset_settings()
            return 303, {"Location": "/quality", **auth_headers}, b""
        if method == "POST" and path == "/quality/settings":
            if self.quality_automation is None:
                return 409, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"quality automation unavailable"
            try:
                values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
                self.quality_automation.update_settings(
                    enabled=values.get("enabled", [""])[0].lower() in {"1", "true", "on", "yes"},
                    run_time=values.get("time", [""])[0],
                    timezone_name=values.get("timezone", [""])[0],
                    max_tasks=int(values.get("max_tasks", [""])[0]),
                    check_limit=int(values.get("check_limit", [""])[0]),
                )
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                return 400, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, str(exc).encode("utf-8")
            return 303, {"Location": "/quality", **auth_headers}, b""
        if method == "GET" and path == "/health":
            page = render_health_page(self.store, task_engine_enabled=self.task_engine_enabled)
            return 200, {"Content-Type": "text/html; charset=utf-8", **auth_headers}, page.encode("utf-8")
        if method == "GET" and path == "/hdhive":
            page = render_hdhive_page(self.hdhive_service, self.hdhive_scheduler, self.background_jobs)
            status = 200 if self.hdhive_service is not None else 409
            return status, {"Content-Type": "text/html; charset=utf-8", **auth_headers}, page.encode("utf-8")
        if method == "POST" and path == "/hdhive/settings":
            scheduler = self.hdhive_scheduler
            if scheduler is None:
                return 409, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"HDHive scheduler unavailable"
            try:
                values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
                scheduler.update_settings(
                    enabled=values.get("enabled", [""])[0].lower() in {"1", "true", "on", "yes"},
                    run_time=values.get("time", [""])[0],
                    timezone_name=values.get("timezone", [""])[0],
                )
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                return 400, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, str(exc).encode("utf-8")
            return 303, {"Location": "/hdhive", **auth_headers}, b""
        if method == "POST" and path == "/hdhive/run":
            scheduler = self.hdhive_scheduler
            if scheduler is None:
                return 409, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"HDHive scheduler unavailable"
            self._submit_background("hdhive:run", scheduler.run_now, description="检查全部 HDHive 订阅")
            return 303, {"Location": "/hdhive", **auth_headers}, b""
        if method == "POST":
            hdhive_parts = [part for part in path.split("/") if part]
            service = self.hdhive_service
            if len(hdhive_parts) == 4 and hdhive_parts[0] == "hdhive" and hdhive_parts[1] in {"subscription", "subscriptions"} and hdhive_parts[2].isdigit():
                if service is None:
                    return 409, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"HDHive service unavailable"
                subscription_id = int(hdhive_parts[2])
                action = hdhive_parts[3]
                if action in {"pause", "resume", "delete"}:
                    try:
                        getattr(service, action)(subscription_id)
                    except KeyError as exc:
                        return 404, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, str(exc).encode("utf-8")
                    return 303, {"Location": "/hdhive", **auth_headers}, b""
                if action == "episode-filter":
                    try:
                        values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
                        service.set_episode_filter(subscription_id, values.get("episode_filter", [""])[0])
                    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                        return 400, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, str(exc).encode("utf-8")
                    return 303, {"Location": "/hdhive", **auth_headers}, b""
                if action == "check":
                    self._submit_background(
                        f"hdhive:subscription:{subscription_id}",
                        lambda: service.check(subscription_id),
                        description=f"检查 HDHive 订阅 #{subscription_id}",
                    )
                    return 303, {"Location": "/hdhive", **auth_headers}, b""
            if len(hdhive_parts) == 4 and hdhive_parts[:2] == ["hdhive", "item"] and hdhive_parts[2].isdigit() and hdhive_parts[3] == "confirm":
                if service is None:
                    return 409, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"HDHive service unavailable"
                item_id = int(hdhive_parts[2])
                self._submit_background(
                    f"hdhive:item:{item_id}",
                    lambda: service.confirm_item(item_id),
                    description=f"确认 HDHive 资源 #{item_id}",
                )
                return 303, {"Location": "/hdhive", **auth_headers}, b""
        if method == "POST" and path == "/history/clear":
            self.store.clear_finished_tasks()
            return 303, {"Location": "/", **auth_headers}, b""
        if method == "GET" and path.startswith("/task/"):
            task_id = parse_task_id_from_path(path)
            if task_id is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            return 200, {"Content-Type": "text/html; charset=utf-8", **auth_headers}, render_task_detail(
                self.store,
                task_id,
                self.submission_store,
                self.max_retries,
            ).encode("utf-8")
        task_action = parse_task_action_path(path) if method == "POST" else None
        if task_action is not None:
            task_id, action = task_action
            if action == "terminate" and not self.task_engine_enabled:
                return (
                    409,
                    {"Content-Type": "text/plain; charset=utf-8", **auth_headers},
                    LEGACY_LIFECYCLE_REASON.encode("utf-8"),
                )
            task = self.store.find_task(task_id)
            if task:
                apply_task_action(self.store, task_id, action, max_retries=self.max_retries, actor="Web")
            return 303, {"Location": f"/task/{task_id}", **auth_headers}, b""
        if path.startswith("/task/"):
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not Found"

    def _serve_frontend(self, path: str, auth_headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        relative = "index.html" if path in {"/app", "/app/"} else path.removeprefix("/app/")
        root = self.frontend_dist_path.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return 404, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"Not Found"
        if not candidate.is_file() and not candidate.suffix:
            candidate = root / "index.html"
        if not candidate.is_file():
            return 404, {"Content-Type": "text/plain; charset=utf-8", **auth_headers}, b"Frontend asset not found"
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        content_type_header = f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"} else content_type
        response_headers = {"Content-Type": content_type_header, **auth_headers}
        if candidate.name != "index.html":
            # Hashed assets (Vite emits content-hashed filenames) can be
            # cached aggressively; the SPA entry must always be revalidated so
            # a deploy is picked up promptly.
            response_headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response_headers["Cache-Control"] = "no-cache"
        return 200, response_headers, candidate.read_bytes()

    @staticmethod
    def _api_body(body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        text = body.decode("utf-8") if body else ""
        if "application/json" in str(headers.get("Content-Type") or headers.get("content-type") or ""):
            value = json.loads(text or "{}")
            return value if isinstance(value, dict) else {}
        values = parse_qs(text, keep_blank_values=True)
        return {key: items[0] if items else "" for key, items in values.items()}

    def _handle_api(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        auth_headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "POST" and path == "/api/v1/tasks/purge":
            try:
                values = self._api_body(body, headers)
            except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
                status, response_headers, response_body = api_response(
                    {"error": "invalid_request"},
                    status=400,
                )
                return status, {**response_headers, **auth_headers}, response_body
            raw_ids = values.get("ids") or []
            if isinstance(raw_ids, str):
                raw_ids = [raw_ids]
            ids: list[int] = []
            for value in raw_ids:
                try:
                    candidate = int(value)
                except (TypeError, ValueError):
                    continue
                if candidate > 0:
                    ids.append(candidate)
            dry_run = bool(values.get("dry_run"))
            deleted: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for task_id in ids:
                if dry_run:
                    task = self.store.find_task(task_id)
                    if task is None:
                        rejected.append({"id": task_id, "reason": "任务不存在或已过期"})
                    elif "delete" not in available_lifecycle_actions(task):
                        rejected.append({"id": task_id, "reason": "任务尚未结束或正在执行"})
                    else:
                        deleted.append({"id": task_id, "reason": "可删除"})
                    continue
                result = delete_task_record_and_submission(self.store, self.submission_store, task_id)
                if result.applied:
                    deleted.append({"id": task_id, "reason": result.reason})
                else:
                    rejected.append({"id": task_id, "reason": result.reason})
            status, response_headers, response_body = api_response(
                {"dry_run": dry_run, "deleted": deleted, "rejected": rejected}
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path.startswith("/api/v1/tasks/"):
            parts = path.split("/")
            if len(parts) == 7 and parts[5] == "actions" and parts[4].isdigit():
                task_id = int(parts[4])
                action = parts[6]
                task = self.store.find_task(task_id)
                if task is None:
                    status, response_headers, response_body = api_response(
                        {"error": "task_not_found", "message": TASK_NOT_FOUND_MESSAGE},
                        status=404,
                    )
                    return status, {**response_headers, **auth_headers}, response_body
                if action == "terminate" and not self.task_engine_enabled:
                    status, response_headers, response_body = api_response(
                        {
                            "error": "action_not_allowed",
                            "action": action,
                            "reason": LEGACY_LIFECYCLE_REASON,
                        },
                        status=409,
                    )
                    return status, {**response_headers, **auth_headers}, response_body
                result = apply_task_action(self.store, task_id, action, max_retries=self.max_retries, actor="Web") if action in TASK_ACTIONS else None
                if result is None or not result.applied:
                    status, response_headers, response_body = api_response(
                        {
                            "error": "action_not_allowed",
                            "action": action,
                            "reason": result.reason if result is not None else "不支持的任务操作",
                        },
                        status=409,
                    )
                    return status, {**response_headers, **auth_headers}, response_body
                status, response_headers, response_body = api_response(
                    api_task_detail(
                        self.store,
                        task_id,
                        lifecycle_actions_enabled=self.task_engine_enabled,
                        max_retries=self.max_retries,
                    )
                )
                return status, {**response_headers, **auth_headers}, response_body
        if method == "DELETE" and path.startswith("/api/v1/tasks/"):
            raw_id = path.removeprefix("/api/v1/tasks/")
            if raw_id.isdigit():
                if not self.task_engine_enabled:
                    task = self.store.find_task(int(raw_id))
                    payload = (
                        {"error": "delete_not_allowed", "reason": LEGACY_LIFECYCLE_REASON}
                        if task is not None
                        else {"error": "task_not_found", "message": TASK_NOT_FOUND_MESSAGE}
                    )
                    status, response_headers, response_body = api_response(
                        payload,
                        status=409 if task is not None else 404,
                    )
                    return status, {**response_headers, **auth_headers}, response_body
                result = delete_task_record(self.store, int(raw_id))
                if result.task is None:
                    status, response_headers, response_body = api_response(
                        {"error": "task_not_found", "message": TASK_NOT_FOUND_MESSAGE},
                        status=404,
                    )
                    return status, {**response_headers, **auth_headers}, response_body
                if not result.applied:
                    status, response_headers, response_body = api_response(
                        {"error": "delete_not_allowed", "reason": result.reason},
                        status=409,
                    )
                    return status, {**response_headers, **auth_headers}, response_body
                status, response_headers, response_body = api_response({"deleted": int(raw_id), "message": result.reason})
                return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/history/clear":
            cleared = self.store.clear_finished_tasks()
            status, response_headers, response_body = api_response({"cleared": cleared})
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path in {
            "/api/v1/quality/action/execute",
            "/api/v1/quality/action/snooze",
            "/api/v1/quality/action/ignore",
            "/api/v1/quality/action/resume",
        }:
            route_action = path.rsplit("/", 1)[-1]
            try:
                values = self._api_body(body, headers)
            except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
                status, response_headers, response_body = api_response({"error": "invalid_quality_request"}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, result = _run_quality_action(self.quality_automation, values, route_action)
            response_status, response_headers, response_body = api_response(result, status=status)
            return response_status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path in {
            "/api/v1/quality/cleanup/dry-run",
            "/api/v1/quality/cleanup/run",
        }:
            if self.quality_automation is None or not self.quality_automation.strm_cleanup_enabled:
                status, response_headers, response_body = api_response(
                    {"error": "quality_strm_cleanup_disabled"}, status=409
                )
                return status, {**response_headers, **auth_headers}, response_body
            try:
                values = self._api_body(body, headers)
            except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
                status, response_headers, response_body = api_response(
                    {"error": "invalid_quality_cleanup_request"}, status=400
                )
                return status, {**response_headers, **auth_headers}, response_body
            task_id = _quality_task_id(values.get("task_id"))
            if task_id is None:
                status, response_headers, response_body = api_response(
                    {"error": "invalid_task_id"}, status=400
                )
                return status, {**response_headers, **auth_headers}, response_body
            if path == "/api/v1/quality/cleanup/dry-run":
                task = self.store.find_task(task_id)
                if task is None:
                    status, response_headers, response_body = api_response(
                        {"error": "task_not_found"}, status=404
                    )
                    return status, {**response_headers, **auth_headers}, response_body
                check_shares = bool(values.get("check_shares"))
                candidates = self.quality_automation.stale_strm_candidates(task, check_shares=check_shares)
                status, response_headers, response_body = api_response(
                    {"enabled": True, "task_id": task_id, "candidates": candidates}
                )
                return status, {**response_headers, **auth_headers}, response_body
            raw_paths = values.get("paths") or []
            if not isinstance(raw_paths, list):
                status, response_headers, response_body = api_response(
                    {"error": "invalid_paths"}, status=400
                )
                return status, {**response_headers, **auth_headers}, response_body
            allow_alive = bool(values.get("allow_alive"))
            result = self.quality_automation.cleanup_stale_strm(
                task_id, [str(item) for item in raw_paths], actor="web", allow_alive=allow_alive
            )
            status, response_headers, response_body = api_response(result)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/quality/fix":
            fixed = fix_quality_issues(self.store, self.quality_automation)
            status, response_headers, response_body = api_response({"fixed": fixed})
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/quality/run":
            if self.quality_automation is None:
                status, response_headers, response_body = api_response({"error": "quality_unavailable"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            submission = self._submit_background("quality:run", self.quality_automation.run_now, description="质量巡检")
            status, payload = self._job_response(submission)
            status, response_headers, response_body = api_response(payload, status=status)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/quality/settings/reset":
            if self.quality_automation is None:
                status, response_headers, response_body = api_response({"error": "quality_unavailable"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            settings = self.quality_automation.reset_settings()
            status, response_headers, response_body = api_response({"settings": settings})
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/quality/settings":
            if self.quality_automation is None:
                status, response_headers, response_body = api_response({"error": "quality_unavailable"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            try:
                values = self._api_body(body, headers)
                settings = self.quality_automation.update_settings(
                    enabled=str(values.get("enabled") or "").lower() in {"1", "true", "on", "yes"},
                    run_time=str(values.get("time") or ""),
                    timezone_name=str(values.get("timezone") or ""),
                    max_tasks=int(values.get("max_tasks")),
                    check_limit=int(values.get("check_limit")),
                )
            except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response({"settings": settings})
            return status, {**response_headers, **auth_headers}, response_body
        parts = path.split("/")
        if method == "POST" and len(parts) == 7 and parts[3] == "hdhive" and parts[4] in {"subscription", "subscriptions"} and parts[5].isdigit():
            service = self.hdhive_service
            if service is None:
                status, response_headers, response_body = api_response({"error": "hdhive_unavailable"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            subscription_id = int(parts[5])
            action = parts[6]
            if action in {"pause", "resume", "delete"}:
                try:
                    getattr(service, action)(subscription_id)
                except KeyError as exc:
                    status, response_headers, response_body = api_response({"error": str(exc)}, status=404)
                    return status, {**response_headers, **auth_headers}, response_body
                status, response_headers, response_body = api_response({"ok": True})
                return status, {**response_headers, **auth_headers}, response_body
            if action == "episode-filter":
                try:
                    values = self._api_body(body, headers)
                    service.set_episode_filter(subscription_id, str(values.get("episode_filter") or ""))
                    serialized = serialize_hdhive_subscription(service, subscription_id)
                except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                    status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                    return status, {**response_headers, **auth_headers}, response_body
                if serialized is None:
                    status, response_headers, response_body = api_response({"error": "subscription_not_found"}, status=404)
                    return status, {**response_headers, **auth_headers}, response_body
                status, response_headers, response_body = api_response({"subscription": serialized})
                return status, {**response_headers, **auth_headers}, response_body
            if action == "check":
                submission = self._submit_background(
                    f"hdhive:subscription:{subscription_id}",
                    lambda: service.check(subscription_id),
                    description=f"检查 HDHive 订阅 #{subscription_id}",
                )
                status, payload = self._job_response(submission)
                status, response_headers, response_body = api_response(payload, status=status)
                return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and len(parts) == 7 and parts[3:5] == ["hdhive", "items"] and parts[5].isdigit() and parts[6] == "confirm":
            service = self.hdhive_service
            if service is None:
                status, response_headers, response_body = api_response({"error": "hdhive_unavailable"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            item_id = int(parts[5])
            submission = self._submit_background(
                f"hdhive:item:{item_id}",
                lambda: service.confirm_item(item_id),
                description=f"确认 HDHive 资源 #{item_id}",
            )
            status, payload = self._job_response(submission)
            status, response_headers, response_body = api_response(payload, status=status)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/hdhive/settings":
            scheduler = self.hdhive_scheduler
            if scheduler is None:
                status, response_headers, response_body = api_response({"error": "hdhive_scheduler_unavailable"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            try:
                values = self._api_body(body, headers)
                settings = scheduler.update_settings(
                    enabled=str(values.get("enabled") or "").lower() in {"1", "true", "on", "yes"},
                    run_time=str(values.get("time") or ""),
                    timezone_name=str(values.get("timezone") or ""),
                )
            except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response({"settings": settings})
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/hdhive/run":
            scheduler = self.hdhive_scheduler
            if scheduler is None:
                status, response_headers, response_body = api_response({"error": "hdhive_scheduler_unavailable"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            submission = self._submit_background("hdhive:run", scheduler.run_now, description="检查全部 HDHive 订阅")
            status, payload = self._job_response(submission)
            status, response_headers, response_body = api_response(payload, status=status)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/overview":
            payload = {
                "tasks": api_tasks(
                    self.store,
                    limit=20,
                    lifecycle_actions_enabled=self.task_engine_enabled,
                    media_enricher=self.media_enricher,
                ),
                "health": serialize_health(
                    self.store,
                    enabled=self.task_engine_enabled,
                    cms_guard=self._cms_strm_guard(),
                    cms_direct_guard=self._cms_direct_strm_guard(),
                    cms_os_guard=self._cms_os_strm_guard(),
                ),
                "strm_default_mode": self.store.get_default_strm_mode(),
                "own_share_receive_code": self._own_share_receive_code_payload(),
            }
            status, response_headers, response_body = api_response(payload)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/settings":
            payload = {
                "app_name": "cms-tg-ingest",
                "version": __version__,
                "strm_default_mode": self.store.get_default_strm_mode(),
                "strm_modes": [
                    {"value": value, "label": STRM_MODE_LABELS[value]}
                    for value in ("shared", "direct", "source_shared")
                ],
                "own_share_receive_code": self._own_share_receive_code_payload(),
                "self_share_receive_cid": self._self_share_receive_cid_payload(),
                "self_share_review": self._self_share_review_payload(),
                "self_share_review_modes": [
                    {"value": "ten_minutes", "label": "10 分钟（推荐）"},
                    {"value": "off", "label": "关闭观察"},
                    {"value": "env", "label": "使用环境配置"},
                ],
            }
            status, response_headers, response_body = api_response(payload)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/tasks":
            status, response_headers, response_body = api_response(
                api_tasks(
                    self.store,
                    lifecycle_actions_enabled=self.task_engine_enabled,
                    max_retries=self.max_retries,
                    media_enricher=self.media_enricher,
                )
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path.startswith("/api/v1/tasks/"):
            try:
                task_id = int(path.removeprefix("/api/v1/tasks/"))
            except ValueError:
                task_id = 0
            detail = api_task_detail(
                self.store,
                task_id,
                lifecycle_actions_enabled=self.task_engine_enabled,
                max_retries=self.max_retries,
            )
            status, response_headers, response_body = api_response(
                detail if detail is not None else {"error": "task_not_found"},
                status=200 if detail is not None else 404,
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/health":
            status, response_headers, response_body = api_response(
                serialize_health(
                    self.store,
                    enabled=self.task_engine_enabled,
                    cms_guard=self._cms_strm_guard(),
                    cms_direct_guard=self._cms_direct_strm_guard(),
                )
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/emby/dashboard":
            # Refresh is requested via header: _serve_remaining_routes receives
            # a query-stripped path, so a ?refresh=1 query would never match.
            refresh = str(headers.get("X-Emby-Dashboard-Refresh") or "").strip() == "1"
            status, response_headers, response_body = api_response(
                api_emby_dashboard(self.emby_client, refresh=refresh)
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/quality":
            status, response_headers, response_body = api_response(
                api_quality(self.store, quality_automation=self.quality_automation, background_jobs=self.background_jobs)
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/quality/runs":
            status, response_headers, response_body = api_response(
                api_quality_runs(self.store, limit=30, days=30)
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/hdhive":
            try:
                payload = serialize_hdhive(self.hdhive_service, self.hdhive_scheduler, self.background_jobs)
            except Exception as exc:
                status, response_headers, response_body = api_response({"error": "hdhive_unavailable", "message": str(exc)[:160]}, status=503)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response(payload)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/cms/version":
            status, response_headers, response_body = api_response(
                api_cms_version(self.cms_version_checker)
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/cms/version/check":
            if self.cms_version_checker is None or not callable(
                getattr(self.cms_version_checker, "check", None)
            ):
                status, response_headers, response_body = api_response(
                    {"error": "cms_version_check_disabled"},
                    status=409,
                )
                return status, {**response_headers, **auth_headers}, response_body
            payload = self.cms_version_checker.check()
            status, response_headers, response_body = api_response(payload)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/cms/version/pull":
            if self.cms_version_checker is None or not callable(
                getattr(self.cms_version_checker, "pull", None)
            ):
                status, response_headers, response_body = api_response(
                    {"error": "cms_version_check_disabled"},
                    status=409,
                )
                return status, {**response_headers, **auth_headers}, response_body
            self.cms_version_checker.pull()
            # Re-serialize through api_cms_version so the upgrade_hint (built
            # from the persisted update_available state) is included.
            payload = api_cms_version(self.cms_version_checker)
            status, response_headers, response_body = api_response(payload)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "GET" and path == "/api/v1/settings/cms-version":
            status, response_headers, response_body = api_response(
                api_cms_version(self.cms_version_checker)
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path in {
            "/api/v1/settings/cms-version",
            "/api/v1/settings/cms-version/reset",
        }:
            if self.cms_version_checker is None or not callable(
                getattr(self.cms_version_checker, "update_settings", None)
            ):
                status, response_headers, response_body = api_response(
                    {"error": "cms_version_check_disabled"},
                    status=409,
                )
                return status, {**response_headers, **auth_headers}, response_body
            try:
                if path.endswith("/reset"):
                    payload = self.cms_version_checker.reset_settings()
                else:
                    values = self._api_body(body, headers)
                    payload = self.cms_version_checker.update_settings(values)
            except (UnicodeDecodeError, TypeError, ValueError, KeyError) as exc:
                status, response_headers, response_body = api_response(
                    {"error": str(exc)},
                    status=400,
                )
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response(payload)
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/settings/strm-mode":
            try:
                values = self._api_body(body, headers)
                mode = self.store.set_default_strm_mode(str(values.get("mode") or ""))
            except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response({"strm_default_mode": mode})
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/settings/own-share-receive-code":
            try:
                values = self._api_body(body, headers)
                clear = values.get("clear") is True or str(values.get("clear") or "").lower() in {"1", "true", "yes"}
                if clear:
                    self.store.clear_own_share_receive_code_override()
                else:
                    self.store.set_own_share_receive_code_override(str(values.get("receive_code") or ""))
            except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response(
                {"own_share_receive_code": self._own_share_receive_code_payload()}
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/settings/self-share-receive-cid":
            try:
                values = self._api_body(body, headers)
                clear = values.get("clear") is True or str(values.get("clear") or "").lower() in {"1", "true", "yes"}
                if clear:
                    self.store.clear_self_share_receive_cid_override()
                else:
                    self.store.set_self_share_receive_cid_override(str(values.get("receive_cid") or ""))
            except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response(
                {"self_share_receive_cid": self._self_share_receive_cid_payload()}
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path == "/api/v1/settings/self-share-review":
            try:
                values = self._api_body(body, headers)
                mode = str(values.get("mode") or "").strip().lower()
                if mode == "env":
                    self.store.clear_self_share_review_mode_override()
                else:
                    self.store.set_self_share_review_mode_override(mode)
                self.store.wake_self_share_review_tasks()
            except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response(
                {"self_share_review": self._self_share_review_payload()}
            )
            return status, {**response_headers, **auth_headers}, response_body
        if method == "POST" and path.startswith("/api/v1/tasks/") and path.endswith("/strm-mode"):
            raw_id = path.removeprefix("/api/v1/tasks/").removesuffix("/strm-mode")
            try:
                task_id = int(raw_id)
                values = self._api_body(body, headers)
                task = self.store.set_task_strm_mode(task_id, str(values.get("mode") or ""))
            except RuntimeError as exc:
                status, response_headers, response_body = api_response({"error": str(exc), "code": "strm_mode_locked"}, status=409)
                return status, {**response_headers, **auth_headers}, response_body
            except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
                status, response_headers, response_body = api_response({"error": str(exc)}, status=400)
                return status, {**response_headers, **auth_headers}, response_body
            status, response_headers, response_body = api_response(
                {
                    "task": api_task_detail(
                        self.store,
                        task.id,
                        lifecycle_actions_enabled=self.task_engine_enabled,
                        max_retries=self.max_retries,
                    )
                }
            )
            return status, {**response_headers, **auth_headers}, response_body
        status, response_headers, response_body = api_response({"error": "not_found"}, status=404)
        return status, {**response_headers, **auth_headers}, response_body


def start_web_server(
    store: TaskStore,
    host: str,
    port: int,
    web_token: str = "",
    web_username: str = "",
    web_password: str = "",
    submission_store: Any | None = None,
    task_engine_enabled: bool = True,
    quality_automation: QualityAutomation | None = None,
    hdhive_service: Any | None = None,
    hdhive_scheduler: Any | None = None,
    self_share_config: SelfShareConfig | None = None,
    frontend_dist_path: str | Path = "/app/frontend/dist",
    max_retries: int = 3,
    background_jobs: BackgroundJobCoordinator | None = None,
    log_hub: LogHub | None = None,
    cms_version_checker: Any | None = None,
    cms_guard_container: str = "cloud-media-sync",
    cms_guard_docker_socket: str = "",
    cms_guard_marker: str = "",
    cms_guard_workflow_mode: str = "",
    tmdb_resolver: Any | None = None,
    emby_client: Any | None = None,
) -> ThreadingHTTPServer:
    app = WebApp(
        store,
        web_token=web_token,
        web_username=web_username,
        web_password=web_password,
        submission_store=submission_store,
        task_engine_enabled=task_engine_enabled,
        quality_automation=quality_automation,
        hdhive_service=hdhive_service,
        hdhive_scheduler=hdhive_scheduler,
        self_share_config=self_share_config,
        frontend_dist_path=frontend_dist_path,
        max_retries=max_retries,
        background_jobs=background_jobs,
        log_hub=log_hub,
        cms_version_checker=cms_version_checker,
        cms_guard_container=cms_guard_container,
        cms_guard_docker_socket=cms_guard_docker_socket,
        cms_guard_marker=cms_guard_marker,
        cms_guard_workflow_mode=cms_guard_workflow_mode,
        media_enricher=(
            (lambda store, tasks: enrich_task_media_metadata(store, tasks, tmdb_resolver))
            if tmdb_resolver is not None
            and getattr(tmdb_resolver, "enabled", False)
            else None
        ),
        emby_client=emby_client,
    )
    sse_capacity = BoundedSemaphore(max(1, int(SSE_MAX_CLIENTS)))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                parsed = _parse_request_target(self.path)
            except (TypeError, ValueError):
                self._request_body_error(400, b"Bad Request")
                return
            if parsed.path == _LOG_STREAM_PATH:
                self._serve_log_stream()
                return
            if parsed.path == _LOG_ANALYZE_PATH:
                self._serve_log_analysis()
                return
            self._serve()

        def do_POST(self):
            try:
                length = parse_content_length(self.headers.get("Content-Length"))
            except RequestBodyTooLarge:
                self._request_body_error(413, b"Payload Too Large")
                return
            except ValueError:
                self._request_body_error(400, b"Invalid Content-Length")
                return
            try:
                body = self._read_request_body(length) if length else b""
            except RequestBodyDisconnected:
                self.close_connection = True
                return
            except ValueError:
                self._request_body_error(400, b"Incomplete request body")
                return
            self._serve(body)

        def do_DELETE(self):
            self._serve()

        def _read_request_body(self, length: int) -> bytes:
            previous_timeout = self.connection.gettimeout()
            deadline = time.monotonic() + REQUEST_BODY_READ_TIMEOUT_SECONDS
            chunks = []
            remaining = length
            try:
                while remaining:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise TimeoutError
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(8192, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            except TimeoutError as exc:
                raise ValueError("request body read timed out") from exc
            except (ConnectionResetError, BrokenPipeError) as exc:
                raise RequestBodyDisconnected from exc
            except OSError as exc:
                if exc.errno in {errno.ECONNABORTED, errno.ECONNRESET, errno.ENOTCONN, errno.EPIPE}:
                    raise RequestBodyDisconnected from exc
                raise
            finally:
                self.connection.settimeout(previous_timeout)
            body = b"".join(chunks)
            if len(body) != length:
                raise ValueError("request body is shorter than Content-Length")
            return body

        def _request_body_error(self, status: int, payload: bytes) -> None:
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def _serve_log_stream(self) -> None:
            status, headers, body, spec = app.prepare_log_stream(self.path, dict(self.headers))
            if status != 200 or spec is None or app.log_hub is None:
                self.close_connection = True
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                return

            shutdown_event = getattr(self.server, "_cms_shutdown_event", None)
            if shutdown_event is not None and shutdown_event.is_set():
                self._request_body_error(503, b"Service Unavailable")
                return
            if not sse_capacity.acquire(blocking=False):
                self._request_body_error(429, b"Too Many Requests")
                return

            stream = None
            previous_timeout: float | None = None
            restore_timeout = False
            try:
                stream = app.log_hub.open_stream(spec, queue_size=SSE_CLIENT_QUEUE_SIZE)
                if shutdown_event is not None and shutdown_event.is_set():
                    return
                previous_timeout = self.connection.gettimeout()
                restore_timeout = True
                self.connection.settimeout(SSE_WRITE_TIMEOUT_SECONDS)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(encode_sse_event("snapshot", {
                    "entries": [entry.payload() for entry in stream.snapshot],
                    "filter_type": spec.filter_type,
                    "lines": spec.lines,
                    "keyword": spec.keyword,
                }))
                self.wfile.flush()
                while shutdown_event is None or not shutdown_event.is_set():
                    event = stream.next_event(SSE_HEARTBEAT_SECONDS)
                    if event is None:
                        frame = encode_sse_event("heartbeat", {"time": time.time()})
                    elif event.kind == "closed":
                        break
                    elif event.kind == "gap":
                        self.wfile.write(encode_sse_event("gap", {"reason": "slow_client", "dropped": event.dropped}))
                        self.wfile.flush()
                        break
                    else:
                        frame = encode_sse_event("log", event.entry.payload(), event_id=event.entry.id)
                    self.wfile.write(frame)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True
            finally:
                try:
                    if restore_timeout:
                        try:
                            self.connection.settimeout(previous_timeout)
                        except OSError:
                            self.close_connection = True
                finally:
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        self.close_connection = True
                    finally:
                        sse_capacity.release()

        def _serve_log_analysis(self) -> None:
            status, headers, body = app.handle_log_analysis(self.path, dict(self.headers))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _serve(self, body: bytes = b""):
            status, headers, payload = app.handle_request(self.command, self.path, dict(self.headers), body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = _WebThreadingHTTPServer((host, port), Handler, app.log_hub)
    server.daemon_threads = True
    server.block_on_close = False
    server._cms_background_jobs = app.background_jobs
    server._cms_owns_background_jobs = app._owns_background_jobs
    thread = Thread(target=server.serve_forever, daemon=True)
    server._cms_thread = thread
    thread.start()
    return server
