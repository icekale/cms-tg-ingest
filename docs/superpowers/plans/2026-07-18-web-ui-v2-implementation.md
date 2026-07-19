# Web UI V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 Python SSR Web 管理页统一重构为状态优先、任务去重、诊断可扫读且移动端可用的 CMS 入库助手。

**Architecture:** 保持 `WebApp`、现有 GET/POST 路由和 TaskStore 数据边界不变，仅重写 `app/web.py` 的呈现辅助函数、HTML 和 CSS。首页、任务详情、质量巡检和健康页共享同一页面框架；所有状态继续来自本地 TaskStore、任务事件和 STRM 文件，不新增外部请求。

**Tech Stack:** Python 3 标准库、服务端渲染 HTML/CSS、原生 `<details>`/表单/`confirm()`、`unittest`、Playwright 视觉验收。

---

## File Map

- Modify: `app/web.py` — 共享页面框架、阶段进度、首页、任务详情、巡检和健康页渲染。
- Modify: `tests/test_web_admin.py` — 新页面结构、互斥任务分类、诊断聚合、操作安全和兼容性测试。
- Reference: `app/models.py` — `TaskStage`、`TaskStatus` 的稳定枚举，不修改。
- Reference: `app/task_health.py` — 复用 `build_task_health` 和 `format_task_health`，不修改。
- Reference: `app/quality.py` — 复用 `QualityIssue`、`scan_task_quality` 和原始报告格式化，不修改。
- Reference: `docs/superpowers/specs/2026-07-18-web-ui-v2-design.md` — 已批准的行为和视觉规范。

## Task 1: Shared Product Shell And Stage Presentation

**Files:**
- Modify: `app/web.py:11-123`
- Modify: `tests/test_web_admin.py:1-12`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write failing tests for shared navigation and stage labels**

Add `render_health_page` and `render_quality_page` to the imports, then add these tests:

```python
def test_pages_share_product_navigation(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
        store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "organizing", title="示例电影")

        pages = {
            "overview": render_task_list(store),
            "quality": render_quality_page(store),
            "health": render_health_page(store),
            "task": render_task_detail(store, task.id),
        }

        for markup in pages.values():
            self.assertIn("CMS 入库助手", markup)
            self.assertIn('href="/"', markup)
            self.assertIn('href="/quality"', markup)
            self.assertIn('href="/health"', markup)
            self.assertIn('class="app-nav"', markup)
        self.assertIn('aria-current="page">运行概览', pages["overview"])
        self.assertIn('aria-current="page">质量巡检', pages["quality"])
        self.assertIn('aria-current="page">本地健康', pages["health"])

def test_task_detail_renders_eight_user_facing_phases(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
        store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.SUCCEEDED, "received")
        store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.SUCCEEDED, "organized")
        store.record_event(task.id, TaskStage.RECOGNIZING, TaskStatus.RUNNING, "recognizing")

        markup = render_task_detail(store, task.id)

        for label in ("接收", "CMS 整理", "分类识别", "建分享", "分享 STRM", "移动入库", "Emby 确认", "清理完成"):
            self.assertIn(label, markup)
        self.assertEqual(markup.count('class="phase-step'), 8)
        self.assertIn('class="phase-step is-current"', markup)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```sh
python3 -m unittest \
  tests.test_web_admin.WebAdminTests.test_pages_share_product_navigation \
  tests.test_web_admin.WebAdminTests.test_task_detail_renders_eight_user_facing_phases -v
```

Expected: FAIL because the shared `.app-nav`, active navigation state, and `.phase-step` markup do not exist.

- [ ] **Step 3: Implement the shared page shell**

Add `_navigation` and change `_page` to accept an active navigation key:

```python
_NAV_ITEMS = (
    ("overview", "/", "运行概览"),
    ("quality", "/quality", "质量巡检"),
    ("health", "/health", "本地健康"),
)


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


