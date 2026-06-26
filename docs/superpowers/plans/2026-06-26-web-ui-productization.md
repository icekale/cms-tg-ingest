# Web UI Productization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current debug-like Web UI into a simple product-grade operations console while keeping existing Python server-rendering and route behavior.

**Architecture:** Keep all Web UI rendering in `app/web.py` for this iteration to avoid unnecessary file churn. Add small pure helper functions for local TaskStore summaries and reusable HTML components, then update the existing render functions. Tests remain in `tests/test_web_admin.py` and assert product-facing copy, structure, and existing POST behavior.

**Tech Stack:** Python stdlib `http.server`, server-rendered HTML/CSS strings, `unittest`, existing `TaskStore`, `TaskStatus`, `TaskStage`, `decide_retry`, `scan_task_quality`, and `format_taskstore_health` helpers.

---

## File Structure

- Modify `app/web.py`: add UI helper functions, product CSS, homepage console, detail/quality/health page styling, and summary helpers.
- Modify `tests/test_web_admin.py`: add/adjust assertions for product console structure while keeping all existing behavior tests.
- No new frontend dependency, build step, database schema, or route required.

---

### Task 1: Add Homepage Product Console Tests

**Files:**
- Modify: `tests/test_web_admin.py`

- [ ] **Step 1: Add failing homepage console assertions**

Update `test_render_task_list_folds_completed_history_by_default` so it also checks product console labels and empty/attention structure:

```python
self.assertIn("运行概览", html)
self.assertIn("需要关注", html)
self.assertIn("当前队列", html)
self.assertIn("处理中", html)
self.assertIn("需处理/失败", html)
self.assertIn("等待资源", html)
self.assertIn("已完成历史", html)
```

Update `test_render_task_list_contains_task_stage_and_error` so it verifies failed tasks are highlighted in the attention section:

```python
self.assertIn("需要关注", html)
self.assertIn("未找到 STRM", html)
self.assertIn("查看详情", html)
self.assertIn("status-failed", html)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_render_task_list_folds_completed_history_by_default tests.test_web_admin.WebAdminTests.test_render_task_list_contains_task_stage_and_error -v
```

Expected: FAIL because the current homepage does not contain the new product console headings/classes.

- [ ] **Step 3: Commit only if tests already fail for the expected reason is not needed**

Do not commit this isolated failing state. Continue to Task 2.

---

### Task 2: Implement Homepage Product Console

**Files:**
- Modify: `app/web.py`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Replace `_page` CSS with product-grade shell styles**

In `app/web.py`, replace the existing `<style>` content inside `_page` with a fuller but dependency-free CSS system. Keep the function signature unchanged:

```python
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
```

- [ ] **Step 2: Add summary helpers below `parse_task_id_from_path`**

Add these pure helpers:

```python
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
```

- [ ] **Step 3: Add task row renderer below helper functions**

Add:

```python
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
```

- [ ] **Step 4: Replace `render_task_list` body**

Replace the existing `render_task_list` implementation with:

```python
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
```

- [ ] **Step 5: Run focused homepage tests**

Run:

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_render_task_list_folds_completed_history_by_default tests.test_web_admin.WebAdminTests.test_render_task_list_contains_task_stage_and_error tests.test_web_admin.WebAdminTests.test_render_task_list_shows_lock_wait_reason -v
```

Expected: PASS.

- [ ] **Step 6: Commit homepage console**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "feat: productize web overview"
```

---

### Task 3: Productize Task Detail Page

**Files:**
- Modify: `app/web.py`
- Modify: `tests/test_web_admin.py`

- [ ] **Step 1: Add focused detail-page structure assertions**

In `test_render_task_detail_contains_event_timeline_and_retry_form`, add:

```python
self.assertIn("任务详情", html)
self.assertIn("任务摘要", html)
self.assertIn("处理时间线", html)
self.assertIn("detail-grid", html)
self.assertIn("timeline", html)
```

- [ ] **Step 2: Run detail test and verify failure**

