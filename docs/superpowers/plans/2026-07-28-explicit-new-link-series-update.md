# Explicit New-Link Series Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely bind a new 115 share link to an explicitly selected completed series task, block cross-TMDB folder reuse, and recover production task `#338` as an update child of `#328`.

**Architecture:** Keep the new link as its own TaskStore task and SubmissionStore row. Freeze that source task with the runtime-hardening branch's unclaimed compare-and-set API, atomically copy only the target series identity into the child submission, then wake the source task after both stores are consistent. Add a TaskStore lookup for existing folder owners and enforce TMDB equality before the workflow persists or shares an organized folder.

**Tech Stack:** Python 3.14-compatible standard library, SQLite, `unittest`, Telegram long polling, Docker Buildx/GitHub Actions, Unraid Compose.

## Global Constraints

- Base implementation on `fix/runtime-side-effect-stability`; never bypass its claim token, lease, operation journal, or CAS checks.
- Preserve task `#328` as completed history and leave task `#341`, file ID `3481694900213253783`, and its existing 115 share unchanged.
- A new-link update command is exactly `追更 #<completed-task-id> <one-115-share-url>`.
- Unmatched `追更 <new-url>` must not create an ordinary intake task.
- New-link targets require `cleaned/succeeded`, self-share mode, a TV category, explicit TMDB identity, and recognition type `tv`.
- A claimed or concurrently changed source task is never overwritten.
- Reusing one `own_share_file_id` is allowed only when every known owner and the current task have the same non-empty TMDB ID.
- No production database, media path, 115 folder, or share is deleted by this change.
- Reserve patch version `0.2.44`; abort release rather than overwrite the tag if `v0.2.44` already exists.

## Baseline

The isolated worktree is:

```text
/Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/explicit-new-link-series-update
```

It starts from runtime-hardening commit `ca44aa8` plus the approved design document. Baseline verification completed with `1011` Python tests passing and a clean worktree.

---

### Task 1: Atomically Prepare A Child Submission

**Files:**
- Modify: `bridge.py:535-932`
- Test: `tests/test_bridge_v02_integration.py`

**Interfaces:**
- Consumes: `ShareKey`, `SubmissionStore._connection()`, and the current submissions schema.
- Produces: `SubmissionStore.prepare_series_update_child(target_row_id: int, child_key: ShareKey, child_url: str) -> dict[str, Any] | None`.

- [ ] **Step 1: Write the failing stable-identity copy test**

Add a test that creates a completed TV target row and a partially processed child row with a wrong movie folder. The target recognition is:

```python
target_recognition = {
    "ok": True,
    "title": "X-悬案-2026-[tmdb=273114]",
    "share_name": "悬案 (2026)",
    "tmdb_id": "273114",
    "type": "tv",
    "category": "国产电视",
    "category_status": "self_share_resolved",
    "organized_parent_id": "tv-parent",
    "parent_id": "tv-parent",
}
```

Prepare the child and assert all of the following in one test:

```python
prepared = submission_store.prepare_series_update_child(
    int(target["id"]),
    bridge.ShareKey("new", "1212"),
    "https://115cdn.com/s/new?password=1212",
)
self.assertEqual(json.loads(prepared["recognition_json"]), target_recognition)
self.assertEqual(prepared["category_choice"], "国产电视")
self.assertEqual(prepared["category_status"], "selected")
self.assertEqual(prepared["workflow_mode"], "self_share_sync")
self.assertEqual(prepared["workflow_phase"], "update_requested")
self.assertEqual(prepared["status"], "received")
self.assertIsNone(prepared["own_share_file_id"])
self.assertIsNone(prepared["own_share_code"])
self.assertIsNone(prepared["move_status"])
self.assertIsNone(prepared["emby_status"])
self.assertIsNone(prepared["cleanup_status"])
self.assertEqual(submission_store.find_by_id(int(target["id"]))["own_share_code"], "old-share")
```

- [ ] **Step 2: Write the missing-target rollback test**

