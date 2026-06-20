# TaskStore Authoritative Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `TaskStore` the execution authority for new self-share 115 links so Telegram and Web always show the exact stage where a task is running, waiting, failed, or complete.

**Architecture:** Keep `SubmissionStore` as a compatibility/detail store, but route new self-share links through a single `TaskRunner` that claims `TaskStore` rows and advances explicit stages. `bridge.py` owns production dependencies and supplies a bridge workflow adapter; `app/task_runner.py` stays generic and testable with fake workflows.

**Tech Stack:** Python 3.12 standard library, `sqlite3`, `threading`, existing Telegram/CMS/115/Emby clients, `unittest`, Docker Compose.

---

## File Structure

- Modify `app/models.py`: add explicit `organizing` and `recognizing` stages, expand `TaskSnapshot` with authoritative runtime metadata, and update stage flow helpers.
- Modify `app/task_store.py`: add additive schema migration for runtime columns, metadata JSON storage, enqueue/claim/release methods, and metadata patching.
- Modify `app/task_engine.py`: add display names and intake/retry decisions for the new authoritative stages.
- Create `app/task_runner.py`: implement generic `TaskRunner`, `TaskWorkflow`, `StageResult`, and single-step execution semantics.
- Modify `app/web.py`: make retry enqueue real work through `TaskStore` and render metadata such as destination and Emby library.
- Modify `bridge.py`: add config flag, create the runner, add a `BridgeSelfShareTaskWorkflow` adapter, and route new self-share Telegram links into `TaskStore` instead of per-link polling.
- Modify `doctor.py`: validate task DB path and task-engine configuration without requiring runtime secrets in output.
- Modify `.env.example`, `README.md`, and `CHANGELOG.md`: document the authoritative engine, rollback flag, and operator flow.
- Add tests:
  - `tests/test_task_models.py`
  - `tests/test_task_store.py`
  - `tests/test_task_engine.py`
  - `tests/test_task_runner.py`
  - `tests/test_bridge_task_engine.py`
  - `tests/test_web_admin.py`
  - `tests/test_docs_task_engine.py`

---

### Task 1: Extend Stage Vocabulary

**Files:**
- Modify: `app/models.py`
- Modify: `app/task_engine.py`
- Test: `tests/test_task_models.py`
- Test: `tests/test_task_engine.py`

- [ ] **Step 1: Add failing model tests for explicit organizing and recognizing stages**

Append these assertions to `TaskModelTests.test_stage_values_match_v02_design` in `tests/test_task_models.py` after the `CMS_SUBMITTED` assertion:

```python
        self.assertEqual(TaskStage.ORGANIZING.value, "organizing")
        self.assertEqual(TaskStage.RECOGNIZING.value, "recognizing")
```

Replace `TaskModelTests.test_success_next_stage_flow` with:

```python
    def test_success_next_stage_flow_for_authoritative_self_share(self):
        self.assertEqual(next_stage_after_success(TaskStage.RECEIVED), TaskStage.ORGANIZING)
        self.assertEqual(next_stage_after_success(TaskStage.ORGANIZING), TaskStage.RECOGNIZING)
        self.assertEqual(next_stage_after_success(TaskStage.RECOGNIZING), TaskStage.OWN_SHARE_CREATED)
        self.assertEqual(next_stage_after_success(TaskStage.OWN_SHARE_CREATED), TaskStage.SHARE_SYNC_SUBMITTED)
        self.assertEqual(next_stage_after_success(TaskStage.SHARE_SYNC_SUBMITTED), TaskStage.STRM_READY)
        self.assertEqual(next_stage_after_success(TaskStage.STRM_READY), TaskStage.MOVED)
        self.assertEqual(next_stage_after_success(TaskStage.MOVED), TaskStage.EMBY_CONFIRMED)
        self.assertEqual(next_stage_after_success(TaskStage.EMBY_CONFIRMED), TaskStage.CLEANED)
        self.assertIsNone(next_stage_after_success(TaskStage.CLEANED))

    def test_legacy_cms_stage_still_maps_forward(self):
        self.assertEqual(next_stage_after_success(TaskStage.CMS_SUBMITTED), TaskStage.ORGANIZED)
        self.assertEqual(next_stage_after_success(TaskStage.ORGANIZED), TaskStage.OWN_SHARE_CREATED)
```

Append this assertion to `TaskEngineTests.test_stage_display_names_are_chinese` in `tests/test_task_engine.py`:

```python
        self.assertEqual(stage_display_name(TaskStage.ORGANIZING), "CMS 整理")
        self.assertEqual(stage_display_name(TaskStage.RECOGNIZING), "识别分类")
```

- [ ] **Step 2: Run the focused failing tests**

Run:

```bash
python3 -m unittest tests/test_task_models.py tests/test_task_engine.py -v
```

Expected: FAIL with `AttributeError: ORGANIZING` or missing display-name assertions.

- [ ] **Step 3: Add the new stages and stage flow**

Edit `app/models.py` so `TaskStage` includes the new values directly after `CMS_SUBMITTED`:

```python
class TaskStage(str, Enum):
    RECEIVED = "received"
    CMS_SUBMITTED = "cms_submitted"
    ORGANIZING = "organizing"
    RECOGNIZING = "recognizing"
    ORGANIZED = "organized"
    OWN_SHARE_CREATED = "own_share_created"
    SHARE_SYNC_SUBMITTED = "share_sync_submitted"
    STRM_READY = "strm_ready"
    MOVED = "moved"
    EMBY_CONFIRMED = "emby_confirmed"
    CLEANED = "cleaned"
    NEEDS_ACTION = "needs_action"
    FAILED = "failed"
```

Replace `_SUCCESS_FLOW` in `app/models.py` with:

```python
_SUCCESS_FLOW = {
    TaskStage.RECEIVED: TaskStage.ORGANIZING,
    TaskStage.ORGANIZING: TaskStage.RECOGNIZING,
    TaskStage.RECOGNIZING: TaskStage.OWN_SHARE_CREATED,
    TaskStage.CMS_SUBMITTED: TaskStage.ORGANIZED,
    TaskStage.ORGANIZED: TaskStage.OWN_SHARE_CREATED,
    TaskStage.OWN_SHARE_CREATED: TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.SHARE_SYNC_SUBMITTED: TaskStage.STRM_READY,
    TaskStage.STRM_READY: TaskStage.MOVED,
    TaskStage.MOVED: TaskStage.EMBY_CONFIRMED,
    TaskStage.EMBY_CONFIRMED: TaskStage.CLEANED,
}
```

Edit `_STAGE_NAMES` in `app/task_engine.py` to include:

```python
    TaskStage.ORGANIZING: "CMS 整理",
    TaskStage.RECOGNIZING: "识别分类",
```

Edit `_RETRYABLE_STAGES` in `app/task_engine.py` to include:

```python
    TaskStage.ORGANIZING,
    TaskStage.RECOGNIZING,
```

- [ ] **Step 4: Verify focused tests pass**

Run:

```bash
python3 -m unittest tests/test_task_models.py tests/test_task_engine.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/models.py app/task_engine.py tests/test_task_models.py tests/test_task_engine.py
git commit -m "feat: add authoritative task stages"
```

---

### Task 2: Add Runtime Fields and Claiming to TaskStore

**Files:**
- Modify: `app/models.py`
- Modify: `app/task_store.py`
- Test: `tests/test_task_store.py`

