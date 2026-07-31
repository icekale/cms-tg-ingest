# Completed Emby Drift Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover historical self-share STRM folders removed by CMS synchronization, refresh the matching Emby library, and make the web task detail warn when a completed task's media directory is currently missing.

**Architecture:** Keep the existing self-share maintenance loop as the recovery owner, but remove its unsafe one-hour eligibility cutoff so it can reconcile all persisted, valid self-share tasks. Pass the existing Emby client into that loop only for a library refresh after a successful restore. Add a read-only live completion-drift field to the task API and render it in the Vue task detail; persisted task success remains unchanged until the recovery workflow has evidence to change it.

**Tech Stack:** Python 3 `unittest`, SQLite-backed `SubmissionStore`/`TaskStore`, Vue 3 + Naive UI, existing CMS/Emby clients.

---

### Task 1: Add regression coverage for historical recovery and Emby refresh

**Files:**
- Modify: `tests/test_self_share_workflow.py`
- Modify: `tests/test_web_api.py`

- [x] **Step 1: Write a failing maintenance test**

Add a test with an old `updated_at`, a missing library destination, and an available regenerated self-share STRM source. Call `restore_missing_self_share_library_folders` without `recent_seconds` and pass a fake Emby client; assert the source moves, the result count is `1`, and Emby receives the destination path for refresh.

- [x] **Step 2: Run the focused test and verify it fails**

Run: `python3 -m unittest tests.test_self_share_workflow.P115FailureHandlingTests.test_maintenance_restores_historical_completed_row_and_refreshes_emby -v`

Expected: FAIL because the current function filters the old row and has no Emby refresh argument.

- [x] **Step 3: Write a failing API test**

Create a completed self-share TaskStore task whose `dest_path` points at a missing directory. Call `serialize_task` and assert it exposes `completion_drift.code == "missing_dest"` and the message `已入库但当前媒体目录缺失`.

- [x] **Step 4: Run the focused API test and verify it fails**

Run: `python3 -m unittest tests.test_web_api.WebApiTests.test_completed_task_exposes_missing_media_directory_drift -v`

Expected: FAIL because the API currently has no live completion-drift field.

### Task 2: Implement historical recovery and refresh

**Files:**
- Modify: `app/media/strm.py:1282-1300`
- Modify: `bridge.py:2738-2745,2781-2794`

- [x] **Step 1: Remove the one-hour default cutoff**

Make `recent_seconds` default to `0`, preserving the explicit opt-in cutoff for callers/tests that need bounded recovery. The normal status-repair and self-share-maintenance loops must call the historical-safe default.

- [x] **Step 2: Refresh Emby only after a successful restore**

Add an optional `emby` parameter to `restore_missing_self_share_library_folders`. After `restore_missing_self_share_library_folder` returns `restored`, call `emby.refresh_library_for_path(metadata["dest_path"])` when Emby is enabled and the method exists. Catch/log refresh failures without undoing the successfully restored file move. Pass the configured Emby client from both maintenance loops.

- [x] **Step 3: Run the focused recovery test**

Run: `python3 -m unittest tests.test_self_share_workflow.P115FailureHandlingTests.test_maintenance_restores_historical_completed_row_and_refreshes_emby -v`

Expected: PASS; the old missing destination is moved back and the fake Emby refresh list contains the destination.

### Task 3: Expose completion drift in the API and Vue detail page

**Files:**
- Modify: `app/web_api.py:195-228`
- Modify: `frontend/src/views/TaskDetail.vue`
- Modify: `frontend/src/styles.css` if a warning style is required

- [x] **Step 1: Add a read-only drift helper**

For a succeeded task at the cleaned stage, inspect the persisted `dest_path`. For self-share tasks, validate the destination with the existing self-share STRM validator when enough metadata is present; for other modes, treat a missing destination or STRM file as drift. Return `None` when healthy, otherwise return a stable object with `code`, `message`, and `detail`.

- [x] **Step 2: Include drift in serialized task data**

Add `completion_drift` to `serialize_task` without changing the persisted TaskStore status or exposing absolute paths in the API response.

- [x] **Step 3: Render the warning in the Vue task detail**

When `task.completion_drift` exists, render a warning card above the details stating `已入库但当前媒体目录缺失` and the recommended action `系统会尝试用自有分享 STRM 恢复；恢复后刷新 Emby。` Keep normal succeeded tasks unchanged.

- [x] **Step 4: Run focused API and frontend tests**

Run: `python3 -m unittest tests.test_web_api.WebApiTests.test_completed_task_exposes_missing_media_directory_drift -v`

Run: `npm test -- --runInBand` from `frontend/`.

Expected: both pass.

### Task 4: Full verification and release readiness

**Files:**
- Modify: `CHANGELOG.md` and version/docs only if the release workflow requires a versioned fix release.

- [x] **Step 1: Run Python verification**

Run: `python3 -m compileall -q app bridge.py doctor.py && python3 -m unittest discover -s tests -p 'test*.py' -q && git diff --check`

Expected: compile succeeds, all Python tests pass, and `git diff --check` is clean.

- [x] **Step 2: Build the frontend**

Run: `npm ci --ignore-scripts && npm run build` from `frontend/`.

Expected: Vite exits with code `0` and creates `frontend/dist` (ignored/generated output).

- [x] **Step 3: Inspect the diff**

Run: `git status --short && git diff --stat && git diff -- app/media/strm.py bridge.py app/web_api.py frontend/src/views/TaskDetail.vue tests/test_self_share_workflow.py tests/test_web_api.py`

Expected: only this bug fix, its tests, plan, and any explicitly required release metadata are present; no secrets or runtime data are staged.

- [ ] **Step 4: Merge and deploy only after verification**

Use `cms-release-deploy` to run the release checks, merge the reviewed branch into `main`, publish the verified image tag, update Unraid while preserving `.env`/`data`/media mounts, and verify the container health endpoint and task #355 recovery.
