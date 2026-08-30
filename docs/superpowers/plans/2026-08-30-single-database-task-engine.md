# Single-Database Task Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace TaskStore/SubmissionStore split state with one normalized SQLite database and one TaskRunner workflow writer while preserving all production history and preventing task 447's maintenance race.

**Architecture:** Ship a small containment release first, then build the unified schema and deterministic importer without touching the two source databases. Preserve `TaskSnapshot` as a joined read model, but move mutable media/share/move/Emby/cleanup facts into normalized tables and commit them with stage state through claim-fenced checkpoints. Web, Telegram, quality, probes, and maintenance submit idempotent commands; TaskRunner alone performs workflow side effects.

**Tech Stack:** Python 3 standard library, SQLite, existing `unittest` suite, Docker Buildx/GitHub Actions, Docker Compose on Unraid.

**Design:** `docs/superpowers/specs/2026-08-30-single-database-task-engine-design.md`

---

## Delivery Boundaries

This is one master plan with independently reviewable milestones:

1. `v0.4.33`: contain competing writers and recover task 447.
2. Unified schema and migration tooling, tested against copied production databases.
3. Claim-fenced checkpoints, command queue, runner lease, archive, and unified backup.
4. Workflow and observer conversion.
5. Legacy executor removal and `v0.5.0` maintenance-window cutover.

Do not begin a later milestone while an earlier milestone has failing tests or unresolved review findings. Intermediate refactor commits are not production releases.

## File Map

**Create:**

- `app/database.py`: unified connections, schema version, schema initialization, integrity checks, transaction helper.
- `app/unified_migration.py`: read-only legacy import, mapping, checksums, validation report.
- `scripts/migrate_unified_db.py`: operator CLI for dry-run/import/validate.
- `tests/test_database.py`: connection PRAGMAs, schema constraints, version compatibility.
- `tests/test_unified_migration.py`: deterministic import and all abort conditions.
- `tests/test_task_commands.py`: command idempotency, claim fencing, lease contention.
- `tests/fixtures/legacy_databases.py`: small programmatic legacy TaskStore/SubmissionStore fixtures; no binary fixtures.

**Modify:**

- `app/sqlite_utils.py`: support configured connection setup and foreign-key checking.
- `app/models.py`: normalized fact/checkpoint/command read models; remove submission linkage from canonical task state.
- `app/task_store.py`: joined task reads, fact writes, checkpoints, commands, lease, archive/purge.
- `app/task_runner.py`: singleton lease, command consumption, atomic checkpoint commits.
- `app/task_actions.py`: enqueue commands and archive instead of cross-store transitions/deletes.
- `app/workflows/self_share.py`: return fact patches; remove SubmissionStore writes; monotonic destination recovery.
- `app/workflows/direct.py`: return fact patches; remove SubmissionStore writes.
- `app/task_bridge.py`: remove submission reset/mirroring and use normalized task facts.
- `app/media/strm.py`: retain pure planning/validation; remove task-owned maintenance executors.
- `app/self_share_health.py`: observe and enqueue invalidation commands only.
- `app/quality_automation.py`: plan and enqueue commands only; no direct workflow claims or cleanup.
- `app/backup.py`: one unified SQLite source and one consistent restore artifact.
- `app/hdhive_subscription_store.py`: use the unified connection/schema.
- `app/hdhive_cards.py`: store TMDB cache in the unified database.
- `app/config.py`: replace runtime `DB_PATH`/`TASK_DB_PATH`/engine flag with `DATABASE_PATH` and startup gates.
- `app/web.py`, `app/web_api.py`, `app/telegram_ui.py`: unified projections, commands, archive/purge semantics.
- `app/task_health.py`, `app/task_diagnostics.py`, `doctor.py`: schema/lease/migration/write-gate health.
- `bridge.py`: remove `SubmissionStore`, `best_effort_task_sync`, legacy startup branches, and competing loops.
- `app/legacy_polling.py`: delete after all imports and callers are removed.
- `.env.example`, `docker-compose.yml`, `README.md`, `docs/dockerhub-overview.md`: unified path and cutover documentation.
- Existing task/workflow/Web/quality/HDHive/backup tests: replace two-store fixtures with one store.

---

### Task 1: Ship Containment and Recover Task 447 (`v0.4.33`)

**Files:**
- Modify: `bridge.py:4860-4975`
- Modify: `app/self_share_health.py:26-114`
- Modify: `app/quality_automation.py:2001-2436`
- Modify: `app/workflows/self_share.py:4429-4515`
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_invalid_share_cleanup.py`
- Test: `tests/test_quality_automation.py`
- Test: `tests/test_task_runner.py`
- Modify for release: `app/__init__.py`, `CHANGELOG.md`, `README.md`, `docs/dockerhub-overview.md`

- [ ] **Step 1: Add the exact competing-maintenance regression test**

Add a `run_forever` test beside `DirectTaskEngineBridgeTests.test_direct_task_engine_run_forever_constructs_runtime_dependencies_once` that patches `start_self_share_maintenance_loop`, runs engine mode with self-share enabled, and asserts the patched function was never called while `TaskRunner.start()` was called once.

```python
self.assertEqual(captured["runner_starts"], 1)
self.assertEqual(captured["maintenance_starts"], 0)
```

- [ ] **Step 2: Add read-only invalid-share and quality tests**

Change the invalid-share success test to assert detection without deletion or task transition:

```python
self.assertEqual(summary.checked_count, 1)
self.assertEqual(summary.cleaned_count, 0)
self.assertTrue(destination.exists())
self.assertEqual(store.find_by_id(int(row["id"]))["move_status"], "moved")
self.assertEqual(task_store.list_recent_tasks(limit=1), [])
self.assertEqual(emby.refreshes, [])
self.assertEqual(telegram.messages, [])
```

Add a quality test proving containment mode returns a skipped/manual result without calling a repair adapter, deleting a file, or claiming a task.

- [ ] **Step 3: Add the task 447 monotonic-recovery test**

Build a task at `STRM_READY` whose SubmissionStore row says `move_status='moved'`, whose source directory is absent, and whose validated destination contains the expected own-share STRM. Assert the stage completes without receiving, creating a share, resubmitting CMS, or moving files:

```python
result = workflow.run_stage(claimed_task)
self.assertEqual(result.outcome, StageOutcome.COMPLETE)
self.assertEqual(result.metadata["move_status"], "moved")
self.assertEqual(result.metadata["dest_path"], str(destination))
self.assertEqual(p115.receive_calls, [])
self.assertEqual(p115.create_share_calls, [])
self.assertEqual(cms.share_sync_calls, [])
```

- [ ] **Step 4: Run the new tests and confirm they fail**

```bash
python3 -m unittest \
  tests.test_bridge_task_engine.DirectTaskEngineBridgeTests \
  tests.test_invalid_share_cleanup.InvalidShareCleanupTests \
  tests.test_quality_automation -q
