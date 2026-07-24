# Vue UI Feature Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the old admin's operational controls and diagnostics into the Vue/Naive UI, then make `/` redirect to `/app/` while retaining server-rendered fallback pages.

**Architecture:** Keep the Python workflow and existing HTML routes as the source of truth. Add authenticated JSON endpoints in `WebApp`, reusing the existing transition and service methods, and let Vue pages call those endpoints and refresh their data. No new persistence or background worker is introduced.

**Tech Stack:** Python `unittest`, existing `WebApp`/`TaskStore`, Vue 3, Vue Router, Naive UI, Vite, Docker.

---

### Task 1: Define API parity contracts with failing tests

**Files:**
- Modify: `tests/test_web_api.py`
- Modify: `tests/test_web_admin.py`
- Modify: `docs/superpowers/specs/2026-07-24-default-vue-ui-design.md`

- [ ] **Step 1: Add tests for root redirect and legacy overview.**
- [ ] **Step 2: Add tests for task action JSON responses** for `retry`, `emby`, `restore`, and `reprocess`, including a 404 for an unknown task and a 409/no-op for an ineligible action.
- [ ] **Step 3: Add tests for history cleanup and quality endpoints**, asserting completed rows are removed, repair count is returned, and quality settings/run calls delegate to the configured automation.
- [ ] **Step 4: Add tests for HDHive action routes** with a small fake service/scheduler, asserting pause/resume/delete/check/confirm/settings/run paths call the existing methods and return JSON.
- [ ] **Step 5: Run `python3 -m unittest tests.test_web_api tests.test_web_admin -v` and confirm the new endpoint tests fail because the routes do not exist yet.

### Task 2: Implement authenticated JSON action endpoints

**Files:**
- Modify: `app/web.py:handle_request` and `app/web.py:_handle_api`
- Modify: `app/web_api.py`
- Test: `tests/test_web_api.py`, `tests/test_web_admin.py`

- [ ] **Step 1: Extract the existing task transition branches into one helper** so HTML and JSON actions use identical eligibility checks and metadata patches.
- [ ] **Step 2: Add `POST /api/v1/tasks/<id>/actions/<action>`** for `retry`, `emby`, `restore`, and `reprocess`; return serialized task detail on success, `404` for a missing task, and `409` with a stable error code when the action is not currently eligible.
- [ ] **Step 3: Add `POST /api/v1/history/clear`, `POST /api/v1/quality/fix`, `POST /api/v1/quality/run`, `POST /api/v1/quality/settings`, and `POST /api/v1/quality/settings/reset`** using the same methods as the old HTML forms.
- [ ] **Step 4: Add HDHive JSON action routes** for subscription pause/resume/delete/check, item confirmation, scheduler settings, and run-now, preserving existing async behavior for checks.
- [ ] **Step 5: Extend `api_quality`, `serialize_health`, and `serialize_hdhive` payloads only with fields needed by the UI; redact tokens, cookies, and share passwords.
- [ ] **Step 6: Run the focused tests and confirm all API contracts pass.

### Task 3: Migrate task and quality controls into Vue

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/views/Overview.vue`
- Modify: `frontend/src/views/Tasks.vue`
- Modify: `frontend/src/views/TaskDetail.vue`
- Modify: `frontend/src/views/Quality.vue`
- Modify: `frontend/src/views/Health.vue`
- Modify: `frontend/src/App.vue`
- Test: `tests/test_frontend.py`

- [ ] **Step 1: Add typed-by-convention API wrappers** for actions, quality controls, history cleanup, and HDHive controls; convert non-2xx JSON responses into user-visible errors.
- [ ] **Step 2: Add task action buttons** to task detail with eligibility-aware display, confirmation for reprocess/restore, action loading state, and reload after success.
- [ ] **Step 3: Add timeline and observability sections** to task detail using `events`, `why_slow`, stage elapsed time, 115 call counts, safe metadata, and error details.
- [ ] **Step 4: Add refresh, repair, automation settings, and history cleanup controls** to the overview/quality pages; keep dangerous operations behind `NPopconfirm`.
- [ ] **Step 5: Add health wait details and latest-problem links** to the health page.
- [ ] **Step 6: Run `npm --prefix frontend run build` and the frontend contract tests.

### Task 4: Migrate HDHive controls and data views

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/views/Hdhive.vue`
- Modify: `frontend/src/styles.css`
- Test: `tests/test_frontend.py`

- [ ] **Step 1: Render account quota, scheduler status/settings, subscription actions, pending confirmations, and unlock records with points/time/task ID.
- [ ] **Step 2: Add pause/resume/delete/check, confirm unlock, save settings, and run-now buttons with loading/error feedback.
- [ ] **Step 3: Verify sensitive fields remain absent from rendered JSON and the UI.

### Task 5: Switch the default entry point and verify the release

**Files:**
- Modify: `app/web.py:handle_request`
- Modify: `tests/test_web_api.py`
- Modify: `README.md` and `docs/dockerhub-overview.md`

- [ ] **Step 1: Make `GET /` return `302 Location: /app/` and serve the previous overview at `GET /legacy`.
- [ ] **Step 2: Run `python3 -W error::ResourceWarning -m unittest discover -s tests -v`, `npm --prefix frontend run build`, and `docker build -t cms-tg-ingest:test .`.
- [ ] **Step 3: Commit the feature, push `main`, create the next release tag, and wait for CI plus multi-architecture Docker Hub/GHCR publishing.
- [ ] **Step 4: Update the Unraid Compose image, recreate the container, and verify `/`, `/app/`, `/api/v1/health`, container health, and `doctor.py --quiet`.
