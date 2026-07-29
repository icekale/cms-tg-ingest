# Web Task Terminate And Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe terminate and delete controls to the new Vue task list without losing active Runner state or touching CMS, 115, STRM, or Emby content.

**Architecture:** TaskStore owns atomic termination requests, claim-token guarded settlement, and terminal-record deletion. TaskRunner cooperatively settles requests at every stage boundary, while the Web API exposes backend-authoritative lifecycle actions and the Vue list renders confirmed row-level controls.

**Tech Stack:** Python 3.14-compatible standard library, SQLite, `unittest`, Vue 3, Naive UI, Vite, Node test runner.

## Global Constraints

- Start implementation from current `main`; it must contain approved design commit `c52409b` and this implementation plan.
- Use the isolated worktree `/Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/web-task-terminate-delete` during implementation.
- Add `TaskStatus.CANCELLED = "cancelled"`; preserve the task's current stage when it is terminated.
- A claimed task keeps its claim and `running` status until the Runner settles the request.
- Writing termination metadata to a claimed task must not change `updated_at`; the active claim snapshot must remain valid.
- Never force-release an active claim, kill the Runner thread, or cancel an already-issued CMS, 115, or Emby request.
- Delete only `succeeded`, `failed`, `needs_action`, or `cancelled` tasks with no active claim.
- Deleting a task removes only its TaskStore task, event, and operation rows; it never performs remote or filesystem cleanup.
- Add controls only to the new Vue task list. Do not add Telegram or legacy Web buttons.
- Do not add frontend or Python dependencies.
- Run Python tests before `npm ci`, because repository secret-hygiene tests scan the working tree.

---

### Task 1: Add Atomic TaskStore Lifecycle Operations

**Files:**
- Modify: `app/models.py:25-36`
- Modify: `app/task_store.py:1-2250`
- Test: `tests/test_task_store.py`

**Interfaces:**
- Produces: `TaskStatus.CANCELLED`.
- Produces: `TERMINATION_REQUESTED_AT_KEY = "termination_requested_at"`.
- Produces: `TERMINATION_REQUESTED_BY_KEY = "termination_requested_by"`.
- Produces: `TaskStore.request_task_termination(task_id: int, actor: str, now: float | None = None) -> TaskSnapshot | None`.
- Produces: `TaskStore.settle_requested_termination(task_id: int, expected_claimed_by: str, expected_claim_token: str, *, error_type: str = "", error_summary: str = "", error_detail: str = "", now: float | None = None) -> TaskSnapshot | None`.
- Produces: `TaskStore.delete_finished_task(task_id: int, *, expected_updated_at: float) -> bool`.

- [ ] **Step 1: Create the isolated implementation worktree**

Use the `superpowers:using-git-worktrees` skill, then verify:

```bash
git -C /Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/web-task-terminate-delete status --short --branch
git -C /Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/web-task-terminate-delete rev-parse HEAD
```

Expected: a clean feature worktree at the same commit as current `main`, with `c52409b` in its ancestry.

- [ ] **Step 2: Write failing TaskStore termination tests**

Add focused tests to `TaskStoreTests`:

