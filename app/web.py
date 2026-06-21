from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import RetryAction, TaskStatus
from .task_engine import decide_retry, stage_display_name
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
</style>
</head>
<body>
{body}
</body>
</html>"""


def render_task_list(store: TaskStore) -> str:
    rows = []
    for task in store.list_recent_tasks(limit=100):
        title = task.title or task.share_code
        rows.append(
            "<tr>"
            f'<td><a href="/task/{task.id}">#{task.id}</a></td>'
            f"<td>{html.escape(title)}</td>"
            f"<td>{html.escape(stage_display_name(task.current_stage))}</td>"
            f"<td>{html.escape(task.status.value)}</td>"
            f'<td class="error">{html.escape(task.error_summary)}</td>'
            "</tr>"
        )
    body = (
        "<h1>cms-tg-ingest 任务</h1>"
        "<table><thead><tr><th>ID</th><th>标题</th><th>阶段</th><th>状态</th><th>错误</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    return _page("任务列表", body)


def render_task_detail(store: TaskStore, task_id: int) -> str:
    task = store.find_task(task_id)
    if not task:
        return _page("任务不存在", "<h1>任务不存在</h1>")
    events = store.list_events(task_id)
    event_items = "".join(
        f"<li><code>{html.escape(event['stage'])}</code> {html.escape(event['status'])} - {html.escape(event['message'])}</li>"
        for event in events
    )
    decision = decide_retry(task)
    retry_form = ""
    if decision.action == RetryAction.RETRY_CURRENT_STAGE:
        retry_form = f'<form method="post" action="/task/{task.id}/retry"><button type="submit">重试当前阶段</button></form>'
    body = f"""
<h1>任务 #{task.id}</h1>
<div class="card">
<p>标题：{html.escape(task.title or task.share_code)}</p>
<p>阶段：{html.escape(stage_display_name(task.current_stage))}</p>
<p>状态：{html.escape(task.status.value)}</p>
<p class="error">错误：{html.escape(task.error_summary)}</p>
<p>重试建议：{html.escape(decision.reason)}</p>
{retry_form}
</div>
<div class="card"><h2>时间线</h2><ul>{event_items}</ul></div>
<p><a href="/">返回任务列表</a></p>
"""
    return _page("任务详情", body)


class WebApp:
    def __init__(self, store: TaskStore, web_token: str = ""):
        self.store = store
        self.web_token = web_token

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
        if method == "GET" and parsed.path.startswith("/task/"):
            task_id = int(parsed.path.split("/")[2])
            return 200, {"Content-Type": "text/html; charset=utf-8"}, render_task_detail(self.store, task_id).encode("utf-8")
        if method == "POST" and parsed.path.startswith("/task/") and parsed.path.endswith("/retry"):
            task_id = int(parsed.path.split("/")[2])
            task = self.store.find_task(task_id)
            if task:
                self.store.record_event(
                    task_id,
                    task.current_stage,
                    TaskStatus.PENDING,
                    "手动触发重试",
                    increment_retry=True,
                    next_run_at=0,
                    clear_claim=True,
                )
            return 303, {"Location": f"/task/{task_id}"}, b""
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"Not Found"


def start_web_server(store: TaskStore, host: str, port: int, web_token: str = "") -> ThreadingHTTPServer:
    app = WebApp(store, web_token=web_token)

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