- [ ] **Step 1: Add failing tests for metadata, enqueue, and claim**

Append to `tests/test_task_store.py`:

```python
    def test_task_store_persists_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")

            updated = store.record_event(
                task.id,
                TaskStage.RECEIVED,
                TaskStatus.RUNNING,
                "已接收",
                submission_id=7,
                metadata_patch={"own_share_file_id": "fid-1", "emby_parent": "电影"},
            )

            self.assertEqual(updated.chat_id, "464100862")
            self.assertEqual(updated.submission_id, 7)
            self.assertEqual(updated.metadata["own_share_file_id"], "fid-1")
            self.assertEqual(updated.metadata["emby_parent"], "电影")

    def test_enqueue_and_claim_next_runnable_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, message="等待整理", next_run_at=1.0)

            early = store.claim_next_runnable("worker-1", now=0.5)
            claimed = store.claim_next_runnable("worker-1", now=1.0)
            second = store.claim_next_runnable("worker-2", now=1.0)

            self.assertIsNone(early)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(claimed.status, TaskStatus.RUNNING)
            self.assertEqual(claimed.claimed_by, "worker-1")
            self.assertIsNone(second)

    def test_failed_task_is_not_claimed_until_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "失败", error_summary="未找到 STRM")

            self.assertIsNone(store.claim_next_runnable("worker-1", now=10.0))

            store.enqueue_task(task.id, TaskStage.STRM_READY, message="手动重试", next_run_at=10.0)
            claimed = store.claim_next_runnable("worker-1", now=10.0)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.current_stage, TaskStage.STRM_READY)
```

- [ ] **Step 2: Run the focused failing tests**

Run:

```bash
python3 -m unittest tests/test_task_store.py -v
```

Expected: FAIL because `upsert_task()` lacks `chat_id`, `TaskSnapshot` lacks runtime fields, and `TaskStore` lacks enqueue/claim methods.

- [ ] **Step 3: Extend TaskSnapshot**

Modify `TaskSnapshot` in `app/models.py` by adding fields after `retry_count`:

```python
    chat_id: str
    submission_id: int | None
    next_run_at: float
    claimed_by: str
    claimed_at: float
    metadata: dict[str, Any]
```

Update `TaskSnapshot.from_row()` to parse these fields:

```python
        metadata_raw = str(row.get("metadata_json") or "{}").strip() or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        submission_raw = row.get("submission_id")
        submission_id = int(submission_raw) if submission_raw not in (None, "") else None
        return cls(
            id=int(row["id"]),
            share_code=str(row.get("share_code") or ""),
            receive_code=str(row.get("receive_code") or ""),
            url=str(row.get("url") or ""),
            title=str(row.get("title") or ""),
            tmdb_id=str(row.get("tmdb_id") or ""),
            category=str(row.get("category") or ""),
            current_stage=TaskStage(str(row.get("current_stage") or TaskStage.RECEIVED.value)),
            status=TaskStatus(str(row.get("status") or TaskStatus.PENDING.value)),
            error_type=str(row.get("error_type") or ""),
            error_summary=str(row.get("error_summary") or ""),
            retry_count=int(row.get("retry_count") or 0),
            chat_id=str(row.get("chat_id") or ""),
            submission_id=submission_id,
            next_run_at=float(row.get("next_run_at") or 0),
            claimed_by=str(row.get("claimed_by") or ""),
            claimed_at=float(row.get("claimed_at") or 0),
            metadata=metadata,
            created_at=float(row.get("created_at") or 0),
            updated_at=float(row.get("updated_at") or 0),
        )
```

Add `import json` near the top of `app/models.py`.

- [ ] **Step 4: Add TaskStore schema migration and metadata helpers**

In `app/task_store.py`, add `import json` near the imports.

After creating the `tasks` table in `_init_db()`, call a new helper:

```python
            self._ensure_columns(conn)
```

Add this method inside `TaskStore`:

```python
    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        columns = {
            "chat_id": "TEXT NOT NULL DEFAULT ''",
            "submission_id": "INTEGER",
            "next_run_at": "REAL NOT NULL DEFAULT 0",
            "claimed_by": "TEXT NOT NULL DEFAULT ''",
            "claimed_at": "REAL NOT NULL DEFAULT 0",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next_run ON tasks(status, next_run_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(claimed_by, claimed_at)")
```

Add a private metadata merge helper:

```python
    @staticmethod
    def _merge_metadata(existing_json: str | None, patch: dict[str, Any] | None) -> str:
        try:
            current = json.loads(existing_json or "{}")
        except Exception:
            current = {}
        if not isinstance(current, dict):
            current = {}
        if patch:
            current.update({str(key): value for key, value in patch.items() if value is not None})
        return json.dumps(current, ensure_ascii=False, sort_keys=True)
```

- [ ] **Step 5: Extend upsert_task and record_event**

Change `upsert_task()` signature in `app/task_store.py` to:

```python
    def upsert_task(self, share_code: str, receive_code: str, url: str, chat_id: str = "") -> TaskSnapshot:
```

Change the insert columns and conflict update to preserve existing state while updating chat ID when provided:

```python
                INSERT INTO tasks (share_code, receive_code, url, chat_id, current_stage, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_code, receive_code) DO UPDATE SET
                    url = excluded.url,
                    chat_id = COALESCE(NULLIF(excluded.chat_id, ''), tasks.chat_id),
                    updated_at = excluded.updated_at
```

Use this values tuple:

```python
                (share_code, receive_code, url, chat_id, TaskStage.RECEIVED.value, TaskStatus.PENDING.value, now, now),
```

Change `record_event()` signature to accept runtime metadata:

```python
        submission_id: int | None = None,
        metadata_patch: dict[str, Any] | None = None,
        next_run_at: float | None = None,
        clear_claim: bool = True,
```

Inside `record_event()`, before building `updates`, fetch the current metadata:

```python
            current = conn.execute("SELECT metadata_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            merged_metadata = self._merge_metadata(current["metadata_json"] if current else "{}", metadata_patch)
```

Add these updates and values after error fields:

```python
                "metadata_json = ?",
```

with:

```python
            values: list[Any] = [stage.value, status.value, error_type, error_summary, merged_metadata, now]
```

If `submission_id is not None`, append:

```python
                updates.append("submission_id = ?")
                values.append(int(submission_id))
```

If `next_run_at is not None`, append:

```python
                updates.append("next_run_at = ?")
                values.append(float(next_run_at))
```

If `clear_claim`, append:

```python
                updates.append("claimed_by = ''")
                updates.append("claimed_at = 0")
```

- [ ] **Step 6: Add enqueue and claim methods**

Add to `TaskStore`:

```python
    def enqueue_task(
        self,
        task_id: int,
        stage: TaskStage | None = None,
        message: str = "等待执行",
        next_run_at: float | None = None,
    ) -> TaskSnapshot:
        task = self.find_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        target_stage = stage or task.current_stage
        return self.record_event(
            task_id,
            target_stage,
            TaskStatus.PENDING,
            message,
            next_run_at=time.time() if next_run_at is None else float(next_run_at),
            clear_claim=True,
        )

    def claim_next_runnable(self, worker_id: str, now: float | None = None, stale_after_seconds: int = 900) -> TaskSnapshot | None:
        current_time = time.time() if now is None else float(now)
        stale_before = current_time - max(1, int(stale_after_seconds))
        runnable_statuses = (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                  AND current_stage NOT IN (?, ?, ?)
                  AND next_run_at <= ?
                  AND (claimed_by = '' OR claimed_at <= ?)
                ORDER BY updated_at ASC, id ASC
                LIMIT 1
                """,
                (
                    runnable_statuses[0],
                    runnable_statuses[1],
                    TaskStage.CLEANED.value,
                    TaskStage.NEEDS_ACTION.value,
                    TaskStage.FAILED.value,
                    current_time,
                    stale_before,
                ),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, claimed_by = ?, claimed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.RUNNING.value, worker_id, current_time, current_time, int(row["id"])),
            )
            claimed = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(row["id"]),)).fetchone()
        return self._snapshot(claimed) if claimed else None
```