```python
def test_unclaimed_task_termination_is_immediate_and_not_runnable(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("terminate-pending", "", "https://115cdn.com/s/terminate-pending")
        store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)

        terminated = store.request_task_termination(task.id, "Web", now=10)

        self.assertEqual(terminated.status, TaskStatus.CANCELLED)
        self.assertEqual(terminated.current_stage, TaskStage.ORGANIZING)
        self.assertEqual(terminated.next_run_at, -1)
        self.assertEqual(terminated.claimed_by, "")
        self.assertIsNone(store.claim_next_runnable("worker", now=10))
        self.assertEqual(store.list_events(task.id)[-1]["message"], "Web 已终止任务")

def test_claimed_task_termination_preserves_claim_version_and_is_idempotent(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("terminate-running", "", "https://115cdn.com/s/terminate-running")
        store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
        claimed = store.claim_next_runnable("worker", now=10)

        requested = store.request_task_termination(task.id, "Web", now=11)
        repeated = store.request_task_termination(task.id, "Web", now=12)

        self.assertEqual(requested.status, TaskStatus.RUNNING)
        self.assertEqual(requested.claim_token, claimed.claim_token)
        self.assertEqual(requested.updated_at, claimed.updated_at)
        self.assertEqual(requested.metadata["termination_requested_at"], 11)
        self.assertEqual(repeated.metadata["termination_requested_at"], 11)
        messages = [event["message"] for event in store.list_events(task.id)]
        self.assertEqual(messages.count("Web 已请求终止，等待当前阶段结束"), 1)

def test_settle_requested_termination_requires_current_claim_token(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("settle", "", "https://115cdn.com/s/settle")
        store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=0)
        claimed = store.claim_next_runnable("worker", now=10)
        store.request_task_termination(task.id, "Web", now=11)

        stale = store.settle_requested_termination(task.id, "worker", "stale-token", now=12)
        settled = store.settle_requested_termination(
            task.id,
            "worker",
            claimed.claim_token,
            error_type="stage_exception",
            error_summary="boom",
            error_detail="RuntimeError('boom')",
            now=13,
        )

        self.assertIsNone(stale)
        self.assertEqual(settled.status, TaskStatus.CANCELLED)
        self.assertEqual(settled.current_stage, TaskStage.STRM_READY)
        self.assertEqual(settled.error_summary, "boom")
        self.assertEqual(settled.claimed_by, "")
        self.assertEqual(settled.next_run_at, -1)
        self.assertNotIn("termination_requested_at", settled.metadata)
        self.assertEqual(store.list_events(task.id)[-1]["status"], "cancelled")
```

- [ ] **Step 3: Write failing safe-delete tests**

Add these tests near `clear_finished_tasks` coverage:

```python
def test_delete_finished_task_removes_task_events_and_operations_atomically(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("delete-finished", "", "https://115cdn.com/s/delete-finished")
        task = store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
        store.prepare_operation(task.id, "delete-test", "cms_submit", {"id": 1})

        deleted = store.delete_finished_task(task.id, expected_updated_at=task.updated_at)

        self.assertTrue(deleted)
        self.assertIsNone(store.find_task(task.id))
        self.assertEqual(store.list_events(task.id), [])
        self.assertIsNone(store.find_operation(task.id, "delete-test"))

def test_delete_finished_task_rejects_active_claim_and_stale_snapshot(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("delete-active", "", "https://115cdn.com/s/delete-active")
        store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
        active = store.claim_next_runnable("worker", now=10)

        self.assertFalse(store.delete_finished_task(active.id, expected_updated_at=active.updated_at))
        store.clear_worker_claims("worker", now=11)
        changed = store.find_task(active.id)
        store.record_event(changed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
        self.assertFalse(store.delete_finished_task(changed.id, expected_updated_at=changed.updated_at))
        self.assertIsNotNone(store.find_task(active.id))

def test_clear_finished_tasks_includes_cancelled_tasks(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("clear-cancelled", "", "https://115cdn.com/s/clear-cancelled")
        store.request_task_termination(task.id, "Web", now=10)

        self.assertEqual(store.clear_finished_tasks(), 1)
        self.assertIsNone(store.find_task(task.id))
```

- [ ] **Step 4: Run the focused tests and verify RED**

Run the exact new tests by name:

```bash
python3 -m unittest -v \
  tests.test_task_store.TaskStoreTests.test_unclaimed_task_termination_is_immediate_and_not_runnable \
  tests.test_task_store.TaskStoreTests.test_claimed_task_termination_preserves_claim_version_and_is_idempotent \
  tests.test_task_store.TaskStoreTests.test_settle_requested_termination_requires_current_claim_token \
  tests.test_task_store.TaskStoreTests.test_delete_finished_task_removes_task_events_and_operations_atomically \
  tests.test_task_store.TaskStoreTests.test_delete_finished_task_rejects_active_claim_and_stale_snapshot \
  tests.test_task_store.TaskStoreTests.test_clear_finished_tasks_includes_cancelled_tasks
```

Expected: failures identify the missing enum value and TaskStore methods, not fixture errors.

- [ ] **Step 5: Implement the minimal TaskStore lifecycle state**

