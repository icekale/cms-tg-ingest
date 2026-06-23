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
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; background: #f7f7f8; color: #161616; }}
a {{ color: #0b63ce; text-decoration: none; }}
table {{ border-collapse: collapse; width: 100%; background: white; }}
th, td {{ border-bottom: 1px solid #e7e7e8; padding: 10px; text-align: left; }}
.card {{ background: white; border: 1px solid #e7e7e8; border-radius: 12px; padding: 16px; margin: 12px 0; }}
.error {{ color: #b42318; }}
button {{ padding: 8px 12px; border: 0; border-radius: 8px; background: #0b63ce; color: white; }}
code {{ background: #eee; padding: 2px 4px; border-radius: 4px; }}
.actions form {{ display: inline-block; margin: 0 8px 8px 0; }}
</style>
</head>
<body>
{body}
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


def render_task_list(store: TaskStore) -> str:
    rows = []
    for task in store.list_recent_tasks(limit=100):
        title = task_display_title(task)
        lock_label = _task_lock_label(task)
        rows.append(
            "<tr>"
            f'<td><a href="/task/{task.id}">#{task.id}</a></td>'
            f"<td>{html.escape(title)}</td>"
            f"<td>{html.escape(stage_display_name(task.current_stage))}</td>"
            f"<td>{html.escape(task.status.value)}</td>"
            f"<td>{html.escape(lock_label)}</td>"
            f'<td class="error">{html.escape(task.error_summary)}</td>'
            "</tr>"
        )
    body = (
        "<h1>cms-tg-ingest 任务</h1>"
        '<div class="actions">'
        '<a href="/quality">TaskStore 本地轻量巡检</a> '
        '<a href="/health">TaskStore 本地健康</a> '
        '<form method="post" action="/history/clear" onsubmit="return confirm(\'只清除已结束任务记录，不删除文件。确定继续？\')">'
        '<button type="submit">清除历史记录</button></form>'
        "</div>"
        "<table><thead><tr><th>ID</th><th>标题</th><th>阶段</th><th>状态</th><th>资源锁</th><th>错误</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    return _page("任务列表", body)


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
