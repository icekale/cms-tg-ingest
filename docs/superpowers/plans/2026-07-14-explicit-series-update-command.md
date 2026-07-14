# Explicit Series Update Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the authorized user send `追更 <115 分享链接>` to start the existing self-share series update flow, while unknown links continue through normal intake.

**Architecture:** Add an exact `TaskStore` lookup for normalized share keys. Extract the update state transition embedded in the Telegram callback into a helper that returns `started`, `not_eligible`, or `failed`; the callback preserves its alerts and the text command falls through to ordinary intake for `not_eligible` links.

**Tech Stack:** Python 3.12, `unittest`, SQLite-backed `TaskStore`, Telegram long polling.

---

### Task 1: Add Exact Task Lookup

**Files:**
- Modify: `app/task_store.py:156`
- Test: `tests/test_task_store.py`

- [ ] **Step 1: Write the failing test**

```python
def test_find_task_by_share_key_returns_only_matching_task(self):
    store = TaskStore(Path(tmp) / "tasks.db")
    expected = store.upsert_task("series", "pass", "https://115cdn.com/s/series?password=pass")
    store.upsert_task("series", "other", "https://115cdn.com/s/series?password=other")
    self.assertEqual(store.find_task_by_share_key("series", "pass").id, expected.id)
    self.assertIsNone(store.find_task_by_share_key("missing", "pass"))
```

- [ ] **Step 2: Verify the red state**

Run: `python3 -m unittest tests.test_task_store.TaskStoreTests.test_find_task_by_share_key_returns_only_matching_task`

Expected: FAIL because `find_task_by_share_key` does not exist.

- [ ] **Step 3: Add the minimal query method**

```python
def find_task_by_share_key(self, share_code: str, receive_code: str) -> TaskSnapshot | None:
    with self._lock, self._connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE share_code = ? AND receive_code = ?",
            (str(share_code), str(receive_code)),
        ).fetchone()
    return self._snapshot(row) if row else None
```

- [ ] **Step 4: Verify green and commit**

Run: `python3 -m unittest tests.test_task_store.TaskStoreTests.test_find_task_by_share_key_returns_only_matching_task`

Expected: PASS.

```bash
git add app/task_store.py tests/test_task_store.py
git commit -m "feat: look up tasks by share key"
```

### Task 2: Extract The Series Update Transition

**Files:**
- Modify: `bridge.py:1935-2089`
- Test: `tests/test_bridge_v02_integration.py:1063`

- [ ] **Step 1: Write the failing helper test**

```python
updated, result = bridge.start_series_update_task(task, submission_store, task_store, source="文本追更")
self.assertEqual(result, "started")
self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
self.assertEqual(updated.metadata["update_requested_run"], 1)
```

- [ ] **Step 2: Verify the red state**

Run: `python3 -m unittest tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_start_series_update_task_requeues_completed_series`

Expected: FAIL because `start_series_update_task` does not exist.

- [ ] **Step 3: Extract the callback state mutation into a helper**

```python
def start_series_update_task(task, store, task_store, *, source: str) -> tuple[TaskSnapshot | None, str]:
    if task.status != TaskStatus.SUCCEEDED or task.current_stage != TaskStage.CLEANED:
        return None, "not_eligible"
    category = str(task.category or task.metadata.get("category") or task.metadata.get("category_final") or "").strip()
    if category not in {"国产电视", "外国电视", "番剧"}:
        return None, "not_eligible"
    # Retain the current metadata reset, SubmissionStore reset, and received enqueue.
```

The helper returns `failed` only after the existing submission-missing failure event is recorded. Replace the `task_update` callback body with this helper and keep its callback-specific alerts.

- [ ] **Step 4: Verify helper and callback behavior, then commit**

Run: `python3 -m unittest tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_completed_tv_task_exposes_update_button_and_resets_for_new_run tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_start_series_update_task_requeues_completed_series`

Expected: PASS.

```bash
git add bridge.py tests/test_bridge_v02_integration.py
git commit -m "refactor: share series update task transition"
```

### Task 3: Route `追更 <URL>` Through The Shared Transition

**Files:**
- Modify: `bridge.py:2385-2493`
- Test: `tests/test_bridge_v02_integration.py`

- [ ] **Step 1: Write failing command tests**

```python
bridge.handle_update(
    self.update("追更 https://115cdn.com/s/abc?password=1234"),
    FakeCmsSubmit(), telegram, "464100862", submission_store,
    poll_status=False, task_store=task_store, task_engine_enabled=True,
)
updated = task_store.find_task(task.id)
self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
self.assertIn("已开始追更", telegram.messages[-1][1])
```

Add separate tests where `追更 <new URL>` creates a normal `received/pending` task, and where a completed movie retains its completed state under ordinary duplicate handling.

- [ ] **Step 2: Verify the red state**

Run: `python3 -m unittest tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_series_update_command_requeues_completed_series tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_series_update_command_falls_back_to_new_intake tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_series_update_command_keeps_completed_movie_in_normal_intake`

Expected: FAIL because text commands are not routed to the update helper.

- [ ] **Step 3: Route explicit command links before the ordinary intake branch**

```python
is_explicit_update = text.startswith("追更")
if is_explicit_update:
    text = text.removeprefix("追更").lstrip(" ：:")
links = extract_share_links(text)

existing_task = task_store.find_task_by_share_key(key.share_code, key.receive_code)
updated, result = start_series_update_task(existing_task, store, task_store, source="文本追更") if existing_task else (None, "not_eligible")
if result == "started":
    result_lines.append(f"{index}. 已开始追更：{format_task_snapshot(updated)}")
    continue
```

For `not_eligible`, preserve the current TaskStore intake branch exactly.

- [ ] **Step 4: Verify command tests, focused regression, and commit**

Run: `python3 -m unittest tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_series_update_command_requeues_completed_series tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_series_update_command_falls_back_to_new_intake tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_series_update_command_keeps_completed_movie_in_normal_intake && python3 -m unittest tests.test_task_store tests.test_bridge_v02_integration tests.test_bridge_task_engine`

Expected: PASS.

```bash
git add bridge.py tests/test_bridge_v02_integration.py
git commit -m "feat: support explicit series update links"
```

### Task 4: Verify The Completed Change

**Files:**
- Verify: `bridge.py`, `app/task_store.py`, `tests/test_task_store.py`, `tests/test_bridge_v02_integration.py`

- [ ] **Step 1: Run final verification**

Run: `python3 -m compileall -q bridge.py app tests && python3 -m unittest discover -s tests -p 'test_*.py' && git diff --check`

Expected: compilation succeeds, every test passes, and no whitespace errors are reported.