Add the enum and module constants:

```python
class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_ACTION = "needs_action"
    CANCELLED = "cancelled"
```

```python
TERMINATION_REQUESTED_AT_KEY = "termination_requested_at"
TERMINATION_REQUESTED_BY_KEY = "termination_requested_by"
_TERMINATION_METADATA_DELETE_KEYS = (
    TERMINATION_REQUESTED_AT_KEY,
    TERMINATION_REQUESTED_BY_KEY,
    "_lock_key",
    "_lock_reason",
    "_lock_waiting",
    "_lock_owner_task_id",
)
_DELETABLE_TASK_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED.value,
        TaskStatus.FAILED.value,
        TaskStatus.NEEDS_ACTION.value,
        TaskStatus.CANCELLED.value,
    }
)
```

Implement `request_task_termination` under one `BEGIN IMMEDIATE` transaction. For a claimed task, merge the two request fields and insert the request event, but update only `metadata_json`; deliberately leave `updated_at`, claim fields, stage, status, and errors unchanged. For an unclaimed active task, remove termination/lock metadata, set `status='cancelled'`, `next_run_at=-1`, clear all claim fields, update `updated_at`, and insert one cancelled event. Return the current snapshot for an already-cancelled task and `None` for missing or other terminal states.

Implement `settle_requested_termination` under one transaction. Require `status='running'`, exact `claimed_by`, exact `claim_token`, and a positive request timestamp. Preserve the current stage, merge away termination/lock metadata, set `status='cancelled'`, `next_run_at=-1`, clear the claim, and insert one cancelled event. Use supplied exception fields only when non-empty; otherwise preserve existing task error fields.

Implement `delete_finished_task` under one transaction. Require exact `expected_updated_at`, an empty `claimed_by`, and a status in `_DELETABLE_TASK_STATUSES`; delete events, operations, and task in that order. Add `TaskStatus.CANCELLED.value` to the existing `clear_finished_tasks` status tuple without changing its treatment of other statuses.

- [ ] **Step 6: Run TaskStore tests and verify GREEN**

Run the Step 4 command, then:

```bash
python3 -W error::ResourceWarning -m unittest -v tests.test_task_store
```

Expected: all TaskStore tests pass with no resource warnings.

- [ ] **Step 7: Review and commit TaskStore behavior**

```bash
git diff --check
git diff -- app/models.py app/task_store.py tests/test_task_store.py
git add app/models.py app/task_store.py tests/test_task_store.py
git commit -m "feat: add atomic task termination state"
```

---

### Task 2: Make Actions And Runner Cooperate With Termination

**Files:**
- Modify: `app/task_actions.py:1-155`
- Modify: `app/task_runner.py:206-500`
- Modify: `app/task_store.py:1304-1405`
- Test: `tests/test_task_actions.py`
- Test: `tests/test_task_runner.py`
- Test: `tests/test_task_store.py`

**Interfaces:**
- Consumes: Task 1 lifecycle methods and metadata constants.
- Produces: `TASK_ACTIONS` containing `terminate`.
- Produces: `task_termination_requested(task: TaskSnapshot) -> bool`.
- Produces: `available_lifecycle_actions(task: TaskSnapshot) -> frozenset[str]` with `terminate` and `delete` decisions.
- Produces: Runner settlement before any new stage side effect and before any stage result is committed.
- Produces: lock-wait `TaskLockClaimResult.task` even after the claim is released.

- [ ] **Step 1: Write failing action-service tests**

Update the claimed-task test and add lifecycle coverage:

```python
def test_claimed_running_task_allows_only_terminate(self):
    store = self.make_store()
    task = store.upsert_task("claimed", "", "https://115cdn.com/s/claimed")
    store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=0)
    store.claim_next_runnable("worker", now=0)
    task = store.find_task(task.id)

    self.assertEqual(available_task_actions(task, 3), frozenset({"terminate"}))
    result = apply_task_action(store, task.id, "terminate", max_retries=3, actor="Web")

    self.assertTrue(result.applied)
    self.assertEqual(result.task.status, TaskStatus.RUNNING)
    self.assertTrue(result.task.metadata["termination_requested_at"] > 0)

def test_cancelled_task_is_delete_only_and_repeat_terminate_is_idempotent(self):
    store = self.make_store()
    task = store.upsert_task("cancelled", "", "https://115cdn.com/s/cancelled")
    first = apply_task_action(store, task.id, "terminate", max_retries=3, actor="Web")
    repeated = apply_task_action(store, task.id, "terminate", max_retries=3, actor="Web")

    self.assertTrue(first.applied)
    self.assertTrue(repeated.applied)
    self.assertEqual(available_lifecycle_actions(repeated.task), frozenset({"delete"}))
```

- [ ] **Step 2: Write failing Runner boundary tests**

Add these cases to `TaskRunnerTests`:

```python
def test_termination_after_claim_skips_workflow(self):
    class TerminateAfterClaimStore(TaskStore):
        def claim_next_runnable(self, worker_id, now=None, stale_after_seconds=21600):
            claimed = super().claim_next_runnable(worker_id, now, stale_after_seconds)
            if claimed is not None:
                self.request_task_termination(claimed.id, "Web", now=2)
            return claimed

    with tempfile.TemporaryDirectory() as tmp:
        store = TerminateAfterClaimStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("before-stage", "", "https://115cdn.com/s/before-stage")
        store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
        workflow = FakeWorkflow([])
        runner = TaskRunner(store, workflow, worker_id="worker", now=lambda: 2)

        self.assertTrue(runner.run_once())
        self.assertEqual(workflow.calls, [])
        self.assertEqual(store.find_task(task.id).status, TaskStatus.CANCELLED)

def test_termination_during_stage_discards_success_result(self):
    class TerminatingWorkflow:
        def __init__(self, store):
            self.store = store

        def run_stage(self, task):
            self.store.request_task_termination(task.id, "Web", now=3)
            return StageResult.complete("must not advance")

    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("during-stage", "", "https://115cdn.com/s/during-stage")
        store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
        runner = TaskRunner(store, TerminatingWorkflow(store), worker_id="worker", now=lambda: 3)

        self.assertTrue(runner.run_once())
        current = store.find_task(task.id)
        self.assertEqual(current.status, TaskStatus.CANCELLED)
        self.assertEqual(current.current_stage, TaskStage.ORGANIZING)

def test_termination_during_stage_exception_records_error_as_cancelled(self):
    class TerminatingFailure:
        def __init__(self, store):
            self.store = store

        def run_stage(self, task):
            self.store.request_task_termination(task.id, "Web", now=3)
            raise RuntimeError("boom")

    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("cancel-error", "", "https://115cdn.com/s/cancel-error")
        store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1)
        runner = TaskRunner(store, TerminatingFailure(store), worker_id="worker", now=lambda: 3)

        self.assertTrue(runner.run_once())
        current = store.find_task(task.id)
        self.assertEqual(current.status, TaskStatus.CANCELLED)
        self.assertEqual(current.error_type, "stage_exception")
        self.assertEqual(current.error_summary, "boom")
```

Add the lock-wait race regression:

```python
def test_termination_during_lock_wait_is_finalized_after_claim_release(self):
    class TerminateDuringLockStore(TaskStore):
        def claim_task_lock(self, task_id, *args, **kwargs):
            self.request_task_termination(task_id, "Web", now=2)
            return super().claim_task_lock(task_id, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        store = TerminateDuringLockStore(Path(tmp) / "tasks.db")
        holder = store.upsert_task("lock-holder", "", "https://115cdn.com/s/lock-holder")
        store.enqueue_task(holder.id, TaskStage.ORGANIZING, next_run_at=1)
        store.claim_next_runnable("holder-worker", now=1)
        waiter = store.upsert_task("lock-waiter", "", "https://115cdn.com/s/lock-waiter")
        store.enqueue_task(waiter.id, TaskStage.ORGANIZING, next_run_at=1)
        workflow = FakeWorkflow([])
        runner = TaskRunner(store, workflow, worker_id="waiter-worker", now=lambda: 2)

        self.assertTrue(runner.run_once())
        current = store.find_task(waiter.id)
        self.assertEqual(workflow.calls, [])
        self.assertEqual(current.status, TaskStatus.CANCELLED)
        self.assertEqual(current.claimed_by, "")
```