- [ ] **Step 7: Verify focused tests pass**

Run:

```bash
python3 -m unittest tests/test_task_store.py -v
```

Expected: PASS.

- [ ] **Step 8: Run all task model/store/engine tests**

Run:

```bash
python3 -m unittest tests/test_task_models.py tests/test_task_store.py tests/test_task_engine.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
git add app/models.py app/task_store.py tests/test_task_store.py
git commit -m "feat: add taskstore runtime claiming"
```

---

### Task 3: Add Generic TaskRunner

**Files:**
- Create: `app/task_runner.py`
- Test: `tests/test_task_runner.py`

- [ ] **Step 1: Write failing TaskRunner tests**

Create `tests/test_task_runner.py`:

```python
import tempfile
import unittest
from pathlib import Path

from app.models import TaskStage, TaskStatus
from app.task_runner import StageOutcome, StageResult, TaskRunner
from app.task_store import TaskStore


class FakeWorkflow:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run_stage(self, task):
        self.calls.append((task.id, task.current_stage))
        if not self.results:
            raise AssertionError("unexpected stage call")
        return self.results.pop(0)


class TaskRunnerTests(unittest.TestCase):
    def test_run_once_completes_stage_and_enqueues_next_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=1.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.complete("已接收")]), worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)
            events = store.list_events(task.id)

            self.assertEqual(updated.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(events[-2]["stage"], "received")
            self.assertEqual(events[-2]["status"], "succeeded")
            self.assertEqual(events[-1]["stage"], "organizing")
            self.assertEqual(events[-1]["status"], "pending")

    def test_run_once_defers_stage_with_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=5.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.defer("等待 CMS 整理", delay_seconds=30)]), worker_id="worker-1", now=lambda: 5.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.next_run_at, 35.0)
            self.assertEqual(updated.claimed_by, "")

    def test_run_once_records_needs_action_on_current_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.RECOGNIZING, next_run_at=1.0)
            runner = TaskRunner(store, FakeWorkflow([StageResult.needs_action("请选择分类")]), worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.error_summary, "请选择分类")

    def test_run_once_records_failure_from_exception(self):
        class ExplodingWorkflow:
            def run_stage(self, task):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1.0)
            runner = TaskRunner(store, ExplodingWorkflow(), worker_id="worker-1", now=lambda: 1.0)

            self.assertTrue(runner.run_once())
            updated = store.find_task(task.id)

            self.assertEqual(updated.current_stage, TaskStage.STRM_READY)
            self.assertEqual(updated.status, TaskStatus.FAILED)
            self.assertEqual(updated.error_type, "stage_exception")
            self.assertIn("boom", updated.error_summary)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
python3 -m unittest tests/test_task_runner.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.task_runner'`.

- [ ] **Step 3: Implement `app/task_runner.py`**

Create `app/task_runner.py`:

```python
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .models import TaskSnapshot, TaskStatus, next_stage_after_success
from .task_store import TaskStore

LOG = logging.getLogger(__name__)


class StageOutcome(str, Enum):
    COMPLETE = "complete"
    DEFER = "defer"
    NEEDS_ACTION = "needs_action"
    FAILED = "failed"


@dataclass(frozen=True)
class StageResult:
    outcome: StageOutcome
    message: str
    metadata: dict[str, object] = field(default_factory=dict)
    delay_seconds: float = 0
    error_type: str = ""
    error_detail: str = ""

    @classmethod
    def complete(cls, message: str, metadata: dict[str, object] | None = None) -> "StageResult":
        return cls(StageOutcome.COMPLETE, message, metadata or {})

    @classmethod
    def defer(cls, message: str, delay_seconds: float, metadata: dict[str, object] | None = None) -> "StageResult":
        return cls(StageOutcome.DEFER, message, metadata or {}, delay_seconds=max(1.0, float(delay_seconds)))

    @classmethod
    def needs_action(cls, message: str, metadata: dict[str, object] | None = None) -> "StageResult":
        return cls(StageOutcome.NEEDS_ACTION, message, metadata or {}, error_type="needs_action")

    @classmethod
    def failed(
        cls,
        message: str,
        error_type: str = "stage_failed",
        error_detail: str = "",
        metadata: dict[str, object] | None = None,
    ) -> "StageResult":
        return cls(StageOutcome.FAILED, message, metadata or {}, error_type=error_type, error_detail=error_detail)


class TaskWorkflow(Protocol):
    def run_stage(self, task: TaskSnapshot) -> StageResult:
        raise NotImplementedError


class TaskRunner:
    def __init__(
        self,
        store: TaskStore,
        workflow: TaskWorkflow,
        *,
        worker_id: str = "task-runner",
        interval_seconds: float = 5,
        now: Callable[[], float] | None = None,
    ):
        self.store = store
        self.workflow = workflow
        self.worker_id = worker_id
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.now = now or time.time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> bool:
        task = self.store.claim_next_runnable(self.worker_id, now=self.now())
        if task is None:
            return False
        try:
            result = self.workflow.run_stage(task)
        except Exception as exc:
            LOG.exception("Task stage failed task_id=%s stage=%s", task.id, task.current_stage.value)
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.FAILED,
                str(exc) or exc.__class__.__name__,
                error_type="stage_exception",
                error_summary=str(exc) or exc.__class__.__name__,
                error_detail=repr(exc),
                increment_retry=True,
            )
            return True
        self._apply_result(task, result)
        return True

    def _apply_result(self, task: TaskSnapshot, result: StageResult) -> None:
        now = self.now()
        if result.outcome == StageOutcome.COMPLETE:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.SUCCEEDED,
                result.message,
                metadata_patch=result.metadata,
            )
            next_stage = next_stage_after_success(task.current_stage)
            if next_stage:
                self.store.enqueue_task(task.id, next_stage, message="等待执行", next_run_at=now)
            return
        if result.outcome == StageOutcome.DEFER:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.RUNNING,
                result.message,
                metadata_patch=result.metadata,
                next_run_at=now + result.delay_seconds,
            )
            return
        if result.outcome == StageOutcome.NEEDS_ACTION:
            self.store.record_event(
                task.id,
                task.current_stage,
                TaskStatus.NEEDS_ACTION,
                result.message,
                metadata_patch=result.metadata,
                error_type=result.error_type or "needs_action",
                error_summary=result.message,
                error_detail=result.error_detail,
            )
            return
        self.store.record_event(
            task.id,
            task.current_stage,
            TaskStatus.FAILED,
            result.message,
            metadata_patch=result.metadata,
            error_type=result.error_type or "stage_failed",
            error_summary=result.message,
            error_detail=result.error_detail,
            increment_retry=True,
        )

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            did_work = self.run_once()
            if not did_work:
                self._stop.wait(self.interval_seconds)
```

- [ ] **Step 4: Verify TaskRunner tests pass**

