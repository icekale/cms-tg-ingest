# Workflow Stability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cloud ingestion and manual task recovery idempotent, source-aware, bounded, and consistent across Telegram and Web without increasing 115 request pressure.

**Architecture:** Keep the existing single TaskRunner and SQLite authority. Split cloud-output discovery from movement so item identities are committed before the external move side effect, pin task-specific routing and receive-directory provenance, and centralize Web/TG task actions behind one compare-and-set transition service. Retain bounded polling and the existing 115 global pacing; do not add worker concurrency.

**Tech Stack:** Python 3.12, `unittest`, SQLite, existing `TaskStore`, TaskRunner, CMS/115/Emby clients, Vue/Naive UI frontend tests.

## Global Constraints

- Preserve the successful ordinary 115 share workflow and existing database rows.
- Magnet and ED2K tasks always produce self-owned shared STRM and therefore persist `strm_mode=shared` regardless of the global default.
- Once an external receive or cloud-download request starts, its selected receive CID is immutable for that task attempt.
- Every 115 side effect must be retry-safe after process termination or a stale TaskRunner CAS result.
- Do not increase TaskRunner concurrency, remove 115 pacing, or perform unbounded 115 pagination.
- Web and Telegram must use the same action eligibility and compare-and-set transition code.
- Existing runtime configuration remains backward compatible; undocumented hard-coded retry limits must be removed.
- Use TDD for every behavioral change and keep each commit independently testable.

---

## File Map

- Create `app/task_actions.py`: shared Web/TG action eligibility and guarded transitions.
- Modify `app/task_store.py`: claim-safe cloud upsert, source-aware reprocess metadata, and task-specific cloud mode.
- Modify `app/clients/p115.py`: side-effect-free cloud output discovery, idempotent movement, bounded task-list pagination, and safe errors.
- Modify `app/workflows/self_share.py`: two-step cloud output handling, pinned receive CID, and bounded CMS trigger wait.
- Keep `app/task_runner.py` structurally unchanged; its existing CAS remains the commit authority.
- Reuse `app/task_engine.py:decide_retry()` with an explicit configured limit; do not change its retry policy.
- Modify `app/web.py`: delegate task actions to `app/task_actions.py` and inject the retry limit.
- Modify `bridge.py`: delegate Telegram actions to `app/task_actions.py`, pass retry configuration, and conditionally render buttons.
- Modify `app/telegram_ui.py`: render only currently eligible actions.
- Modify `app/clients/http.py`: redact sensitive URLs in non-JSON errors.
- Modify focused test modules listed in each task.
- Modify release documentation only after the complete regression suite passes.

---

### Task 1: Protect Active Cloud Claims From Duplicate Intake

**Files:**
- Modify: `app/task_store.py:593`
- Test: `tests/test_task_store.py`
- Test: `tests/test_cloud_workflow.py`

**Interfaces:**
- Consumes: existing `TaskStore.upsert_cloud_task(source_key, url, chat_id="", title="")`.
- Produces: the same public signature; conflict updates are ignored while `claimed_by` is non-empty, and newly created cloud tasks persist `metadata["strm_mode"] == "shared"`.

- [ ] **Step 1: Add an active-claim duplicate-intake regression test**

```python
def test_upsert_cloud_task_does_not_mutate_active_claim(self):
    task = store.upsert_cloud_task("btih:abc", MAGNET, chat_id="464100862")
    store.enqueue_task(task.id, TaskStage.CLOUD_DOWNLOADING, next_run_at=0)
    claimed = store.claim_next_runnable("worker-1", now=100)

    duplicate = store.upsert_cloud_task("btih:abc", MAGNET, chat_id="464100862")

    self.assertEqual(duplicate.updated_at, claimed.updated_at)
    self.assertEqual(duplicate.claimed_by, "worker-1")
    self.assertEqual(duplicate.metadata["strm_mode"], "shared")
```

- [ ] **Step 2: Run the focused test and verify the current unconditional `updated_at` update fails it**

Run: `python3 -m unittest tests.test_task_store.TaskStoreTests.test_upsert_cloud_task_does_not_mutate_active_claim -v`

Expected: FAIL because the duplicate intake changes `updated_at`.

- [ ] **Step 3: Make the cloud upsert claim-safe and pin shared mode**

Change the cloud `INSERT` to include `metadata_json` with `{"strm_mode":"shared"}` and mirror ordinary share conflict guards:

```sql
ON CONFLICT(source_type, source_key) DO UPDATE SET
    url = CASE WHEN tasks.claimed_by = '' THEN excluded.url ELSE tasks.url END,
    title = CASE
        WHEN tasks.claimed_by = '' THEN COALESCE(NULLIF(excluded.title, ''), tasks.title)
        ELSE tasks.title
    END,
    chat_id = CASE
        WHEN tasks.claimed_by = '' THEN COALESCE(NULLIF(excluded.chat_id, ''), tasks.chat_id)
        ELSE tasks.chat_id
    END,
    updated_at = CASE WHEN tasks.claimed_by = '' THEN excluded.updated_at ELSE tasks.updated_at END
```

Do not overwrite an existing task's explicit metadata on conflict.

- [ ] **Step 4: Add a routing regression test**

Construct `ModeRoutingWorkflow(default_mode="direct")`, create a cloud task, and assert `effective_task_strm_mode(task) == "shared"` and the shared workflow receives `CLOUD_DOWNLOADING`.

- [ ] **Step 5: Run focused tests**

Run: `python3 -m unittest tests.test_task_store tests.test_cloud_workflow -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/task_store.py tests/test_task_store.py tests/test_cloud_workflow.py
git commit -m "fix: protect active cloud intake claims"
```

---

### Task 2: Make Cloud Output Discovery and Movement Durable

**Files:**
- Modify: `app/clients/p115.py:925`
- Modify: `app/workflows/self_share.py:481`
- Test: `tests/test_p115_cloud_download.py`
- Test: `tests/test_cloud_workflow.py`

**Interfaces:**
- Produces: `P115WebClient.discover_cloud_download_outputs(status: dict[str, Any]) -> list[dict[str, Any]]`.
- Produces: `P115WebClient.ensure_cloud_outputs_in_target(items: list[dict[str, Any]], target_cid: str) -> list[dict[str, Any]]`.
- Persisted metadata: `cloud_output_items`, a JSON-safe list containing `file_id`, `file_name`, `parent_id`, and `is_folder`.
- Replaces: single-item assumptions in `resolve_cloud_download_output()` and `received_items` construction.

- [ ] **Step 1: Add discovery tests for single and multi-item containers**

```python
def test_discover_cloud_outputs_returns_all_children_without_moving(self):
    client = P115WebClient("UID=1", http=FakeHttp([
        {"state": True, "data": [
            {"fid": "video", "cid": "container", "n": "S01E01.mkv"},
            {"fid": "subtitle", "cid": "container", "n": "S01E01.zh.srt"},
        ]},
    ]))

    items = client.discover_cloud_download_outputs({"file_id": "container"})

    self.assertEqual([item["file_id"] for item in items], ["video", "subtitle"])
    self.assertFalse(any(call["url"].endswith("/files/move") for call in client.http.calls))
```

Add cases asserting:

- an empty successful listing raises a retryable `P115CloudOutputPendingError`;
- a listing transport/API error is propagated and never converted into the container itself;
- folder children preserve `is_folder=True`;
- more than one child is accepted.

- [ ] **Step 2: Run discovery tests and verify current behavior fails**

Run: `python3 -m unittest tests.test_p115_cloud_download -v`

Expected: FAIL because the current resolver rejects multiple children and falls back after errors.

- [ ] **Step 3: Implement side-effect-free discovery**

Add:

```python
class P115CloudOutputPendingError(RuntimeError):
    pass
```

`discover_cloud_download_outputs()` must only list and normalize children. It must not call `move_file()`. Treat an empty list as pending unless the status payload contains an explicit file record (`fid` plus a non-empty file name); do not catch generic exceptions.

- [ ] **Step 4: Persist discovered IDs before moving**

In `_stage_cloud_downloading()`:

```python
if not metadata.get("cloud_output_items"):
    try:
        items = self.p115.discover_cloud_download_outputs(status)
    except P115CloudOutputPendingError as exc:
        return StageResult.defer(str(exc), self.self_share_config.cloud_poll_seconds, metadata)
    metadata["cloud_output_items"] = items
    return StageResult.defer(
        "已识别云下载输出，等待移动到待整理目录",
        1,
        metadata,
    )
```

This defer is the durable boundary: TaskRunner commits the IDs before the next run performs any move.

- [ ] **Step 5: Add idempotent movement tests**

Cover:

