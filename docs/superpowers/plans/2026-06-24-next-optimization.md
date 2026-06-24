# CMS TG Ingest Next Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve runtime stability, task speed, and backend maintainability without increasing 115/CMS request pressure.

**Architecture:** Keep TaskRunner/TaskStore as the authoritative path for new self-share links. Add local observability and recovery helpers first, then replace fixed waits with conservative condition-based checks, then continue thinning `bridge.py` by extracting Telegram/status glue while preserving compatibility exports.

**Tech Stack:** Python 3.12 standard library, SQLite, unittest, Docker, existing CMS/115/Emby clients.

---

## File Structure

- `app/task_diagnostics.py`: new local-only helpers for stage age, wait reason, next run display, and stuck-task classification.
- `app/task_runner.py`: apply stuck-stage metadata and reuse diagnostics when deferring tasks.
- `app/task_store.py`: keep storage APIs minimal; add only query helpers needed for local diagnostics if tests require them.
- `app/workflows/self_share.py`: improve stage wait messages and conservative condition probes without increasing 115 scan breadth.
- `app/media/strm.py`: expose local STRM readiness details used by diagnostics.
- `app/telegram_ui.py`: new Telegram formatting/keyboards extracted from `bridge.py`.
- `app/legacy_polling.py`: optional extraction target for old `start_status_poll` path after behavior is locked by tests.
- `bridge.py`: remain executable entrypoint and compatibility facade; import and re-export moved symbols.
- `tests/test_task_diagnostics.py`: new unit tests for wait reason/stuck classification.
- `tests/test_bridge_v02_integration.py`: regression tests for `/status`, `/health`, and Telegram task replies.
- `tests/test_bridge_task_engine.py`: regression tests for stage wait messages and speed-safe condition checks.
- `tests/test_refactor_imports.py`: compatibility export smoke tests when moving UI/legacy helpers.
- `README.md` and `CHANGELOG.md`: document operational changes.

---

### Task 1: Add Local Task Diagnostics

**Files:**
- Create: `app/task_diagnostics.py`
- Modify: `tests/test_task_diagnostics.py`
- Test: `tests/test_task_diagnostics.py`

- [ ] **Step 1: Write failing diagnostics tests**

Create `tests/test_task_diagnostics.py`:

```python
import unittest

from app.models import TaskStage, TaskStatus, TaskSnapshot
from app.task_diagnostics import describe_task_wait, classify_stuck_task


def snapshot(**overrides):
    data = {
        "id": 1,
        "share_code": "abc",
        "receive_code": "1212",
        "url": "https://115cdn.com/s/abc?password=1212",
        "chat_id": "464100862",
        "title": "示例电影",
        "tmdb_id": "123456",
        "category": "欧美电影",
        "current_stage": TaskStage.STRM_READY.value,
        "status": TaskStatus.RUNNING.value,
        "error_type": "",
        "error_summary": "",
        "retry_count": 0,
        "created_at": 100.0,
        "updated_at": 120.0,
        "next_run_at": 180.0,
        "claimed_by": "",
        "claimed_at": 0,
        "submission_id": 7,
        "metadata_json": "{}",
    }
    data.update(overrides)
    return TaskSnapshot.from_row(data)


class TaskDiagnosticsTests(unittest.TestCase):
    def test_describe_task_wait_shows_stage_reason_age_and_next_run(self):
        task = snapshot(
            metadata_json='{"_defer_message":"等待自有分享 STRM", "_defer_count": 3}',
            updated_at=100.0,
            next_run_at=145.0,
        )

        text = describe_task_wait(task, now=130.0)

        self.assertIn("等待自有分享 STRM", text)
        self.assertIn("已等待 30 秒", text)
        self.assertIn("下次检查 15 秒后", text)
        self.assertIn("第 3 次", text)

    def test_classify_stuck_task_marks_long_running_stage(self):
        task = snapshot(
            current_stage=TaskStage.ORGANIZING.value,
            status=TaskStatus.RUNNING.value,
            updated_at=0.0,
            metadata_json='{"_defer_message":"等待 CMS 整理完成", "_defer_count": 31}',
        )

        issue = classify_stuck_task(task, now=3600.0)

        self.assertEqual(issue.code, "stuck_stage")
        self.assertEqual(issue.stage, TaskStage.ORGANIZING)
        self.assertIn("等待 CMS 整理完成", issue.message)

    def test_classify_stuck_task_ignores_recent_task(self):
        task = snapshot(updated_at=100.0, metadata_json='{"_defer_message":"等待自有分享 STRM"}')

        issue = classify_stuck_task(task, now=120.0)

        self.assertEqual(issue.code, "")
```

