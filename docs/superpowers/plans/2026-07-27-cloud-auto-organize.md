# Cloud Auto Organize Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trigger CMS auto-organize immediately after a completed cloud download is moved into the receive directory.

**Architecture:** Keep 115 output resolution and movement in `P115WebClient`. Extend the cloud-downloading workflow stage to trigger CMS after movement, persist `auto_organize_submitted` only after success, and defer CMS failures without resubmitting the cloud job.

**Tech Stack:** Python, `unittest`, SQLite-backed `TaskStore`, existing CMS and 115 clients.

---

### Task 1: Regression tests

**Files:**
- Modify: `tests/test_cloud_workflow.py`

- [ ] Add a fake CMS call counter and assert a completed cloud stage calls `run_auto_organize()` once after output resolution.
- [ ] Add a failure case asserting CMS errors return `defer`, retain cloud metadata, and do not add another cloud-download request.
- [ ] Run `python3 -m unittest tests.test_cloud_workflow -v` and confirm the new behavior fails before production code changes.

### Task 2: Cloud stage implementation

**Files:**
- Modify: `app/workflows/self_share.py:463-565`

- [ ] Keep the existing completed-output resolution as the movement boundary.
- [ ] After creating/updating the submission row, call `self.cms.run_auto_organize()`.
- [ ] On success update `workflow_phase` to `auto_organize_submitted` and complete the current stage.
- [ ] On CMS exception return a low-frequency defer result with the existing cloud metadata, without calling cloud download again or deleting files.

### Task 3: Verification and release

**Files:**
- Modify: `app/__init__.py`
- Modify: `CHANGELOG.md`
- Modify: `README.md`

- [ ] Run compile, focused tests, full tests, and `git diff --check`.
- [ ] Publish a version tag and multi-architecture Docker image through the existing release workflow.
- [ ] Update Unraid to the fixed image while preserving environment, data, and media mounts.
- [ ] Verify container health, API health, doctor output, logs, and task #343 progress.
