# Audit Stability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the confirmed data-loss, wrong-association, rate-limit, recovery, Web API, HDHive, backup, and quality-scan defects found in the v0.2.48 audit.

**Architecture:** Preserve the current workflow and storage model. Tighten identity proof at destructive boundaries, make physical I/O observable and recoverable, align the HTTP adapter with the WebApp API contract, and isolate failures at the smallest useful unit.

**Tech Stack:** Python 3.12+, `unittest`, SQLite TaskStore, stdlib HTTP server, Vue/Node test runner.

## Global Constraints

- Add no runtime dependency and no database schema migration.
- Keep existing environment variables and successful workflow behavior compatible.
- Write and run a failing regression before each production change.
- Do not increase normal 115 polling frequency.

---

### Task 1: Harden destructive 115 and identity matching

**Files:** `tests/test_self_share_workflow.py`, `tests/test_http_clients.py`, `tests/test_direct_workflow.py`, `app/clients/p115.py`, `app/clients/http.py`, `app/media/strm.py`

- [x] Add regressions proving missing timestamps and partial-title matches are never cleanup candidates.
- [x] Require exact TMDB or complete normalized title plus a valid task-time boundary.
- [x] Add a 429 retry regression that observes every physical attempt, delay, and request count.
- [x] Move retry ownership into `P115WebClient` or disable hidden HTTP retries for its transport.
- [x] Add a recent-directory regression with one unrelated candidate and require no result.
- [x] Run `python3 -m unittest -v tests.test_http_clients tests.test_self_share_workflow tests.test_direct_workflow`.

### Task 2: Make claim and side-effect recovery bounded

**Files:** `tests/test_task_runner.py`, `tests/test_cloud_workflow.py`, `app/task_runner.py`, `app/workflows/self_share.py`

- [x] Inject a result-persistence failure and prove the task can be reclaimed without a six-hour wait.
- [x] Keep or release the active claim safely around `_apply_result`.
- [x] Add operation-journal regressions for cloud download submission and CMS auto-organize trigger.
- [x] Implement the minimum recovery metadata needed to avoid blind duplicate submission.
- [x] Run `python3 -m unittest -v tests.test_task_runner tests.test_cloud_workflow`.

### Task 3: Make STRM replacement recoverable

**Files:** `tests/test_self_share_workflow.py`, `tests/test_direct_workflow.py`, `app/media/strm.py`, `app/workflows/direct.py`

- [x] Prove journal failure and copy failure leave the existing destination STRM intact.
- [x] Journal before mutation and replace each STRM atomically from a sibling temporary file.
- [x] Treat an already-complete destination as recovered success after a direct-move crash.
- [x] Run focused STRM and workflow tests.

### Task 4: Align Web protocol and secret handling

**Files:** `tests/test_web_api.py`, `tests/test_web_logs.py`, `tests/test_secret_hygiene.py`, `app/web.py`, `app/web_api.py`, `app/background_jobs.py`, `app/logging_system.py`, `frontend/src/api.js`

- [x] Add a real-server DELETE regression and implement `do_DELETE`.
- [x] Add nested metadata, event, summary, HDHive exception, quoted cookie, and Bearer regressions.
- [x] Route all API-facing strings and containers through one recursive redaction boundary.
- [x] Add a server-shutdown SSE regression and close active streams on shutdown.
- [x] Run Web, logging, secret, and frontend tests.

### Task 5: Harden HDHive, backup, and quality automation

**Files:** `tests/test_hdhive_subscriptions.py`, `tests/test_backup.py`, `tests/test_quality.py`, `tests/test_quality_automation.py`, `app/hdhive_subscriptions.py`, `app/backup.py`, `app/quality.py`

- [x] Distinguish successful empty Emby results from lookup exceptions and fail closed only on exceptions.
- [x] Keep partial backups retryable and contain failures while persisting backup failure state.
- [x] Convert unreadable STRM files into per-task issues instead of aborting the run.
- [x] Reuse directory scan results for repeated task destinations without changing marker evaluation.
- [x] Run focused HDHive, backup, and quality tests.

### Task 6: Full verification and review

**Files:** all changed files

- [x] Run `python3 -m unittest discover -s tests`.
- [x] Run `python3 -m compileall -q app bridge.py doctor.py`.
- [x] Run `npm test` and `npm run build` in `frontend/`.
- [x] Inspect `git diff --check`, the final diff, and worktree status.
- [x] Confirm each audit finding is either fixed with a regression or explicitly documented as residual risk.