- [ ] **Step 2: Run new tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_task_diagnostics -v
```

Expected: import error for `app.task_diagnostics`.

- [ ] **Step 3: Implement diagnostics module**

Create `app/task_diagnostics.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from .models import TaskSnapshot, TaskStage, TaskStatus


@dataclass(frozen=True)
class StuckTaskIssue:
    code: str = ""
    stage: TaskStage | None = None
    message: str = ""


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    return f"{hours} 小时"


def describe_task_wait(task: TaskSnapshot, *, now: float) -> str:
    metadata = task.metadata or {}
    reason = str(metadata.get("_defer_message") or task.error_summary or "等待执行").strip()
    age = _duration(float(now) - float(task.updated_at or task.created_at or now))
    next_delay = _duration(float(task.next_run_at or now) - float(now))
    try:
        count = int(metadata.get("_defer_count") or 0)
    except (TypeError, ValueError):
        count = 0
    suffix = f"，第 {count} 次" if count else ""
    return f"{reason}，已等待 {age}，下次检查 {next_delay}后{suffix}"


def classify_stuck_task(task: TaskSnapshot, *, now: float, threshold_seconds: int = 1800) -> StuckTaskIssue:
    if task.status not in {TaskStatus.RUNNING, TaskStatus.PENDING}:
        return StuckTaskIssue()
    age = float(now) - float(task.updated_at or task.created_at or now)
    if age < threshold_seconds:
        return StuckTaskIssue()
    reason = str(task.metadata.get("_defer_message") or task.error_summary or task.current_stage.value).strip()
    return StuckTaskIssue("stuck_stage", task.current_stage, f"{reason} 已持续 {_duration(age)}")
```

- [ ] **Step 4: Run diagnostics tests**

Run:

```bash
python3 -m unittest tests.test_task_diagnostics -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/task_diagnostics.py tests/test_task_diagnostics.py
git commit -m "feat: add local task diagnostics"
```

---

### Task 2: Surface Wait Reasons in Telegram and Web Health

**Files:**
- Modify: `bridge.py`
- Modify: `app/task_health.py`
- Modify: `app/web.py`
- Modify: `tests/test_bridge_v02_integration.py`
- Modify: `tests/test_web_admin.py`
- Test: `tests/test_bridge_v02_integration.py`, `tests/test_web_admin.py`

- [ ] **Step 1: Write failing Telegram status test**

Add to `tests/test_bridge_v02_integration.py` near `/status` tests:

```python
    def test_status_command_shows_task_wait_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1212", "https://115cdn.com/s/abc?password=1212", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM",
                metadata_patch={"_defer_message": "等待自有分享 STRM", "_defer_count": 2},
                next_run_at=9999999999,
                clear_claim=True,
            )
            telegram = FakeTelegram()
            cms = FakeCmsSubmit()

            bridge.handle_update(
                self.update("/status"),
                cms,
                telegram,
                "464100862",
                submission_store,
                task_store=task_store,
                task_engine_enabled=True,
            )

            self.assertIn("等待自有分享 STRM", telegram.messages[-1][1])
            self.assertIn("第 2 次", telegram.messages[-1][1])
```

- [ ] **Step 2: Write failing Web health test**

Add to `tests/test_web_admin.py` near health tests:

```python
    def test_health_page_shows_wait_reason_for_running_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "1212", "https://115cdn.com/s/abc?password=1212")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM",
                metadata_patch={"_defer_message": "等待自有分享 STRM", "_defer_count": 2},
                clear_claim=True,
            )
            body = self.get_page(store, "/health")

            self.assertIn("等待自有分享 STRM", body)
            self.assertIn("第 2 次", body)