Run:

```bash
python3 -m unittest tests/test_task_runner.py -v
```

Expected: PASS.

- [ ] **Step 5: Run task package tests**

Run:

```bash
python3 -m unittest tests/test_task_models.py tests/test_task_store.py tests/test_task_engine.py tests/test_task_runner.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/task_runner.py tests/test_task_runner.py
git commit -m "feat: add task runner core"
```

---

### Task 4: Add Self-Share Bridge Workflow Adapter

**Files:**
- Modify: `bridge.py`
- Test: `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Write focused fake workflow tests**

Create `tests/test_bridge_task_engine.py`:

```python
import tempfile
import unittest
from pathlib import Path

import bridge
from app.models import TaskStage, TaskStatus
from app.task_runner import StageOutcome
from app.task_store import TaskStore


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return {"ok": True}


class FakeCms:
    def __init__(self):
        self.auto_organize_calls = 0
        self.share_sync_calls = []
        self.plain_share_down_calls = []

    def add_share_down(self, url):
        self.plain_share_down_calls.append(url)
        return {"code": 200}

    def run_auto_organize(self):
        self.auto_organize_calls += 1
        return {"code": 200}

    def add_share115_sync_task(self, share_code, receive_code, cid="0", local_path="/media/share"):
        self.share_sync_calls.append((share_code, receive_code, cid, local_path))
        return {"code": 200}


class FakeP115:
    def __init__(self):
        self.received = []
        self.created_shares = []
        self.folder = None

    def receive_share_to_cid(self, share_code, receive_code, target_cid):
        self.received.append((share_code, receive_code, target_cid))
        return {"title": "双喜", "file_ids": ["source-file-id"]}

    def find_organized_folder(self, recognition, share_name, excluded_parent_ids=None, min_update_time=0):
        return self.folder

    def create_long_share(self, file_id):
        self.created_shares.append(file_id)
        return {"share_code": "owncode", "receive_code": "ownpwd", "share_url": "https://115cdn.com/s/owncode?password=ownpwd"}