```

Change the existing signature to `def _page(title: str, body: str, *, active: str = "") -> str`, set `navigation = _navigation(active)` before returning the existing document string, and replace the current `<body>` block with:

```html
<body>
{navigation}
<main class="shell">
{body}
</main>
</body>
```

Rewrite the existing CSS block in place. It must define these exact shared selectors used by later tasks: `.app-header`, `.app-header-inner`, `.app-brand`, `.app-nav`, `.page-heading`, `.status-strip`, `.metrics-grid`, `.metric`, `.workspace-grid`, `.panel`, `.panel-heading`, `.task-list`, `.task-row`, `.badge`, `.phase-track`, `.phase-step`, `.summary-grid`, `.timeline`, `.diagnostic-details`, `.danger-zone`, `.quality-summary`, `.quality-row`, `.health-grid`, `.empty-state`, `.button`, `.button-primary`, and `.button-danger`.

Use neutral surfaces, 8px-or-less radii, no gradients, no shadows on ordinary panels, `letter-spacing: 0`, visible `:focus-visible`, and a `@media (max-width: 760px)` block. Do not include external URLs.

- [ ] **Step 4: Implement the eight-phase helper**

Add the exact stage groups and render helper:

```python
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


def _event_stage(value: object) -> TaskStage | None:
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
        if current is not None and index < current:
            state = " is-done"
        elif current is not None and index == current:
            state = " is-done" if task.status == TaskStatus.SUCCEEDED else " is-current"
        steps.append(f'<div class="phase-step{state}"><i></i><span>{html.escape(label)}</span></div>')
    return f'<div class="phase-track" aria-label="任务处理进度">{"".join(steps)}</div>'
```

Use these exact return calls in the four renderers:

```python
return _page("运行概览", body, active="overview")
return _page("任务详情", body)
return _page("质量巡检", body, active="quality")
return _page("本地健康", body, active="health")
```

Task-not-found uses `_page("任务不存在", '<section class="empty-state"><h1>任务不存在</h1></section>')`.

- [ ] **Step 5: Run the focused tests and verify pass**

Run the same command from Step 2.

Expected: PASS.

- [ ] **Step 6: Commit the shared shell**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "feat: add shared web ui shell"
```

## Task 2: Deduplicated Operations Overview

**Files:**
- Modify: `app/web.py:201-306`
- Modify: `tests/test_web_admin.py:13-199`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write failing tests for mutually exclusive columns and overflow**

Add:

```python
def test_overview_deduplicates_attention_and_active_queue(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
        store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed", title="只在关注栏")
        pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
        store.record_event(
            pending.id,
            TaskStage.ORGANIZING,
            TaskStatus.PENDING,
            "waiting",
            title="只在队列栏",
            metadata_patch={"_defer_message": "等待 CMS 整理"},
            next_run_at=9999999999.0,
        )

        markup = render_task_list(store)

        self.assertEqual(markup.count("只在关注栏"), 1)
        self.assertEqual(markup.count("只在队列栏"), 1)
        attention = markup.split('data-section="attention"', 1)[1].split('data-section="queue"', 1)[0]
        queue = markup.split('data-section="queue"', 1)[1].split('data-section="maintenance"', 1)[0]
        self.assertIn("只在关注栏", attention)
        self.assertNotIn("只在队列栏", attention)
        self.assertIn("只在队列栏", queue)
        self.assertNotIn("只在关注栏", queue)

def test_overview_keeps_attention_overflow_accessible(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        for index in range(10):
            task = store.upsert_task(f"failed-{index}", "", f"https://115cdn.com/s/failed-{index}")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed", title=f"问题任务 {index}")

        markup = render_task_list(store)

        self.assertIn("查看其余 2 项", markup)
        self.assertIn("问题任务 0", markup)
        self.assertIn("问题任务 9", markup)
        self.assertIn("<details", markup)
```

- [ ] **Step 2: Run the new tests and verify failure**

```sh
python3 -m unittest \
  tests.test_web_admin.WebAdminTests.test_overview_deduplicates_attention_and_active_queue \
  tests.test_web_admin.WebAdminTests.test_overview_keeps_attention_overflow_accessible -v
```

Expected: FAIL because the current queue contains all non-succeeded tasks and overflow tasks are discarded.

- [ ] **Step 3: Implement mutually exclusive task selection**

Use these predicates and lists in `render_task_list`:

```python
def _is_attention_task(task: Any) -> bool:
    return task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or is_unscheduled_active_task(task)


def _is_queue_task(task: Any) -> bool:
    return is_dispatchable_active_task(task) and not _is_attention_task(task)


attention_tasks = [task for task in tasks if _is_attention_task(task)]
queue_tasks = [task for task in tasks if _is_queue_task(task)]
```

Render the first eight attention rows normally and all remaining rows inside:

```python
overflow_html = ""
if len(attention_tasks) > 8:
    remaining = "".join(_render_task_row(task, compact=True) for task in attention_tasks[8:])
    overflow_html = (
        '<details class="overflow-tasks">'
        f'<summary>查看其余 {len(attention_tasks) - 8} 项</summary>'
        f'<div class="task-list">{remaining}</div>'
        "</details>"
    )
```

Add `data-section="attention"`, `data-section="queue"`, and `data-section="maintenance"` to the three top-level sections so tests and accessibility tooling can distinguish them.

- [ ] **Step 4: Add status strip, metrics, phase progress, and maintenance area**

Import `build_task_health` from `app.task_health`. Build the local summary once per request:

```python
health = build_task_health(store, enabled=True)
cooldown_label = "115 风控冷却中" if health.p115_cooldown_until > time.time() else "115 未冷却"
engine_label = "任务引擎正常" if health.enabled else "任务引擎已停用"
```

The overview markup must contain:

- `.status-strip` with overall status, problem/active count, `engine_label`, and `cooldown_label`.
- `.metrics-grid` with the four existing counts.
- `.workspace-grid` with attention and queue panels.
- Queue task rows with `_render_phase_track(task, store.list_events(task.id))`.
- A separate maintenance section containing plain GET reload and the existing `/history/clear` POST form.

Keep the exact confirmation text `只清除已结束任务记录，不删除文件。确定继续？` and change the visible label to `清理已结束记录`.

- [ ] **Step 5: Run overview tests**

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests -k overview -v
python3 -m unittest \
  tests.test_web_admin.WebAdminTests.test_render_task_list_folds_completed_history_by_default \
  tests.test_web_admin.WebAdminTests.test_render_task_list_shows_lock_wait_reason \
  tests.test_web_admin.WebAdminTests.test_render_task_list_treats_unscheduled_running_task_as_attention_not_active -v
```

Expected: PASS after updating obsolete copy assertions to the approved V2 wording. Do not weaken endpoint or task-count assertions.

- [ ] **Step 6: Commit the overview**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "feat: redesign web operations overview"
```

## Task 3: Focused Task Detail And Safe Actions

**Files:**
- Modify: `app/web.py:308-386`
- Modify: `tests/test_web_admin.py:223-384`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write failing tests for detail hierarchy and action safety**

Add:

```python
def test_task_detail_prioritizes_recommendation_and_isolates_danger(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
        store.record_event(
            task.id,
            TaskStage.STRM_READY,
            TaskStatus.FAILED,
            "STRM missing",
            title="示例电影",
            error_summary="未找到 STRM",
            metadata_patch={"dest_path": "/library/示例电影", "emby_parent": "欧美电影"},
        )

        markup = render_task_detail(store, task.id)

        self.assertIn('class="incident-strip"', markup)
        self.assertIn('class="button button-primary"', markup)
        self.assertIn("重试当前阶段", markup)
        self.assertIn('<details class="diagnostic-details"', markup)
        self.assertIn('<details class="danger-zone"', markup)
        self.assertIn('action="/task/1/reprocess"', markup)
        self.assertIn("将从接收阶段重新执行该任务", markup)
        self.assertLess(markup.index("重试当前阶段"), markup.index("从头重跑"))

def test_task_detail_shows_recent_events_newest_first_and_folds_older_events(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
        for index in range(10):
            store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, f"event-{index}")

        markup = render_task_detail(store, task.id)

        self.assertLess(markup.index("event-9"), markup.index("event-8"))
        self.assertIn("查看更早事件", markup)
        self.assertIn("event-0", markup)
```

- [ ] **Step 2: Run the tests and verify failure**

```sh
python3 -m unittest \
  tests.test_web_admin.WebAdminTests.test_task_detail_prioritizes_recommendation_and_isolates_danger \
  tests.test_web_admin.WebAdminTests.test_task_detail_shows_recent_events_newest_first_and_folds_older_events -v
```