Run:

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_render_task_detail_contains_event_timeline_and_retry_form -v
```

Expected: FAIL because current detail page does not use the new structure.

- [ ] **Step 3: Replace `render_task_detail` body construction**

Keep task lookup and lazy backfill behavior unchanged. Replace event item/action/body HTML inside `render_task_detail` with:

```python
events = store.list_events(task.id)
event_items = "".join(
    "<li>"
    f'<div class="task-meta"><code>{html.escape(event["stage"])}</code>{_badge(str(event["status"]), "")}</div>'
    f'<div class="task-message">{html.escape(event["message"])}</div>'
    "</li>"
    for event in events
)
decision = decide_retry(task)
retry_form = ""
if decision.action == RetryAction.RETRY_CURRENT_STAGE:
    retry_form = f'<form method="post" action="/task/{task.id}/retry"><button class="button-primary" type="submit">重试当前阶段</button></form>'
emby_form = f'<form method="post" action="/task/{task.id}/emby"><button type="submit">查 Emby</button></form>'
restore_form = f'<form method="post" action="/task/{task.id}/restore"><button type="submit">恢复 STRM</button></form>'
reprocess_form = f'<form method="post" action="/task/{task.id}/reprocess" onsubmit="return confirm(\'将从接收阶段重新执行该任务。确定继续？\')"><button class="button-danger" type="submit">从头重跑</button></form>'
media_library = str(task.metadata.get("emby_parent") or task.metadata.get("emby_refresh_library") or "-")
dest_path = str(task.metadata.get("dest_path") or task.metadata.get("emby_path") or "-")
error_summary = str(task.error_summary or "-")
body = f"""
<div class="topbar">
  <div>
    <p class="eyebrow">任务详情</p>
    <h1>#{task.id} {html.escape(task_display_title(task))}</h1>
  </div>
  {_badge(task.status.value, _status_class(task.status))}
</div>

<section class="panel">
  <div class="panel-header"><h2>任务摘要</h2></div>
  <div class="detail-grid">
    <div class="detail-item"><div class="detail-label">当前阶段</div><div class="detail-value">{html.escape(stage_display_name(task.current_stage))}</div></div>
    <div class="detail-item"><div class="detail-label">媒体库</div><div class="detail-value">{html.escape(media_library)}</div></div>
    <div class="detail-item"><div class="detail-label">路径</div><div class="detail-value">{html.escape(dest_path)}</div></div>
    <div class="detail-item"><div class="detail-label">资源/等待</div><div class="detail-value">{html.escape(_task_wait_message(task) or _task_lock_label(task))}</div></div>
    <div class="detail-item"><div class="detail-label">错误</div><div class="detail-value">{html.escape(error_summary)}</div></div>
    <div class="detail-item"><div class="detail-label">重试建议</div><div class="detail-value">{html.escape(decision.reason)}</div></div>
  </div>
  <div class="actions" style="margin-top: 14px;">
    {retry_form}
    {emby_form}
    {restore_form}
    {reprocess_form}
  </div>
</section>

<section class="panel">
  <div class="panel-header"><h2>处理时间线</h2></div>
  <ul class="timeline">{event_items}</ul>
</section>

<p><a class="button" href="/">返回运行概览</a></p>
"""
return _page("任务详情", body)
```

- [ ] **Step 4: Run focused detail tests**

Run:

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_render_task_detail_contains_event_timeline_and_retry_form tests.test_web_admin.WebAdminTests.test_render_task_title_prefers_folder_name_over_share_code tests.test_web_admin.WebAdminTests.test_task_detail_lazy_backfills_legacy_submission_by_id -v
```

Expected: PASS.