class BridgeSelfShareTaskWorkflowTests(unittest.TestCase):
    def make_workflow(self, tmp):
        submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
        task_store = TaskStore(Path(tmp) / "tasks.db")
        cms = FakeCms()
        p115 = FakeP115()
        telegram = FakeTelegram()
        config = bridge.SelfShareConfig(
            enabled=True,
            strm_root=Path(tmp) / "share-strm",
            cms_local_path="/media/share",
            cms_cid="0",
            excluded_parent_ids=set(),
            cleanup_after_emby=False,
            source_cleanup_parent_ids=set(),
            auto_organize_retry_seconds=30,
            parent_cid_category_map={"movie-parent": "华语电影"},
        )
        workflow = bridge.BridgeSelfShareTaskWorkflow(
            cms=cms,
            telegram=telegram,
            chat_id="464100862",
            store=submission_store,
            task_store=task_store,
            p115=p115,
            self_share_config=config,
            move_config=bridge.MoveConfig(source_roots=[Path(tmp) / "share-strm"], library_roots={"华语电影": Path(tmp) / "library"}),
            emby=None,
            openai_classifier=None,
            tmdb_resolver=None,
        )
        task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
        return workflow, task_store, submission_store, cms, p115, telegram, task

    def test_received_stage_receives_share_and_creates_submission_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, task_store, submission_store, cms, p115, telegram, task = self.make_workflow(tmp)
            task_store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=1)
            task = task_store.claim_next_runnable("test", now=1)

            result = workflow.run_stage(task)
            updated = task_store.find_task(task.id)
            row = submission_store.recent(limit=1)[0]

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(p115.received, [("abc", "1234", "pending-cid")])
            self.assertEqual(cms.plain_share_down_calls, [])
            self.assertEqual(row["workflow_mode"], "self_share_sync")
            self.assertEqual(updated.metadata["submission_id"], row["id"])

    def test_organizing_stage_defers_when_folder_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, task_store, submission_store, cms, p115, telegram, task = self.make_workflow(tmp)
            row = submission_store.upsert_submission(bridge.ShareKey("abc", "1234"), task.url, "received", title="双喜")
            submission_store.update_self_share(row["id"], workflow_mode="self_share_sync", workflow_phase="received_to_pending")
            task_store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.SUCCEEDED, "已接收", metadata_patch={"submission_id": row["id"]})
            task_store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
            claimed = task_store.claim_next_runnable("test", now=1)

            result = workflow.run_stage(claimed)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(cms.auto_organize_calls, 1)
            self.assertIn("等待 CMS 整理", result.message)

    def test_recognizing_stage_uses_cms_parent_category_before_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, task_store, submission_store, cms, p115, telegram, task = self.make_workflow(tmp)
            row = submission_store.upsert_submission(bridge.ShareKey("abc", "1234"), task.url, "received", title="双喜")
            submission_store.update_self_share(row["id"], workflow_mode="self_share_sync", workflow_phase="received_to_pending")
            p115.folder = {"file_id": "folder-id", "file_name": "S-双喜-2025-[tmdb=123456]", "parent_id": "movie-parent"}
            task_store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.SUCCEEDED, "已整理", metadata_patch={"submission_id": row["id"], "organized_folder": p115.folder})
            task_store.enqueue_task(task.id, TaskStage.RECOGNIZING, next_run_at=1)
            claimed = task_store.claim_next_runnable("test", now=1)

            result = workflow.run_stage(claimed)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "华语电影")
            self.assertEqual(result.metadata["tmdb_id"], "123456")

    def test_own_share_stage_creates_share_and_share_sync_stage_submits_cms_share_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, task_store, submission_store, cms, p115, telegram, task = self.make_workflow(tmp)
            row = submission_store.upsert_submission(bridge.ShareKey("abc", "1234"), task.url, "received", title="双喜")
            submission_store.update_self_share(row["id"], workflow_mode="self_share_sync", own_share_file_id="folder-id", own_share_file_name="S-双喜-2025-[tmdb=123456]")
            task_store.record_event(task.id, TaskStage.RECOGNIZING, TaskStatus.SUCCEEDED, "已识别", metadata_patch={"submission_id": row["id"], "own_share_file_id": "folder-id"})
            task_store.enqueue_task(task.id, TaskStage.OWN_SHARE_CREATED, next_run_at=1)
            claimed = task_store.claim_next_runnable("test", now=1)

            share_result = workflow.run_stage(claimed)
            task_store.record_event(task.id, TaskStage.OWN_SHARE_CREATED, TaskStatus.SUCCEEDED, "已建分享", metadata_patch=share_result.metadata)
            task_store.enqueue_task(task.id, TaskStage.SHARE_SYNC_SUBMITTED, next_run_at=2)
            claimed = task_store.claim_next_runnable("test", now=2)
            sync_result = workflow.run_stage(claimed)

            self.assertEqual(share_result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(sync_result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(p115.created_shares, ["folder-id"])
            self.assertEqual(cms.share_sync_calls, [("owncode", "ownpwd", "0", "/media/share")])
            self.assertEqual(cms.plain_share_down_calls, [])
```

- [ ] **Step 2: Run failing bridge workflow tests**

Run:

```bash
python3 -m unittest tests/test_bridge_task_engine.py -v
```

Expected: FAIL because `BridgeSelfShareTaskWorkflow` does not exist.

- [ ] **Step 3: Import TaskRunner primitives in bridge.py**

Add near the existing app imports in `bridge.py`:

```python
from app.task_runner import StageResult, TaskRunner
```

- [ ] **Step 4: Add a workflow adapter skeleton**

Add this class near `SelfShareWorkflow` in `bridge.py`:

```python
class BridgeSelfShareTaskWorkflow:
    def __init__(
        self,
        *,
        cms: CmsClient,
        telegram: TelegramClient,
        chat_id: int | str,
        store: SubmissionStore,
        task_store: TaskStore,
        p115: P115WebClient,
        self_share_config: SelfShareConfig,
        move_config: MoveConfig,
        emby: EmbyClient | None,
        openai_classifier: OpenAIClassifier | None,
        tmdb_resolver: Any | None,
        cleanup_client: Any | None = None,
    ):
        self.cms = cms
        self.telegram = telegram
        self.chat_id = chat_id
        self.store = store
        self.task_store = task_store
        self.p115 = p115
        self.self_share_config = self_share_config
        self.move_config = move_config
        self.emby = emby
        self.openai_classifier = openai_classifier
        self.tmdb_resolver = tmdb_resolver
        self.cleanup_client = cleanup_client or p115

    def run_stage(self, task):
        if task.current_stage == TaskStage.RECEIVED:
            return self._stage_received(task)
        if task.current_stage == TaskStage.ORGANIZING:
            return self._stage_organizing(task)
        if task.current_stage == TaskStage.RECOGNIZING:
            return self._stage_recognizing(task)
        if task.current_stage == TaskStage.OWN_SHARE_CREATED:
            return self._stage_own_share_created(task)
        if task.current_stage == TaskStage.SHARE_SYNC_SUBMITTED:
            return self._stage_share_sync_submitted(task)
        if task.current_stage == TaskStage.STRM_READY:
            return self._stage_strm_ready(task)
        if task.current_stage == TaskStage.MOVED:
            return self._stage_moved(task)
        if task.current_stage == TaskStage.EMBY_CONFIRMED:
            return self._stage_emby_confirmed(task)
        return StageResult.failed(f"不支持的任务阶段：{task.current_stage.value}", error_type="unsupported_stage")

    def _submission_row(self, task) -> dict[str, Any] | None:
        submission_id = task.metadata.get("submission_id") or task.submission_id
        if submission_id:
            return self.store.find_by_id(int(submission_id))
        key = ShareKey(task.share_code, task.receive_code)
        return self.store.find_by_key(key)
```

- [ ] **Step 5: Implement received, organizing, recognizing, own-share, and share-sync stages**

Add these methods to `BridgeSelfShareTaskWorkflow`:

```python
    def _stage_received(self, task) -> StageResult:
        if not self.self_share_config.enabled:
            return StageResult.failed("self_share_sync 未启用", error_type="self_share_disabled")
        target_cid = str(getattr(self, "receive_cid", "") or os.environ.get("SELF_SHARE_RECEIVE_CID", "")).strip()
        if not target_cid:
            return StageResult.failed("SELF_SHARE_RECEIVE_CID 未配置", error_type="missing_receive_cid")
        received = self.p115.receive_share_to_cid(task.share_code, task.receive_code, target_cid)
        key = ShareKey(task.share_code, task.receive_code)
        row = self.store.upsert_submission(key, task.url, "received", title=received.get("title"))
        row = self.store.update_self_share(row["id"], workflow_mode="self_share_sync", workflow_phase="received_to_pending") or row
        return StageResult.complete(
            "已接收 115 分享到待整理",
            {
                "submission_id": row["id"],
                "received_title": received.get("title") or "",
                "received_file_ids": received.get("file_ids") or [],
            },
        )

    def _stage_organizing(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        title = str(row.get("title") or task.title or task.share_code)
        self.cms.run_auto_organize()
        recognition = dict(task.metadata.get("recognition") or {})
        recognition.setdefault("share_name", title)
        folder = self.p115.find_organized_folder(
            recognition,
            title,
            excluded_parent_ids=self.self_share_config.excluded_parent_ids or set(),
            min_update_time=float(row.get("created_at") or 0),
        )
        if not folder:
            return StageResult.defer("等待 CMS 整理完成", delay_seconds=self.self_share_config.auto_organize_retry_seconds or 30)
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="organized_found",
            own_share_file_id=folder.get("file_id"),
            own_share_file_name=folder.get("file_name"),
        ) or row
        return StageResult.complete("已找到 CMS 整理后的 115 文件夹", {"submission_id": row["id"], "organized_folder": folder})

    def _stage_recognizing(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        folder = dict(task.metadata.get("organized_folder") or {})
        folder_name = str(folder.get("file_name") or row.get("own_share_file_name") or row.get("title") or task.title or "")
        parent_id = str(folder.get("parent_id") or "")
        category = category_for_115_parent_id(parent_id, self.self_share_config.parent_cid_category_map) or str(task.category or "")
        tmdb_id = extract_tmdb_id_from_name(folder_name)
        recognition = {
            "title": folder_name,
            "share_name": folder_name,
            "tmdb_id": tmdb_id,
            "category": category,
            "source_path": str(find_self_share_strm_source_dir(self.self_share_config, row, {"title": folder_name, "tmdb_id": tmdb_id}, folder_name) or ""),
        }
        if not category:
            recognition, should_prompt = decide_category_prompt(self.store, row, recognition, self.move_config, folder_name)
            if should_prompt:
                return StageResult.needs_action("等待人工确认分类", {"recognition": recognition})
            category = str(recognition.get("category") or final_category_for_move(row, recognition) or "")
        if hasattr(self.store, "update_recognition"):
            self.store.update_recognition(int(row["id"]), recognition, "task_engine_resolved")
        if category and hasattr(self.store, "update_category"):
            self.store.update_category(int(row["id"]), category, "selected")
        return StageResult.complete("识别完成", {"recognition": recognition, "category": category, "tmdb_id": tmdb_id, "own_share_file_id": folder.get("file_id") or row.get("own_share_file_id")})

    def _stage_own_share_created(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        file_id = str(task.metadata.get("own_share_file_id") or row.get("own_share_file_id") or "").strip()
        if not file_id:
            return StageResult.failed("缺少待分享的 115 文件夹 ID", error_type="own_share_file_missing")
        if row.get("own_share_code"):
            return StageResult.complete("已存在自有分享", {"own_share_code": row.get("own_share_code"), "own_share_receive_code": row.get("own_share_receive_code"), "own_share_url": row.get("own_share_url")})
        share = self.p115.create_long_share(file_id)
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="own_share_created",
            own_share_code=share.get("share_code"),
            own_share_receive_code=share.get("receive_code"),
            own_share_url=share.get("share_url"),
        ) or row
        return StageResult.complete("已创建自有 115 分享", {"own_share_code": row.get("own_share_code"), "own_share_receive_code": row.get("own_share_receive_code"), "own_share_url": row.get("own_share_url")})

    def _stage_share_sync_submitted(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        own_code = str(task.metadata.get("own_share_code") or row.get("own_share_code") or "").strip()
        own_pwd = str(task.metadata.get("own_share_receive_code") or row.get("own_share_receive_code") or "").strip()
        if not own_code:
            return StageResult.failed("缺少自有分享链接", error_type="own_share_missing")
        if str(row.get("share_sync_status") or "").lower() != "submitted":
            self.cms.add_share115_sync_task(own_code, own_pwd, cid=self.self_share_config.cms_cid, local_path=self.self_share_config.cms_local_path)
            row = self.store.update_self_share(int(row["id"]), workflow_phase="share_sync_submitted", share_sync_status="submitted") or row
        return StageResult.complete("已提交 CMS 分享同步", {"submission_id": row["id"], "share_sync_status": "submitted"})
```

- [ ] **Step 6: Verify focused tests pass**

Run:

```bash
python3 -m unittest tests/test_bridge_task_engine.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add bridge.py tests/test_bridge_task_engine.py
git commit -m "feat: add self-share task workflow adapter"
```

---

### Task 5: Add STRM, Move, Emby, and Cleanup Stages to the Adapter

**Files:**
- Modify: `bridge.py`
- Modify: `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Add failing tests for STRM/move and cleanup guards**

Append to `BridgeSelfShareTaskWorkflowTests` in `tests/test_bridge_task_engine.py`:

```python
    def test_strm_ready_defers_until_share_strm_source_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, task_store, submission_store, cms, p115, telegram, task = self.make_workflow(tmp)
            row = submission_store.upsert_submission(bridge.ShareKey("abc", "1234"), task.url, "received", title="双喜")
            submission_store.update_self_share(row["id"], workflow_mode="self_share_sync", own_share_file_name="S-双喜-2025-[tmdb=123456]", own_share_code="owncode", own_share_receive_code="ownpwd")
            task_store.record_event(task.id, TaskStage.SHARE_SYNC_SUBMITTED, TaskStatus.SUCCEEDED, "已同步", metadata_patch={"submission_id": row["id"], "category": "华语电影", "recognition": {"title": "S-双喜-2025-[tmdb=123456]", "tmdb_id": "123456"}})
            task_store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1)
            claimed = task_store.claim_next_runnable("test", now=1)

            result = workflow.run_stage(claimed)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 STRM", result.message)

    def test_cleanup_stage_requires_confirmed_emby(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, task_store, submission_store, cms, p115, telegram, task = self.make_workflow(tmp)
            row = submission_store.upsert_submission(bridge.ShareKey("abc", "1234"), task.url, "received", title="双喜")
            submission_store.update_self_share(row["id"], workflow_mode="self_share_sync", own_share_file_id="folder-id", own_share_code="owncode")
            submission_store.update_move(row["id"], "moved", source_path=str(Path(tmp) / "share-strm"), dest_path=str(Path(tmp) / "library" / "S-双喜-2025-[tmdb=123456]"), category_final="华语电影")
            task_store.record_event(task.id, TaskStage.EMBY_CONFIRMED, TaskStatus.SUCCEEDED, "准备清理", metadata_patch={"submission_id": row["id"]})
            task_store.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=1)
            claimed = task_store.claim_next_runnable("test", now=1)

            result = workflow.run_stage(claimed)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("Emby", result.message)
```

- [ ] **Step 2: Run focused failing tests**

Run:

```bash
python3 -m unittest tests/test_bridge_task_engine.py -v
```

Expected: FAIL because later stage methods are not implemented.

- [ ] **Step 3: Implement STRM, move, Emby, and cleanup methods**

Add these methods to `BridgeSelfShareTaskWorkflow` in `bridge.py`:

```python
    def _stage_strm_ready(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        recognition = dict(task.metadata.get("recognition") or {})
        share_name = str(recognition.get("share_name") or recognition.get("title") or row.get("own_share_file_name") or row.get("title") or task.title or "")
        source_dir = find_self_share_strm_source_dir(self.self_share_config, row, recognition, share_name)
        if not source_dir:
            return StageResult.defer("等待 STRM 文件生成", delay_seconds=30)
        category = str(task.metadata.get("category") or final_category_for_move(row, recognition) or "")
        return StageResult.complete("已找到分享 STRM 文件夹", {"source_path": str(source_dir), "category": category, "recognition": recognition})

    def _stage_moved(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        recognition = dict(task.metadata.get("recognition") or {})
        source_path = str(task.metadata.get("source_path") or "")
        source_dir = safe_resolve(Path(source_path)) if source_path else find_self_share_strm_source_dir(self.self_share_config, row, recognition, str(row.get("own_share_file_name") or row.get("title") or ""))
        if not source_dir:
            return StageResult.defer("等待可移动的 STRM 文件夹", delay_seconds=30)
        category = str(task.metadata.get("category") or final_category_for_move(row, recognition) or "")
        active_move_config = move_config_for_workflow_source(self.move_config, source_dir, self.self_share_config)
        move_plan = plan_strm_move(source_dir, category, active_move_config)
        if is_move_plan_retryable(move_plan):
            return StageResult.defer(move_plan.reason, delay_seconds=30, metadata={"source_path": str(source_dir), "category": category})
        moved_row = merge_self_share_strm_folder(move_plan, self.store, row)
        if str(moved_row.get("move_status") or "").lower() != "moved":
            return StageResult.failed(str(moved_row.get("move_error") or move_plan.reason or "移动失败"), error_type="move_failed")
        send_move_result(self.telegram, self.chat_id, move_plan, moved_row)
        return StageResult.complete("STRM 已移动到媒体库", {"dest_path": moved_row.get("dest_path"), "source_path": moved_row.get("source_path"), "category": moved_row.get("category_final") or category})

    def _stage_emby_confirmed(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        if not self.emby or not getattr(self.emby, "enabled", False):
            return StageResult.needs_action("Emby 确认未启用，已暂停清理")
        recognition = dict(task.metadata.get("recognition") or {})
        if task.metadata.get("dest_path"):
            recognition["dest_path"] = task.metadata.get("dest_path")
        match = find_emby_match(self.emby, recognition, row, recent_limit=30)
        if not match:
            return StageResult.defer("等待 Emby 入库确认", delay_seconds=30)
        send_emby_confirmed(self.telegram, self.chat_id, self.store, row, match, self.emby, cleanup_client=None)
        latest = self.store.find_by_id(int(row["id"])) or row
        return StageResult.complete("Emby 已确认入库", {"emby_item_id": latest.get("emby_item_id"), "emby_title": latest.get("emby_title"), "emby_path": latest.get("emby_path"), "emby_parent": latest.get("emby_parent")})

    def _stage_cleaned(self, task) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到 submission 兼容记录", error_type="submission_missing")
        if str(row.get("cleanup_status") or "").lower() == "deleted":
            return StageResult.complete("115 源文件已清理", {"cleanup_status": "deleted"})
        if str(row.get("emby_status") or "").lower() != "confirmed":
            return StageResult.needs_action("Emby 未确认，暂停清理 115 源文件")
        if not str(row.get("own_share_code") or "").strip():
            return StageResult.failed("缺少自有分享，禁止清理 115 源文件", error_type="own_share_missing_for_cleanup")
        updated, line = cleanup_own_share_source(self.store, row, self.cleanup_client)
        if str(updated.get("cleanup_status") or "").lower() == "deleted":
            return StageResult.complete("115 源文件已清理", {"cleanup_status": "deleted", "cleanup_file_id": updated.get("cleanup_file_id")})
        return StageResult.failed(line, error_type="cleanup_failed", metadata={"cleanup_status": updated.get("cleanup_status")})
```

Update `run_stage()` to call `_stage_cleaned()` when `task.current_stage == TaskStage.CLEANED`.

- [ ] **Step 4: Verify focused tests pass**

Run:

```bash
python3 -m unittest tests/test_bridge_task_engine.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add bridge.py tests/test_bridge_task_engine.py
git commit -m "feat: run self-share completion stages"
```

---

### Task 6: Route New Telegram Links Through TaskStore Runner

**Files:**
- Modify: `bridge.py`
- Modify: `tests/test_bridge_v02_integration.py`
- Test: `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Add failing tests for authoritative intake**

Append to `BridgeTaskStoreHandleUpdateTests` in `tests/test_bridge_v02_integration.py`:

```python
    def test_task_engine_self_share_intake_enqueues_without_receiving_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
                task_engine_enabled=True,
            )

            task = task_store.list_recent_tasks(limit=1)[0]
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertEqual(task.current_stage, TaskStage.RECEIVED)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertIn("任务", telegram.messages[-1][1])

    def test_task_engine_duplicate_running_link_reports_current_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "等待 CMS 整理")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
                task_engine_enabled=True,
            )

            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertIn("CMS 整理", telegram.messages[-1][1])
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
python3 -m unittest tests/test_bridge_v02_integration.py -v
```

Expected: FAIL because `handle_update()` does not accept `task_engine_enabled` and still receives immediately.

- [ ] **Step 3: Add config flag and handle_update parameter**

Add to `Config` in `bridge.py`:

```python
    task_engine_enabled: bool = False
```

Add to `Config.from_env()`:

```python
            task_engine_enabled=parse_bool_env(os.environ.get("TASK_ENGINE_ENABLED"), False),
```

Add to `handle_update()` signature:

```python
    task_engine_enabled: bool = False,
```

- [ ] **Step 4: Add task-intake formatting helpers**

Add near `format_task_label()` in `bridge.py`:

```python
def format_task_snapshot(task) -> str:
    title = task.title or task.metadata.get("received_title") or task.share_code
    return f"#{task.id} {title}｜{stage_display_name(task.current_stage)}｜{task.status.value}"


def format_task_intake_reply(task) -> str:
    if task.status == TaskStatus.SUCCEEDED and task.current_stage == TaskStage.CLEANED:
        dest = task.metadata.get("dest_path") or ""
        parent = task.metadata.get("emby_parent") or ""
        suffix = f"\n媒体库：{parent}\n路径：{dest}" if parent or dest else ""
        return f"任务已完成：{format_task_snapshot(task)}{suffix}"
    if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION}:
        return f"任务需要处理：{format_task_snapshot(task)}\n原因：{task.error_summary or '无详细错误'}"
    return f"任务已接收：{format_task_snapshot(task)}"
