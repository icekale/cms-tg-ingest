# Quality Auto Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a daily, idempotent quality-repair scheduler that automatically repairs deterministic STRM/Emby problems while preserving existing 115, CMS, and deletion safety gates.

**Architecture:** Add a focused QualityAutomation service that owns due-date calculation, one-run claims, issue planning, repair summaries, and abnormal notifications. It reuses TaskStore runtime state and compare-and-set transitions, existing STRM restore/reprocess helpers, the existing invalid-share probe, and current Web/TG surfaces. No new container or external database is introduced.

**Tech Stack:** Python 3.12 standard library, zoneinfo, SQLite TaskStore, existing TaskRunner, standard-library HTTP server, Telegram client, unittest, Docker Compose.

---

## File Map

- Create app/quality_automation.py: scheduler, due-date calculation, run claim, issue planning, automatic repair orchestration, and result formatting.
- Modify app/config.py: parse daily quality automation settings and validate time/timezone/limits.
- Modify app/task_store.py: atomically claim one quality run per local date and persist settings/run summaries in runtime_state.
- Modify bridge.py: construct one automation service, start/stop its daemon loop, and route abnormal summaries to Telegram.
- Modify app/web.py: render automation status/settings and add manual run/settings endpoints.
- Modify app/telegram_ui.py: format abnormal quality notifications and action buttons.
- Modify .env.example, README.md, and CHANGELOG.md: document defaults, overrides, safety behavior, and operation.
- Create tests/test_quality_automation.py: scheduling, idempotency, planning, repair gates, and notifications.
- Modify tests/test_task_store.py, tests/test_bridge_v02_integration.py, tests/test_web_admin.py, and tests/test_docs_task_engine.py.

## Task 1: Configuration and Atomic Daily Run State

**Files:**
- Modify: app/config.py
- Modify: app/task_store.py
- Modify: .env.example
- Test: tests/test_quality_automation.py, tests/test_task_store.py

- [ ] Step 1: Write failing configuration tests.

Add tests that require Config.from_env() to parse QUALITY_AUTO_ENABLED=true, QUALITY_AUTO_TIME=02:50, QUALITY_AUTO_TIMEZONE=Asia/Shanghai, QUALITY_AUTO_MAX_TASKS=50, and QUALITY_AUTO_115_CHECK_LIMIT=3. Assert invalid HH:MM, invalid timezone, zero limit, and negative limit raise ValueError.

- [ ] Step 2: Run the focused tests and verify they fail.

Run:
~~~bash
python3 -m unittest tests.test_quality_automation -v
~~~
Expected: import or attribute failures because the settings do not exist.

- [ ] Step 3: Add strict configuration parsing.

Add Config fields:
~~~python
quality_auto_enabled: bool = False
quality_auto_time: str = "02:50"
quality_auto_timezone: str = "Asia/Shanghai"
quality_auto_max_tasks: int = 50
quality_auto_115_check_limit: int = 3
~~~
Validate HH:MM with datetime.time and validate the timezone with zoneinfo.ZoneInfo. Keep code default disabled so existing deployments do not change until .env enables it.

- [ ] Step 4: Write the atomic run-claim test.

Create one TaskStore, call claim_quality_run("2026-07-20", now=...) twice, and assert only the first call returns True. A different date must be claimable. Use two store instances against one temporary database.

- [ ] Step 5: Implement the run claim and summary state.

Add TaskStore.claim_quality_run(run_date: str, now: float) -> bool using BEGIN IMMEDIATE, reading quality_auto_run_date, and writing it only when the stored date differs. Store JSON summaries under quality_auto_last_summary and status under quality_auto_status. Reuse existing generic runtime_state methods where possible.

- [ ] Step 6: Run focused tests and commit.

Run:
~~~bash
python3 -m unittest tests.test_quality_automation tests.test_task_store -v
~~~
Expected: all focused tests pass. Commit:
~~~bash
git add app/config.py app/task_store.py .env.example tests/test_quality_automation.py tests/test_task_store.py
git commit -m "feat: add quality automation settings and run claims"
~~~

## Task 2: Quality Automation Core and Safe Issue Planning

**Files:**
- Create: app/quality_automation.py
- Test: tests/test_quality_automation.py

- [ ] Step 1: Write due-time and idempotency tests.

Cover:
~~~python
service.next_run_at(datetime(2026, 7, 20, 2, 49, tzinfo=tz))
service.run_if_due(datetime(2026, 7, 20, 2, 50, tzinfo=tz))
service.run_if_due(datetime(2026, 7, 20, 3, 0, tzinfo=tz))
~~~
Assert the first due call starts one run, the second same-date call does nothing, and the next local date starts another run. Add a restart simulation using a new service instance with the same TaskStore.