```

Expected: failures showing engine mode starts maintenance, invalid-share cleanup deletes the destination, quality can execute repair, and `strm_ready` waits on the missing source.

- [ ] **Step 5: Implement the containment guards**

Make these minimal changes:

```python
# bridge.run_forever: do not start start_self_share_maintenance_loop in engine mode.
config.quality_auto_repair_enabled = False
# Construct QualityAutomation without _QualityRepairAdapter.
# self_share_health: record probe timestamp/state only; do not call _clean_invalid_self_share.
```

Log once at startup that automated quality repair is temporarily read-only. Remove this containment assignment in Task 10 when quality actions enqueue runner commands.

In `_stage_strm_ready`, before returning “waiting for source,” accept the later persisted fact only when all checks pass:

```python
if str(row.get("move_status") or "").lower() == "moved":
    destination = safe_resolve(Path(str(row.get("dest_path") or "")))
    library_roots = list(self.move_config.library_roots.values())
    if is_under_any_root(destination, library_roots):
        issue = validate_self_share_strm_destination(destination, row, required_relative_path)
        if not issue:
            return StageResult.complete(
                "STRM 已在目标目录，按已完成移动继续",
                {"move_status": "moved", "dest_path": str(destination)},
            )
```

Do not accept a destination outside configured library roots, an empty destination, or a destination whose STRM does not prove the expected permanent share.

- [ ] **Step 6: Run focused and full verification**

```bash
python3 -m unittest \
  tests.test_bridge_task_engine \
  tests.test_invalid_share_cleanup \
  tests.test_quality_automation \
  tests.test_task_runner -q
python3 -m unittest discover -s tests -p 'test*.py' -q
python3 -m compileall -q app bridge.py doctor.py tests
python3 -m unittest tests.test_secret_hygiene -q
git diff --check
```

Expected: zero failures and no direct maintenance/invalid-share/quality side effect outside TaskRunner.

- [ ] **Step 7: Commit the containment fix**

```bash
git add bridge.py app/self_share_health.py app/quality_automation.py app/workflows/self_share.py \
  tests/test_bridge_task_engine.py tests/test_invalid_share_cleanup.py \
  tests/test_quality_automation.py tests/test_task_runner.py
