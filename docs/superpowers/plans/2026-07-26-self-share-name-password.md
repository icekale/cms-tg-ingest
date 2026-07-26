# Self-Share Name And Password Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve CMS folder names for new self-shares and make the own-share receive code configurable from Web UI with CMS and environment fallbacks.

**Architecture:** Add a small resolver at the TaskStore/config boundary, pass its result into `P115WebClient.create_long_share`, and keep the persisted task receive code authoritative downstream. Leave the existing alias-aware STRM code intact for historical tasks.

**Tech Stack:** Python 3.12, SQLite, stdlib HTTP server, Vue 3 with Naive UI, unittest/pytest.

---

### Task 1: Preserve Canonical 115 Folder Names

**Files:**
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_bridge_task_engine.py`

- [x] Add a failing test asserting `SHARE_ALIAS_PREPARED` does not call `rename_file` and persists no `asset-*` alias.
- [x] Run the focused test and verify it fails against the current rename behavior.
- [x] Change the stage to complete with the existing canonical name while retaining existing-alias compatibility.
- [x] Run the focused workflow tests.

### Task 2: Resolve And Apply The Own-Share Receive Code

**Files:**
- Modify: `app/config.py`
- Modify: `app/task_store.py`
- Modify: `app/clients/p115.py`
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_task_store.py`
- Test: `tests/test_self_share_workflow.py`
- Test: `tests/test_bridge_task_engine.py`

- [x] Add failing tests for Web override, CMS database fallback, environment fallback, and `1212` fallback.
- [x] Add a failing client test showing the preferred code is sent to `share/updateshare` and returned.
- [x] Implement the minimal resolver and client parameter.
- [x] Pass the resolved code only when creating a new share; preserve persisted codes for existing tasks.
- [x] Run focused Python tests.

### Task 3: Expose A Safe Web Setting

**Files:**
- Modify: `app/web.py`
- Modify: `app/web_api.py`
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/views/Tasks.vue`
- Test: `tests/test_web_api.py`
- Test: `tests/test_frontend.py`

- [x] Add failing API tests for masked reads, validated writes, and clearing the override.
- [x] Add the settings API and overview metadata without exposing the plaintext password.
- [x] Add a compact receive-code control beside the default STRM mode.
- [x] Run backend and frontend tests/build.

### Task 4: Documentation And Regression Verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/dockerhub-overview.md`

- [x] Document precedence and the `SELF_SHARE_OWN_SHARE_PASSWORD` fallback.
- [x] Run the complete Python test suite.
- [x] Build the Vue frontend.
- [x] Inspect the final diff for secrets, unrelated edits, and accidental changes to historical compatibility paths.