- [ ] Step 2: Run tests and verify the expected failures.

Run:
~~~bash
python3 -m unittest tests.test_quality_automation.QualityScheduleTests -v
~~~
Expected: failure because QualityAutomation is not implemented.

- [ ] Step 3: Implement the scheduler core.

Define:
~~~python
class QualityAutomation:
    def next_run_at(self, now: datetime | None = None) -> datetime: ...
    def run_if_due(self, now: datetime | None = None) -> QualityRunSummary | None: ...
    def run_once(self, run_id: str, now: datetime | None = None) -> QualityRunSummary: ...
    def run_now(self) -> bool: ...
~~~
Use ZoneInfo(config.quality_auto_timezone), compare local HH:MM, and create a run ID containing local date plus a short monotonic suffix. Claim the local date before scanning. Persist running, succeeded, or failed and summary JSON even when an exception occurs.

- [ ] Step 4: Write issue-planning tests.

Create fake tasks and local STRM trees for missing destination -> restore, missing STRM -> restore, direct/unexpected STRM -> reprocess, active/claimed task -> skip(task_busy), and missing metadata or path outside allowed roots -> skip(unsafe_metadata). Assert planning does not call 115, CMS, Emby, or delete functions.

- [ ] Step 5: Implement planning and summary types.

Add immutable QualityRunSummary and QualityRepairPlan dataclasses. Reuse scan_task_quality(store, limit=...), group issues by task ID, and produce one action per task. A task is eligible only when unclaimed, not actively running, below quality_auto_max_tasks, and its metadata path resolves below configured source/library roots.

- [ ] Step 6: Run core tests and commit.

Run:
~~~bash
python3 -m unittest tests.test_quality_automation -v
~~~
Expected: all schedule and planning tests pass. Commit:
~~~bash
git add app/quality_automation.py tests/test_quality_automation.py
git commit -m "feat: add quality automation scheduler and planner"
~~~

## Task 3: Automatic Repairs and Deletion Gates

**Files:**
- Modify: app/quality_automation.py
- Modify: app/quality.py only when issue metadata needs an explicit repair reason
- Test: tests/test_quality_automation.py, tests/test_self_share_workflow.py, tests/test_invalid_share_cleanup.py

- [ ] Step 1: Write repair orchestration tests.

Use fakes and assert:
~~~text
missing_dest -> restore transition
direct_strm -> RECEIVED transition with force_reprocess=True
confirmed invalid share -> rebuild workflow
risk-control/unknown share error -> skip, no rebuild, no delete
Emby confirmation failure -> no cleanup call
~~~
Also assert a successful replacement records cleanup only after share marker, playback, destination, and Emby checks all return true.

- [ ] Step 2: Run repair tests and verify they fail.

Run:
~~~bash
python3 -m unittest tests.test_quality_automation tests.test_invalid_share_cleanup -v
~~~
Expected: failures because the automation service has no repair executor.

- [ ] Step 3: Reuse existing safe repair paths.

Implement _execute_plan(plan, run_id) as a dispatcher:
~~~python
if plan.action == "restore":
    enqueue_downstream_restore(plan.task, run_id)
elif plan.action == "reprocess":
    enqueue_reprocess(plan.task, run_id)
elif plan.action == "invalid_share":
    run_confirmed_invalid_share_rebuild(plan.task, run_id)
else:
    record_skip(plan, run_id)
~~~
Use TaskStore.compare_and_set_transition(..., require_unclaimed=True) for queued actions. Call existing STRM move, Emby, cleanup, and invalid-share helpers through narrow adapters instead of duplicating their logic.

- [ ] Step 4: Add hard deletion guards.

Before any cleanup adapter is called, require: current own share available; STRM contains current share marker and no /d/; destination is allowed; Emby confirmation is positive and unique; and TaskStore contains the success event. Otherwise record blocked_cleanup and leave files/shares intact.

- [ ] Step 5: Disable duplicate invalid-share scheduling.

Do not start the old invalid-share probe loop when daily quality automation owns invalid-share checks. Keep the old loop when the feature is disabled. Add a startup test asserting exactly one owner is active.

- [ ] Step 6: Run repair tests and commit.

Run:
~~~bash
python3 -m unittest tests.test_quality_automation tests.test_invalid_share_cleanup tests.test_self_share_workflow -v
~~~
Expected: all repair and deletion-gate tests pass. Commit:
~~~bash
git add app/quality_automation.py app/quality.py bridge.py tests/test_quality_automation.py tests/test_invalid_share_cleanup.py tests/test_self_share_workflow.py
git commit -m "feat: automate safe quality repairs"
~~~

## Task 4: Scheduler Lifecycle and Abnormal Notifications