- all discovered children are moved once;
- an item already visible under `target_cid` is skipped;
- a retry after all items moved performs no second move;
- partial movement moves only the missing IDs;
- workflow metadata records the real `is_folder` values and exact expected count.

- [ ] **Step 6: Implement idempotent movement and multi-item workflow metadata**

`ensure_cloud_outputs_in_target()` must list `target_cid` once, compare item IDs, move only missing IDs, and return every item with `parent_id=target_cid`. `_stage_cloud_downloading()` must build `received_file_ids`, `received_items`, and `received_expected_item_count` from the complete returned list instead of forcing one item and `is_folder=False`.

- [ ] **Step 7: Add the crash-boundary workflow test**

Simulate discovery commit, perform movement, discard the stage result as a stale CAS, then rerun from persisted `cloud_output_items`. Assert no duplicate 115 cloud submission and no duplicate move.

- [ ] **Step 8: Run focused tests**

Run: `python3 -m unittest tests.test_p115_cloud_download tests.test_cloud_workflow -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/clients/p115.py app/workflows/self_share.py tests/test_p115_cloud_download.py tests/test_cloud_workflow.py
git commit -m "fix: make cloud output movement resumable"
```

---

### Task 3: Pin Receive CID and Bound CMS Trigger Recovery

**Files:**
- Modify: `app/workflows/self_share.py:412`
- Modify: `app/workflows/self_share.py:463`
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_cloud_workflow.py`

**Interfaces:**
- Produces: `BridgeSelfShareTaskWorkflow._task_receive_cid(task: TaskSnapshot) -> str`.
- Persisted metadata: `receive_target_cid` for ordinary share intake; existing `cloud_target_cid` remains authoritative for cloud tasks.
- Produces error type: `cloud_auto_organize_timeout` with `TaskStatus.NEEDS_ACTION`.

- [ ] **Step 1: Add a CID-change regression test for ordinary shares**

Receive a share under CID `old`, persist the completed `RECEIVED` result, change the runtime override to CID `new`, and execute `ORGANIZING`. Assert scan exclusions, received-source validation, and delayed recovery continue using `old`.

- [ ] **Step 2: Run the CID test and verify it fails**

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_receive_cid_is_pinned_after_receive -v`

Expected: FAIL because downstream code calls `_configured_receive_cid()` again.

- [ ] **Step 3: Persist and consistently resolve task receive provenance**

Implement:

```python
def _task_receive_cid(self, task: TaskSnapshot) -> str:
    return (
        str(task.metadata.get("receive_target_cid") or "").strip()
        or str(task.metadata.get("cloud_target_cid") or "").strip()
        or self._configured_receive_cid()
    )
```

Store `receive_target_cid` immediately after ordinary `receive_share_to_cid()`. Use `_task_receive_cid(task)` in recovery listing, excluded-parent construction, unverified-source guards, and share creation checks. Legacy rows fall back to the current configured CID.

- [ ] **Step 4: Add a persistent CMS failure deadline test**

Run a cloud task with `auto_organize_pending=True`, `cloud_started_at=100`, `cloud_timeout_seconds=300`, an always-failing CMS client, and `now=401`. Assert the result is `NEEDS_ACTION`, has `error_type="cloud_auto_organize_timeout"`, and does not call `cloud_download_add()` or `cloud_download_status()`.

- [ ] **Step 5: Enforce the deadline before retrying CMS**

Move the elapsed-time check ahead of the `auto_organize_pending` branch. Distinguish an unfinished cloud download from a completed download waiting for CMS; the latter transitions to manual action with the moved item metadata intact.

- [ ] **Step 6: Run focused tests**

Run: `python3 -m unittest tests.test_bridge_task_engine tests.test_cloud_workflow -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py tests/test_cloud_workflow.py
git commit -m "fix: pin receive cid and bound cms trigger waits"
```

---

### Task 4: Make Reprocess Source-Aware

**Files:**
- Modify: `app/task_store.py:22`
- Modify: `app/task_store.py:1700`
- Test: `tests/test_task_store.py`
- Test: `tests/test_web_admin.py`

**Interfaces:**
- Produces: `reprocess_stage_for(task: TaskSnapshot) -> TaskStage`.
- Produces: `reprocess_delete_keys_for(task: TaskSnapshot) -> tuple[str, ...]`.
- Ordinary shares still return to `TaskStage.RECEIVED`.
- Cloud sources return to `TaskStage.CLOUD_DOWNLOADING` and clear all attempt-specific cloud metadata.