git commit -m "fix: contain competing workflow writers"
```

- [ ] **Step 8: Publish and deploy `v0.4.33` using the release skill**

Update version and release docs to `0.4.33`, run the full release preflight, commit `release: publish v0.4.33`, push `main`, create/push `v0.4.33`, wait for `.github/workflows/release-images.yml`, and verify both `linux/amd64` and `linux/arm64` manifests before deployment.

Back up `/mnt/user/appdata/cms-tg-ingest/data`, `.env`, and Compose metadata; change only the image tag; recreate `cms-tg-ingest`; confirm healthy status, version `0.4.33`, and a fresh runner heartbeat.

- [ ] **Step 9: Recover task 447 without replaying upstream operations**

With the containment release running and intake temporarily paused:

1. Back up `tasks.db` and `submissions.db` with timestamped names.
2. Verify destination STRM, own-share marker, Emby TMDB item, SubmissionStore move fact, and successful receive/create-share/CMS-sync operations.
3. Requeue task 447 at `STRM_READY` through the supported task action/store API; do not edit operation rows or call 115/CMS manually.
4. Let TaskRunner converge through moved, Emby, and cleanup checks.
5. Verify task 447 is terminal-success, no duplicate upstream operation exists, source remains absent, destination remains valid, and Emby still resolves the expected item.

Commit no production database files.

---

### Task 2: Add the Unified Database Schema and Connection Contract

**Files:**
- Create: `app/database.py`
- Modify: `app/sqlite_utils.py`
- Create: `tests/test_database.py`
- Modify: `tests/test_task_store.py`

- [ ] **Step 1: Write schema contract tests**

Create tests that initialize an empty database and assert:

```python
EXPECTED_TABLES = {
    "schema_meta", "tasks", "task_media", "task_shares", "task_moves",
    "task_emby", "task_cleanups", "task_probes", "task_targets",
    "task_events", "task_operations", "task_commands", "runner_leases",
    "runtime_state", "quality_runs", "parent_category_memory",
    "hdhive_subscriptions", "hdhive_subscription_items",
    "hdhive_subscription_runs", "hdhive_subscription_settings",
    "tmdb_details", "legacy_submission_map", "legacy_submission_archive",
    "migration_runs", "task_purge_audit",
}
```

Also assert:

- `PRAGMA foreign_keys` returns `1` on every connection.
- `PRAGMA foreign_key_check` returns no rows.
- duplicate `(source_type, source_key)` is rejected.
- duplicate share identity is rejected only when `source_type='share'`.
- two non-share tasks with empty source-share fields are accepted.
- a child fact without a task is rejected.
- unsupported schema versions fail startup before any mutation.

- [ ] **Step 2: Run the schema tests and confirm import failure**

```bash
python3 -m unittest tests.test_database -q
```

Expected: FAIL because `app.database` does not exist.

- [ ] **Step 3: Implement `app/database.py`**

Implement these public operations with the exact signatures below; the implementation bodies are the connection, transaction, initialization, and verification logic described immediately after the list:

```text
Database(path: str | Path, *, busy_timeout_ms: int = 30_000)
Database.connect(*, read_only: bool = False) -> sqlite3.Connection
Database.transaction(*, immediate: bool = False) -> ContextManager[sqlite3.Connection]
Database.initialize() -> None
Database.verify() -> None
```

Set `SCHEMA_VERSION = 1` and define `SchemaVersionError(RuntimeError)`.

`connect()` must set row factory, `foreign_keys=ON`, and `busy_timeout`; read-only mode must use `mode=ro`. `initialize()` creates the exact tables listed in the design with these rules:

- `tasks` owns source identity, execution state, claims, scheduling, archive fields, `origin`, and `is_executable`.
- domain tables use `task_id PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE`.
- `task_targets` uses `UNIQUE(task_id, target_key)`.
- events and operations retain explicit integer IDs.
- commands use `UNIQUE(idempotency_key)`.
- purge audit keeps a task identity snapshot and has no cascading task FK.
- legacy raw archive uses `legacy_submission_id PRIMARY KEY`, canonical JSON, and SHA-256.

Do not enable WAL in this task; WAL is decided only after the Unraid filesystem test.

- [ ] **Step 4: Extend SQLite verification helpers**

Add `sqlite_foreign_key_check(database)` beside `sqlite_quick_check()` and make it raise `sqlite3.IntegrityError` with the offending table/row details when rows are returned.

- [ ] **Step 5: Run focused tests and commit**

```bash
python3 -m unittest tests.test_database tests.test_task_store -q
python3 -m compileall -q app tests/test_database.py
git diff --check
git add app/database.py app/sqlite_utils.py tests/test_database.py tests/test_task_store.py
git commit -m "feat: add unified database schema"
```

---

### Task 3: Build Deterministic Legacy Fixtures and the Importer

**Files:**
- Create: `tests/fixtures/legacy_databases.py`
- Create: `app/unified_migration.py`
- Create: `tests/test_unified_migration.py`

- [ ] **Step 1: Build programmatic legacy fixtures**

The fixture builder must create source files using the current legacy schemas and include:

- one matched task/submission linked only by `metadata_json`.
- one matched task/submission linked by typed `submission_id`.
- one task-only non-share record with empty share fields.
- one submission-only completed record.
- preserved task/event/operation IDs with gaps.
- runtime state, quality runs, parent-category memory, and HDHive rows.

`tmdb-card-cache.db` is a rebuildable cache, not historical workflow state. Create an empty `tmdb_details` table in the unified database and do not import the old cache file.

Return paths and expected logical counts; do not commit generated `.db` files.

- [ ] **Step 2: Write the successful import test**

```python
report = migrate_legacy_databases(tasks_db, submissions_db, output_db)
self.assertEqual(report.matched_submissions, 2)
self.assertEqual(report.synthetic_tasks, 1)
self.assertEqual(report.unmapped_rows, 0)
self.assertEqual(report.foreign_key_errors, ())
self.assertEqual(load_task_ids(output_db), {10, 20, 30, 31})
self.assertFalse(load_task(output_db, 31)["is_executable"])
self.assertEqual(load_legacy_map(output_db, legacy_submission_id=9), 31)
```

Assert the original canonical submission JSON hashes back to the stored checksum and existing event/operation IDs and immutable request identities are unchanged.

- [ ] **Step 3: Write abort-condition tests**

Create independent tests for:

- duplicate canonical source identity.
- conflicting typed and JSON links.
- two tasks claiming one submission.
- linked identity mismatch.
- orphan event or operation.
- malformed required source identity.
- duplicate operation key with different request identity.
- unmapped legacy submission column.
- synthetic task becoming runnable.

Each must assert no output database remains after failure.

- [ ] **Step 4: Run tests and confirm `app.unified_migration` is missing**

```bash
python3 -m unittest tests.test_unified_migration -q
```

Expected: FAIL on import.

- [ ] **Step 5: Implement migration data types and source checks**

Expose the following exact migration API; implement it with the source checks and one-transaction mapping rules in Steps 5-6:

```python
@dataclass(frozen=True)
class MigrationReport:
    source_hashes: dict[str, str]
    source_counts: dict[str, int]
    destination_counts: dict[str, int]
    matched_submissions: int
    synthetic_tasks: int
    unmapped_rows: int
    logical_checksums: dict[str, str]
    foreign_key_errors: tuple[str, ...]

class MigrationError(RuntimeError):
    pass
```

```text
migrate_legacy_databases(tasks_path: str | Path, submissions_path: str | Path, output_path: str | Path) -> MigrationReport
```

Open both sources with `mode=ro`; record file SHA-256/size and run `quick_check` before creating the output. Reject an existing output path.

- [ ] **Step 6: Implement deterministic mapping and import**

Within one output transaction:

1. Copy existing tasks, events, operations, runtime state, quality, and HDHive data with original IDs.
2. Normalize share identity with the existing intake identity helpers, not a new parser.
3. Validate typed/JSON links against identity matches.
4. Merge each matched submission into the canonical task domain tables.
5. Allocate synthetic task IDs from `max(existing_task_id) + 1` in ascending legacy submission ID order.
6. Set synthetic tasks to `origin='legacy_import'`, `is_executable=0`, `next_run_at=-1`, and no claim/command.
7. Store one legacy map and one canonical raw archive row per submission.
8. Reset all `sqlite_sequence` values to imported maxima.

Use sorted-key, compact JSON and SHA-256 for stable row archives and logical table checksums.

- [ ] **Step 7: Validate and commit**

```bash
python3 -m unittest tests.test_unified_migration tests.test_database -q
python3 -m compileall -q app tests/fixtures tests/test_unified_migration.py
git diff --check
git add app/unified_migration.py tests/fixtures/legacy_databases.py tests/test_unified_migration.py
git commit -m "feat: migrate legacy workflow history"
```

---

### Task 4: Add the Migration CLI and Production-Copy Dry Run

**Files:**
- Create: `scripts/migrate_unified_db.py`
- Modify: `tests/test_unified_migration.py`
- Modify: `doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Add CLI tests**

Test these modes through `subprocess.run()`:

```text
--tasks <path> --submissions <path> --output <path> --report <json>
--validate <unified-path> [--print-migration-id]
--open-runner-gate <unified-path> --migration-id <id>
--open-intake-gate <unified-path> --migration-id <id>
```

Assert nonzero exit on ambiguity, JSON report creation on success, no secret-bearing URLs in stdout/stderr, and refusal to overwrite an existing output. Gate state starts `closed`, moves only `closed -> runner_open -> open` for the matching migration ID, records both timestamps, and has no close/reverse command.

- [ ] **Step 2: Implement the thin CLI**

The script must only parse arguments, call `migrate_legacy_databases()`, unified validation, or the one-way write-gate transition, write reports with mode `0600`, and return `0` on success or `2` on validation/migration/gate failure. Keep migration and gate transaction logic in `app/unified_migration.py`.

- [ ] **Step 3: Add doctor read-only validation**

