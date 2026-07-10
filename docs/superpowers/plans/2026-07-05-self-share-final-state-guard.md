# Self-share Final-state Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep completed self-share tasks from ending with CMS direct-link STRM after CMS ordinary sync events.

**Architecture:** Extend the existing local STRM maintenance helpers in `app/media/strm.py`. Reuse the current self-share maintenance loop; do not add new services, queues, database tables, or 115 scans.

**Tech Stack:** Python stdlib, SQLite-backed existing stores, `unittest`, Docker Compose deployment on Unraid.

---

### Task 1: Guard Missing Destination And Same-TMDB Direct Duplicates

**Files:**
- Modify: `app/media/strm.py`
- Test: `tests/test_self_share_workflow.py`

- [ ] **Step 1: Write regression test for same-TMDB direct duplicate while restoring**

Add a test that creates a completed self-share row, a regenerated share STRM source folder, and a duplicate same-TMDB direct STRM folder. Expected result: restore moves share STRM to destination and deletes only the duplicate direct `.strm` file.

Run: `python3 -m unittest tests.test_self_share_workflow.SelfShareWorkflowTests.test_restore_missing_self_share_library_folder_removes_duplicate_direct_tmdb_strm`
Expected before implementation: FAIL because duplicate direct STRM remains.

- [ ] **Step 2: Implement same-TMDB direct cleanup helper**

Add `cleanup_direct_strm_for_task_identity(config, row, recognition)` to remove direct `.strm` files under library folders with the same TMDB ID as the self-share task. If no TMDB ID exists, fall back to exact `own_share_file_name` cleanup.

- [ ] **Step 3: Call cleanup after restore and when destination already exists**

In `restore_missing_self_share_library_folder()`, call the helper when destination exists and after a missing destination is restored.

- [ ] **Step 4: Verify targeted tests**

Run:
`python3 -m unittest tests.test_self_share_workflow.SelfShareWorkflowTests.test_restore_missing_self_share_library_folder_removes_duplicate_direct_tmdb_strm tests.test_self_share_workflow.SelfShareWorkflowTests.test_restore_missing_self_share_library_folder_moves_regenerated_share_strm`
Expected: OK.

### Task 2: Recover Wrong TMDB Recognition From CMS Direct STRM Signal

**Files:**
- Modify: `app/media/strm.py`
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Write regression test for wrong TMDB search result**

Add a test where TMDB search resolves JoJo to `60862`, while CMS direct STRM appears under folder `tmdb=45790`. Expected result: organizing stage adopts `45790`, finds organized folder, and removes direct STRM.

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_stage_uses_recent_direct_strm_to_recover_wrong_tmdb_search`
Expected before implementation: FAIL with organizing still deferred.

- [ ] **Step 2: Implement recent direct-library STRM finder**

Add `find_recent_direct_library_strm_source_dir()` to scan only local configured library roots and recent media-root folders. It must only return a source when there is a safe exact or single-token match.

- [ ] **Step 3: Use direct STRM signal in organizing stage**

When normal 115 organized-folder lookup fails, use the local direct STRM signal to update recognition/category, remove direct STRM, and retry the organized-folder lookup with the corrected TMDB/folder name.

- [ ] **Step 4: Verify targeted tests**

Run:
`python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_stage_uses_recent_direct_strm_to_recover_wrong_tmdb_search tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_stage_uses_direct_strm_library_as_cms_category_signal`
Expected: OK.

### Task 3: Full Verification And Unraid Deploy

**Files:**
- No additional source changes.

- [ ] **Step 1: Run full test suite**

Run: `python3 -m unittest discover -s tests`
Expected: all tests OK.

- [ ] **Step 2: Deploy to Unraid**

Run rsync excluding `.env`, `data/`, `backups/`, `docker-compose.yml`, caches, and git metadata. Then rebuild/recreate with Docker Compose in `/mnt/user/appdata/cms-tg-ingest`.

- [ ] **Step 3: Verify runtime**

Run container health, Python compile, and local JOJO STRM counts:
- `cms-tg-ingest` is healthy.
- `py_compile` succeeds for changed files.
- affected task has zero same-TMDB direct STRM and at least one share STRM.