- [ ] **Step 5: Commit detail page**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "feat: productize task detail page"
```

---

### Task 4: Productize Quality and Health Pages

**Files:**
- Modify: `app/web.py`
- Modify: `tests/test_web_admin.py`

- [ ] **Step 1: Add page structure assertions**

In `test_quality_page_runs_local_taskstore_scan`, add:

```python
self.assertIn("本地质量巡检", html)
self.assertIn("diagnostic", html)
self.assertIn("不会扫描 115", html)
```

In `test_health_page_shows_local_taskstore_summary`, add:

```python
self.assertIn("本地队列健康", html)
self.assertIn("diagnostic", html)
self.assertIn("只展示本地 TaskStore 状态", html)
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_quality_page_runs_local_taskstore_scan tests.test_web_admin.WebAdminTests.test_health_page_shows_local_taskstore_summary -v
```

Expected: FAIL because the new explanatory copy/classes are not present yet.

- [ ] **Step 3: Replace `render_quality_page`**

Replace the function body with:

```python
def render_quality_page(store: TaskStore) -> str:
    report = format_task_quality_report(scan_task_quality(store))
    body = f"""
<div class="topbar">
  <div>
    <p class="eyebrow">本地质量巡检</p>
    <h1>TaskStore 本地轻量巡检</h1>
    <p class="subtle">只读取本地 TaskStore 和 STRM 文件路径，不会扫描 115。</p>
  </div>
  <a class="button" href="/">返回运行概览</a>
</div>
<section class="panel">
  <div class="panel-header">
    <div><h2>巡检结果</h2><p class="subtle">发现缺失目录或直链 STRM 时，可以入队执行安全修复。</p></div>
    <div class="actions">
      <form method="post" action="/quality/fix" onsubmit="return confirm('将按巡检结果入队修复：缺失目录恢复 STRM，直链 STRM 从头重跑。确定继续？')">
        <button class="button-primary" type="submit">修复全部巡检问题</button>
      </form>
    </div>
  </div>
  <pre class="diagnostic">{html.escape(report)}</pre>
</section>
"""
    return _page("质量巡检", body)
```

- [ ] **Step 4: Replace `render_health_page`**

Replace the function body with:

```python
def render_health_page(store: TaskStore) -> str:
    report = format_taskstore_health(store, enabled=True)
    body = f"""
<div class="topbar">
  <div>
    <p class="eyebrow">本地队列健康</p>
    <h1>TaskStore 本地健康</h1>
    <p class="subtle">只展示本地 TaskStore 状态，不会主动请求 115、CMS 或 Emby。</p>
  </div>
  <a class="button" href="/">返回运行概览</a>
</div>
<section class="panel">
  <div class="panel-header"><h2>健康报告</h2></div>
  <pre class="diagnostic">{html.escape(report)}</pre>
</section>
"""
    return _page("本地健康", body)
```

- [ ] **Step 5: Run quality and health tests**

Run:

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_quality_page_runs_local_taskstore_scan tests.test_web_admin.WebAdminTests.test_quality_fix_endpoint_restores_missing_dest_and_reprocesses_bad_strm tests.test_web_admin.WebAdminTests.test_health_page_shows_local_taskstore_summary tests.test_web_admin.WebAdminTests.test_health_page_shows_taskstore_wait_reason tests.test_web_admin.WebAdminTests.test_health_page_limits_wait_details_and_reports_overflow tests.test_web_admin.WebAdminTests.test_health_page_shows_lock_wait_summary -v
```

Expected: PASS.

- [ ] **Step 6: Commit quality/health pages**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "feat: productize web diagnostics"
```

---

### Task 5: Full Verification and Cleanup

**Files:**
- Modify if needed: `app/web.py`
- Modify if needed: `tests/test_web_admin.py`

- [ ] **Step 1: Run Web admin tests**

Run:

```sh
python3 -m unittest tests.test_web_admin -v
```

Expected: all tests pass.

- [ ] **Step 2: Run full suite with ResourceWarning as error**

Run:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Inspect generated homepage HTML manually**

Run a small render smoke test:

```sh
python3 - <<'PY'
import tempfile
from pathlib import Path
from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore
from app.web import render_task_list
with tempfile.TemporaryDirectory() as tmp:
    store = TaskStore(Path(tmp) / 'tasks.db')
    task = store.upsert_task('abc', '', 'https://115cdn.com/s/abc')
    store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, 'failed', title='示例电影', error_summary='未找到 STRM')
    html = render_task_list(store)
    assert 'cms-tg-ingest 运行概览' in html
    assert '示例电影' in html
    assert '未找到 STRM' in html
print('homepage smoke ok')
PY
```

Expected: `homepage smoke ok`.

- [ ] **Step 4: Check git diff for accidental backend changes**

Run:

```sh
git diff --stat HEAD~3..HEAD
```

Expected: only `app/web.py` and `tests/test_web_admin.py` changed after the design commit sequence.

- [ ] **Step 5: Commit final cleanup if any changes were made**

If Step 1-4 required additional edits:

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "test: verify productized web ui"
```

If no additional edits were needed, do not create an empty commit.