- [ ] **Step 1: Add source-aware reprocess tests**

```python
def test_cloud_reprocess_returns_to_cloud_downloading_and_clears_attempt_state(self):
    task = make_failed_cloud_task(metadata={
        "cloud_info_hash": "abc",
        "cloud_task_id": "task-1",
        "cloud_started_at": 100,
        "cloud_target_cid": "old",
        "cloud_output_items": [{"file_id": "f1"}],
        "auto_organize_pending": True,
        "strm_mode": "shared",
    })

    updated = store.reprocess_task(task.id, next_run_at=0)

    self.assertEqual(updated.current_stage, TaskStage.CLOUD_DOWNLOADING)
    self.assertEqual(updated.metadata["strm_mode"], "shared")
    self.assertNotIn("cloud_task_id", updated.metadata)
    self.assertNotIn("cloud_output_items", updated.metadata)
```

Also assert ordinary share reprocess remains `RECEIVED`.

- [ ] **Step 2: Run the tests and verify cloud reprocess currently fails**

Run: `python3 -m unittest tests.test_task_store -v`

Expected: FAIL because every task currently returns to `RECEIVED`.

- [ ] **Step 3: Implement source-aware stage and metadata cleanup**

Cloud cleanup keys must include:

```python
CLOUD_REPROCESS_METADATA_DELETE_KEYS = REPROCESS_METADATA_DELETE_KEYS + (
    "cloud_info_hash",
    "cloud_task_id",
    "cloud_started_at",
    "cloud_target_cid",
    "cloud_status",
    "cloud_output_file_id",
    "cloud_output_parent_id",
    "cloud_output_name",
    "cloud_output_items",
    "auto_organize_pending",
    "auto_organize_last_error",
    "auto_organize_submitted_at",
)
```

Preserve source identity (`source_type`, `source_key`, `url`) and `strm_mode=shared`.

- [ ] **Step 4: Run focused tests**

Run: `python3 -m unittest tests.test_task_store tests.test_web_admin -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/task_store.py tests/test_task_store.py tests/test_web_admin.py
git commit -m "fix: make task reprocess source aware"
```

---

### Task 5: Unify Web and Telegram Task Actions

**Files:**
- Create: `app/task_actions.py`
- Modify: `app/web.py:368`
- Modify: `app/web.py:770`
- Modify: `bridge.py:2920`
- Modify: `app/telegram_ui.py:259`
- Test: `tests/test_task_actions.py`
- Test: `tests/test_bridge_v02_integration.py`
- Test: `tests/test_web_admin.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Produces: `TaskActionResult(applied: bool, task: TaskSnapshot | None, reason: str)`.
- Produces: `available_task_actions(task: TaskSnapshot, max_retries: int) -> frozenset[str]`.
- Produces: `apply_task_action(store: TaskStore, task_id: int, action: str, *, max_retries: int, actor: str) -> TaskActionResult`.
- Supported actions: `retry`, `emby`, `restore`, `reprocess`.

- [ ] **Step 1: Write policy tests before moving production code**

Cover this matrix:

```text
claimed RUNNING task       -> no mutating actions
FAILED retryable task      -> retry, reprocess
NEEDS_ACTION task          -> reprocess only; retry rejected
SUCCEEDED CLEANED share    -> reprocess; downstream recovery only when eligible
FAILED cloud task          -> retry, reprocess to CLOUD_DOWNLOADING
retry_count >= configured  -> retry rejected
```

Add a stale-snapshot race test: claim the task after eligibility is read and assert compare-and-set returns `applied=False` without clearing the claim.

- [ ] **Step 2: Run the new tests and verify the module is absent**

Run: `python3 -m unittest tests.test_task_actions -v`

Expected: FAIL with import error.

- [ ] **Step 3: Implement the shared action service**

Move the existing Web eligibility rules into `app/task_actions.py`. Every mutation must use `TaskStore.compare_and_set_transition()` with the expected stage, status, `updated_at`, and `require_unclaimed=True`. `retry` must require `RetryAction.RETRY_CURRENT_STAGE`; never fall back to `RECEIVED` for `MANUAL_ACTION_REQUIRED` or `NO_RETRY`.

- [ ] **Step 4: Replace Web private action logic**

Have HTML and JSON routes call `apply_task_action()`. Preserve current HTTP semantics: accepted action returns success; rejected/stale action returns conflict or no-op according to the existing route contract.

- [ ] **Step 5: Replace Telegram action logic and keyboard visibility**

`handle_task_action_callback()` must call the same service and show `result.reason` when rejected. `task_action_keyboard()` accepts `max_retries` and renders only actions from `available_task_actions()`. Old callback buttons remain safe because server-side eligibility is re-evaluated.

- [ ] **Step 6: Run all action tests**

Run: `python3 -m unittest tests.test_task_actions tests.test_bridge_v02_integration tests.test_web_admin tests.test_web_api -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/task_actions.py app/web.py app/telegram_ui.py bridge.py tests/test_task_actions.py tests/test_bridge_v02_integration.py tests/test_web_admin.py tests/test_web_api.py
git commit -m "fix: unify guarded task actions"
```

---

### Task 6: Wire `TASK_MAX_RETRIES` Into Every Action Surface

**Files:**
- Modify: `bridge.py:400`
- Modify: `bridge.py:2920`
- Modify: `app/config.py:258`
- Modify: `app/web.py:1340`
- Test: `tests/test_bridge_v02_integration.py`
- Test: `tests/test_web_admin.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- `WebApp(..., task_max_retries: int = 3)` stores a normalized positive integer.
- Telegram update handling receives `task_max_retries` from `Config`.
- Shared task action functions receive the same value.