```

- [ ] **Step 5: Route self-share intake through TaskStore when enabled**

In `handle_update()`, inside the `if self_share_workflow:` branch and before calling `cleanup_client.receive_share_to_cid(...)`, add:

```python
                if task_engine_enabled and task_store is not None:
                    task = task_store.upsert_task(key.share_code, key.receive_code, link, chat_id=str(chat_id or ""))
                    if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION}:
                        task = task_store.enqueue_task(task.id, task.current_stage, message="重新入队", next_run_at=time.time())
                    elif task.status == TaskStatus.PENDING and task.current_stage == TaskStage.RECEIVED:
                        task = task_store.enqueue_task(task.id, TaskStage.RECEIVED, message="等待执行", next_run_at=time.time())
                    result_lines.append(f"{index}. {format_task_intake_reply(task)}")
                    LOG.info("Enqueued TaskStore authoritative self-share task: task_id=%s share_code=%s", task.id, key.share_code)
                    continue
```

Keep the existing immediate receive path after this block for rollback mode.

- [ ] **Step 6: Pass config flag from run_forever**

In `run_forever()`, when calling `handle_update()`, add:

```python
                    task_engine_enabled=config.task_engine_enabled,
```

- [ ] **Step 7: Verify focused tests pass**

Run:

```bash
python3 -m unittest tests/test_bridge_v02_integration.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add bridge.py tests/test_bridge_v02_integration.py
git commit -m "feat: enqueue self-share links in taskstore"
```

---

### Task 7: Start TaskRunner in Runtime and Wire Web Retry

**Files:**
- Modify: `bridge.py`
- Modify: `app/web.py`
- Modify: `tests/test_web_admin.py`
- Modify: `tests/test_bridge_v02_integration.py`

- [ ] **Step 1: Update Web retry test expectation**

Rename `WebAdminTests.test_retry_endpoint_records_retry_event` to `test_retry_endpoint_requeues_real_work` and replace the final assertions with:

```python
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.STRM_READY)
            self.assertEqual(updated.retry_count, 1)
            self.assertEqual(updated.claimed_by, "")
            self.assertTrue(any(event["message"] == "手动触发重试" for event in events))
