from __future__ import annotations

import html
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import RetryAction, TaskStage, TaskStatus
from .quality import QualityIssue, format_task_quality_report, scan_task_quality
from .task_bridge import sync_task_from_submission
from .task_diagnostics import (
    _duration,
    format_stage_observability,
    format_task_observability,
    is_dispatchable_active_task,
    is_unscheduled_active_task,
)
from .task_engine import decide_retry, stage_display_name
from .task_health import build_task_health, format_task_health
from .task_store import TaskStore


_NAV_ITEMS = (
    ("overview", "/", "运行概览"),
    ("quality", "/quality", "质量巡检"),
    ("health", "/health", "本地健康"),
)

_TASK_PHASES = (
    ("接收", {TaskStage.RECEIVED, TaskStage.CMS_SUBMITTED}),
    ("CMS 整理", {TaskStage.ORGANIZING, TaskStage.ORGANIZED}),
    ("分类识别", {TaskStage.RECOGNIZING}),
    ("建分享", {TaskStage.SHARE_ALIAS_PREPARED, TaskStage.OWN_SHARE_CREATED, TaskStage.SHARE_VALIDATED}),
    ("分享 STRM", {TaskStage.SHARE_SYNC_SUBMITTED, TaskStage.STRM_READY, TaskStage.CMS_DELETE_SETTLED}),
    ("移动入库", {TaskStage.MOVED}),
    ("Emby 确认", {TaskStage.EMBY_CONFIRMED}),
    ("清理完成", {TaskStage.CLEANED}),
)