- [ ] **Step 1: Add configuration behavior tests**

Create a failed task with `retry_count=3`. Assert retry is unavailable with `max_retries=3` and available with `max_retries=5` through both Web and TG.

- [ ] **Step 2: Run tests and verify the configured value is currently ignored**

Run: `python3 -m unittest tests.test_bridge_v02_integration tests.test_web_admin tests.test_web_api -v`

Expected: FAIL for the `max_retries=5` cases.

- [ ] **Step 3: Inject the setting from startup boundaries**

Pass `config.task_max_retries` into `WebApp`, Telegram callback handling, keyboard rendering, and `apply_task_action()`. Normalize invalid non-positive values during config construction rather than silently falling back at each call site.

- [ ] **Step 4: Prove no production call relies on `decide_retry(task)` defaults**

Run: `rg -n "decide_retry\(task\)" app bridge.py`

Expected: no Web/TG production call without an explicit configured limit; tests may still exercise the default directly.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m unittest tests.test_bridge_v02_integration tests.test_web_admin tests.test_web_api -v`

```bash
git add app/config.py app/web.py bridge.py tests/test_bridge_v02_integration.py tests/test_web_admin.py tests/test_web_api.py
git commit -m "fix: honor configured task retry limit"
```

---

### Task 7: Bound Cloud Task Lookup and Redact All HTTP Errors

**Files:**
- Modify: `app/clients/p115.py:882`
- Modify: `app/clients/http.py:89`
- Test: `tests/test_p115_cloud_download.py`
- Test: `tests/test_http_clients.py`
- Test: `tests/test_telegram_client.py`

**Interfaces:**
- Produces: `P115WebClient._find_cloud_task(identity: dict[str, Any] | None = None, source_url: str = "", max_pages: int = 3) -> dict[str, Any]`, shared by status and source recovery.
- Both `HttpJson` and `FormHttp` use `_redact_url(url)` for every exception message.

- [ ] **Step 1: Add bounded pagination tests**

Mock page 1 with 30 unrelated tasks and page 2 with the matching identity. Assert the match is returned after two requests. Add a no-match case asserting requests stop after page 3 and never loop indefinitely.

- [ ] **Step 2: Implement lazy bounded pagination**

Keep page 1 as the fast path. Request later pages only when the identity/source is not found and the previous page was full. Use `page_size=30`, `max_pages=3`, and stop on a short or empty page. Do not increase any background polling frequency.

- [ ] **Step 3: Add non-JSON secret-redaction tests**

```python
def test_non_json_telegram_response_redacts_bot_token(self):
    with patch("app.clients.http.urllib.request.urlopen", return_value=FakeResponse("<html>bad gateway</html>")):
        with self.assertRaises(RuntimeError) as raised:
            HttpJson(timeout=1).request("https://api.telegram.org/botSECRET/getMe")

    self.assertNotIn("SECRET", str(raised.exception))
    self.assertIn("bot<redacted>", str(raised.exception))