```

Adjust `self.get_page` to the existing test helper name if different.

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_bridge_v02_integration tests.test_web_admin -v
```

Expected: new assertions fail because wait reason is not shown.

- [ ] **Step 4: Update status and health formatting**

In `bridge.py`, import and use `describe_task_wait` in `format_taskstore_status`:

```python
from app.task_diagnostics import describe_task_wait
```

Inside each task line, append a short wait detail when status is running or pending:

```python
wait_detail = describe_task_wait(task, now=time.time()) if task.status in {TaskStatus.RUNNING, TaskStatus.PENDING} else ""
if wait_detail:
    lines.append(f"   {wait_detail}")
```

In `app/task_health.py`, include latest lock wait or latest running task wait detail using `describe_task_wait`.

In `app/web.py`, render the same wait detail on `/health` for recent running/pending tasks. Keep the page local-only; do not add external checks.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_bridge_v02_integration tests.test_web_admin -v
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add bridge.py app/task_health.py app/web.py tests/test_bridge_v02_integration.py tests/test_web_admin.py
git commit -m "feat: show task wait reasons"
```

---

### Task 3: Add Conservative Stuck-Task Recovery Notices

**Files:**
- Modify: `app/task_runner.py`
- Modify: `tests/test_task_runner.py`
- Test: `tests/test_task_runner.py`

- [ ] **Step 1: Write failing stuck recovery test**

Add to `tests/test_task_runner.py`:

```python
    def test_long_repeated_strm_wait_becomes_needs_action(self):
        current_time = 3600.0

        def now():
            return current_time

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "1212", "https://115cdn.com/s/abc?password=1212")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM",
                metadata_patch={
                    "_defer_stage": TaskStage.STRM_READY.value,
                    "_defer_message": "等待自有分享 STRM",
                    "_defer_count": 19,
                    "_lock_key": "tmdb:123456",
                    "_lock_waiting": False,
                    "tmdb_id": "123456",
                },
                next_run_at=current_time,
                clear_claim=True,
            )
            runner = TaskRunner(
                store,
                FakeWorkflow([StageResult.defer("等待自有分享 STRM", delay_seconds=15)]),
                worker_id="worker-1",
                now=now,
            )

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.error_type, "stage_wait_timeout")
            self.assertIn("等待自有分享 STRM", updated.error_summary)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.STRM_READY.value)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
python3 -m unittest tests.test_task_runner.TaskRunnerTests.test_long_repeated_strm_wait_becomes_needs_action -v
```

Expected: task remains running/deferred.

- [ ] **Step 3: Implement generic wait timeout policy**

In `app/task_runner.py`, add constants:

```python
_STAGE_MAX_DEFER_COUNT = {
    TaskStage.ORGANIZING: 30,
    TaskStage.STRM_READY: 20,
    TaskStage.EMBY_CONFIRMED: 20,
}
```

In `_apply_result` defer branch, after computing `defer_count`, replace the organizing-only timeout with generic logic:

```python
max_defer = _STAGE_MAX_DEFER_COUNT.get(task.current_stage)
if max_defer and defer_count >= max_defer:
    metadata_patch.update({
        "retry_from_stage": task.current_stage.value,
        "retry_stage": task.current_stage.value,
        "_lock_key": "",
        "_lock_waiting": False,
        "_lock_owner_task_id": "",
    })
    self.store.record_event(
        task.id,
        TaskStage.NEEDS_ACTION,
        TaskStatus.NEEDS_ACTION,
        f"{result.message} 等待超时，请人工检查后重试当前阶段",
        error_type="stage_wait_timeout",
        error_summary=f"{result.message} 等待超时",
        metadata_patch=metadata_patch,
        clear_claim=True,
    )
    return