Expected: FAIL because all facts/actions have equal weight and events are oldest-first.

- [ ] **Step 3: Implement the incident strip and valid primary action**

Build the recommendation from the existing decision without changing workflow rules:

```python
decision = decide_retry(task)
primary_form = ""
if decision.action == RetryAction.RETRY_CURRENT_STAGE:
    primary_form = (
        f'<form method="post" action="/task/{task.id}/retry">'
        '<button class="button button-primary" type="submit">重试当前阶段</button></form>'
    )
incident_message = error_summary if error_summary != "-" else (wait_label if wait_label != "-" else decision.reason)
```

Render `.incident-strip` before the phase track. It contains the escaped incident message, escaped `decision.reason`, and `primary_form` only when valid. Keep “查 Emby” and “恢复 STRM” as secondary forms because their existing endpoints remain user-invoked recovery tools.

- [ ] **Step 4: Restructure summary, diagnostics, events, and danger zone**

The visible `.summary-grid` contains exactly these labels: 当前阶段、目标媒体库、为什么慢、执行耗时、115 调用、推荐操作.

Put destination path, error summary, per-stage elapsed summary, and per-stage 115 counts inside:

```html
<details class="diagnostic-details">
  <summary>技术详情与文件路径</summary>
  <div class="technical-grid">
    <div><span>目标路径</span><strong>{html.escape(dest_path)}</strong></div>
    <div><span>错误</span><strong>{html.escape(error_summary)}</strong></div>
    <div><span>各阶段耗时</span><strong>{html.escape(stage_elapsed_summary)}</strong></div>
    <div><span>各阶段 115 调用</span><strong>{html.escape(stage_p115_summary)}</strong></div>
  </div>
</details>
```

Reverse events for display, keep the newest eight visible, and fold older events:

```python
newest_events = list(reversed(events))
recent_events = newest_events[:8]
older_events = newest_events[8:]
```

Render the reprocess form only inside:

```html
<details class="danger-zone">
  <summary>高风险操作</summary>
  <p>从接收阶段重新执行，可能再次调用 115 和 CMS。</p>
  {reprocess_form}
</details>
```

- [ ] **Step 5: Run task detail and endpoint regression tests**

```sh
python3 -m unittest \
  tests.test_web_admin.WebAdminTests.test_render_task_detail_contains_event_timeline_and_retry_form \
  tests.test_web_admin.WebAdminTests.test_render_task_list_and_detail_show_observability_summary \
  tests.test_web_admin.WebAdminTests.test_retry_endpoint_enqueues_failed_stage_for_worker_claim \
  tests.test_web_admin.WebAdminTests.test_reprocess_endpoint_requeues_task_from_received_stage \
  tests.test_web_admin.WebAdminTests.test_restore_endpoint_enqueues_emby_confirmation_restore_path \
  tests.test_web_admin.WebAdminTests.test_emby_endpoint_enqueues_emby_confirmation_stage -v
```

Expected: PASS. Update only presentation assertions; endpoint state-transition assertions remain unchanged.