```

Add the equivalent query-token case for `FormHttp`.

- [ ] **Step 4: Redact URLs in JSON decode errors**

Replace both raw URL interpolations with `_redact_url(url)`. Keep only the first 300 response characters as today.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m unittest tests.test_p115_cloud_download tests.test_http_clients tests.test_telegram_client -v`

```bash
git add app/clients/p115.py app/clients/http.py tests/test_p115_cloud_download.py tests/test_http_clients.py tests/test_telegram_client.py
git commit -m "fix: bound cloud lookup and redact http errors"
```

---

### Task 8: Full Regression, Fault Injection, Documentation, and Release Gate

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `CHANGELOG.md`
- Modify: `docs/dockerhub-overview.md`
- Modify: `app/__init__.py`
- Test: all affected test modules and full suite

**Interfaces:**
- No new runtime interface; this task verifies and documents the completed behavior.

- [ ] **Step 1: Add an end-to-end fault-injection test**

Build one TaskRunner test that covers:

1. duplicate cloud intake while claimed;
2. cloud output discovery commit;
3. process interruption after one move;
4. idempotent resume of remaining moves;
5. CMS trigger failure followed by success;
6. completed shared STRM, Emby confirmation, and cleanup.

Assert exactly one cloud-download POST, no duplicate move for the completed item, one final submission row, and final `CLEANED/SUCCEEDED` state.

- [ ] **Step 2: Run syntax and focused verification**

Run:

```bash
python3 -m py_compile bridge.py doctor.py
python3 -m unittest tests.test_task_store tests.test_task_runner tests.test_task_actions tests.test_p115_cloud_download tests.test_cloud_workflow tests.test_bridge_task_engine tests.test_bridge_v02_integration tests.test_web_admin tests.test_web_api tests.test_http_clients tests.test_telegram_client -v
npm test --prefix frontend
git diff --check
```

Expected: all pass with no ResourceWarning.

- [ ] **Step 3: Run the complete backend suite**

Run: `python3 -W error::ResourceWarning -m unittest discover -s tests -v`

Expected: all existing and new tests pass; the baseline before implementation was 922 passing backend tests and 2 passing frontend tests.

- [ ] **Step 4: Verify request-pressure invariants**

Assert in tests and diff review that:

- TaskRunner remains single-worker;
- each cloud poll still performs one page-1 request in the normal case;
- pagination is lazy and capped at three pages;
- output movement lists the target directory once per stage attempt;
- CMS failure uses existing backoff and has a finite deadline;
- no new background scanner or timer is introduced.

- [ ] **Step 5: Update user-facing documentation**

Document:

- magnet/ED2K always use self-owned shared STRM;
- changing the receive CID affects new attempts only;
- task retry limits apply equally to Web and TG;
- cloud reprocess restarts cloud download safely;
- multi-file and full-season cloud outputs are supported;
- the bounded 115 lookup may inspect at most 90 recent cloud tasks.

- [ ] **Step 6: Bump version and changelog**

Use version `v0.2.41`. Describe fixes without claiming higher concurrency or faster 115 scanning.

- [ ] **Step 7: Final review gate**

Review the diff specifically for external side effects before durable state, unguarded `record_event(..., clear_claim=True)` calls from user actions, raw sensitive URLs in errors, and new unbounded loops.

- [ ] **Step 8: Commit release preparation**

```bash
git add app/__init__.py README.md .env.example CHANGELOG.md docs/dockerhub-overview.md tests
git commit -m "release: prepare workflow stability hardening"
```

- [ ] **Step 9: Release only after explicit user approval**

Use the existing `cms-release-deploy` workflow to merge/push GitHub, build Docker Hub, update the Unraid Compose image, restart, and verify container health. Do not combine release execution with implementation review.

---

## Completion Criteria

- Duplicate magnet/ED2K intake cannot mutate a live claim or cause a second 115 cloud-download POST.
- Cloud output IDs are durable before movement; retries handle partial movement and multiple children.
- Cloud tasks run correctly under every global STRM default and always produce self-owned shared STRM.
- Ordinary and cloud tasks retain the receive CID selected for their current attempt.
- Persistent CMS trigger failure reaches manual action within the configured cloud deadline.
- Cloud reprocess returns to `CLOUD_DOWNLOADING` with stale attempt metadata removed.
- Web and TG expose and execute identical guarded actions.
- `TASK_MAX_RETRIES` changes actual behavior.
- Cloud task lookup is bounded and sensitive URLs are always redacted.
- Full backend/frontend tests, diff checks, container build, and Unraid health verification pass before release.
