from __future__ import annotations

import html
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import RetryAction, TaskStage, TaskStatus
from .task_engine import decide_retry, stage_display_name
from .task_bridge import sync_task_from_submission
from .task_health import format_taskstore_health
from .quality import format_task_quality_report, scan_task_quality
from .task_store import TaskStore


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f5f7fb;
  --surface: #ffffff;
  --surface-muted: #f8fafc;
  --border: #dfe5ee;
  --border-soft: #edf1f6;
  --text: #111827;
  --muted: #64748b;
  --muted-strong: #475569;
  --primary: #2563eb;
  --primary-dark: #1d4ed8;
  --success-bg: #dcfce7;
  --success-text: #166534;
  --warning-bg: #fef3c7;
  --warning-text: #92400e;
  --danger-bg: #fee2e2;
  --danger-text: #991b1b;
  --info-bg: #dbeafe;
  --info-text: #1e40af;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  line-height: 1.5;
}}
a {{ color: var(--primary); text-decoration: none; }}
a:hover {{ color: var(--primary-dark); text-decoration: underline; }}
.shell {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 40px; }}
.topbar {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }}
.eyebrow {{ color: var(--muted); font-size: 13px; margin: 0 0 4px; }}
h1 {{ font-size: 28px; line-height: 1.2; margin: 0; letter-spacing: -0.02em; }}
h2 {{ font-size: 18px; margin: 0; }}
p {{ margin: 0; }}
.subtle {{ color: var(--muted); }}
.panel {{ background: var(--surface); border: 1px solid var(--border); border-radius: 18px; padding: 18px; margin: 14px 0; }}
.panel-header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }}
.stats-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }}
.stat-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 16px; }}
.stat-label {{ color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
.stat-value {{ font-size: 28px; line-height: 1; font-weight: 700; letter-spacing: -0.02em; }}
.badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 650; white-space: nowrap; }}
.status-succeeded, .status-healthy {{ background: var(--success-bg); color: var(--success-text); }}
.status-running, .status-pending, .status-busy {{ background: var(--info-bg); color: var(--info-text); }}
.status-needs_action, .status-attention {{ background: var(--warning-bg); color: var(--warning-text); }}
.status-failed {{ background: var(--danger-bg); color: var(--danger-text); }}
.task-list {{ display: grid; gap: 10px; }}
.task-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 14px; border: 1px solid var(--border-soft); border-radius: 14px; background: var(--surface-muted); }}
.task-title {{ font-weight: 650; margin-bottom: 4px; overflow-wrap: anywhere; }}
.task-meta {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 13px; }}
.task-message {{ margin-top: 6px; color: var(--muted-strong); font-size: 13px; overflow-wrap: anywhere; }}
.task-message.error {{ color: var(--danger-text); }}
.empty-state {{ padding: 22px; text-align: center; color: var(--muted); background: var(--surface-muted); border: 1px dashed var(--border); border-radius: 14px; }}
.actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.actions form {{ display: inline-block; margin: 0; }}
.button, button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 36px; padding: 8px 12px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface); color: var(--text); font: inherit; font-weight: 650; cursor: pointer; }}
.button:hover, button:hover {{ border-color: #cbd5e1; text-decoration: none; }}
.button-primary {{ border-color: var(--primary); background: var(--primary); color: white; }}
.button-danger {{ border-color: #fecaca; background: var(--danger-bg); color: var(--danger-text); }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
th, td {{ border-bottom: 1px solid var(--border-soft); padding: 11px 10px; text-align: left; vertical-align: top; }}
th {{ color: var(--muted); font-size: 12px; font-weight: 700; }}
code {{ background: #eef2f7; padding: 2px 5px; border-radius: 6px; }}
.diagnostic {{ margin: 0; padding: 16px; border-radius: 14px; background: #0f172a; color: #e5edf8; overflow: auto; font-size: 13px; line-height: 1.6; }}
.detail-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.detail-item {{ background: var(--surface-muted); border: 1px solid var(--border-soft); border-radius: 12px; padding: 12px; }}
.detail-label {{ color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
.detail-value {{ overflow-wrap: anywhere; }}
.timeline {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
.timeline li {{ padding: 12px; border: 1px solid var(--border-soft); border-radius: 12px; background: var(--surface-muted); }}
@media (max-width: 760px) {{
  .shell {{ width: min(100% - 20px, 1180px); padding-top: 18px; }}
  .topbar {{ display: grid; }}
  .stats-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .task-row {{ grid-template-columns: 1fr; }}
  .detail-grid {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<main class="shell">
{body}
</main>
</body>
</html>"""

def _task_lock_label(task: Any) -> str:
    reason = str(task.metadata.get("_lock_reason") or "").strip()
    if not reason:
        return "-"
    if task.metadata.get("_lock_waiting"):
        owner = str(task.metadata.get("_lock_owner_task_id") or "").strip()
        return f"等待资源锁: #{owner} {reason}" if owner else f"等待资源锁: {reason}"
    return reason


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
    parts = str(path or "").strip("/").split("/")
    if len(parts) < 2 or parts[0] != "task":
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None



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


def _task_counts(tasks: list[Any]) -> dict[str, int]:
    return {
        "active": sum(1 for task in tasks if task.status in {TaskStatus.RUNNING, TaskStatus.PENDING}),
        "problem": sum(1 for task in tasks if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION}),
        "waiting": sum(1 for task in tasks if _task_wait_message(task)),
        "completed": sum(1 for task in tasks if task.status == TaskStatus.SUCCEEDED),
    }


def _overall_status(counts: dict[str, int]) -> tuple[str, str]:
    if counts["problem"]:
        return "需要关注", "status-attention"
    if counts["active"] or counts["waiting"]:
        return "正在处理", "status-busy"
    return "运行正常", "status-healthy"


def _render_task_row(task: Any, *, compact: bool = False) -> str:
    title = task_display_title(task)
    stage = stage_display_name(task.current_stage)
    status_label = task.status.value
    message = _task_issue_message(task)
    message_class = " error" if task.status == TaskStatus.FAILED else ""
    message_html = f'<div class="task-message{message_class}">{html.escape(message)}</div>' if message else ""
    detail_label = "查看详情" if compact else f"查看详情 #{task.id}"
    return (
        '<div class="task-row">'
        '<div>'
        f'<div class="task-title">{html.escape(title)}</div>'
        '<div class="task-meta">'
        f'<span>#{task.id}</span>'
        f'<span>{html.escape(stage)}</span>'
        f'{_badge(status_label, _status_class(task.status))}'
        '</div>'
        f'{message_html}'
        '</div>'
        f'<a class="button" href="/task/{task.id}">{detail_label}</a>'
        '</div>'
    )


def render_task_list(store: TaskStore) -> str:
    tasks = store.list_recent_tasks(limit=100)
    visible_tasks = [task for task in tasks if task.status != TaskStatus.SUCCEEDED]
    attention_tasks = [
        task
        for task in visible_tasks
        if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or _task_wait_message(task)
    ]
    counts = _task_counts(tasks)
    overall_label, overall_class = _overall_status(counts)

    attention_html = "".join(_render_task_row(task, compact=True) for task in attention_tasks[:8])
    if not attention_html:
        attention_html = '<div class="empty-state">暂无需要处理的任务</div>'

    queue_rows = "".join(_render_task_row(task) for task in visible_tasks[:25])
    if not queue_rows:
        queue_rows = '<div class="empty-state">当前没有运行中或失败任务</div>'

    body = f"""
<div class="topbar">
  <div>
    <p class="eyebrow">Telegram 115 入库外挂 / 自分享 STRM 工作流</p>
    <h1>cms-tg-ingest 运行概览</h1>
  </div>
  {_badge(overall_label, overall_class)}
</div>

<section class="stats-grid" aria-label="任务概览">
  <div class="stat-card"><div class="stat-label">处理中</div><div class="stat-value">{counts['active']}</div></div>
  <div class="stat-card"><div class="stat-label">需处理/失败</div><div class="stat-value">{counts['problem']}</div></div>
  <div class="stat-card"><div class="stat-label">等待资源</div><div class="stat-value">{counts['waiting']}</div></div>
  <div class="stat-card"><div class="stat-label">已完成历史</div><div class="stat-value">{counts['completed']}</div></div>
</section>

<section class="panel">
  <div class="panel-header">
    <div><h2>需要关注</h2><p class="subtle">失败、需人工处理、等待资源锁或等待本地文件稳定的任务会出现在这里。</p></div>
  </div>
  <div class="task-list">{attention_html}</div>
</section>

<section class="panel">
  <div class="panel-header">
    <div><h2>当前队列</h2><p class="subtle">活跃/问题任务 {len(visible_tasks)} 个；已完成历史 {counts['completed']} 个。已完成任务默认折叠。</p></div>
    <div class="actions">
      <a class="button" href="/quality">本地轻量巡检</a>
      <a class="button" href="/health">本地健康</a>
      <form method="post" action="/history/clear" onsubmit="return confirm('只清除已结束任务记录，不删除文件。确定继续？')">
        <button class="button-danger" type="submit">清除历史记录</button>
      </form>
    </div>
  </div>
  <div class="task-list">{queue_rows}</div>
</section>
"""
    return _page("运行概览", body)

def render_task_detail(store: TaskStore, task_id: int, submission_store: Any | None = None) -> str:
    task = store.find_task(task_id)
    if not task and submission_store is not None and hasattr(submission_store, "find_by_id"):
        row = submission_store.find_by_id(task_id)
        if row:
            task = sync_task_from_submission(store, row, message="打开详情页时懒回填旧记录")
    if not task:
        return _page("任务不存在", "<h1>任务不存在</h1>")
    events = store.list_events(task.id)
    event_items = "".join(
        f"<li><code>{html.escape(event['stage'])}</code> {html.escape(event['status'])} - {html.escape(event['message'])}</li>"
        for event in events
    )
    decision = decide_retry(task)
    retry_form = ""
    if decision.action == RetryAction.RETRY_CURRENT_STAGE:
        retry_form = f'<form method="post" action="/task/{task.id}/retry"><button type="submit">重试当前阶段</button></form>'
    emby_form = f'<form method="post" action="/task/{task.id}/emby"><button type="submit">查 Emby</button></form>'
    restore_form = f'<form method="post" action="/task/{task.id}/restore"><button type="submit">恢复 STRM</button></form>'
    reprocess_form = f'<form method="post" action="/task/{task.id}/reprocess"><button type="submit">从头重跑</button></form>'
    body = f"""
<h1>任务 #{task.id}</h1>
<div class="card">
<p>标题：{html.escape(task_display_title(task))}</p>
<p>阶段：{html.escape(stage_display_name(task.current_stage))}</p>
<p>状态：{html.escape(task.status.value)}</p>
<p class="error">错误：{html.escape(task.error_summary)}</p>
<p>媒体库：{html.escape(str(task.metadata.get("emby_parent") or task.metadata.get("emby_refresh_library") or "-"))}</p>
<p>路径：{html.escape(str(task.metadata.get("dest_path") or task.metadata.get("emby_path") or "-"))}</p>
<p>资源锁：{html.escape(_task_lock_label(task))}</p>
<p>重试建议：{html.escape(decision.reason)}</p>
<div class="actions">
{retry_form}
{emby_form}
{restore_form}
{reprocess_form}
</div>
</div>
<div class="card"><h2>时间线</h2><ul>{event_items}</ul></div>
<p><a href="/">返回任务列表</a></p>
"""
    return _page("任务详情", body)


def render_quality_page(store: TaskStore) -> str:
    report = format_task_quality_report(scan_task_quality(store))
    body = f"""
<h1>TaskStore 本地轻量巡检</h1>
<div class="card"><pre>{html.escape(report)}</pre></div>
<div class="actions">
<form method="post" action="/quality/fix" onsubmit="return confirm('将按巡检结果入队修复：缺失目录恢复 STRM，直链 STRM 从头重跑。确定继续？')">
<button type="submit">修复全部巡检问题</button>
</form>
</div>
<p>只读取本地 TaskStore 和 STRM 文件路径，不扫描 115。</p>
<p><a href="/">返回任务列表</a></p>
"""
    return _page("质量巡检", body)


def fix_quality_issues(store: TaskStore) -> int:
    fixed_task_ids: set[int] = set()
    for issue in scan_task_quality(store):
        if issue.task_id in fixed_task_ids:
            continue
        task = store.find_task(issue.task_id)
        if not task or task.status not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            continue
        if issue.code in {"missing_dest", "missing_strm"}:
            store.record_event(
                task.id,
                TaskStage.EMBY_CONFIRMED,
                TaskStatus.PENDING,
                "Web 巡检自动修复：恢复 STRM",
                metadata_patch={"retry_from_stage": task.current_stage.value, "retry_stage": TaskStage.EMBY_CONFIRMED.value},
                clear_claim=True,
            )
            store.enqueue_task(task.id, TaskStage.EMBY_CONFIRMED, message="Web 巡检恢复 STRM 已入队", next_run_at=0)
            fixed_task_ids.add(task.id)
        elif issue.code in {"direct_strm", "unexpected_strm"}:
            store.reprocess_task(task.id, message="Web 巡检自动修复：从头重跑", next_run_at=0)
            fixed_task_ids.add(task.id)
    return len(fixed_task_ids)


def render_health_page(store: TaskStore) -> str:
    report = format_taskstore_health(store, enabled=True)
    body = f"""
<h1>TaskStore 本地健康</h1>
<div class="card"><pre>{html.escape(report)}</pre></div>
<p>只读取本地 TaskStore 队列状态，不扫描 115。</p>
<p><a href="/">返回任务列表</a></p>
"""
    return _page("健康检查", body)


class WebApp:
    def __init__(self, store: TaskStore, web_token: str = "", submission_store: Any | None = None):
        self.store = store
        self.web_token = web_token
        self.submission_store = submission_store

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
            return 200, {"Content-Type": "text/html; charset=utf-8"}, render_task_list(self.store).encode("utf-8")
        if method == "GET" and parsed.path == "/quality":
            return 200, {"Content-Type": "text/html; charset=utf-8"}, render_quality_page(self.store).encode("utf-8")
        if method == "POST" and parsed.path == "/quality/fix":
            fix_quality_issues(self.store)
            return 303, {"Location": "/quality"}, b""
        if method == "GET" and parsed.path == "/health":
            return 200, {"Content-Type": "text/html; charset=utf-8"}, render_health_page(self.store).encode("utf-8")
        if method == "POST" and parsed.path == "/history/clear":
            self.store.clear_finished_tasks()
            return 303, {"Location": "/"}, b""
        if method == "GET" and parsed.path.startswith("/task/"):
            task_id = parse_task_id_from_path(parsed.path)
            if task_id is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            return 200, {"Content-Type": "text/html; charset=utf-8"}, render_task_detail(self.store, task_id, self.submission_store).encode("utf-8")
        if method == "POST" and parsed.path.startswith("/task/") and parsed.path.endswith("/emby"):
            task_id = parse_task_id_from_path(parsed.path)
            if task_id is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            if self.store.find_task(task_id):
                self.store.enqueue_task(task_id, TaskStage.EMBY_CONFIRMED, message="Web 触发 Emby 检查", next_run_at=0)
            return 303, {"Location": f"/task/{task_id}"}, b""
        if method == "POST" and parsed.path.startswith("/task/") and parsed.path.endswith("/restore"):
            task_id = parse_task_id_from_path(parsed.path)
            if task_id is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            task = self.store.find_task(task_id)
            if task:
                self.store.record_event(
                    task_id,
                    TaskStage.EMBY_CONFIRMED,
                    TaskStatus.PENDING,
                    "Web 触发 STRM 恢复",
                    metadata_patch={"retry_from_stage": task.current_stage.value, "retry_stage": TaskStage.EMBY_CONFIRMED.value},
                    clear_claim=True,
                )
                self.store.enqueue_task(task_id, TaskStage.EMBY_CONFIRMED, message="Web STRM 恢复已入队", next_run_at=0)
            return 303, {"Location": f"/task/{task_id}"}, b""
        if method == "POST" and parsed.path.startswith("/task/") and parsed.path.endswith("/reprocess"):
            task_id = parse_task_id_from_path(parsed.path)
            if task_id is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            if self.store.find_task(task_id):
                self.store.reprocess_task(task_id, message="Web 触发从头重跑", next_run_at=0)
            return 303, {"Location": f"/task/{task_id}"}, b""
        if method == "POST" and parsed.path.startswith("/task/") and parsed.path.endswith("/retry"):
            task_id = parse_task_id_from_path(parsed.path)
            if task_id is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            task = self.store.find_task(task_id)
            if task:
                decision = decide_retry(task)
                if decision.action == RetryAction.RETRY_CURRENT_STAGE:
                    target_stage = decision.stage or task.current_stage
                    if target_stage in {TaskStage.NEEDS_ACTION, TaskStage.FAILED}:
                        target_stage = TaskStage.RECEIVED
                    self.store.record_event(
                        task_id,
                        task.current_stage,
                        TaskStatus.PENDING,
                        "手动触发重试",
                        increment_retry=True,
                        clear_claim=True,
                    )
                    self.store.enqueue_task(task_id, target_stage, message="手动重试已入队", next_run_at=0)
            return 303, {"Location": f"/task/{task_id}"}, b""
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not Found"


def start_web_server(
    store: TaskStore,
    host: str,
    port: int,
    web_token: str = "",
    submission_store: Any | None = None,
) -> ThreadingHTTPServer:
    app = WebApp(store, web_token=web_token, submission_store=submission_store)

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