Teach `doctor.py` a unified database check that reports schema version, `quick_check`, `foreign_key_check`, migration ID, and whether any `legacy_import` task is executable. It must not initialize or alter a database during diagnostics.

- [ ] **Step 4: Run focused tests and commit**

```bash
python3 -m unittest tests.test_unified_migration tests.test_doctor -q
python3 -m compileall -q app scripts/migrate_unified_db.py doctor.py
git diff --check
git add scripts/migrate_unified_db.py app/unified_migration.py doctor.py \
  tests/test_unified_migration.py tests/test_doctor.py
git commit -m "feat: add unified migration checks"
```

- [ ] **Step 5: Run a read-only dry run against copied production databases**

Do not pause intake for this dry run. Create consistent copies with SQLite's online backup API; never use filesystem `cp` against live SQLite files and never point the importer at writable live files. Store copies outside the application data filenames, then run:

```bash
python3 scripts/migrate_unified_db.py \
  --tasks tasks.production-copy.db \
  --submissions submissions.production-copy.db \
  --output cms-tg-ingest.dry-run.db \
  --report migration-dry-run.json
python3 scripts/migrate_unified_db.py --validate cms-tg-ingest.dry-run.db
```

Expected report baseline:

```text
legacy tasks: 174
legacy submissions: 310
matched submissions: 151
synthetic legacy_import tasks: 159
unmapped rows: 0
foreign-key errors: 0
```

Run the import a second time to a different output path and compare normalized reports and logical table checksums. Investigate any count drift before proceeding; do not weaken an abort condition to fit production data.

---

### Task 5: Convert TaskStore to Joined Facts and Atomic Checkpoints

**Files:**
- Modify: `app/models.py`
- Modify: `app/task_store.py`
- Modify: `app/task_runner.py`
- Test: `tests/test_task_store.py`
- Test: `tests/test_task_runner.py`
- Test: `tests/test_taskstore_workflow_events.py`

- [ ] **Step 1: Add checkpoint value types**

Move `StageOutcome` and `StageResult` from `app/task_runner.py` to `app/models.py`, add the checkpoint types below, and let `app/task_runner.py` import/re-export the moved names until all callers are converted:

```python
@dataclass(frozen=True)
class OperationCompletion:
    operation_key: str
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""

@dataclass(frozen=True)
class StageCheckpoint:
    media: dict[str, Any] = field(default_factory=dict)
    share: dict[str, Any] = field(default_factory=dict)
    move: dict[str, Any] = field(default_factory=dict)
    emby: dict[str, Any] = field(default_factory=dict)
    cleanup: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    targets: tuple[dict[str, Any], ...] = ()
    operations: tuple[OperationCompletion, ...] = ()
```

Add `checkpoint: StageCheckpoint = field(default_factory=StageCheckpoint)` to `StageResult`. Keep presentation fields on `TaskSnapshot` as a joined read model, but remove canonical `submission_id` use.

- [ ] **Step 2: Write failing atomicity tests**

Test that `commit_claimed_result()` atomically writes domain facts, operation completion, execution state, and event. Trigger a deliberate constraint error in the fact patch and assert none of those writes commit.

Add stale-claim tests where stage, status, claim token, or `updated_at` differs; each must return `None` without changing facts, operation state, task state, or events.

- [ ] **Step 3: Implement joined reads**

Use one explicit SQL projection joining `tasks`, `task_media`, `task_shares`, `task_moves`, `task_emby`, `task_cleanups`, and `task_probes`. Keep `TaskSnapshot.from_row()` stable for consumers while deleting duplicated mutable metadata writes as each workflow migrates.

- [ ] **Step 4: Implement the checkpoint transaction**

Add the exact public method below to `TaskStore`; its body is the six-step transaction that follows:

```text
commit_claimed_result(task: TaskSnapshot, worker_id: str, result: StageResult, *, next_stage: TaskStage | None, next_run_at: float) -> TaskSnapshot | None
```

Inside one `BEGIN IMMEDIATE` transaction:

1. Reload task and verify expected stage, `RUNNING`, worker, exact claim token, claimed timestamp, and `updated_at`.
2. Apply allowlisted columns for each fact table; reject unknown keys.
3. Upsert target rows by `(task_id, target_key)`.
4. Finish only the named started/uncertain operations with immutable request identity unchanged.
5. Insert result and next-stage events.
6. Update execution state and release/schedule the claim.

Do not permit filesystem or network callbacks inside this method.

- [ ] **Step 5: Route TaskRunner result handling through the checkpoint**

Replace `complete_claimed_stage()` and claimed `record_event()` calls in `_apply_result()` with `commit_claimed_result()`. Keep claim-loss handling and termination checks, but make all successful/deferred/needs-action/failed results use the same fenced transaction.

- [ ] **Step 6: Run focused tests and commit**

```bash
python3 -m unittest \
  tests.test_task_store tests.test_task_runner tests.test_taskstore_workflow_events -q
python3 -m compileall -q app

git diff --check
git add app/models.py app/task_store.py app/task_runner.py \
  tests/test_task_store.py tests/test_task_runner.py tests/test_taskstore_workflow_events.py
git commit -m "refactor: checkpoint task facts atomically"
```

---

### Task 6: Add Durable Commands and the Singleton Runner Lease

**Files:**
- Modify: `app/models.py`
- Modify: `app/task_store.py`
- Modify: `app/task_runner.py`
- Create: `tests/test_task_commands.py`
- Modify: `tests/test_task_runner.py`

- [ ] **Step 1: Write command idempotency tests**

Assert two submissions with the same idempotency key produce one command, conflicting payloads raise `ValueError`, only one runner can claim it, a stale claim token cannot complete it, and completed commands are not reclaimed.

Command types for this refactor are fixed:

```python
COMMAND_TYPES = {
    "retry", "reprocess", "resume_organizing", "emby_check", "restore",
    "repair_move", "invalidate_share", "quality_repair", "terminate",
}
```

- [ ] **Step 2: Write lease contention tests**

Use two `TaskStore` instances on the same database. Assert owner A acquires `task_runner`, owner B is rejected while A's lease is fresh, A can renew only with the exact token, and B can acquire only after expiry. A stale A token must never renew or release B's lease.

- [ ] **Step 3: Implement command methods**

Add `enqueue_command`, `claim_next_command`, `complete_command`, `fail_command`, and `cancel_pending_commands`. Every claim/complete update checks the exact command claim token.