```

Keep existing organizing message behavior if tests require exact `organizing_timeout`; otherwise update tests to the new generic error type only if product copy accepts it.

- [ ] **Step 4: Run task runner tests**

Run:

```bash
python3 -m unittest tests.test_task_runner -v
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/task_runner.py tests/test_task_runner.py
git commit -m "feat: mark long waits as needs action"
```

---

### Task 4: Make Waits Faster Without More 115 Pressure

**Files:**
- Modify: `app/workflows/self_share.py`
- Modify: `app/media/strm.py`
- Modify: `tests/test_bridge_task_engine.py`
- Test: `tests/test_bridge_task_engine.py`, `tests/test_self_share_workflow.py`

- [ ] **Step 1: Write failing STRM condition test**

Add to `tests/test_bridge_task_engine.py` near STRM_READY tests:

```python
    def test_strm_ready_rechecks_local_share_folder_without_115_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row()
            source = self.config.strm_root / row["own_share_file_name"]
            task = self._claim_task("abc", "1234", TaskStage.STRM_READY, {"submission_id": row["id"]}, row["id"])

            waiting = workflow.run_stage(task)
            self._write_strm(source, content="http://cms/s/ownshare_1212_file.mkv")
            ready = workflow.run_stage(task)

            self.assertEqual(waiting.outcome, StageOutcome.DEFER)
            self.assertLessEqual(waiting.delay_seconds, 5)
            self.assertEqual(ready.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.list_calls, [])
```

Adjust fake field names if the fixture records P115 calls differently.

- [ ] **Step 2: Run test and verify failure or current delay mismatch**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_strm_ready_rechecks_local_share_folder_without_115_scan -v
```

Expected: fail if delay is too high or P115 calls occur.

- [ ] **Step 3: Tighten local-only waits**

In `app/workflows/self_share.py`, for STRM_READY when waiting for local `SELF_SHARE_STRM_ROOT`, return a 5-second defer max:

```python
return StageResult.defer("等待自有分享 STRM", delay_seconds=min(self.retry_seconds, 5), metadata=metadata)
```

Do not add new P115 folder scans in STRM_READY.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine tests.test_self_share_workflow -v
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "perf: speed local strm readiness checks"
```

---

### Task 5: Extract Telegram UI Formatting from `bridge.py`

**Files:**
- Create: `app/telegram_ui.py`
- Modify: `bridge.py`
- Modify: `tests/test_refactor_imports.py`
- Test: `tests/test_refactor_imports.py`, `tests/test_bridge_v02_integration.py`

- [ ] **Step 1: Add failing import/compat test**

Add to `tests/test_refactor_imports.py`:

```python
    def test_telegram_ui_exports_formatters_and_bridge_compat(self):
        import bridge
        from app.telegram_ui import format_history, format_status, task_action_keyboard

        self.assertIs(bridge.format_history, format_history)
        self.assertIs(bridge.format_status, format_status)
        self.assertIs(bridge.task_action_keyboard, task_action_keyboard)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
python3 -m unittest tests.test_refactor_imports -v
```

Expected: import error for `app.telegram_ui`.

- [ ] **Step 3: Move Telegram formatting helpers**

Create `app/telegram_ui.py` and move these functions from `bridge.py` without behavior changes:

```python
format_history
format_taskstore_history
format_failure_summary
format_library_summary
quality_issue_for_row
format_quality_report
quality_issue_rows
quality_keyboard
format_metrics
format_status
format_taskstore_status
task_action_keyboard
clear_history_keyboard
menu_keyboard
```

Move constants only when required by those functions. Keep imports local and minimal. If a formatter needs `format_task_label`, import it from `app.workflows.self_share`.

In `bridge.py`, import the moved functions from `app.telegram_ui` and delete local duplicates.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_refactor_imports tests.test_bridge_v02_integration -v
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/telegram_ui.py bridge.py tests/test_refactor_imports.py
git commit -m "refactor: extract telegram ui formatting"
```

---

### Task 6: Extract Legacy Polling Path from `bridge.py`

**Files:**
- Create: `app/legacy_polling.py`
- Modify: `bridge.py`
- Modify: `tests/test_refactor_imports.py`
- Test: `tests/test_refactor_imports.py`, `tests/test_taskstore_workflow_events.py`, `tests/test_bridge_v02_integration.py`