- [ ] **Step 3: Run action and Runner tests and verify RED**

```bash
python3 -m unittest -v tests.test_task_actions tests.test_task_runner
```

Expected: failures show missing `terminate` availability and missing Runner settlement; existing retry/reprocess assertions may also fail until action availability is updated.

- [ ] **Step 4: Implement action availability and idempotent termination**

Add:

```python
TASK_ACTIONS = frozenset({"retry", "emby", "restore", "reprocess", "terminate"})

def task_termination_requested(task: TaskSnapshot) -> bool:
    value = task.metadata.get("termination_requested_at")
    if isinstance(value, bool):
        return value
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def available_lifecycle_actions(task: TaskSnapshot) -> frozenset[str]:
    requested = task_termination_requested(task)
    if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        return frozenset() if requested else frozenset({"terminate"})
    if task.status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.NEEDS_ACTION,
        TaskStatus.CANCELLED,
    } and not str(task.claimed_by or "").strip():
        return frozenset({"delete"})
    return frozenset()
```

In `available_task_actions`, add `terminate` before the existing claimed-task early return, but do not add `delete` to the POST action set. In `apply_task_action`, handle `terminate` before the retry transition logic: already-cancelled is a successful idempotent response; active tasks call `request_task_termination`; other terminal tasks return “任务已经结束，无需终止”. Keep all existing actions unchanged.

- [ ] **Step 5: Implement Runner settlement and released-claim cleanup**

Add a helper that does not rely on `updated_at`:

```python
def _settle_requested_termination(
    self,
    task: TaskSnapshot,
    *,
    error_type: str = "",
    error_summary: str = "",
    error_detail: str = "",
) -> bool:
    settled = self.store.settle_requested_termination(
        task.id,
        self.worker_id,
        task.claim_token,
        error_type=error_type,
        error_summary=error_summary,
        error_detail=error_detail,
        now=self.now(),
    )
    return settled is not None
```

Call it immediately after `claim_next_runnable`, after `_prepare_lock`, after `workflow.run_stage`, and in exception handlers before recording a normal failed result. Persist the global 115 risk cooldown first when handling `P115RiskControlError`, then allow termination to win the task status while retaining the risk exception details.

For paths that release a claim before returning, finalize a preserved request:

```python
def _finish_released_termination(self, task: TaskSnapshot | None) -> None:
    if task is None or not task.metadata.get("termination_requested_at"):
        return
    actor = str(task.metadata.get("termination_requested_by") or "Web")
    self.store.request_task_termination(task.id, actor, now=self.now())
```

Make `_defer_for_p115_risk_cooldown` retain the snapshot returned by `_record_claimed_event` and pass it to this helper. Make the lock-holder branch of `claim_task_lock` return the updated unclaimed waiter in `TaskLockClaimResult.task`; `_prepare_lock` passes it to the same helper before returning `None`. This closes races where a request arrives after the first boundary check but before a cooldown or lock wait releases the claim.

- [ ] **Step 6: Run focused and adjacent tests**

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_task_actions \
  tests.test_task_runner \
  tests.test_task_store
```

Expected: all tests pass, including existing stale-claim, lock-wait, heartbeat, retry, and reprocess cases.

- [ ] **Step 7: Review and commit cooperative termination**

```bash
git diff --check
git diff -- app/task_actions.py app/task_runner.py app/task_store.py tests/test_task_actions.py tests/test_task_runner.py tests/test_task_store.py
git add app/task_actions.py app/task_runner.py app/task_store.py tests/test_task_actions.py tests/test_task_runner.py tests/test_task_store.py
git commit -m "feat: stop task runner at termination boundaries"
```

---

### Task 3: Expose Lifecycle Actions Through The Web API

**Files:**
- Modify: `app/task_actions.py`
- Modify: `app/web_api.py:114-295`
- Modify: `app/web.py:1520-1780`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Consumes: `available_lifecycle_actions`, `apply_task_action`, and `TaskStore.delete_finished_task`.
- Produces: task JSON fields `available_actions: list[str]` and `termination_requested: bool`.
- Produces: `POST /api/v1/tasks/{id}/actions/terminate`.
- Produces: `DELETE /api/v1/tasks/{id}`.

- [ ] **Step 1: Write failing serialization and endpoint tests**

Add tests to `WebApiTests`:

```python
def test_task_api_exposes_backend_lifecycle_actions(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("actions", "", "https://115cdn.com/s/actions")
        app = WebApp(store)

        status, _headers, body = app.handle_request("GET", f"/api/v1/tasks/{task.id}", {}, b"")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["available_actions"], ["terminate"])
        self.assertFalse(payload["termination_requested"])