- [ ] **Step 4: Implement runner lease methods**

Add `acquire_runner_lease`, `renew_runner_lease`, and `release_runner_lease`. `TaskRunner.start()` must acquire before starting worker/heartbeat threads; heartbeat renews it; startup raises a clear error on contention; `stop()` releases only its own token.

- [ ] **Step 5: Consume commands before runnable tasks**

`run_once()` first claims one command and translates it into a fenced task transition or a runner-owned repair stage. Command completion and the resulting task/event transition occur in one transaction. Unknown command types fail closed and do not mutate the task.

- [ ] **Step 6: Run tests and commit**

```bash
python3 -m unittest tests.test_task_commands tests.test_task_runner tests.test_task_store -q
python3 -m compileall -q app
git diff --check
git add app/models.py app/task_store.py app/task_runner.py \
  tests/test_task_commands.py tests/test_task_runner.py tests/test_task_store.py
git commit -m "feat: queue runner commands and lease"
```

---

### Task 7: Replace Hard Delete and Dual Backup

**Files:**
- Modify: `app/task_store.py`
- Modify: `app/task_actions.py`
- Modify: `app/backup.py`
- Test: `tests/test_task_actions.py`
- Test: `tests/test_task_store.py`
- Test: `tests/test_backup.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Write archive and purge tests**

Test that archive:

- requires a terminal, unclaimed task.
- writes archive actor/reason/timestamp and an event atomically.
- cancels pending commands.
- hides the task from default queue/history but preserves facts/events/operations/map/archive.
- is idempotent for the same task.

Test that purge rejects active/unarchived/claimed/pending-command tasks, requires explicit confirmation, deletes task-owned rows, and leaves an immutable `task_purge_audit` identity snapshot.

- [ ] **Step 2: Implement archive and explicit purge**

Replace `delete_finished_task()` and `clear_finished_tasks()` with these exact methods and implement the guards described in Step 1:

```text
archive_task(task_id: int, *, actor: str, reason: str, expected_updated_at: float) -> bool
purge_archived_task(task_id: int, *, actor: str, confirmation: str) -> bool
```

The required confirmation is the exact string `PURGE TASK <id>`. Do not expose bulk purge in this refactor.

- [ ] **Step 3: Remove cross-store deletion**

Delete `delete_task_record_and_submission()`. `delete_task_record()` becomes archive and returns `任务已归档`; Web/TG ordinary delete uses it. Add a separate admin-only purge endpoint/action with explicit confirmation.

- [ ] **Step 4: Convert backup to one source**

Keep the existing online SQLite backup implementation, but configure it with only `{ "cms-tg-ingest": database_path }`. Add a restore test that opens the backup, runs `quick_check`/`foreign_key_check`, and verifies task, fact, command, event, operation, and migration-map counts are consistent.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest \
  tests.test_task_actions tests.test_task_store tests.test_backup tests.test_web_api -q
python3 -m compileall -q app
git diff --check
git add app/task_store.py app/task_actions.py app/backup.py \
  tests/test_task_actions.py tests/test_task_store.py tests/test_backup.py tests/test_web_api.py
git commit -m "feat: archive tasks and unify backups"
```

---

### Task 8: Convert the Self-Share Workflow to Fact Checkpoints

**Files:**
- Modify: `app/workflows/self_share.py`
- Modify: `app/media/strm.py`
- Modify: `app/task_bridge.py`
- Modify: `app/task_runner.py`
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_self_share_workflow.py`
- Test: `tests/test_runtime_recovery.py`
- Test: `tests/test_task_runner.py`

- [ ] **Step 1: Add external-success crash-window tests**

For receive, create-share, CMS sync, STRM move, and cleanup, simulate:

1. operation marked started.
2. external side effect succeeds.
3. process exits before checkpoint.
4. next run observes the postcondition.
5. operation and facts converge without replay.

Assert exact operation attempt counts and no duplicate P115/CMS calls.

- [ ] **Step 2: Add the final task 447 race test**

Run TaskRunner at `STRM_READY`, invoke the maintenance observer between source detection and the next runner iteration, and assert the observer only enqueues one `repair_move` command and never moves the directory. Then place a valid destination with source absent and assert TaskRunner checkpoints move state and continues rather than reaching `stage_wait_timeout`.

- [ ] **Step 3: Replace SubmissionStore row lookup with a task-fact projection**

Add one read method returning the existing dict-shaped keys from normalized facts for helper compatibility:

```text
workflow_facts(task_id: int) -> dict[str, Any]
```

Implement it as one joined `SELECT`; it is read-only and must not expose a mutable legacy submission ID or create missing records.

- [ ] **Step 4: Convert stages in execution order**

For each stage group, first change its tests, then remove direct `self.store.update_*` calls and return a `StageCheckpoint`:

1. received/cloud download and intake identity.
2. organizing/recognition and multi-target facts.
3. alias/share creation/share validation.
4. CMS share sync.
5. STRM readiness and CMS delete settlement.
6. move, Emby confirmation, review, and cleanup.

A stage may prepare/start an operation before the external call, but operation completion and domain facts must be returned in the checkpoint.

- [ ] **Step 5: Preserve multi-target serialization**

Store each target in `task_targets`; keep deterministic ordinal ordering. Assert only one target CMS sync is active, completed target operations are reused, all sources are retained on any uncertain/failed target, and cleanup runs only after every target is confirmed.

- [ ] **Step 6: Remove task-owned execution from `app/media/strm.py`**

Keep pure validators/planners and TaskRunner-called move helpers. Split candidate discovery into a pure `find_stranded_self_share_moves()` that returns task IDs/facts without mutating files or state; remove `repair_stranded_self_share_moves` and task-associated restore execution. Task 10 will schedule commands from the pure detector. Any remaining direct-mode media repair must reject paths owned by a task.

- [ ] **Step 7: Remove submission reset/mirroring**

Delete `reset_self_share_submission_for_reprocess()` and make reprocess clear normalized fact rows/fields in the same command transaction while preserving successful receive facts according to the existing operation-journal rule.

- [ ] **Step 8: Run focused and broad workflow tests**

```bash
python3 -m unittest \
  tests.test_bridge_task_engine tests.test_self_share_workflow \
  tests.test_runtime_recovery tests.test_task_runner \
  tests.test_task_actions tests.test_intake_identity -q
