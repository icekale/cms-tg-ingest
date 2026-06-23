# Safe Multitask Guards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve multi-task safety without increasing high concurrency.

**Architecture:** Keep the current conservative worker model, add local resource-lock metadata and queue visibility. 115-sensitive and file-moving stages get explicit lock keys so tasks do not collide on the same share, TMDB item, or destination path. Web and health pages show queue/running/lock-wait status so stuck tasks are easier to diagnose.

**Tech Stack:** Python stdlib, SQLite-backed `TaskStore`, existing `TaskRunner`, existing Web admin.

---

### Task 1: Add Resource Lock Decisions

**Files:**
- Modify: `app/task_runner.py`
- Modify: `tests/test_task_runner.py`

- [ ] Write tests that a task stage produces lock metadata before execution.
- [ ] Implement a small stage-to-lock mapping: `received/organizing/own_share_created/cleaned` use `115:global`; `strm_ready/moved/emby_confirmed` also use destination or TMDB lock when metadata is available.
- [ ] Verify lock metadata is stored as `_lock_key` / `_lock_reason` in task metadata.

### Task 2: Add Queue Visibility

**Files:**
- Modify: `app/task_store.py`
- Modify: `app/task_health.py`
- Modify: `app/web.py`
- Modify: `tests/test_task_store.py`
- Modify: `tests/test_web_admin.py`

- [ ] Write tests for a lightweight queue summary: pending count, running count, needs-action count, lock-wait count.
- [ ] Implement a read-only summary query over recent TaskStore rows.
- [ ] Show lock wait reason on `/health` and `/` without scanning 115.

### Task 3: Deploy Conservatively

**Files:**
- Modify: `.env.example`

- [ ] Add documented defaults: `TASK_MAX_CONCURRENT=1` and keep current behavior safe.
- [ ] Run `python3 -m py_compile bridge.py doctor.py app/*.py`.
- [ ] Run `python3 -W error::ResourceWarning -m unittest discover -s tests -v`.
- [ ] Deploy to Unraid and verify `/health` and `/quality` still load.