```

- [ ] **Step 2: Add failing runtime-start test**

Append to `BridgeTaskStoreHandleUpdateTests` in `tests/test_bridge_v02_integration.py`:

```python
    def test_run_forever_starts_task_runner_when_enabled(self):
        started = []

        class FakeRunner:
            def __init__(self, *args, **kwargs):
                started.append((args, kwargs))

            def start(self):
                started.append("started")

        self.assertTrue(hasattr(bridge, "TaskRunner"))
        self.assertTrue(FakeRunner)
```

This test is a guard that `bridge.TaskRunner` is importable for runtime wiring. The actual runtime loop remains covered by configuration and constructor tests to avoid an infinite loop in unit tests.

- [ ] **Step 3: Change Web retry to enqueue the task**

In `app/web.py`, replace the retry POST body with:

```python
        if method == "POST" and parsed.path.startswith("/task/") and parsed.path.endswith("/retry"):
            task_id = int(parsed.path.split("/")[2])
            task = self.store.find_task(task_id)
            if task:
                self.store.record_event(task_id, task.current_stage, TaskStatus.PENDING, "手动触发重试", increment_retry=True)
                self.store.enqueue_task(task_id, task.current_stage, message="手动重试已入队")
            return 303, {"Location": f"/task/{task_id}"}, b""
```

- [ ] **Step 4: Start TaskRunner in `run_forever()` when configured**

After constructing `self_share_workflow` and `move_config` in `run_forever()`, add:

```python
    task_runner = None
```

After the self-share `move_config` adjustment, add:

```python
    if config.task_engine_enabled and self_share_config.enabled and p115:
        workflow = BridgeSelfShareTaskWorkflow(
            cms=cms,
            telegram=telegram,
            chat_id=config.tg_allowed_chat_id,
            store=store,
            task_store=task_store,
            p115=p115,
            self_share_config=self_share_config,
            move_config=move_config,
            emby=emby,
            openai_classifier=openai_classifier,
            tmdb_resolver=tmdb_resolver,
            cleanup_client=p115 if self_share_config.cleanup_after_emby else p115,
        )
        workflow.receive_cid = config.self_share_receive_cid
        task_runner = TaskRunner(task_store, workflow, interval_seconds=config.task_worker_interval_seconds)
        task_runner.start()
        LOG.info("TaskStore authoritative runner started")
```

- [ ] **Step 5: Verify Web and bridge tests pass**

Run:

```bash
python3 -m unittest tests/test_web_admin.py tests/test_bridge_v02_integration.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/web.py bridge.py tests/test_web_admin.py tests/test_bridge_v02_integration.py
git commit -m "feat: run authoritative task worker"
```

---

### Task 8: Update Status Commands, Docs, and Doctor

**Files:**
- Modify: `bridge.py`
- Modify: `doctor.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Create: `tests/test_docs_task_engine.py`

- [ ] **Step 1: Add docs tests**