- [ ] **Step 6: Commit the task detail**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "feat: focus web task detail actions"
```

## Task 4: Structured Quality And Health Pages

**Files:**
- Modify: `app/web.py:388-454`
- Modify: `tests/test_web_admin.py:385-645`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write failing tests for quality aggregation and structured health**

Add:

```python
def test_quality_page_groups_file_issues_by_task(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = TaskStore(root / "tasks.db")
        dest = root / "direct"
        dest.mkdir()
        (dest / "one.strm").write_text("https://115.com/d/one.mkv", encoding="utf-8")
        (dest / "two.strm").write_text("https://115.com/d/two.mkv", encoding="utf-8")
        task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
        store.record_event(
            task.id,
            TaskStage.MOVED,
            TaskStatus.SUCCEEDED,
            "moved",
            title="直链电影",
            metadata_patch={"dest_path": str(dest), "own_share_code": "ownabc"},
        )

        markup = render_quality_page(store)

        self.assertIn("问题总数", markup)
        self.assertIn("受影响任务", markup)
        self.assertEqual(markup.count('class="quality-row"'), 1)
        self.assertIn("直链 STRM", markup)
        self.assertIn(">2<", markup)
        self.assertIn('href="/task/1"', markup)
        self.assertIn("查看完整原始报告（2 条）", markup)
        self.assertIn(str(dest / "one.strm"), markup)

def test_health_page_renders_structured_local_summary(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("cooldown", "", "https://115cdn.com/s/cooldown")
        store.record_event(
            task.id,
            TaskStage.ORGANIZING,
            TaskStatus.RUNNING,
            "115 风控冷却中",
            title="冷却电影",
            metadata_patch={"p115_risk_cooldown_until": 9999999999.0},
            next_run_at=9999999999.0,
        )

        markup = render_health_page(store)

        self.assertIn('class="health-status is-warning"', markup)
        self.assertIn("任务引擎运行正常", markup)
        self.assertIn("115 风控冷却中", markup)
        for label in ("待执行", "运行中", "需人工", "锁等待"):
            self.assertIn(label, markup)
        self.assertIn("查看完整健康报告", markup)
        self.assertIn("TaskEngine: ENABLED", markup)
```

- [ ] **Step 2: Run the tests and verify failure**

```sh
python3 -m unittest \
  tests.test_web_admin.WebAdminTests.test_quality_page_groups_file_issues_by_task \
  tests.test_web_admin.WebAdminTests.test_health_page_renders_structured_local_summary -v
```

Expected: FAIL because both pages currently render only a raw `<pre>` report.

- [ ] **Step 3: Implement quality issue grouping**

Import `QualityIssue` and add:

```python
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
```

In `render_quality_page`, call `scan_task_quality(store)` once. Compute counts for `missing_dest` plus `missing_strm`, `direct_strm`, and `unexpected_strm`. Render `.quality-summary`, one `.quality-row` per grouped task, and the escaped `format_task_quality_report(issues)` inside `.diagnostic-details`.

Keep the existing fix form and confirmation text exactly unchanged. The visible safety copy remains `只读取本地 TaskStore 和 STRM 文件路径，不会扫描 115。`.

- [ ] **Step 4: Implement structured local health**

Replace the single formatted report call with:

```python
summary = build_task_health(store, enabled=True)
report = format_task_health(summary)
cooldown_active = summary.p115_cooldown_until > time.time()
health_class = "is-warning" if cooldown_active or summary.problem_count else "is-healthy"
health_title = "任务引擎运行正常" if summary.enabled else "任务引擎已停用"
cooldown_text = "115 风控冷却中" if cooldown_active else "115 未处于风控冷却"
```

Render `.health-status`, `.health-grid`, latest problem/lock-wait panel when present, and the full escaped report in a closed `.diagnostic-details`. Preserve every existing report line so current health tests continue to verify wait details, overflow, truncation, and lock ownership.

- [ ] **Step 5: Run quality, health, and endpoint tests**

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_quality_page_runs_local_taskstore_scan -v
python3 -m unittest tests.test_web_admin.WebAdminTests.test_quality_fix_endpoint_restores_missing_dest_and_reprocesses_bad_strm -v
python3 -m unittest tests.test_web_admin.WebAdminTests -k health_page -v
```

Expected: PASS. The quality GET still scans local files once; POST behavior remains unchanged.

- [ ] **Step 6: Commit diagnostics pages**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "feat: structure web quality and health reports"
```

## Task 5: Responsive, Accessibility, And Full Regression Verification

**Files:**
- Modify: `app/web.py:25-123`
- Modify: `tests/test_web_admin.py`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Add static contract tests for responsive and safe markup**

Add:

```python
def test_web_ui_includes_responsive_and_accessible_contracts(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        markup = render_task_list(store)

        self.assertIn("@media (max-width: 760px)", markup)
        self.assertIn(":focus-visible", markup)
        self.assertIn("prefers-reduced-motion", markup)
        self.assertIn('aria-label="主导航"', markup)
        self.assertIn('aria-label="任务概览"', markup)
        self.assertNotIn("https://fonts.", markup)
        self.assertNotIn("<script src=", markup)
        self.assertNotIn("linear-gradient", markup)
```

- [ ] **Step 2: Run the contract test and fix any missing CSS/ARIA**

```sh
python3 -m unittest tests.test_web_admin.WebAdminTests.test_web_ui_includes_responsive_and_accessible_contracts -v
```

Expected: PASS after the shared style block includes mobile stacking, focus styles, and reduced-motion handling.

The mobile CSS must enforce:

- `.workspace-grid`, `.summary-grid`, and `.health-grid` become one column where appropriate.
- `.metrics-grid` becomes two columns.
- `.task-row` becomes one column without moving the detail action off-screen.
- `.phase-track` uses `grid-template-columns: repeat(8, minmax(72px, 1fr))` with horizontal overflow.
- `.quality-row` becomes a task block and does not require page-level horizontal scrolling.
- Long words and paths use `overflow-wrap: anywhere`.

- [ ] **Step 3: Run focused Web tests**

```sh
python3 -m unittest tests.test_web_admin -v
```

Expected: all Web tests PASS.

- [ ] **Step 4: Run the complete regression suite**

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all 392 baseline tests plus the new Web tests PASS, with no `ResourceWarning` failure.

- [ ] **Step 5: Start a local fixture server and capture Playwright screenshots**

Run a local server backed by a temporary TaskStore fixture; do not connect it to production services:

```sh
python3 -c '
import tempfile, time
from pathlib import Path
from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore
from app.web import start_web_server
root = Path(tempfile.mkdtemp())
store = TaskStore(root / "tasks.db")
running = store.upsert_task("running", "", "https://115cdn.com/s/running")
store.record_event(running.id, TaskStage.STRM_READY, TaskStatus.RUNNING, "等待分享 STRM", title="幼女战记", metadata_patch={"_defer_message": "等待分享 STRM 文件稳定", "p115_total_request_count": 18}, next_run_at=time.time() + 60)
failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "分享状态不可用", title="帝国的毁灭", error_summary="自有分享已失效")
server = start_web_server(store, "127.0.0.1", 18788)
print("http://127.0.0.1:18788", flush=True)
try:
    time.sleep(3600)
finally:
    server.shutdown()
'
```

Use Playwright to inspect `/`, `/task/1`, `/quality`, and `/health` at 1440x1000, 768x1024, and 390x844. Verify:

- no page-level horizontal overflow;
- no overlapping navigation, badges, buttons, stage labels, or paths;
- desktop overview columns are parallel and mobile columns stack;
- diagnostic sections are closed by default;
- all four pages render nonblank content;
- network requests target only `127.0.0.1:18788`.

- [ ] **Step 6: Commit final responsive adjustments**

```sh
git add app/web.py tests/test_web_admin.py
git commit -m "test: verify responsive web ui v2"
```

## Task 6: Final Review And Branch Readiness

**Files:**
- Review: `app/web.py`
- Review: `tests/test_web_admin.py`
- Review: `docs/superpowers/specs/2026-07-18-web-ui-v2-design.md`

- [ ] **Step 1: Check scope and diff quality**

```sh
git status --short
git diff --check main...HEAD
git diff --stat main...HEAD
git log --oneline main..HEAD
```

Expected: only `app/web.py`, `tests/test_web_admin.py`, the approved spec, and this plan are changed; no runtime data, screenshots, environment files, or generated caches are tracked.

- [ ] **Step 2: Verify route and security compatibility**

```sh
python3 -m unittest \
  tests.test_web_admin.WebAdminTests.test_web_token_blocks_requests_without_token \
  tests.test_web_admin.WebAdminTests.test_task_routes_return_404_for_malformed_task_ids \
  tests.test_web_admin.WebAdminTests.test_clear_history_endpoint_removes_finished_tasks_only \
  tests.test_web_admin.WebAdminTests.test_quality_fix_endpoint_restores_missing_dest_and_reprocesses_bad_strm -v
```

Expected: PASS.

- [ ] **Step 3: Request code review**

Use `superpowers:requesting-code-review` against `main...HEAD`. Resolve correctness or regression findings before integration; do not expand scope into backend workflow changes.

- [ ] **Step 4: Finish the branch**

Use `superpowers:finishing-a-development-branch` after all tests and review pass. Present merge/integration options without deploying to Unraid until the branch is integrated into the intended release branch.
