# CMS TG Ingest Stability Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce noisy state, surface timing bottlenecks, and make STRM wait handling more stable without increasing 115/CMS request pressure.

**Architecture:** Keep the existing task engine and workflow flow unchanged. Add only local observability and cleanup helpers: clear stale defer metadata on terminal tasks, record per-stage elapsed time inside task metadata, expose those timings in the Web health/detail views, and tighten the STRM stability wait path so it reports clearer readiness instead of repeatedly retrying fixed sleeps. All changes stay behind the current SQLite TaskStore and existing Web/TG surfaces.

**Tech Stack:** Python 3.12 standard library, SQLite, unittest, existing CMS/115/Emby clients.

---

## File Structure

- `app/task_store.py`: add a small metadata cleanup helper for terminal tasks and preserve event history semantics.
- `app/task_runner.py`: clear stale defer metadata when a task reaches terminal state and attach stage timing metadata when a stage finishes.
- `app/task_diagnostics.py`: format per-stage elapsed time and stale-wait summaries for the Web health page.
- `app/task_health.py`: include stage timing details in task health summaries.
- `app/web.py`: show stage timing on task detail pages and add a lightweight history/terminal-task summary if needed by tests.
- `app/media/strm.py`: improve STRM stability checks so a changing directory reports its age explicitly instead of only a generic retry.
- `tests/test_task_runner.py`: regression tests for terminal metadata cleanup and stage timing metadata.
- `tests/test_task_diagnostics.py`: regression tests for stage timing formatting.
- `tests/test_task_store.py`: regression tests for metadata cleanup helper behavior.
- `tests/test_web_admin.py`: regression tests for health/detail rendering and history cleanup visibility.
- `tests/test_self_share_workflow.py`: regression tests for STRM stability handling and retry messages.

---

### Task 1: Clear Stale Defer Metadata on Terminal Tasks

**Files:**
- Modify: `app/task_store.py`
- Modify: `app/task_runner.py`
- Test: `tests/test_task_store.py`, `tests/test_task_runner.py`

- [ ] **Step 1: Write the failing cleanup test**

Add to `tests/test_task_store.py`:

```python
    def test_terminal_record_event_clears_wait_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM",
                metadata_patch={"_defer_stage": "strm_ready", "_defer_message": "等待自有分享 STRM", "_defer_count": 4},
            )

            updated = store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "已完成",
                metadata_delete_keys=("_defer_stage", "_defer_message", "_defer_count"),
            )

            self.assertNotIn("_defer_stage", updated.metadata)
            self.assertNotIn("_defer_message", updated.metadata)
            self.assertNotIn("_defer_count", updated.metadata)
```

- [ ] **Step 2: Run the test and verify it fails for missing helper coverage**

Run:

```bash
python3 -m unittest tests.test_task_store.TaskStoreTests.test_terminal_record_event_clears_wait_metadata -v
```

Expected: fail because terminal cleanup is not yet consistently applied from the runner path.

- [ ] **Step 3: Add minimal runner-side cleanup**

Update `app/task_runner.py` so `StageOutcome.COMPLETE`, `NEEDS_ACTION`, and `FAILED` terminal writes remove `_defer_stage`, `_defer_message`, and `_defer_count` when the task is no longer waiting.

```python
terminal_defer_keys = ("_defer_stage", "_defer_message", "_defer_count")
```

Use that tuple in the `record_event(...)` calls that end a stage for good.

- [ ] **Step 4: Run the targeted tests**

Run:

```bash
python3 -m unittest tests.test_task_store tests.test_task_runner -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/task_store.py app/task_runner.py tests/test_task_store.py tests/test_task_runner.py
git commit -m "perf: clear stale wait metadata on terminal tasks"
```

---

### Task 2: Record Per-Stage Timing Metadata

**Files:**
- Modify: `app/task_runner.py`
- Modify: `app/task_diagnostics.py`
- Modify: `app/task_health.py`
- Test: `tests/test_task_runner.py`, `tests/test_task_diagnostics.py`, `tests/test_web_admin.py`

- [ ] **Step 1: Write failing timing tests**

Add to `tests/test_task_runner.py`:

```python
    def test_run_once_records_stage_elapsed_seconds_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=10.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.complete("已找到")]), worker_id="worker-1", now=lambda: 13.5)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.metadata["stage_elapsed_seconds"], 3.5)
            self.assertEqual(updated.metadata["stage_started_at"], 10.0)
```

Add to `tests/test_task_diagnostics.py`:

```python
    def test_describe_task_wait_includes_stage_elapsed_seconds_when_present(self):
        task = snapshot(metadata_json='{"stage_elapsed_seconds": 12.5, "_defer_message": "等待自有分享 STRM"}', next_run_at=145.0)

        text = describe_task_wait(task, now=130.0)

        self.assertIn("阶段耗时 12.5 秒", text)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_task_runner.TaskRunnerTests.test_run_once_records_stage_elapsed_seconds_on_success tests.test_task_diagnostics.TaskDiagnosticsTests.test_describe_task_wait_includes_stage_elapsed_seconds_when_present -v
```

Expected: fail because metadata is not set yet.

- [ ] **Step 3: Implement timing capture and formatting**

In `app/task_runner.py`, when a stage result is applied, compute:

```python
stage_elapsed_seconds = max(0.0, now - float(task.updated_at or task.created_at or now))
```

Store it in metadata as `stage_elapsed_seconds` along with `stage_started_at` or equivalent start marker.