**Files:**
- Modify: bridge.py
- Modify: app/telegram_ui.py
- Test: tests/test_bridge_v02_integration.py, tests/test_quality_automation.py

- [ ] Step 1: Write lifecycle tests.

Patch the automation constructor and assert run_forever starts one daemon scheduler when enabled, does not start it when disabled, and stops it during shutdown. Assert a summary with failures calls telegram.send_message, while a fully successful summary does not.

- [ ] Step 2: Add a daemon loop with stop support.

The loop calls run_if_due() every 30 seconds and uses threading.Event.wait(30) so shutdown wakes immediately. It catches and persists unexpected exceptions instead of terminating the process.

- [ ] Step 3: Format abnormal Telegram output.

Include run ID, counts, skipped/failed reasons, affected task IDs, and existing task action buttons. Never include cookies, API keys, full share passwords, or full source URLs.

- [ ] Step 4: Run lifecycle tests and commit.

Run:
~~~bash
python3 -m unittest tests.test_bridge_v02_integration tests.test_quality_automation -v
~~~
Expected: all lifecycle and notification tests pass. Commit:
~~~bash
git add bridge.py app/telegram_ui.py tests/test_bridge_v02_integration.py tests/test_quality_automation.py
git commit -m "feat: run quality automation in bridge"
~~~

## Task 5: Web Settings, Status, and Manual Run

**Files:**
- Modify: app/web.py
- Test: tests/test_web_admin.py

- [ ] Step 1: Write Web rendering tests.

Assert /quality includes enabled state, 02:50, timezone, last/next run, summary counts, and forms with /quality/settings, /quality/run, and /quality/settings/reset. Assert a run in progress disables duplicate manual starts.

- [ ] Step 2: Add local-only settings/status rendering.

Render the automation panel from QualityAutomation.status_snapshot() and escape all stored values. GET /quality remains local-only and must not call 115, CMS, or Emby.

- [ ] Step 3: Add POST handlers.

Implement:
~~~text
POST /quality/settings       validate and persist enabled/time/timezone/limits
POST /quality/settings/reset clear runtime overrides and return to env defaults
POST /quality/run            start one background run if no run is active
~~~
Return 303 /quality for successful actions and 400 for invalid settings without changing the old settings. Preserve existing Web authorization.

- [ ] Step 4: Run Web tests and commit.

Run:
~~~bash
python3 -m unittest tests.test_web_admin.WebAdminTests -v
~~~
Expected: all existing and new quality UI tests pass. Commit:
~~~bash
git add app/web.py tests/test_web_admin.py
git commit -m "feat: add quality automation controls to web"
~~~

## Task 6: Documentation and Full Verification

**Files:**
- Modify: .env.example
- Modify: README.md
- Modify: CHANGELOG.md
- Modify: tests/test_docs_task_engine.py

- [ ] Step 1: Document operation and rollback.

Document default 02:50, Web override precedence, daily 115 limit, abnormal-only TG notifications, deletion guards, and rollback with QUALITY_AUTO_ENABLED=false.

- [ ] Step 2: Add documentation assertions.

Assert README and changelog mention configurable schedule, automatic repair, safety gates, and abnormal-only notifications.

- [ ] Step 3: Run complete verification.

Run:
~~~bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m compileall -q app bridge.py doctor.py
git diff --check
docker build --pull=false -t cms-tg-ingest:quality-auto-check .
~~~
Expected: all tests pass, compilation succeeds, diff check is clean, and Docker build exits 0.

- [ ] Step 4: Commit documentation and verify the worktree.

~~~bash
git add .env.example README.md CHANGELOG.md tests/test_docs_task_engine.py
git commit -m "docs: document automatic quality repair"
git status --short --branch
~~~
Expected: worktree is clean.

## Task 7: Production Rollout

**Files:**
- Verify: Unraid Compose project and running container

- [ ] Step 1: Publish a versioned image.

After Task 6 passes, bump app/__init__.py and CHANGELOG.md to the next patch version, commit, push the feature branch, and push a version tag. Wait for release-images.yml to finish successfully before deployment.

- [ ] Step 2: Back up and deploy Unraid.

On 192.168.5.28, back up /mnt/user/appdata/cms-tg-ingest and its Compose file, update only the image tag, run docker compose pull and docker compose up -d --no-build, and preserve .env, /data, the 115 cookie, CMS database, and STRM mounts.

- [ ] Step 3: Verify production behavior.

Check container version/health, /, /health, and /quality status codes, then inspect quality automation status without triggering an early 115 scan. Confirm configured schedule is 02:50 and automatic mode is enabled only after production configuration is updated.

- [ ] Step 4: Commit and report evidence.

Report image tag/digest, test count, scheduler configuration, and skipped/failed quality items. Do not claim a full production repair until TaskStore events and filesystem/Emby checks confirm it.