Call the method with target row ID `99999`, assert it returns `None`, and assert `find_by_key(ShareKey("new", "1212"))` is still `None`. This proves an invalid target does not leave a child row behind.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_prepare_series_update_child_copies_only_stable_identity \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_prepare_series_update_child_missing_target_creates_nothing
```

Expected: both tests fail with `AttributeError` because `prepare_series_update_child` does not exist.

- [ ] **Step 4: Implement one SubmissionStore transaction**

Add the following method next to `reset_self_share_for_update`:

```python
def prepare_series_update_child(
    self,
    target_row_id: int,
    child_key: ShareKey,
    child_url: str,
) -> dict[str, Any] | None:
    now = time.time()
    with self._lock, self._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT * FROM submissions WHERE id = ?",
            (int(target_row_id),),
        ).fetchone()
        if target is None:
            return None
        if (
            str(target["share_code"] or "") == child_key.share_code
            and str(target["receive_code"] or "") == child_key.receive_code
        ):
            return None
        try:
            target_recognition = json.loads(str(target["recognition_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            target_recognition = {}
        if not isinstance(target_recognition, dict):
            target_recognition = {}
        recognition_json = json.dumps(target_recognition, ensure_ascii=False, sort_keys=True)
        category = str(
            target["category_choice"]
            or target["category_final"]
            or target_recognition.get("category")
            or ""
        ).strip()
        conn.execute(
            """
            INSERT INTO submissions (
                share_code, receive_code, url, title, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'received', ?, ?)
            ON CONFLICT(share_code, receive_code) DO UPDATE SET
                url = excluded.url,
                updated_at = excluded.updated_at
            """,
            (
                child_key.share_code,
                child_key.receive_code,
                str(child_url),
                str(target["title"] or ""),
                now,
                now,
            ),
        )
        child = conn.execute(
            "SELECT id FROM submissions WHERE share_code = ? AND receive_code = ?",
            (child_key.share_code, child_key.receive_code),
        ).fetchone()
        if child is None or int(child["id"]) == int(target_row_id):
            return None
        conn.execute(
            """
            UPDATE submissions
            SET cms_task_id = NULL,
                title = ?, status = 'received', last_error = NULL,
                category_choice = ?, category_status = 'selected',
                recognition_json = ?, workflow_mode = 'self_share_sync',
                workflow_phase = 'update_requested',
                own_share_file_id = NULL, own_share_file_name = NULL,
                own_share_code = NULL, own_share_receive_code = NULL,
                own_share_url = NULL, share_sync_status = NULL,
                canonical_manifest_json = NULL, share_alias_name = NULL,
                share_alias_level = NULL, share_validation_status = NULL,
                share_validation_error = NULL, share_probe_at = NULL,
                share_invalid_at = NULL, share_invalid_reason = NULL,
                source_path = NULL,
                dest_path = NULL, move_status = NULL, move_error = NULL,
                move_started_at = NULL, move_finished_at = NULL,
                category_final = NULL, emby_status = NULL,
                emby_item_id = NULL, emby_title = NULL, emby_path = NULL,
                emby_parent = NULL, cleanup_status = NULL,
                cleanup_file_id = NULL, cleanup_error = NULL,
                cleanup_finished_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                str(target["title"] or ""),
                category,
                recognition_json,
                now,
                int(child["id"]),
            ),
        )
        prepared = conn.execute(
            "SELECT * FROM submissions WHERE id = ?",
            (int(child["id"]),),
        ).fetchone()
    return self._row_to_dict(prepared)
```

Keep the target and child identity validation in the bridge helper from Task 3; this store method is responsible only for one atomic copy/reset transaction.

- [ ] **Step 5: Run focused and SubmissionStore regression tests**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_prepare_series_update_child_copies_only_stable_identity \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_prepare_series_update_child_missing_target_creates_nothing \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_completed_tv_task_exposes_update_button_and_resets_for_new_run
```

Expected: PASS.

- [ ] **Step 6: Commit the atomic submission preparation**

```bash
git add bridge.py tests/test_bridge_v02_integration.py
git commit -m "feat: prepare series update child submissions"
```

---

### Task 2: Query Existing Folder Owners

**Files:**
- Modify: `app/task_store.py:921-940`
- Test: `tests/test_task_store.py:820`

**Interfaces:**
- Consumes: TaskStore metadata JSON and `TaskSnapshot.from_row`.
- Produces: `TaskStore.list_tasks_by_own_share_file_id(file_id: str, *, exclude_task_id: int | None = None) -> list[TaskSnapshot]`.

- [ ] **Step 1: Write the failing exact-owner lookup test**

Create three tasks. Give two tasks metadata `own_share_file_id="folder-1"`, give the third `folder-2`, and assert:

```python
owners = store.list_tasks_by_own_share_file_id("folder-1")
self.assertEqual([task.id for task in owners], [second.id, first.id])
self.assertEqual(
    [task.id for task in store.list_tasks_by_own_share_file_id("folder-1", exclude_task_id=second.id)],
    [first.id],
)
self.assertEqual(store.list_tasks_by_own_share_file_id(""), [])
```

Record the owner metadata through `record_event`; do not write SQL directly in the test.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest tests.test_task_store.TaskStoreTests.test_list_tasks_by_own_share_file_id_returns_exact_other_owners
```

Expected: FAIL because the lookup method does not exist.

- [ ] **Step 3: Implement the exact local metadata query**

Add:

```python
def list_tasks_by_own_share_file_id(
    self,
    file_id: str,
    *,
    exclude_task_id: int | None = None,
) -> list[TaskSnapshot]:
    normalized = str(file_id or "").strip()
    if not normalized:
        return []
    params: list[Any] = [normalized]
    exclude_clause = ""
    if exclude_task_id is not None:
        exclude_clause = " AND id <> ?"
        params.append(int(exclude_task_id))
    with self._lock, self._connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM tasks
            WHERE json_valid(metadata_json)
              AND CAST(json_extract(metadata_json, '$.own_share_file_id') AS TEXT) = ?
              {exclude_clause}
            ORDER BY updated_at DESC, id DESC
            """,
            params,
        ).fetchall()
    return [self._snapshot(row) for row in rows]
```

This query is local SQLite state only and does not make a 115 request.

- [ ] **Step 4: Run focused TaskStore regressions**

Run:

```bash
python3 -m unittest \
  tests.test_task_store.TaskStoreTests.test_list_tasks_by_own_share_file_id_returns_exact_other_owners \
  tests.test_task_store.TaskStoreTests.test_compare_and_set_transition_records_initial_and_target_events_atomically \
  tests.test_task_store.TaskStoreTests.test_find_task_by_share_key_returns_only_matching_task
```

Expected: PASS.

- [ ] **Step 5: Commit the lookup**

```bash
git add app/task_store.py tests/test_task_store.py
git commit -m "feat: find tasks that own a shared folder"
```

---

### Task 3: Bind A New Link To An Explicit Series Task

**Files:**
- Modify: `bridge.py:2834-2950`
- Modify: `bridge.py:3500-3565`
- Modify: `bridge.py:211` (Telegram help text)
- Test: `tests/test_bridge_v02_integration.py:1147-1270`

**Interfaces:**
- Consumes: `SubmissionStore.prepare_series_update_child`, `TaskStore.compare_and_set_transition`, `TaskStore.record_event`, `TaskStore.upsert_task`, and `reprocess_delete_keys_for(task)`.
- Produces: `parse_explicit_series_update_command(text: str) -> tuple[bool, int | None, str]`.
- Produces: `start_series_update_from_link(target_task, key, link, chat_id, store, task_store, *, source) -> tuple[Any | None, str]`.
- Result codes: `started`, `already_started`, `not_eligible`, `source_busy`, `source_conflict`, and `failed`.

- [ ] **Step 1: Write parser tests**

Add table-driven assertions for:

```python
self.assertEqual(
    bridge.parse_explicit_series_update_command("追更 #328 https://115cdn.com/s/new?password=1212"),
    (True, 328, "https://115cdn.com/s/new?password=1212"),
)
self.assertEqual(
    bridge.parse_explicit_series_update_command("追更：https://115cdn.com/s/old?password=1212"),
    (True, None, "https://115cdn.com/s/old?password=1212"),
)
self.assertEqual(
    bridge.parse_explicit_series_update_command("https://115cdn.com/s/plain?password=1212"),
    (False, None, "https://115cdn.com/s/plain?password=1212"),
)
```

- [ ] **Step 2: Write the `#338`-shape child repair test**

Create a completed target with TMDB `273114` and an existing unclaimed source at `own_share_created/running` whose child submission and metadata point to movie folder `3481694900213253783`. Call `start_series_update_from_link` and assert:

```python
updated, result = bridge.start_series_update_from_link(
    target,
    bridge.ShareKey("new", "1212"),
    "https://115cdn.com/s/new?password=1212",
    "464100862",
    submission_store,
    task_store,
    source="生产修复",
)
self.assertEqual(result, "started")
self.assertEqual(updated.id, source_task.id)
self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
self.assertEqual(updated.status, TaskStatus.PENDING)
self.assertEqual(updated.tmdb_id, "273114")
self.assertEqual(updated.category, "国产电视")
self.assertEqual(updated.metadata["series_update_parent_task_id"], target.id)
self.assertEqual(updated.metadata["update_requested_run"], 1)
self.assertEqual(updated.metadata["update_received_run"], 0)
self.assertNotIn("own_share_file_id", updated.metadata)
self.assertNotIn("share_create_status", updated.metadata)
self.assertEqual(updated.next_run_at, 0)
```

Also assert the child submission contains TV recognition, the target row retains its old share, and the wrong movie folder fields are gone.

- [ ] **Step 3: Write source safety tests**

Add separate tests proving:

- a source with a live `claimed_by`/`claim_token` returns `source_busy` and is unchanged;
- a source with `series_update_parent_task_id` pointing to another task returns `source_conflict`;
- a source that already has an `own_share_code` from a completed remote side effect returns `source_conflict` rather than orphaning that share;
- repeating the same command after a child is queued returns `already_started` without incrementing `update_requested_run`.

- [ ] **Step 4: Write command routing tests**

Replace the old fall-through expectation with these behaviors:

```python
bridge.handle_update(self.update("追更 https://115cdn.com/s/new?password=1212"), ...)
self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))
self.assertIn("需指定历史任务号", telegram.messages[-1][1])
```

Add targeted command tests for a successful new link, a movie target, an unfinished target, a missing target, more than one URL, and a magnet link. Every rejected case must leave the new share key absent. Keep the existing exact-link test green.

- [ ] **Step 5: Run all new tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_parse_explicit_series_update_command \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_new_link_series_update_repairs_existing_unclaimed_source \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_new_link_series_update_rejects_claimed_source \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_new_link_series_update_rejects_different_parent \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_new_link_series_update_is_idempotent \
  tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_series_update_command_requires_target_for_new_link
```

Expected: FAIL because the parser and new-link helper do not exist and the current command still creates ordinary intake.

- [ ] **Step 6: Add the parser and stable target validation**

Add:

```python
_EXPLICIT_SERIES_UPDATE_RE = re.compile(
    r"^追更(?:\s*#(?P<task_id>\d+))?(?:\s*[：:])?\s*(?P<payload>.*)$",
    re.DOTALL,
)


def parse_explicit_series_update_command(text: str) -> tuple[bool, int | None, str]:
    match = _EXPLICIT_SERIES_UPDATE_RE.match(str(text or "").strip())
    if not match:
        return False, None, str(text or "")
    task_id = int(match.group("task_id")) if match.group("task_id") else None
    return True, task_id, str(match.group("payload") or "").strip()
```

Add a target identity helper that reads the target submission's `recognition_json` and resolves identity in this order:

```python
tmdb_id = str(target_task.tmdb_id or target_task.metadata.get("tmdb_id") or recognition.get("tmdb_id") or "").strip()
category = str(
    target_task.category
    or target_task.metadata.get("category")
    or target_row.get("category_final")
    or target_row.get("category_choice")
    or recognition.get("category")
    or ""
).strip()
```

Return `None` unless status/stage, workflow mode, category, TMDB ID, and `recognition["type"] == "tv"` all satisfy the global constraints.

- [ ] **Step 7: Implement guarded freeze, child preparation, and activation**

Import `reprocess_delete_keys_for` from `app.task_store`. In `start_series_update_from_link`:

1. Validate the target and load its submission before calling `upsert_task`.
2. Upsert or load the source task by `key`.
3. Reject a different parent relation, a live claim, or an already-created foreign share.
4. Return `already_started` for an active child already linked to the same parent.
5. Freeze the source with:

```python
frozen = task_store.compare_and_set_transition(
    child.id,
    child.current_stage,
    {child.status},
    require_unclaimed=True,
    target_stage=TaskStage.RECEIVED,
    target_status=TaskStatus.PENDING,
    target_event_message=f"{source}绑定历史剧集，准备子任务提交记录",
    metadata_patch={
        "series_update_parent_task_id": int(target_task.id),
        "series_update_parent_submission_id": int(target_row["id"]),
        "update_requested_run": update_run,
        "update_received_run": update_run - 1,
        "update_started_at": started_at,
        "previous_own_share_code": str(target_row.get("own_share_code") or ""),
        "force_reprocess": True,
        "reprocess_started_at": started_at,
        "recognition": recognition,
        "title": title,
        "tmdb_id": tmdb_id,
        "category": category,
    },
    metadata_delete_keys=reprocess_delete_keys_for(child),
    next_run_at=-1,
    clear_errors=True,
    clear_claim=True,
    expected_updated_at=child.updated_at,
)
```

6. Call `store.prepare_series_update_child` only after `frozen` succeeds.
7. Activate with `record_event`, requiring `received/pending` and `frozen.updated_at`, setting title/TMDB/category/submission ID, deleting defer and share-create metadata, and setting `next_run_at=0`.
8. If child submission preparation returns `None` or raises, record `needs_action` against the frozen snapshot with `next_run_at=-1`; do not wake the runner.

The target validator must also reject a non-empty `target_task.claimed_by`. Refactor
the existing same-link `start_series_update_task` to freeze `cleaned/succeeded`
through `compare_and_set_transition(require_unclaimed=True,
expected_updated_at=task.updated_at)` before resetting its submission. Activate
it only after the reset succeeds. Add a regression test where a claimed completed
series remains unchanged.

- [ ] **Step 8: Route Telegram commands without fall-through**

Parse the command before `parse_media_sources`. For a targeted command, require exactly one parsed source with `source_type == "share"`. For an untargeted update:

- retain exact-link `start_series_update_task` behavior;
- when no eligible exact task exists, append `新分享链接追更需指定历史任务号，例如：追更 #328 <115链接>` and `continue`;
- never reach ordinary `upsert_task` intake for an unmatched explicit update.

Update `HELP_TEXT` with `追更 #任务号 <新115链接>`.

- [ ] **Step 9: Run focused command and bridge regressions**

Run:

```bash
python3 -m unittest tests.test_bridge_v02_integration
```

Expected: PASS with the new no-fall-through contract and existing button/exact-link behavior intact.

- [ ] **Step 10: Commit explicit binding**

```bash
git add bridge.py tests/test_bridge_v02_integration.py
git commit -m "feat: bind new links to explicit series tasks"
```

---

### Task 4: Block Cross-TMDB Folder Ownership

**Files:**
- Modify: `app/workflows/self_share.py:100-140`
- Modify: `app/workflows/self_share.py:1264-1345`
- Modify: `app/workflows/self_share.py:1352-1380`
- Test: `tests/test_bridge_task_engine.py:700-980`

**Interfaces:**
- Consumes: `TaskStore.list_tasks_by_own_share_file_id` and existing TMDB extraction helpers.
- Produces: `BridgeSelfShareTaskWorkflow._conflicting_folder_owner(task, folder, recognition, row, share_name) -> Any | None`.

- [ ] **Step 1: Write the mismatched-owner organizing test**

Create an owner task with:

```python
{"own_share_file_id": "shared-folder", "tmdb_id": "9533", "recognition": {"tmdb_id": "9533"}}
```

Create a current task/submission with TV recognition `tmdb_id=273114`. Make `FakeP115.folder` return file ID `shared-folder` with a correctly named `tmdb=273114` folder. Assert organizing returns `StageOutcome.NEEDS_ACTION`, the submission never persists `shared-folder`, and the message mentions another TMDB task.

- [ ] **Step 2: Write the same-owner and ambiguous-owner tests**

Add separate tests proving:

- an owner with the same non-empty TMDB `273114` permits organizing to complete;
- an owner with no resolvable TMDB causes `NEEDS_ACTION` because equality cannot be proven;
- recognizing a legacy persisted cross-TMDB folder stops before `create_long_share` is called.

- [ ] **Step 3: Run the four focused tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_stage_rejects_folder_owned_by_different_tmdb_task \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_stage_allows_folder_owned_by_same_tmdb_task \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_stage_rejects_folder_with_ambiguous_owner \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_recognizing_stage_stops_legacy_cross_tmdb_folder_before_share
```

Expected: the different/ambiguous owner tests fail because ownership is not queried; the same-owner behavior defines the allowed path.

- [ ] **Step 4: Implement owner TMDB extraction**

Add a small helper that resolves another task's identity without network access:

```python
def task_tmdb_identity(task: Any) -> str:
    metadata = getattr(task, "metadata", {}) or {}
    recognition = metadata.get("recognition")
    if not isinstance(recognition, dict):
        recognition = {}
    return str(
        getattr(task, "tmdb_id", "")
        or metadata.get("tmdb_id")
        or recognition.get("tmdb_id")
        or extract_tmdb_id_from_name(str(metadata.get("own_share_file_name") or ""))
        or ""
    ).strip()
```

Implement `_conflicting_folder_owner` by resolving the current expected TMDB as
`expected_task_tmdb_id(recognition, row) or task_tmdb_identity(task)`, querying
every other owner, and returning the first owner when either identity is empty
or differs.

- [ ] **Step 5: Enforce the guard at both boundaries**

In `_stage_organizing`, call the guard after all folder resolution and verification but before `update_self_share`. Return:

```python
StageResult.needs_action(
    "CMS 整理目录已被其他 TMDB 任务占用，已阻止创建自有分享",
    {"submission_id": int(row["id"]), "own_share_file_id": ""},
)
```

Call the same guard at the start of `_stage_recognizing` as defense in depth for persisted legacy rows. This path must stop before child-name lookup, aliasing, or share creation.

- [ ] **Step 6: Run workflow and task-engine regressions**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine tests.test_self_share_workflow
```

Expected: PASS.

- [ ] **Step 7: Commit the folder guard**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "fix: block cross-tmdb folder reuse"
```

---

### Task 5: Integrate The Concurrent Runtime Optimization

**Files:**
- Review: all files changed by `fix/runtime-side-effect-stability`
- Verify: `bridge.py`, `app/task_store.py`, `app/workflows/self_share.py`

**Interfaces:**
- Consumes: the final clean tip of `fix/runtime-side-effect-stability`.
- Produces: one feature branch containing both the approved runtime hardening and the new-link series update without dropped lease/operation behavior.

- [ ] **Step 1: Confirm both worktrees are clean**

Run:

```bash
git status --short --branch
git -C ../runtime-side-effect-stability status --short --branch
```

Expected: neither command lists modified or untracked files.

- [ ] **Step 2: Inspect optimization commits added after the feature baseline**

Run:

```bash
git log --oneline ca44aa8..fix/runtime-side-effect-stability
git diff --name-status ca44aa8..fix/runtime-side-effect-stability
```

Read every new diff touching `bridge.py`, `app/task_store.py`, `app/workflows/self_share.py`, or their tests before integrating.

- [ ] **Step 3: Rebase onto the final optimization tip**

Run:

```bash
git rebase fix/runtime-side-effect-stability
```

Resolve conflicts by retaining the optimization branch's claim token, lease renewal, operation journal, and receive reconciliation behavior, then reapply only the explicit update and folder-owner changes. Do not select an entire side for an overlapping core file.

- [ ] **Step 4: Run overlap-focused tests**

Run:

```bash
python3 -m unittest \
  tests.test_task_store \
  tests.test_task_runner \
  tests.test_bridge_v02_integration \
  tests.test_bridge_task_engine \
  tests.test_http_clients
```

Expected: PASS.

- [ ] **Step 5: Run full baseline-equivalent verification**

Run:

```bash
python3 -m compileall -q app bridge.py doctor.py
python3 -m unittest discover -s tests -p 'test*.py' -q
git diff --check
```

Expected: at least `1011` tests plus the new tests pass, compilation succeeds, and no whitespace errors are reported.

---

### Task 6: Release `0.2.44` And Recover Production Task `#338`

**Files:**
- Modify: `app/__init__.py:3`
- Modify: `CHANGELOG.md:3`
- Modify: `README.md:36,440-453`
- Modify: `docs/dockerhub-overview.md:28,38`
- Modify: `tests/test_release_workflows.py:35-45`

**Interfaces:**
- Consumes: the fully integrated and verified feature branch.
- Produces: GitHub tag `v0.2.44`, multi-architecture image `icekale/cms-tg-ingest:0.2.44`, a healthy Unraid deployment, and a correctly rebound task `#338`.

- [ ] **Step 1: Prove the release tag is unused**

Run:

```bash
git fetch origin --tags
test -z "$(git tag -l v0.2.44)"
```

Expected: both commands exit 0 and no `v0.2.44` tag exists. Stop release if the assertion fails.

- [ ] **Step 2: Update release tests first**

Change `tests/test_release_workflows.py` expectations from `0.2.43` to `0.2.44`, then run:

```bash
python3 -m unittest tests.test_release_workflows
```

Expected: FAIL because README and Docker Hub documentation still reference `0.2.43`.

- [ ] **Step 3: Bump version and public fixed tags**

Set `app.__version__` to `0.2.44`, add a dated changelog entry describing explicit new-link updates and cross-TMDB ownership protection, and update every current release command/image reference in README and `docs/dockerhub-overview.md` to `0.2.44`.

- [ ] **Step 4: Run all release gates**

Run:

```bash
python3 -m compileall -q app bridge.py doctor.py
python3 -m unittest discover -s tests -p 'test*.py' -q
npm ci --prefix frontend
npm test --prefix frontend
npm run build --prefix frontend
git diff --check
git status --short --branch
```

Expected: all commands succeed; status lists only reviewed release files before commit.

- [ ] **Step 5: Commit and push the integrated release**

```bash
git add app/__init__.py CHANGELOG.md README.md docs/dockerhub-overview.md tests/test_release_workflows.py
git commit -m "release: publish v0.2.44"
git push origin HEAD:main
git tag -a v0.2.44 -m "release v0.2.44"
git push origin v0.2.44
```

Do not force-push. If `origin/main` advanced, integrate it normally, rerun Step 4, and then push.

- [ ] **Step 6: Wait for and verify the multi-architecture image**

Run:

```bash
release_run_id=$(gh run list --workflow release-images.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$release_run_id"
docker buildx imagetools inspect icekale/cms-tg-ingest:0.2.44
docker buildx imagetools inspect icekale/cms-tg-ingest:latest
```

Expected: workflow succeeds and both `linux/amd64` and `linux/arm64` manifests exist.

- [ ] **Step 7: Snapshot production evidence before stopping the container**

Read tasks `#328`, `#338`, and `#341` and store a sanitized JSON snapshot containing task ID, stage, status, TMDB, category, submission ID, `own_share_file_id`, `own_share_code`, claim fields, and `updated_at`. Do not print share URLs, receive codes, cookies, tokens, or environment variables.

- [ ] **Step 8: Stop only CMS ingest and back up both SQLite databases**

On Unraid, resolve the Compose directory, stop only `cms-tg-ingest`, and run:

```bash
series_repair_stamp=$(date +%Y%m%d-%H%M%S)
cp -a /mnt/user/appdata/cms-tg-ingest/data/tasks.db \
  "/mnt/user/appdata/cms-tg-ingest/data/backups/tasks-before-series-338-${series_repair_stamp}.db"
cp -a /mnt/user/appdata/cms-tg-ingest/data/submissions.db \
  "/mnt/user/appdata/cms-tg-ingest/data/backups/submissions-before-series-338-${series_repair_stamp}.db"
docker run --rm --entrypoint python \
  -v /mnt/user/appdata/cms-tg-ingest/data/backups:/backups:ro \
  icekale/cms-tg-ingest:0.2.43 \
  -c 'import sqlite3,sys; [print(path, sqlite3.connect(path).execute("PRAGMA quick_check").fetchone()[0]) for path in sys.argv[1:]]' \
  "/backups/tasks-before-series-338-${series_repair_stamp}.db" \
  "/backups/submissions-before-series-338-${series_repair_stamp}.db"
```

Expected: both backups report `ok`. If either check fails, restart the old container and stop.

- [ ] **Step 9: Deploy the pinned image and verify health before repair**

Change only the Compose image tag to `icekale/cms-tg-ingest:0.2.44`, then run:

```bash
docker compose pull cms-tg-ingest
docker compose up -d --no-build cms-tg-ingest
docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' cms-tg-ingest
docker exec cms-tg-ingest python /app/doctor.py --quiet
curl -fsS http://127.0.0.1:8788/api/v1/health
docker logs --tail=200 cms-tg-ingest
```

Expected: running/healthy, doctor exits 0, health is 2xx with a fresh runner heartbeat, and logs contain no migration or runner exception.

- [ ] **Step 10: Guardedly bind existing task `#338` to `#328`**

Stop the newly verified service again so the runner cannot race the identity
check. Re-read both tasks through a one-off Compose container and require
`#338.claimed_by == ""`. Invoke the same helper used by Telegram without
embedding the private URL in the command:

```bash
docker compose stop cms-tg-ingest
docker compose run --rm --no-deps --entrypoint python cms-tg-ingest \
  -c 'import bridge; from app.task_store import TaskStore; ts=TaskStore("/data/tasks.db"); ss=bridge.SubmissionStore("/data/submissions.db"); parent=ts.find_task(328); child=ts.find_task(338); updated,result=bridge.start_series_update_from_link(parent, bridge.ShareKey(child.share_code, child.receive_code), child.url, child.chat_id, ss, ts, source="生产修复"); print(result, updated.id if updated else "", updated.current_stage.value if updated else "", updated.status.value if updated else "")'
```

Expected output starts with `started 338 received pending`. A `source_busy`, `source_conflict`, `not_eligible`, or `failed` result stops the repair; do not edit SQLite manually.

- [ ] **Step 11: Verify identity before the runner reaches sharing**

Read task `#338` and child submission immediately. Require:

```text
series_update_parent_task_id = 328
tmdb_id = 273114
type = tv
category = 国产电视
own_share_file_id != 3481694900213253783
share_create_status != pending
```

Also compare the saved `#341` snapshot and confirm its task/submission/share fields are unchanged.

Only after these assertions pass, restart the service with:

```bash
docker compose up -d --no-build cms-tg-ingest
```

- [ ] **Step 12: Monitor to a safe terminal result**

Poll `#338` at its scheduled cadence without waking or retrying it manually. Success is `cleaned/succeeded` with a TMDB `273114` series folder and healthy Emby confirmation. `needs_action` is acceptable only when the message is specific and no wrong folder/share was created; investigate before any retry. Never loosen share creation timestamps or copy `#341`'s share code.

- [ ] **Step 13: Final production verification**

Run container health, doctor, API health, and the latest 200 logs again. Confirm `runner_heartbeat_stale=false`, no repeated `等待 115 完成分享创建` loop for `#338`, and no new error for task `#341`.

---

## Final Acceptance Checklist

- [ ] `追更 #328 <new-url>` queues the new URL with TMDB `273114`, type `tv`, and category `国产电视`.
- [ ] Unmatched `追更 <new-url>` creates no ordinary task and tells the user to include a historical task ID.
- [ ] Exact-link and Telegram button updates remain compatible.
- [ ] Claimed, concurrently changed, movie, unfinished, identity-less, and differently linked tasks are rejected without mutation.
- [ ] A cross-TMDB `own_share_file_id` is blocked before share creation; same-TMDB series reuse remains allowed.
- [ ] Runtime-hardening lease, claim-token, receive reconciliation, and operation-journal tests remain green.
- [ ] Full Python and frontend verification passes after synchronizing the final optimization branch.
- [ ] Docker image `0.2.44` contains amd64 and arm64 manifests and Unraid runs the pinned tag healthy.
- [ ] Production task `#338` no longer references file ID `3481694900213253783` and finishes under parent task `#328` identity.
- [ ] Task `#341`, its folder, and its existing share remain unchanged.