In `app/task_diagnostics.py`, append the elapsed-stage text only when the metadata key exists.

In `app/task_health.py`, continue reusing `describe_task_wait(...)` so the health page automatically picks it up.

- [ ] **Step 4: Run the targeted tests again**

Run:

```bash
python3 -m unittest tests.test_task_runner tests.test_task_diagnostics tests.test_web_admin -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/task_runner.py app/task_diagnostics.py app/task_health.py tests/test_task_runner.py tests/test_task_diagnostics.py tests/test_web_admin.py
git commit -m "feat: surface stage timing in task diagnostics"
```

---

### Task 3: Improve STRM Stability Wait Feedback

**Files:**
- Modify: `app/media/strm.py`
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_self_share_workflow.py`

- [ ] **Step 1: Write the failing stability test**

Add to `tests/test_self_share_workflow.py`:

```python
    def test_plan_strm_move_reports_not_stable_when_source_is_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "share" / "S-示例电影-2025-[tmdb=123456]"
            target_root = root / "Movie"
            source.mkdir(parents=True)
            (source / "示例.strm").write_text("http://cms/s/demo", encoding="utf-8")
            now = time.time()
            os.utime(source, (now, now))
            os.utime(source / "示例.strm", (now, now))
            config = bridge.MoveConfig(source_roots=[root / "share"], library_roots={"欧美电影": target_root}, stable_seconds=30)

            plan = bridge.plan_strm_move(source, "欧美电影", config)

            self.assertEqual(plan.status, "skipped")
            self.assertEqual(plan.reason, "STRM 源目录仍在更新")
```

- [ ] **Step 2: Run the test and verify current behavior**

Run:

```bash
python3 -m unittest tests.test_self_share_workflow.P115FailureHandlingTests.test_plan_strm_move_reports_not_stable_when_source_is_recent -v
```

Expected: fail if the exact behavior is not yet covered.

- [ ] **Step 3: Make the wait feedback more explicit**

In `app/media/strm.py`, keep the same stability gate, but make the move plan metadata expose why it is waiting by returning the source path and letting the workflow reuse the generic message. If needed, add a helper that returns the newest mtime age so diagnostics can say how old the directory is.

In `app/workflows/self_share.py`, when `is_move_plan_retryable(plan)` is true for `"STRM 源目录仍在更新"`, persist that specific reason unchanged.

- [ ] **Step 4: Run the workflow tests**

Run:

```bash
python3 -m unittest tests.test_self_share_workflow tests.test_bridge_task_engine -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/media/strm.py app/workflows/self_share.py tests/test_self_share_workflow.py
git commit -m "perf: clarify strm stability waits"
```

---

### Task 4: Surface History/Terminal Task Summaries

**Files:**
- Modify: `app/task_store.py`
- Modify: `app/web.py`
- Modify: `app/task_health.py`
- Test: `tests/test_web_admin.py`, `tests/test_task_store.py`

- [ ] **Step 1: Write history cleanup and summary tests**

Add to `tests/test_task_store.py`:

```python
    def test_clear_finished_tasks_keeps_running_and_needs_action_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            done = store.upsert_task("done", "", "https://115cdn.com/s/done")
            store.record_event(done.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.enqueue_task(running.id, TaskStage.ORGANIZING, next_run_at=0)
            manual = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "choose")

            removed = store.clear_finished_tasks()

            self.assertEqual(removed, 1)
            self.assertIsNotNone(store.find_task(running.id))
            self.assertIsNotNone(store.find_task(manual.id))
```

Add to `tests/test_web_admin.py`:

```python
    def test_history_clear_button_removes_only_finished_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            done = store.upsert_task("done", "", "https://115cdn.com/s/done")
            store.record_event(done.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            live = store.upsert_task("live", "", "https://115cdn.com/s/live")
            store.enqueue_task(live.id, TaskStage.RECEIVED, next_run_at=0)
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", "/history/clear", {}, b"")

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/")
            self.assertIsNone(store.find_task(done.id))
            self.assertIsNotNone(store.find_task(live.id))
            self.assertEqual(body, b"")
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_task_store.TaskStoreTests.test_clear_finished_tasks_keeps_running_and_needs_action_tasks tests.test_web_admin.WebAdminTests.test_history_clear_button_removes_only_finished_tasks -v
```

Expected: one or more failures until the UI summary/cleanup path is implemented exactly.

- [ ] **Step 3: Implement history handling and summary exposure**

Keep `clear_finished_tasks()` deleting only terminal tasks and their events. In `app/web.py`, keep the existing `/history/clear` handler and, if needed, add a small summary line in the task list header using `format_taskstore_health(...)` so users can see live vs terminal state without opening the health page.

- [ ] **Step 4: Run all related tests**

Run:

```bash
python3 -m unittest tests.test_task_store tests.test_web_admin tests.test_task_diagnostics -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add app/task_store.py app/web.py app/task_health.py tests/test_task_store.py tests/test_web_admin.py
git commit -m "feat: keep history focused on live task state"
```

---

### Final Verification

**Files:**
- All files changed above

- [ ] Run the full suite:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] Build and run the app doctor locally or in container if available:

```bash
docker compose up -d --build
docker compose exec cms-tg-ingest python /app/doctor.py --quiet
```

Expected: healthy container and zero doctor errors.

- [ ] Deploy to Unraid using the established safe rsync + rebuild pattern.
- [ ] Verify `/health` and `/quality` still load and that recent terminal tasks show cleaned metadata and clearer wait summaries.