def test_terminate_api_is_idempotent_for_claimed_task(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("api-terminate", "", "https://115cdn.com/s/api-terminate")
        store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
        store.claim_next_runnable("worker", now=1)
        app = WebApp(store)

        first_status, _headers, first_body = app.handle_request(
            "POST", f"/api/v1/tasks/{task.id}/actions/terminate", {}, b""
        )
        second_status, _headers, second_body = app.handle_request(
            "POST", f"/api/v1/tasks/{task.id}/actions/terminate", {}, b""
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(json.loads(first_body)["termination_requested"])
        self.assertTrue(json.loads(second_body)["termination_requested"])

def test_delete_task_api_rejects_active_and_deletes_terminal_record(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("api-delete", "", "https://115cdn.com/s/api-delete")
        app = WebApp(store)

        conflict_status, _headers, conflict_body = app.handle_request(
            "DELETE", f"/api/v1/tasks/{task.id}", {}, b""
        )
        store.request_task_termination(task.id, "Web", now=1)
        deleted_status, _headers, deleted_body = app.handle_request(
            "DELETE", f"/api/v1/tasks/{task.id}", {}, b""
        )
        missing_status, _headers, missing_body = app.handle_request(
            "DELETE", f"/api/v1/tasks/{task.id}", {}, b""
        )

        self.assertEqual(conflict_status, 409)
        self.assertEqual(json.loads(conflict_body)["error"], "delete_not_allowed")
        self.assertEqual(deleted_status, 200)
        self.assertEqual(json.loads(deleted_body)["deleted"], task.id)
        self.assertEqual(missing_status, 404)
        self.assertEqual(json.loads(missing_body)["error"], "task_not_found")
```

Add the terminal and missing-task response test:

```python
def test_terminate_api_rejects_finished_task_and_reports_missing_task(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("terminate-finished", "", "https://115cdn.com/s/terminate-finished")
        store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
        app = WebApp(store)

        conflict_status, _headers, conflict_body = app.handle_request(
            "POST", f"/api/v1/tasks/{task.id}/actions/terminate", {}, b""
        )
        missing_status, _headers, missing_body = app.handle_request(
            "POST", "/api/v1/tasks/999/actions/terminate", {}, b""
        )

        self.assertEqual(conflict_status, 409)
        self.assertEqual(json.loads(conflict_body)["error"], "action_not_allowed")
        self.assertEqual(missing_status, 404)
        self.assertEqual(json.loads(missing_body)["error"], "task_not_found")
```

- [ ] **Step 2: Run API tests and verify RED**

```bash
python3 -m unittest -v \
  tests.test_web_api.WebApiTests.test_task_api_exposes_backend_lifecycle_actions \
  tests.test_web_api.WebApiTests.test_terminate_api_is_idempotent_for_claimed_task \
  tests.test_web_api.WebApiTests.test_delete_task_api_rejects_active_and_deletes_terminal_record
```

Expected: missing JSON fields and unsupported DELETE route failures.

- [ ] **Step 3: Serialize backend-authoritative lifecycle state**

In `serialize_task`, add:

```python
termination_requested = task_termination_requested(task)
available_actions = sorted(available_lifecycle_actions(task))
```

Return both fields in every list/detail task payload. Import `task_termination_requested` and `available_lifecycle_actions` from `task_actions`. Do not expose `termination_requested_by` separately; the existing safe metadata serializer may return the non-secret actor label.

- [ ] **Step 4: Add safe delete service and API route**

Add `delete_task_record(store: TaskStore, task_id: int) -> TaskActionResult` to `task_actions.py`. It loads the task, requires `delete` from `available_lifecycle_actions`, and calls `delete_finished_task(..., expected_updated_at=task.updated_at)`. Return precise reasons for missing, active/claimed, and raced state.

In `WebApp._handle_api`, route exact `DELETE /api/v1/tasks/{numeric_id}` before the GET task-prefix handler:

```python
if method == "DELETE" and path.startswith("/api/v1/tasks/"):
    raw_id = path.removeprefix("/api/v1/tasks/")
    if raw_id.isdigit():
        result = delete_task_record(self.store, int(raw_id))
        if result.task is None:
            return api_response({"error": "task_not_found"}, status=404)
        if not result.applied:
            return api_response(
                {"error": "delete_not_allowed", "reason": result.reason},
                status=409,
            )
        return api_response({"deleted": int(raw_id), "message": result.reason})
```

Merge authentication headers exactly as other API branches do. Keep the existing POST action route; adding `terminate` to `TASK_ACTIONS` makes it reuse `apply_task_action`. Return `404` for missing tasks and `409` for incompatible terminal states.

- [ ] **Step 5: Run Web API regression tests**

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_web_api \
  tests.test_web_admin
```

Expected: new lifecycle endpoints and all legacy/new Web routes pass.

- [ ] **Step 6: Review and commit the API**

```bash
git diff --check
git diff -- app/task_actions.py app/web_api.py app/web.py tests/test_web_api.py
git add app/task_actions.py app/web_api.py app/web.py tests/test_web_api.py
git commit -m "feat: expose web task lifecycle actions"
```

---

### Task 4: Add Confirmed Row Actions To The Vue Task List

**Files:**
- Create: `frontend/src/taskView.js`
- Create: `frontend/test/taskView.test.js`
- Modify: `frontend/src/api.js:1-38`
- Modify: `frontend/src/views/Tasks.vue:1-25`

**Interfaces:**
- Consumes: task payload `available_actions` and `termination_requested`.
- Produces: `taskStatusLabel(status: string) -> string`.
- Produces: `taskLifecycleState(task: object) -> { canTerminate: boolean, canDelete: boolean, terminationRequested: boolean }`.
- Produces: `api.deleteTask(id)`.
- Produces: confirmed per-row terminate/delete controls.

- [ ] **Step 1: Write failing frontend state tests**

Create `frontend/test/taskView.test.js`:

```javascript
import assert from 'node:assert/strict'
import test from 'node:test'
import { taskLifecycleState, taskStatusLabel } from '../src/taskView.js'

test('maps cancelled status without changing existing raw labels', () => {
  assert.equal(taskStatusLabel('cancelled'), '已终止')
  assert.equal(taskStatusLabel('running'), 'running')
})

test('uses backend lifecycle actions and termination flag', () => {
  assert.deepEqual(
    taskLifecycleState({ available_actions: ['terminate'], termination_requested: false }),
    { canTerminate: true, canDelete: false, terminationRequested: false },
  )
  assert.deepEqual(
    taskLifecycleState({ available_actions: [], termination_requested: true }),
    { canTerminate: false, canDelete: false, terminationRequested: true },
  )
  assert.deepEqual(
    taskLifecycleState({ available_actions: ['delete'], termination_requested: false }),
    { canTerminate: false, canDelete: true, terminationRequested: false },
  )
})
```

- [ ] **Step 2: Run frontend tests and verify RED**

```bash
npm test --prefix frontend
```

Expected: failure because `frontend/src/taskView.js` does not exist.

- [ ] **Step 3: Implement the pure task-view helper and API client**

Create:

```javascript
export function taskStatusLabel(status) {
  return status === 'cancelled' ? '已终止' : status
}

export function taskLifecycleState(task = {}) {
  const actions = new Set(Array.isArray(task.available_actions) ? task.available_actions : [])
  return {
    canTerminate: actions.has('terminate'),
    canDelete: actions.has('delete'),
    terminationRequested: task.termination_requested === true,
  }
}
```

Add to `api`:

```javascript
deleteTask: (id) => request(`tasks/${id}`, { method: 'DELETE' }),
```

- [ ] **Step 4: Add the task-list action column**

Import `NPopconfirm` and the helper functions. Add row-local busy state keyed by `${row.id}:${action}` rather than one global action flag:

```javascript
const busyActions = ref({})
const actionKey = (row, action) => `${row.id}:${action}`
const isActionBusy = (row, action) => Boolean(busyActions.value[actionKey(row, action)])
function setActionBusy(row, action, value) {
  const next = { ...busyActions.value }
  if (value) next[actionKey(row, action)] = true
  else delete next[actionKey(row, action)]
  busyActions.value = next
}
```

Implement `runLifecycleAction(row, action)` so `terminate` calls `api.taskAction(row.id, 'terminate')`, `delete` calls `api.deleteTask(row.id)`, each shows a specific success message, and both await `load()` on success or failure before clearing row loading.

Render the operation cell with:

- a warning “终止” button inside `NPopconfirm` when `canTerminate`;
- an error/ghost “删除” button inside `NPopconfirm` when `canDelete`;
- a small warning tag “终止处理中” when `terminationRequested`;
- a muted dash if none applies.

Use these exact confirmation messages:

```text
终止只会阻止后续阶段，当前已发出的 CMS/115 请求可能仍会完成。确认终止？
将永久删除本地任务、时间线和操作记录，不会删除网盘或媒体内容。确认删除？
```

Change the status cell to `taskStatusLabel(row.status)`. Keep all existing columns, task links, pagination, and manual refresh behavior.

- [ ] **Step 5: Run frontend unit and production checks**

```bash
npm test --prefix frontend
npm run build --prefix frontend
```

Expected: all Node tests pass and Vite produces `frontend/dist` without compile errors. Existing chunk-size advisories are non-blocking.

- [ ] **Step 6: Review and commit the Vue controls**

```bash
git diff --check
git diff -- frontend/src/taskView.js frontend/test/taskView.test.js frontend/src/api.js frontend/src/views/Tasks.vue
git add frontend/src/taskView.js frontend/test/taskView.test.js frontend/src/api.js frontend/src/views/Tasks.vue
git commit -m "feat: add task terminate and delete controls"
```

---

### Task 5: Run The Full Regression Gate

**Files:**
- Verify: all tracked Python, frontend, test, and documentation files.

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: a clean feature branch whose complete task lifecycle behavior is safe to integrate.

- [ ] **Step 1: Verify focused lifecycle behavior together**

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_task_store \
  tests.test_task_actions \
  tests.test_task_runner \
  tests.test_web_api \
  tests.test_web_admin
```

Expected: all focused and adjacent tests pass.

- [ ] **Step 2: Run the complete Python suite before frontend installation**

```bash
python3 -m compileall -q app bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test*.py' -q
```

Expected: zero failures, errors, and resource warnings.

- [ ] **Step 3: Run clean frontend verification**

```bash
npm ci --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
```

Expected: all frontend tests pass and the production build exits zero.

- [ ] **Step 4: Inspect the final branch payload**

```bash
git diff --check
git status --short --branch
git log --oneline --decorate c52409b..HEAD
git diff --stat c52409b..HEAD
```

Expected: a clean worktree, only the planned lifecycle commits, and no generated `node_modules` or unrelated files in the diff.

- [ ] **Step 5: Review against the approved design**

Verify all of the following from tests and the final diff:

- queued tasks terminate immediately;
- claimed tasks keep their claim until Runner settlement;
- no next workflow stage begins after a termination request;
- stale workers cannot settle a request;
- task deletion is terminal-only, claim-safe, and local-only;
- API action availability comes from backend state;
- Vue buttons require confirmation and use independent row loading;
- TG and legacy Web do not gain new buttons;
- no CMS, 115, STRM, or Emby cleanup call was added.

- [ ] **Step 6: Hand off the verified branch for integration**

Invoke `superpowers:finishing-a-development-branch`. Do not push, release, build Docker Hub, or deploy Unraid unless the user explicitly requests those operations after reviewing the implementation.