- [ ] **Step 1: Add failing compat test**

Add to `tests/test_refactor_imports.py`:

```python
    def test_legacy_polling_exports_start_status_poll_and_bridge_compat(self):
        import bridge
        from app.legacy_polling import start_status_poll

        self.assertIs(bridge.start_status_poll, start_status_poll)
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
python3 -m unittest tests.test_refactor_imports -v
```

Expected: import error for `app.legacy_polling`.

- [ ] **Step 3: Move legacy polling function**

Create `app/legacy_polling.py` and move `start_status_poll` plus only its private helper dependencies if not already in app modules.

Keep the public signature exactly:

```python
def start_status_poll(
    cms,
    telegram,
    chat_id,
    store,
    row,
    status_poll_seconds,
    status_poll_interval,
    *,
    emby=None,
    move_config=None,
    openai_classifier=None,
    tmdb_resolver=None,
    self_share_workflow=None,
    cleanup_client=None,
    task_store=None,
):
    ...
```

If moving all dependencies would create a large dependency tangle, stop after moving only a wrapper and document remaining blockers. Do not alter behavior.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_refactor_imports tests.test_taskstore_workflow_events tests.test_bridge_v02_integration -v
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/legacy_polling.py bridge.py tests/test_refactor_imports.py
git commit -m "refactor: extract legacy status polling"
```

---

### Task 7: Update Docs and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: full suite, Docker build

- [ ] **Step 1: Document optimization changes**

In `README.md`, add a short subsection under `## 后端结构`:

```markdown
### 运行稳定性

TaskRunner 会记录每个阶段的等待原因、等待次数和下一次检查时间。`/status`、Web 任务页和 `/health` 会优先显示这些本地状态；长时间等待会进入 `NEEDS_ACTION`，方便从当前阶段安全重试。STRM 等待使用本地目录条件检查，不增加 115 扫描频率。
```

At the top of `CHANGELOG.md` under `0.2.0-alpha.2 - Unreleased`, add:

```markdown
- 增加任务等待原因、等待次数和下一次检查时间展示，长时间等待会转入 NEEDS_ACTION 方便人工恢复。
- 优化本地 STRM 就绪检查，缩短分享同步后等待时间，同时不增加 115 扫描频率。
- 继续拆分 `bridge.py` 中的 Telegram UI 和旧轮询逻辑，降低后续维护成本。
```

- [ ] **Step 2: Run compile**

Run:

```bash
python3 -m py_compile bridge.py doctor.py app/*.py app/clients/*.py app/media/*.py app/workflows/*.py
```

Expected: exit 0.

- [ ] **Step 3: Run full tests**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 4: Build Docker image**

Run:

```bash
docker build -t cms-tg-ingest:next-optimization-check .
```

Expected: build succeeds.

- [ ] **Step 5: Run doctor inside image**

Run:

```bash
docker run --rm cms-tg-ingest:next-optimization-check python /app/doctor.py || true
```

Expected: command starts and reports missing local config/filesystem rather than import or syntax errors.

- [ ] **Step 6: Commit docs**

Run:

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document runtime optimization improvements"
```

---

## Self-Review

Spec coverage:

- Stability: Tasks 1, 2, and 3 add diagnostics, status visibility, and stuck-stage recovery.
- Speed: Task 4 shortens local STRM readiness checks without new 115 scans.
- Maintainability: Tasks 5 and 6 continue thinning `bridge.py` while preserving compatibility exports.
- Documentation and verification: Task 7 covers README, CHANGELOG, full tests, and Docker dry run.

Placeholder scan:

- No TBD/TODO placeholders remain.
- Every task has exact files, tests, commands, and expected outputs.

Type consistency:

- `describe_task_wait(task, now=...)` and `classify_stuck_task(task, now=...)` are defined before use.
- `TaskSnapshot`, `TaskStage`, and `TaskStatus` names match existing modules.
- Commit messages are unique and scoped.