python3 -m compileall -q app bridge.py
git diff --check
```

- [ ] **Step 9: Commit**

```bash
git add app/workflows/self_share.py app/media/strm.py app/task_bridge.py \
  app/task_runner.py tests/test_bridge_task_engine.py tests/test_self_share_workflow.py \
  tests/test_runtime_recovery.py tests/test_task_runner.py tests/test_task_actions.py
git commit -m "refactor: checkpoint self-share workflow facts"
```

---

### Task 9: Convert Direct, Source-Share, and Cloud Workflows

**Files:**
- Modify: `app/workflows/direct.py`
- Modify: `app/workflows/self_share.py` cloud/source routing sections
- Modify: `app/task_engine.py`
- Test: `tests/test_direct_workflow.py`
- Test: `tests/test_source_share_workflow.py`
- Test: `tests/test_cloud_workflow.py`
- Test: `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Add checkpoint assertions to each workflow family**

For each workflow, assert no fact table changes until TaskRunner commits the returned checkpoint. Add a stale-claim test proving external success cannot be applied by a lost owner.

- [ ] **Step 2: Convert direct workflow writes**

Replace `update_status`, `update_recognition`, `update_move`, `update_emby`, and `update_self_share` calls with `StageCheckpoint` facts. Journal CMS submissions, filesystem moves, and Emby refresh requests.

- [ ] **Step 3: Convert source-share and cloud workflows**

Use the same task/fact/checkpoint API. Preserve source-specific `(source_type, source_key)` uniqueness, cloud resume semantics, and successful-operation reuse. Do not introduce a generic plugin/factory layer.

- [ ] **Step 4: Run tests and commit**

```bash
python3 -m unittest \
  tests.test_direct_workflow tests.test_source_share_workflow \
  tests.test_cloud_workflow tests.test_bridge_task_engine tests.test_task_engine -q
python3 -m compileall -q app
git diff --check
git add app/workflows/direct.py app/workflows/self_share.py app/task_engine.py \
  tests/test_direct_workflow.py tests/test_source_share_workflow.py \
  tests/test_cloud_workflow.py tests/test_bridge_task_engine.py tests/test_task_engine.py
git commit -m "refactor: checkpoint remaining workflows"
```

---

### Task 10: Convert Maintenance, Invalid-Share, and Quality to Commands

**Files:**
- Modify: `app/self_share_health.py`
- Modify: `app/quality_automation.py`
- Modify: `app/quality.py`
- Modify: `bridge.py`
- Test: `tests/test_invalid_share_cleanup.py`
- Test: `tests/test_quality_automation.py`
- Test: `tests/test_quality_checks.py`
- Test: `tests/test_quality_telegram.py`
- Test: `tests/test_task_commands.py`

- [ ] **Step 1: Add observer-command tests**

Assert repeated scans create one idempotent command per task/finding/version, do not claim the task, do not mutate workflow facts, and do not call P115 delete, CMS mutation, filesystem move/delete, or Emby refresh.

Use one helper so keys never persist raw share codes, receive codes, URLs, or paths:

```python
def command_key(kind: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{kind}:{hashlib.sha256(material).hexdigest()}"

command_key("invalid-share", task_id, share_code, observed_state)
command_key("repair-move", task_id, expected_destination)
command_key("quality", task_id, rule_id, rule_version, action)
```

Logs may include task ID and command type, but not the unhashed key material.

- [ ] **Step 2: Convert invalid-share probing**

Persist probe observations in `task_probes` and enqueue `invalidate_share`. TaskRunner validates the current claim/task state, destination proof, and operation identity before deleting or restoring anything; notification occurs only after command completion.

- [ ] **Step 3: Convert quality automation**

Keep scanning, rules, planning, cooldowns, and UI summaries. Delete `_QualityRepairAdapter` side effects and direct compare-and-set workflow transitions. `execute_plan()` enqueues `quality_repair`; TaskRunner converts the command to the appropriate stage. Remove the temporary `config.quality_auto_repair_enabled = False` containment assignment from `bridge.py` only after these command tests pass.

- [ ] **Step 4: Convert stranded/missing detection**

Replace self-share maintenance execution with a read-only detector that enqueues `repair_move`/`restore`. It must skip tasks with incomplete identity and must never call move/restore helpers itself.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest \
  tests.test_invalid_share_cleanup tests.test_quality_automation \
  tests.test_quality_checks tests.test_quality_telegram tests.test_task_commands -q
python3 -m compileall -q app bridge.py
git diff --check
git add app/self_share_health.py app/quality_automation.py app/quality.py bridge.py \
  tests/test_invalid_share_cleanup.py tests/test_quality_automation.py \
  tests/test_quality_checks.py tests/test_quality_telegram.py tests/test_task_commands.py
git commit -m "refactor: route observers through task commands"
```

---

### Task 11: Convert Web, Telegram, HDHive, Cache, and Background State

**Files:**
- Modify: `app/web.py`, `app/web_api.py`, `app/telegram_ui.py`
- Modify: `app/hdhive_subscription_store.py`, `app/hdhive_subscriptions.py`, `app/hdhive_cards.py`
- Modify: `app/background_jobs.py`, `app/backup.py`
- Modify: `bridge.py`
- Test: `tests/test_web_admin.py`, `tests/test_web_api.py`
- Test: `tests/test_telegram_client.py`, `tests/test_quality_telegram.py`
- Test: `tests/test_hdhive_subscription_store.py`, `tests/test_hdhive_subscriptions.py`, `tests/test_hdhive_cards.py`
- Test: `tests/test_background_jobs.py`, `tests/test_backup.py`

- [ ] **Step 1: Replace two-store read tests with joined projections**

Update fixtures to create one `TaskStore(database_path)`. Assert historical synthetic tasks, normalized facts, and archived tasks render correctly without numeric submission-ID fallback or lazy backfill.

- [ ] **Step 2: Route user actions to commands**

Web/TG retry, reprocess, resume-organizing, restore, Emby check, and terminate enqueue idempotent commands. Archive uses the guarded atomic archive operation. Purge remains admin-only and requires the exact confirmation string.

- [ ] **Step 3: Point HDHive and TMDB cache at the unified database**

Reuse `Database.connect()` and existing table names. Remove independent connection initialization and separate `tmdb-card-cache.db`. Keep HDHive subscription scheduling independent, but any resulting ingest/media mutation must enqueue a task/command.

- [ ] **Step 4: Use unified runtime and backup state**

Background job coordinator, health heartbeat, backup scheduler, and settings use the same `runtime_state` table with namespaced keys. Backup contains one database source.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest \
  tests.test_web_admin tests.test_web_api tests.test_telegram_client \
  tests.test_quality_telegram tests.test_hdhive_subscription_store \
  tests.test_hdhive_subscriptions tests.test_hdhive_cards \
  tests.test_background_jobs tests.test_backup -q
python3 -m compileall -q app bridge.py
git diff --check
git add app/web.py app/web_api.py app/telegram_ui.py app/hdhive_subscription_store.py \
  app/hdhive_subscriptions.py app/hdhive_cards.py app/background_jobs.py app/backup.py bridge.py \
  tests/test_web_admin.py tests/test_web_api.py tests/test_telegram_client.py \
  tests/test_quality_telegram.py tests/test_hdhive_subscription_store.py \
  tests/test_hdhive_subscriptions.py tests/test_hdhive_cards.py \
  tests/test_background_jobs.py tests/test_backup.py
git commit -m "refactor: use unified application state"
```