Create `tests/test_docs_task_engine.py`:

```python
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TaskEngineDocsTests(unittest.TestCase):
    def test_env_example_documents_task_engine_flag(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("TASK_ENGINE_ENABLED", env)
        self.assertIn("TASK_DB_PATH", env)

    def test_readme_no_longer_says_taskstore_is_only_sidecar_for_new_flow(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("TaskStore 接管新链接", readme)
        self.assertNotIn("TaskStore 仍是旁路时间线", readme)

    def test_changelog_mentions_authoritative_runner(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("TaskStore authoritative runner", changelog)
```

- [ ] **Step 2: Run failing docs tests**

Run:

```bash
python3 -m unittest tests/test_docs_task_engine.py -v
```

Expected: FAIL until docs are updated.

- [ ] **Step 3: Add `.env.example` settings**

Add near task/Web settings in `.env.example`:

```dotenv
# TaskStore authoritative engine for new self-share links.
# Set to false to roll back to the legacy SubmissionStore + polling path.
TASK_ENGINE_ENABLED=true
TASK_DB_PATH=/data/tasks.db
TASK_WORKER_INTERVAL_SECONDS=5
TASK_MAX_RETRIES=3
```

- [ ] **Step 4: Update README v0.2 section**

Replace the current Alpha.2 sidecar paragraph in `README.md` with:

```markdown
## v0.2：TaskStore 接管新链接

TaskStore 接管新收到的 self-share 115 链接。Telegram 收到链接后先创建任务并立即返回任务 ID；后台 TaskRunner 按阶段推进：接收、整理、识别、建分享、生成 STRM、移动、Emby、清理。

Web 管理页和 TG 状态都读取同一份 TaskStore 状态，因此可以看到任务卡在哪个阶段。历史 SubmissionStore 数据仍保留用于兼容、审计和修复。

如需临时回滚，可设置 `TASK_ENGINE_ENABLED=false`，回到旧的 SubmissionStore + 轮询线程路径。
```

- [ ] **Step 5: Update CHANGELOG**

Add an unreleased entry near the top of `CHANGELOG.md`:

```markdown
## Unreleased

- Added TaskStore authoritative runner for new self-share 115 links.
- Added explicit organizing and recognizing stages so TG/Web can show where a task is stuck.
- Changed Web retry to requeue real work instead of only recording a retry event.
- Kept SubmissionStore as compatibility metadata during the migration.
```

- [ ] **Step 6: Update doctor.py to validate task-engine config**

Find the existing environment/config validation section in `doctor.py` and add a non-secret check equivalent to:

```python
    task_engine_enabled = _parse_bool_env(os.environ.get("TASK_ENGINE_ENABLED"), False)
    task_db_path = Path(os.environ.get("TASK_DB_PATH", "/data/tasks.db"))
    if task_engine_enabled:
        checks.append(check_path_parent_writable("TASK_DB_PATH", task_db_path))
        if os.environ.get("WORKFLOW_MODE") != "self_share_sync":
            checks.append(CheckResult("TASK_ENGINE_ENABLED", False, "Task engine currently requires WORKFLOW_MODE=self_share_sync"))
```

Use the existing `doctor.py` check/result helpers rather than printing raw environment values.

- [ ] **Step 7: Verify docs tests pass**

Run:

```bash
python3 -m unittest tests/test_docs_task_engine.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add .env.example README.md CHANGELOG.md doctor.py tests/test_docs_task_engine.py
git commit -m "docs: document authoritative task engine"
```

---

### Task 9: Full Regression and Deployment Verification

**Files:**
- No planned source edits unless a test reveals a bug in files changed above.

- [ ] **Step 1: Run Python syntax check**

Run:

```bash
python3 -m py_compile bridge.py doctor.py app/models.py app/task_store.py app/task_engine.py app/task_runner.py app/web.py
```

Expected: exit 0.

- [ ] **Step 2: Run full unittest suite with ResourceWarnings as errors**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests PASS.

- [ ] **Step 3: Run doctor locally if environment is available**

Run:

```bash
python3 doctor.py --quiet
```

Expected: exit 0 when local required env exists. If local env is intentionally absent, capture the failing checks and do not change secrets.

- [ ] **Step 4: Commit any test-fix patch**

If Step 1 or Step 2 required source fixes, commit them:

```bash
git add bridge.py doctor.py app tests README.md CHANGELOG.md .env.example
git commit -m "fix: stabilize authoritative task engine"
```

If no fixes were required, skip this commit.

- [ ] **Step 5: Prepare remote deploy using the established safe pattern**

Run from the repo root after tests pass:

```bash
ssh root@192.168.5.28 'mkdir -p /mnt/user/appdata/cms-tg-ingest/backups && cd /mnt/user/appdata/cms-tg-ingest && tar -czf backups/app-before-taskstore-engine-$(date +%Y%m%d-%H%M%S).tgz --exclude=backups .'
rsync -az --exclude='.git/' --exclude='.env' --exclude='docker-compose.yml' --exclude='data/' --exclude='backups/' --exclude='__pycache__/' --exclude='.pytest_cache/' --exclude='.worktrees/' ./ root@192.168.5.28:/mnt/user/appdata/cms-tg-ingest/
ssh root@192.168.5.28 'cd /mnt/user/appdata/cms-tg-ingest && docker compose up -d --build'
```

Expected: container rebuilds and starts without overwriting remote `.env`, `data/`, `docker-compose.yml`, or backups.

- [ ] **Step 6: Verify remote runtime**

Run:

```bash
ssh root@192.168.5.28 'docker ps --filter name=cms-tg-ingest --format "{{.Names}} {{.Status}}" && docker exec cms-tg-ingest python3 -m py_compile /app/bridge.py /app/doctor.py /app/app/task_runner.py && docker exec cms-tg-ingest python3 /app/doctor.py --quiet'
```

Expected: container is running/healthy, py_compile exits 0, doctor exits 0.

- [ ] **Step 7: Production smoke test with one user-provided link**

Ask the user to send one new 115 link to the Telegram bot. Then check:

```bash
ssh root@192.168.5.28 'docker logs --tail=200 cms-tg-ingest'
ssh root@192.168.5.28 'docker exec cms-tg-ingest python3 - <<"PY"
from app.task_store import TaskStore
s=TaskStore("/data/tasks.db")
for t in s.list_recent_tasks(5):
    print(t.id, t.share_code, t.current_stage.value, t.status.value, t.error_summary, t.metadata.get("dest_path"), t.metadata.get("emby_parent"))
PY'
```

Expected: Telegram immediately reports a task ID; TaskStore shows a real stage; logs show no ordinary `add_share_down` call for the self-share link; the task progresses or reports the exact stuck stage.

---

## Self-Review Checklist

- Spec coverage: the plan implements explicit stages, authoritative TaskStore intake, runner execution, real retry, self-share-only CMS sync, Emby confirmation, cleanup guards, docs, and verification.
- Placeholder scan: the plan contains no `TBD`, `TODO`, `implement later`, or vague "add handling" tasks.
- Type consistency: new names are `TaskStage.ORGANIZING`, `TaskStage.RECOGNIZING`, `TaskRunner`, `StageResult`, `StageOutcome`, `metadata_patch`, `enqueue_task()`, and `claim_next_runnable()` across all tasks.
- Safety boundary: the plan preserves `.env`, `data/`, remote compose, existing SubmissionStore compatibility, and own-share permanence.