_DOWNSTREAM_RECOVERY_STAGES = {TaskStage.MOVED, TaskStage.EMBY_CONFIRMED, TaskStage.CLEANED}
_TERMINAL_ACTION_STATUSES = {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION, TaskStatus.SUCCEEDED}
_TASK_ACTIONS = {"retry", "emby", "restore", "reprocess"}


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
  --text: #202124;
  --muted: #6a6f75;
  --muted-strong: #4f555b;
  --primary: #1f5f99;
  --primary-dark: #174b7a;
  --success-bg: #e8f4ec;
  --success-text: #24643b;
  --warning-bg: #fff4d6;
  --warning-text: #805d10;
  --danger-bg: #fbe9e9;
  --danger-text: #9b2c2c;
  --info-bg: #e8f1f8;
  --info-text: #245b85;
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
.incident-strip[data-status="failed"] {{ border-color: #e2b8b8; background: #fffafa; }}
.incident-strip[data-status="needs_action"], .incident-strip[data-status="attention"] {{ border-color: #e1cf9d; background: #fffbef; }}
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
.danger-zone {{ margin-top: 20px; border: 1px solid #e2b8b8; border-radius: 6px; background: #fffafa; }}
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
.health-status.is-healthy, .empty-state.is-healthy {{ border-color: #b9d8c3; background: var(--success-bg); color: var(--success-text); }}
.health-status.is-warning {{ border-color: #e1cf9d; background: var(--warning-bg); color: var(--warning-text); }}
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
.button:hover, button:hover {{ border-color: #aeb3b8; text-decoration: none; }}
.button-primary {{ border-color: var(--primary); background: var(--primary); color: white; }}
.button-secondary {{ border-color: var(--border); background: var(--surface); color: var(--text); }}
.button-danger {{ border-color: #d7a6a6; background: var(--danger-bg); color: var(--danger-text); }}
:focus-visible {{ outline: 3px solid var(--primary-dark); outline-offset: 2px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
th, td {{ border-bottom: 1px solid var(--border-soft); padding: 11px 10px; text-align: left; vertical-align: top; }}
th {{ color: var(--muted); font-size: 12px; font-weight: 700; }}
code {{ background: #eef2f7; padding: 2px 5px; border-radius: 6px; }}
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
    if len(parts) != 4 or parts[0] or parts[1] != "task" or parts[3] not in _TASK_ACTIONS:
        return None
    try:
        return int(parts[2]), parts[3]
    except (TypeError, ValueError):
        return None


def _task_can_retry(task: Any, decision: Any | None = None) -> bool:
    if str(task.claimed_by or "").strip():
        return False
    retry_decision = decision or decide_retry(task)
    return task.status == TaskStatus.FAILED and retry_decision.action == RetryAction.RETRY_CURRENT_STAGE


def _task_can_use_downstream_actions(task: Any) -> bool:
    if str(task.claimed_by or "").strip():
        return False
    return task.current_stage in _DOWNSTREAM_RECOVERY_STAGES and task.status in _TERMINAL_ACTION_STATUSES


def _task_is_unscheduled_legacy(task: Any) -> bool:
    return task.current_stage != TaskStage.RECEIVED and is_unscheduled_active_task(task)


def _task_can_reprocess(task: Any) -> bool:
    if str(task.claimed_by or "").strip():
        return False
    if task.status in _TERMINAL_ACTION_STATUSES:
        return True
    return _task_is_unscheduled_legacy(task)



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

def render_task_detail(store: TaskStore, task_id: int, submission_store: Any | None = None) -> str:
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
    decision = decide_retry(task)
    retry_eligible = _task_can_retry(task, decision)
    downstream_actions_eligible = _task_can_use_downstream_actions(task)
    reprocess_eligible = _task_can_reprocess(task)
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
        recommendation = decision.reason if retry_eligible or task.status in _TERMINAL_ACTION_STATUSES else "请关注当前任务状态"
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


def _quality_repair_action(issue: QualityIssue, task: Any | None) -> str | None:
    if task is None:
        return None
    if issue.code in {"missing_dest", "missing_strm"} and _task_can_use_downstream_actions(task):
        return "restore"
    if issue.code in {"direct_strm", "unexpected_strm"} and _task_can_reprocess(task):
        return "reprocess"
    return None


def _apply_web_transition(
    store: TaskStore,
    task: Any,
    *,
    target_stage: TaskStage,
    target_event_message: str,
    initial_event_message: str | None = None,
    initial_event_stage: TaskStage | None = None,
    increment_retry: bool = False,
    metadata_patch: dict[str, Any] | None = None,
) -> bool:
    updated = store.compare_and_set_transition(
        task.id,
        task.current_stage,
        {task.status},
        require_unclaimed=True,
        target_stage=target_stage,
        target_status=TaskStatus.PENDING,
        target_event_message=target_event_message,
        initial_event_message=initial_event_message,
        initial_event_stage=initial_event_stage,
        increment_retry=increment_retry,
        metadata_patch=metadata_patch,
        metadata_delete_keys=("_defer_stage", "_defer_message", "_defer_count"),
        next_run_at=0,
        clear_errors=True,
        clear_claim=True,
    )
    return updated is not None


def render_quality_page(store: TaskStore) -> str:
    issues = scan_task_quality(store)
    report = format_task_quality_report(issues)
    grouped = _group_quality_issues(issues)
    tasks: dict[int, Any | None] = {}
    actionable_task_ids: set[int] = set()
    for issue in issues:
        if issue.task_id not in tasks:
            tasks[issue.task_id] = store.find_task(issue.task_id)
        if _quality_repair_action(issue, tasks[issue.task_id]):
            actionable_task_ids.add(issue.task_id)
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
        rows.append(
            f"""<article class="quality-row">
  <div class="quality-task">
    <div class="task-title"><a href="/task/{task_id}">#{task_id} {title}</a></div>
    <div class="quality-issue-counts">{counts_markup}</div>
  </div>
  <div class="quality-row-action"><span class="quality-total">共 {row['total']} 条</span><a class="button" href="/task/{task_id}">查看任务</a></div>
</article>"""
        )
    results_markup = (
        f'<div class="quality-list">{"".join(rows)}</div>'
        if rows
        else '<div class="empty-state is-healthy"><strong>未发现本地 STRM 问题</strong><p>当前本地文件巡检结果健康。</p></div>'
    )
    fix_action = ""
    if actionable_task_ids:
        fix_action = f"""<form method="post" action="/quality/fix" onsubmit="return confirm('将按巡检结果入队修复：缺失目录恢复 STRM，直链 STRM 从头重跑。确定继续？')">
        <button class="button-primary" type="submit">修复 {len(actionable_task_ids)} 个可处理任务</button>
      </form>"""
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

def fix_quality_issues(store: TaskStore) -> int:
    fixed_task_ids: set[int] = set()
    for issue in scan_task_quality(store):
        if issue.task_id in fixed_task_ids:
            continue
        task = store.find_task(issue.task_id)
        action = _quality_repair_action(issue, task)
        if action == "restore":
            if _apply_web_transition(
                store,
                task,
                target_stage=TaskStage.EMBY_CONFIRMED,
                target_event_message="Web 巡检恢复 STRM 已入队",
                initial_event_message="Web 巡检自动修复：恢复 STRM",
                initial_event_stage=TaskStage.EMBY_CONFIRMED,
                metadata_patch={
                    "retry_from_stage": task.current_stage.value,
                    "retry_stage": TaskStage.EMBY_CONFIRMED.value,
                },
            ):
                fixed_task_ids.add(task.id)
        elif action == "reprocess":
            if _apply_web_transition(
                store,
                task,
                target_stage=TaskStage.RECEIVED,
                target_event_message="Web 巡检自动修复：从头重跑",
                increment_retry=True,
                metadata_patch={
                    "retry_from_stage": task.current_stage.value,
                    "retry_stage": TaskStage.RECEIVED.value,
                    "force_reprocess": True,
                },
            ):
                fixed_task_ids.add(task.id)
    return len(fixed_task_ids)


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
    warning = not summary.enabled or cooldown_active or summary.problem_count > 0
    health_class = "is-warning" if warning else "is-healthy"
    health_title = "任务引擎运行正常" if summary.enabled else "任务引擎已停用"
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

class WebApp:
    def __init__(
        self,
        store: TaskStore,
        web_token: str = "",
        submission_store: Any | None = None,
        task_engine_enabled: bool = True,
    ):
        self.store = store
        self.web_token = web_token
        self.submission_store = submission_store
        self.task_engine_enabled = task_engine_enabled

    def _authorized(self, path: str, headers: dict[str, str]) -> bool:
        if not self.web_token:
            return True
        query = parse_qs(urlparse(path).query)
        return query.get("token", [""])[0] == self.web_token or headers.get("X-Web-Token") == self.web_token

    def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        del body
        if not self._authorized(path, headers):
            return 403, {"Content-Type": "text/plain; charset=utf-8"}, b"Forbidden"
        parsed = urlparse(path)
        if method == "GET" and parsed.path == "/":
            page = render_task_list(self.store, task_engine_enabled=self.task_engine_enabled)
            return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")
        if method == "GET" and parsed.path == "/quality":
            return 200, {"Content-Type": "text/html; charset=utf-8"}, render_quality_page(self.store).encode("utf-8")
        if method == "POST" and parsed.path == "/quality/fix":
            fix_quality_issues(self.store)
            return 303, {"Location": "/quality"}, b""
        if method == "GET" and parsed.path == "/health":
            page = render_health_page(self.store, task_engine_enabled=self.task_engine_enabled)
            return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode("utf-8")
        if method == "POST" and parsed.path == "/history/clear":
            self.store.clear_finished_tasks()
            return 303, {"Location": "/"}, b""
        if method == "GET" and parsed.path.startswith("/task/"):
            task_id = parse_task_id_from_path(parsed.path)
            if task_id is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            return 200, {"Content-Type": "text/html; charset=utf-8"}, render_task_detail(self.store, task_id, self.submission_store).encode("utf-8")
        task_action = parse_task_action_path(parsed.path) if method == "POST" else None
        if task_action is not None:
            task_id, action = task_action
            task = self.store.find_task(task_id)
            if task and action == "emby" and _task_can_use_downstream_actions(task):
                _apply_web_transition(
                    self.store,
                    task,
                    target_stage=TaskStage.EMBY_CONFIRMED,
                    target_event_message="Web 触发 Emby 检查",
                )
            elif task and action == "restore" and _task_can_use_downstream_actions(task):
                _apply_web_transition(
                    self.store,
                    task,
                    target_stage=TaskStage.EMBY_CONFIRMED,
                    target_event_message="Web STRM 恢复已入队",
                    initial_event_message="Web 触发 STRM 恢复",
                    initial_event_stage=TaskStage.EMBY_CONFIRMED,
                    metadata_patch={
                        "retry_from_stage": task.current_stage.value,
                        "retry_stage": TaskStage.EMBY_CONFIRMED.value,
                    },
                )
            elif task and action == "reprocess" and _task_can_reprocess(task):
                _apply_web_transition(
                    self.store,
                    task,
                    target_stage=TaskStage.RECEIVED,
                    target_event_message="Web 触发从头重跑",
                    increment_retry=True,
                    metadata_patch={
                        "retry_from_stage": task.current_stage.value,
                        "retry_stage": TaskStage.RECEIVED.value,
                        "force_reprocess": True,
                    },
                )
            elif task and action == "retry":
                decision = decide_retry(task)
                if _task_can_retry(task, decision):
                    target_stage = decision.stage or task.current_stage
                    if target_stage in {TaskStage.NEEDS_ACTION, TaskStage.FAILED}:
                        target_stage = TaskStage.RECEIVED
                    _apply_web_transition(
                        self.store,
                        task,
                        target_stage=target_stage,
                        target_event_message="手动重试已入队",
                        initial_event_message="手动触发重试",
                        increment_retry=True,
                    )
            return 303, {"Location": f"/task/{task_id}"}, b""
        if parsed.path.startswith("/task/"):
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not Found"


def start_web_server(
    store: TaskStore,
    host: str,
    port: int,
    web_token: str = "",
    submission_store: Any | None = None,
    task_engine_enabled: bool = True,
) -> ThreadingHTTPServer:
    app = WebApp(
        store,
        web_token=web_token,
        submission_store=submission_store,
        task_engine_enabled=task_engine_enabled,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._serve()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self._serve(self.rfile.read(length) if length else b"")

        def _serve(self, body: bytes = b""):
            status, headers, payload = app.handle_request(self.command, self.path, dict(self.headers), body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