---

### Task 12: Remove SubmissionStore and Legacy Execution

**Files:**
- Delete: `app/legacy_polling.py`
- Modify: `bridge.py`
- Modify: `app/task_bridge.py`
- Modify: `app/config.py`
- Modify: `app/task_health.py`, `app/task_diagnostics.py`, `doctor.py`
- Modify: `.env.example`, `docker-compose.yml`, `README.md`, `docs/dockerhub-overview.md`
- Modify: `tests/test_refactor_imports.py`, `tests/test_bridge_v02_integration.py`
- Modify: `tests/test_docs_task_engine.py`, `tests/test_docs_v02.py`, `tests/test_doctor.py`

- [ ] **Step 1: Add absence/startup-gate tests**

Assert:

```python
self.assertFalse(hasattr(bridge, "SubmissionStore"))
self.assertFalse(hasattr(bridge, "best_effort_task_sync"))
```

Also assert no source imports `app.legacy_polling`, no runtime code reads `TASK_ENGINE_ENABLED`, and startup fails if legacy executor settings are enabled or the database schema/write gate is incompatible.

- [ ] **Step 2: Replace configuration**

Add `database_path: str = '/data/cms-tg-ingest.db'` populated by `DATABASE_PATH`. Remove runtime `db_path`, `task_db_path`, and `task_engine_enabled`. Keep `DB_PATH` and `TASK_DB_PATH` only as explicit CLI migration arguments, never application fallbacks. `run_forever` reads the one-way gate in `migration_runs`: `closed` exposes read-only history/health only; `runner_open` starts TaskRunner but not Telegram intake, command-producing observers, or mutating Web actions; `open` enables the full runtime.

- [ ] **Step 3: Delete active legacy code**

Remove:

- `SubmissionStore` and every mutable submission helper.
- `best_effort_task_sync` and `sync_*_task_event` mirroring.
- `start_status_poll`, status repair, engine-off execution, and engine flags.
- task/submission linkage in metadata and lazy Web submission-ID backfill.
- obsolete tests that assert legacy execution remains available.

Do not delete migration reader code or immutable legacy archive/map tables.

- [ ] **Step 4: Update health and docs**

Health/doctor must report schema version, migration ID, write-gate state, singleton runner lease owner/expiry, runner heartbeat, pending commands, and integrity status. Update Compose and docs to mount/use only `/data/cms-tg-ingest.db`; document that `/cms/cms-online.db` remains external CMS state.

- [ ] **Step 5: Prove no competing writers remain**

Run:

```bash
rg -n "SubmissionStore|best_effort_task_sync|start_status_poll|start_status_repair_loop|repair_stranded_self_share_moves|TASK_ENGINE_ENABLED|TASK_DB_PATH|DB_PATH" app bridge.py doctor.py
```

Expected: no active runtime matches. Allowed matches are migration CLI/input documentation and migration tests only.

- [ ] **Step 6: Run full verification and commit**

```bash
python3 -m unittest discover -s tests -p 'test*.py' -q
python3 -m compileall -q app bridge.py doctor.py scripts/migrate_unified_db.py tests
python3 -m unittest tests.test_secret_hygiene -q
git diff --check
git add -A app bridge.py doctor.py .env.example docker-compose.yml README.md \
  docs/dockerhub-overview.md tests
git commit -m "refactor: remove legacy workflow executor"
```

---

### Task 13: Audit the Complete Refactor Before Release

**Files:**
- Modify only files required by verified review findings.

- [ ] **Step 1: Run the exact required behavioral matrix**

```bash
python3 -m unittest \
  tests.test_database tests.test_unified_migration tests.test_task_commands \
  tests.test_task_store tests.test_task_runner tests.test_runtime_recovery \
  tests.test_bridge_task_engine tests.test_self_share_workflow \
  tests.test_direct_workflow tests.test_source_share_workflow \
  tests.test_cloud_workflow tests.test_invalid_share_cleanup \
  tests.test_quality_automation tests.test_task_actions \
  tests.test_backup tests.test_doctor -q
```

Expected: zero failures, including task 447 race, crash windows, claim loss, duplicate commands/operations, lease contention, migration ambiguity, archive/purge, and restore.

- [ ] **Step 2: Run the full repository gates**

```bash
python3 -m unittest discover -s tests -p 'test*.py' -q
python3 -m compileall -q app bridge.py doctor.py scripts/migrate_unified_db.py tests
python3 -m unittest tests.test_secret_hygiene -q
git diff --check
git status --short --branch
```

- [ ] **Step 3: Run independent reviews**

Request three read-only reviews against the design and this plan:

1. correctness/writer ownership and task 447 recovery.
2. migration completeness/idempotency/rollback gate.
3. simplicity/dead legacy code and unnecessary compatibility layers.

Apply only verified findings with focused regression tests. Commit fixes as `fix: address unified engine review findings`.

- [ ] **Step 4: Repeat the production-copy migration**

Use fresh copied source databases. Require the same logical counts and zero ambiguity as Task 4, then test a unified backup/restore and read-only Web/API history against the migrated copy.

- [ ] **Step 5: Test SQLite mode on the Unraid filesystem**

With the application stopped and a disposable migrated copy, run concurrent reader/single-writer tests plus backup/restore on the real `/mnt/user/appdata/cms-tg-ingest/data` filesystem. Keep rollback-journal mode unless WAL passes without lock, durability, or backup anomalies. Record the chosen mode in the migration report and docs.

---

### Task 14: Release `v0.5.0` and Perform the Maintenance-Window Cutover

**Files:**
- Modify: `app/__init__.py`, `CHANGELOG.md`, `README.md`, `docs/dockerhub-overview.md`
- Modify only if required: `.github/workflows/release-images.yml`
- Production: Unraid Compose and application data, only during approved window.

- [ ] **Step 1: Add release metadata and run preflight**

Set version `0.5.0`; document the unified database, single writer, migration requirement, archive semantics, and forward-fix boundary.

```bash
git fetch origin
python3 -m unittest discover -s tests -p 'test*.py' -q
python3 -m compileall -q app bridge.py doctor.py scripts/migrate_unified_db.py tests
python3 -m unittest tests.test_secret_hygiene -q
git diff --check
git status --short --branch
```

- [ ] **Step 2: Commit, push, tag, and verify image provenance**

```bash
git add app/__init__.py CHANGELOG.md README.md docs/dockerhub-overview.md
# Add .github/workflows/release-images.yml only if it actually changed.
git commit -m "release: publish v0.5.0"
git push origin main
git tag -a v0.5.0 -m "release v0.5.0"
git push origin v0.5.0
```

Wait for the tagged GitHub Actions run. Verify the release/tag/image point to the same commit and Docker Hub provides `linux/amd64` and `linux/arm64`.

- [ ] **Step 3: Close all production writers**

Pause intake and mutating Web/TG actions, then stop `cms-tg-ingest`. Confirm no TaskRunner, quality, probe, maintenance, HDHive scheduler, or backup process remains. Do not stop or modify the external CMS database.

- [ ] **Step 4: Create paired legacy snapshots and evidence**

Use SQLite backup for both legacy databases. Record timestamp, path, size, SHA-256, `quick_check`, image ID, Compose file, and `.env`. Keep snapshots read-only and do not remove earlier backups.

- [ ] **Step 5: Import and validate the cutover database**

```bash
python3 scripts/migrate_unified_db.py \
  --tasks /data/cutover/tasks.db \
  --submissions /data/cutover/submissions.db \
  --output /data/cms-tg-ingest.new.db \
  --report /data/cutover/migration-report.json
python3 scripts/migrate_unified_db.py --validate /data/cms-tg-ingest.new.db
```

Require report counts to match the immediately preceding source snapshots, every legacy submission to map once, zero FK/integrity errors, unchanged existing event/operation IDs, and no executable synthetic task.

- [ ] **Step 6: Atomically install the new database and start read-only**

Rename on the same filesystem to `/data/cms-tg-ingest.db`. Start `v0.5.0` with intake, TaskRunner, and mutating UI actions closed. Validate:

- read-only Web/API history and normalized facts.
- offline Telegram rendering against migrated task projections without polling updates.
- task 447 terminal success and destination/Emby facts.
- archived records and legacy-import history visibility.
- schema version and migration ID.
- unified online backup and restore.
- no runner lease yet and no workflow side-effect logs.

This is the final lossless rollback gate.

- [ ] **Step 7: Roll back immediately if read-only validation fails**

Stop `v0.5.0`, retain the failed unified DB/report, restore the old image/config and both paired snapshots, run `quick_check`, and restart `v0.4.33`. Do not attempt partial reverse import.

- [ ] **Step 8: Open the unified write gate**

If validation passes, first require the cutover report to show zero executable tasks currently scheduled to run. If any exist, abort the cutover at the lossless rollback gate, restore `v0.4.33`, drain or explicitly resolve them in the legacy runtime, and repeat the snapshots/import/validation. Never edit runnable flags in the migrated database merely to pass the gate.

Once the runnable count is zero, stop the read-only instance, open only the runner gate, and restart `v0.5.0`:

```bash
migration_id="$(python3 scripts/migrate_unified_db.py --validate /data/cms-tg-ingest.db --print-migration-id)"
python3 scripts/migrate_unified_db.py \
  --open-runner-gate /data/cms-tg-ingest.db \
  --migration-id "$migration_id"
```

Confirm one lease and a fresh heartbeat. Enqueue one deliberate read-only Emby-check smoke command for a known completed task and verify command completion without filesystem, 115, or CMS mutation. Production is forward-fix-only from `runner_opened_at`.

Stop the runner-only instance, open intake, and restart:

```bash
python3 scripts/migrate_unified_db.py \
  --open-intake-gate /data/cms-tg-ingest.db \
  --migration-id "$migration_id"
```

Confirm Telegram intake and mutating Web actions are now available.

- [ ] **Step 9: Verify production after write gate**

Confirm container/image/version/health, compatible schema, one active runner lease, non-stale heartbeat, empty failed-command queue, successful unified backup, and no new P115/CMS/STRM/Emby errors. Verify one real intake reaches terminal state with exactly one operation identity per side effect.

- [ ] **Step 10: Retain evidence and close the migration**

Keep paired legacy snapshots and the immutable migration report for the configured retention window. Do not reactivate legacy executors after unified writes. Any post-gate issue freezes intake, backs up the unified database, and is fixed forward.

---

## Final Acceptance Checklist

- [ ] Production has one application-owned SQLite database; `/cms/cms-online.db` remains external.
- [ ] All 174 legacy tasks and 310 submissions from the measured snapshot map exactly once, with source-count drift handled by the same invariant at actual cutover.
- [ ] Submission-only records are visible, complete, and non-runnable.
- [ ] TaskRunner is the only workflow side-effect executor.
- [ ] Every critical side effect has an immutable operation identity and recovery check.
- [ ] Every stage checkpoint verifies stage, status, claim token, and expected version.
- [ ] The exact task 447 race cannot move files from an observer or regress a valid destination to source-wait.
- [ ] Duplicate commands, operations, and runner startups are fenced.
- [ ] Ordinary delete archives; permanent purge is explicit, constrained, and audited.
- [ ] Unified backup/restore and pre-write rollback are verified on Unraid storage.
- [ ] `SubmissionStore`, legacy polling/status repair, `best_effort_task_sync`, and engine-off execution are absent from runtime code.
- [ ] Full unittest, compileall, secret-hygiene, migration dry-run, logical checksum, and `git diff --check` gates pass.
