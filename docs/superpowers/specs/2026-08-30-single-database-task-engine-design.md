# Single-Database Task Engine Design

**Date:** 2026-08-30

## Goal

Replace the current TaskStore/SubmissionStore split with one application-owned SQLite database and one workflow executor. TaskRunner becomes the only component allowed to advance task execution or perform side effects against 115, CMS, STRM files, and Emby.

The refactor must preserve complete recorded history, migrate production data without guessing through ambiguity, remove the legacy executor, and make task recovery converge forward from observable facts instead of replaying completed work.

## Problem Statement

The application describes TaskStore as authoritative, but runtime behavior still has multiple state owners:

- TaskRunner advances `tasks`, `task_events`, and `task_operations`.
- Workflow stages write related facts to SubmissionStore in a separate database.
- Self-share maintenance can move or restore STRM files by scanning SubmissionStore independently of TaskRunner claims.
- Invalid-share and quality repair paths can perform their own mutations and side effects.
- Legacy polling and status repair preserve a second execution model.

Moving the two existing schemas into one file would not fix this. The defect is duplicate authority, not merely separate SQLite files.

## Task 447 Evidence

Task 447 demonstrates the race:

1. TaskRunner successfully received the share, found the CMS-organized folder, created and validated the permanent share, and submitted CMS share sync.
2. CMS generated the expected shared STRM at 20:29:09. TaskRunner found the source and entered playback validation at 20:29:11; CMS then logged successful 302 responses for the expected share URL at 20:29:11, 20:29:26, and 20:29:41.
3. The self-share maintenance loop independently selected SubmissionStore row 437 as a stranded move and moved the STRM directory to the media library at 20:29:54.
4. The next TaskRunner pass still executed `strm_ready`. It only searched the source directory, did not accept the validated destination and persisted `move_status=moved` as a later fact, and returned to "waiting for source".
5. Emby imported the movie successfully at 20:31:31, but TaskRunner timed out after 30 minutes and marked the task `needs_action`.

The external workflow succeeded. The application's two state machines disagreed about ownership and progress.

## Production Data Shape

The migration design is based on a read-only production inventory:

- 174 TaskStore tasks, including 151 share tasks.
- 310 SubmissionStore rows.
- All 151 share tasks match one submission by normalized share identity.
- 159 historical submissions have no TaskStore task.
- 146 task-to-submission links exist only in `metadata_json`.
- No linked-ID mismatches were found among records that could be checked.

Complete-history migration must therefore create non-runnable historical tasks for unmatched submissions and retain an explicit legacy-ID mapping.

## Scope

### In Scope

- One application-owned SQLite database.
- Normalized task execution and workflow fact tables.
- One TaskRunner side-effect owner.
- A durable command queue for Web, Telegram, quality, and observer requests.
- Migration of all tasks, submissions, events, operations, runtime settings, quality runs, category memory, and HDHive tables.
- Removal of active legacy polling, status repair, and direct maintenance executors.
- Archive-by-default deletion.
- A maintenance-window production cutover with a pre-write rollback gate.
- Unified online backup and restore.

### Out of Scope

- Importing CMS-owned `/cms/cms-online.db` into the application database.
- Supporting multiple concurrent TaskRunner processes.
- Reconstructing historical events that were never recorded.
- Automatic reverse export from the unified database to the two legacy schemas.
- Redesigning CMS, 115, or Emby APIs.
- Event sourcing the entire application.

## Decisions

- Production migration may use a short maintenance window.
- Complete recorded history must be retained.
- One SQLite file may contain multiple normalized tables.
- The legacy executor will be removed, not retained behind a runtime flag.
- Ordinary deletion archives a task; permanent purge is separate and explicit.
- Rollback is lossless only before intake and TaskRunner reopen. Once unified writes begin, recovery is forward-fix.
- TaskRunner remains single-process and single-active-runner for this refactor.

## Rejected Alternatives

### Merge Existing Tables Without Changing Ownership

Rejected because it preserves duplicate mutable status and the maintenance race inside one database.

### Put Every Submission Field in `tasks` or `metadata_json`

Rejected because it creates an oversized row, weak constraints, duplicated mutable state, and difficult migrations.

### Full Event Sourcing

Rejected because existing history is incomplete and deriving every read model from events would add substantial complexity without improving recoverability over normalized facts plus an operation journal.

## Target Architecture

### Database Boundary

The application uses one database path, defaulting to:

```text
/data/cms-tg-ingest.db
```

`DB_PATH` and `TASK_DB_PATH` are accepted only as migration inputs. Runtime code uses one `DATABASE_PATH` setting after cutover.

One database component owns connection setup and transaction creation. Every connection enables:

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = <configured milliseconds>;
```

WAL is enabled only after tests confirm correct behavior on the production Unraid mount. No network or filesystem call may occur while a SQLite transaction is open.

Repository modules may remain separated by domain, but a cross-domain checkpoint must use one supplied connection and one transaction.

### Canonical Tables

#### `schema_meta`

Stores schema version, migration ID, migration timestamp, and binary compatibility range. Startup refuses unsupported versions.

#### `tasks`

The sole execution record:

- Source identity: `source_type`, `source_key`, share identity where applicable, URL, chat ID, and origin.
- Display identity: title and timestamps.
- Runner state: current stage, execution status, error, retry count, scheduling, claim owner/token/heartbeat.
- Archive state: `archived_at`, `archived_by`, and archive reason.

`UNIQUE(source_type, source_key)` remains the canonical intake identity. Share identity uniqueness is a partial constraint applying only to share tasks, so non-share sources with empty share fields do not collide.

`tasks` does not contain submission IDs, workflow-phase mirrors, move status, Emby status, cleanup status, or mutable workflow links in JSON.

#### `task_media`

One-to-one media and recognition facts:

- CMS task ID.
- Recognized title and media type.
- TMDB ID, category, recognition status, and immutable recognition evidence.
- Poster, backdrop, release, genre, and other presentation facts currently needed by Web and Telegram.

#### `task_shares`

One-to-one permanent-share facts for single-target tasks:

- 115 file/folder identity.
- Permanent share code and receive code.
- Canonical name, alias, canonical manifest, creation timestamp.
- Validation and review facts.
- CMS share-sync submission facts.

Secrets and full URLs remain subject to existing redaction rules.

#### `task_moves`

One-to-one STRM movement facts:

- Expected source and destination.
- Move state, error, start and finish timestamps.
- Validated destination identity and validation timestamp.

Move truth is monotonic. A missing source plus a validated expected destination and matching operation evidence is `moved`, never "waiting for source".

#### `task_emby`

One-to-one Emby facts: confirmation state, item ID, title, path, library, refresh request timestamp, and confirmation timestamp.

#### `task_cleanups`

One-to-one cleanup facts: target identity, cleanup state, error, attempt timestamp, and completion timestamp.

#### `task_probes`

One-to-one validation/probe facts: last probe, invalid timestamp and reason, review status, and next required check.

#### `task_targets`

Normalized per-target facts for multi-directory tasks. Each row owns one target's folder identity, recognition, share, STRM, move, Emby, cleanup, and ordinal.

`UNIQUE(task_id, target_key)` prevents duplicate targets. Critical per-target progress must not remain mutable only inside `organized_targets` JSON.

#### `task_events`

Append-only audit timeline. Existing event IDs and content are preserved.

#### `task_operations`

The existing idempotency journal, extended to cover every external or filesystem side effect, including STRM movement and repair. Existing IDs, requests, results, and operation keys are preserved.

#### `task_commands`

Durable requests submitted by non-runner components:

- Command ID and idempotency key.
- Task ID, command type, request payload, actor, and source.
- `pending`, `claimed`, `completed`, `failed`, or `cancelled` state.
- Claim token, result, error, and timestamps.

Web, Telegram, quality, maintenance observers, and invalid-share observers may create commands but may not perform the corresponding side effect.

#### Supporting Tables

The unified file also owns `runtime_state`, `quality_runs`, `parent_category_memory`, HDHive subscription tables, and background-job state. Each has a clear domain owner and foreign keys where applicable.

#### Migration Audit Tables

- `legacy_submission_map(legacy_submission_id PRIMARY KEY, task_id UNIQUE, imported_at)`.
- `legacy_submission_archive` containing the immutable original row payload, source checksum, and import metadata.
- `migration_runs` containing source file hashes/sizes, counts, validation results, and write-gate status.

No mutable compatibility `submissions` table remains after consumers are migrated. A temporary adapter may expose old dict-shaped reads during development, but it must be read-only and removed before production cutover.

## Execution Ownership

### TaskRunner

TaskRunner is the only component permitted to:

- Call 115 receive, create-share, delete, or cloud-download mutations.
- Submit or mutate CMS workflow operations.
- Move, delete, restore, or replace task-owned STRM files.
- Trigger Emby refresh or advance Emby workflow state.
- Advance task execution state.
- Complete repair, cleanup, or invalidation commands.

A database-backed singleton runner lease prevents a second process from starting another executor. Supporting several workers requires a separate future design with resource leases and is not enabled by this refactor.

### Observers and Interfaces

- Telegram and Web create tasks or commands and read projections.
- Quality automation scans and plans, then creates commands.
- Self-share maintenance detects missing or stranded facts, then creates commands.
- Invalid-share probing records observations and creates invalidation commands.
- HDHive may own its independent subscription tables, but any ingest task or media side effect enters through tasks and commands.
- Backup is infrastructure-only and does not mutate workflow facts.

`best_effort_task_sync` and direct workflow-state mirroring are removed.

## Claim-Aware Checkpoints

Each stage follows this sequence:

1. TaskRunner claims a task and reads the expected stage and domain facts.
2. It prepares or loads an immutable operation journal entry.
3. It commits the prepared/started operation state before the external call.
4. It performs network or filesystem work outside a database transaction.
5. It reconciles the observable postcondition when the external result is uncertain or when recovering a started operation.
6. It commits one checkpoint transaction that verifies:
   - Task ID.
   - Expected stage.
   - Expected execution status.
   - Exact claim token.
7. That transaction atomically writes:
   - Domain facts.
   - Operation result or uncertainty.
   - New execution stage/status.
   - The task event.
   - Claim release or next scheduling state.

If the claim or expected state no longer matches, the checkpoint is discarded and cannot overwrite a newer task state.

## Recovery Invariants

1. Exactly one task exists per `(source_type, source_key)`.
2. Every normalized domain row belongs to one task through an enforced foreign key.
3. Mutable task linkage and workflow state are not duplicated in JSON.
4. Only TaskRunner advances execution or performs workflow side effects.
5. Every claimed checkpoint verifies stage, status, and claim token.
6. External and filesystem work occurs outside SQLite transactions.
7. Every side effect has an immutable operation identity and observable recovery rule.
8. Retry reconciles postconditions before replaying an operation.
9. Workflow facts move forward. A valid later fact cannot be downgraded because an earlier artifact has disappeared.
10. A validated destination is authoritative after a completed move.
11. Observers enqueue idempotent commands and never mutate task-owned resources.
12. Startup fails on schema mismatch, failed integrity checks, runner lease conflict, or enabled legacy execution configuration.
13. Synthetic historical tasks are never runnable.

## Deletion Semantics

Ordinary delete becomes an atomic archive operation:

- Set archive timestamp, actor, and reason.
- Cancel pending commands.
- Exclude the task from normal queue/history views unless archived records are requested.
- Preserve events, operations, workflow facts, and legacy mappings.

Permanent purge is a separate explicit operation requiring confirmation. It is allowed only for an archived, terminal, unclaimed task with no pending command. The purge audit stores an immutable task identity snapshot and actor without a cascading foreign key, so the record survives after task data is removed.

## Immediate Containment

The full migration requires several milestones. Before that work begins, ship a small containment release that:

1. Stops engine-mode self-share maintenance from moving or restoring task-owned files.
2. Disables direct invalid-share cleanup and quality repair side effects; scans remain read-only.
3. Preserves TaskRunner as the only active executor.
4. Repairs task 447 using already verified facts:
   - The expected permanent-share STRM exists in the validated media destination.
   - Submission history records the completed move.
   - Emby has imported the expected TMDB item.
   - No receive, share-create, or CMS-sync operation is replayed.
5. Resumes 447 from the first not-yet-authoritatively-recorded stage and lets TaskRunner finish Emby/cleanup checks.

Containment is not a substitute for the refactor. It closes the proven production race while the migration is implemented.

## Migration Algorithm

### Inputs and Output

The migration command reads immutable copies of:

- Legacy `tasks.db`.
- Legacy `submissions.db`.

It writes a new `cms-tg-ingest.new.db`. It never modifies either input.

### Import Rules

1. Record source paths, byte sizes, hashes, SQLite versions, and `quick_check` results.
2. Create the target schema in one transaction.
3. Preserve all existing task, event, operation, runtime, quality, and HDHive IDs.
4. Match share tasks and submissions by normalized `(share_code, receive_code)`.
5. Validate typed links and metadata-only links against the identity match.
6. Merge matched submission facts into normalized task-domain tables.
7. For every unmatched submission:
   - Allocate a task ID above the maximum existing task ID.
   - Set `origin='legacy_import'`.
   - Derive a conservative terminal/non-runnable display status.
   - Import all normalized facts.
   - Store its legacy ID mapping and immutable raw archive.
   - Do not create pending commands or `next_run_at` work.
8. Preserve unmatched non-share tasks and their operations unchanged.
9. Reset SQLite sequences to imported maxima.
10. Commit only after every source row is mapped exactly once.

### Abort Conditions

Migration aborts without producing a cutover database when it finds:

- Duplicate canonical source identities.
- Conflicting typed and JSON links.
- Multiple tasks claiming one submission.
- A linked submission whose source identity differs from its task.
- Orphan events, operations, commands, or normalized child rows.
- Malformed required source identity.
- An unmapped legacy column.
- A synthetic historical task that would be runnable.
- Checksum, count, uniqueness, FK, or integrity failure.

Ambiguous records are reported with IDs and reasons; the migration never guesses.

### Validation

After import, run:

- `PRAGMA quick_check`.
- `PRAGMA foreign_key_check`.
- Source/destination table counts.
- One-to-one legacy mapping counts.
- Raw archive checksum reconciliation.
- Source identity and share identity uniqueness checks.
- Event and operation parent checks.
- Operation key/request identity checks.
- Runnable-task and command queue checks.
- Schema-version compatibility checks.

The migration command supports repeatable dry-run and produces a machine-readable report for production approval.

## Implementation Milestones

### Milestone 0: Contain the Production Race

Disable non-runner workflow side effects and safely recover task 447. Add an exact concurrency regression test.

### Milestone 1: Unified Schema and Migration Tool

Implement the database component, schema, importer, audit tables, integrity validation, and production-sized dry-run tests.

### Milestone 2: Repository and Checkpoint API

Implement normalized repositories, claim-aware checkpoints, command queue, singleton runner lease, archive, purge audit, and one-file backup.

### Milestone 3: Workflow Conversion

Convert shared, direct, source-share, cloud, Emby, and cleanup workflows to normalized facts and journaled operations. Remove mutable SubmissionStore writes and metadata linkage.

### Milestone 4: Observer and Interface Conversion

Convert maintenance, quality, invalid-share probing, Web, Telegram, health, doctor, and documentation to unified reads or durable commands.

### Milestone 5: Remove Legacy Execution

Delete active legacy polling/status repair, `best_effort_task_sync`, engine-off fallback, obsolete configuration, and mutable compatibility adapters.

### Milestone 6: Cutover and Production Verification

Dry-run against production copies, perform the maintenance-window migration, validate before opening writes, then enable Runner and intake.

Each milestone uses one implementation writer, focused tests, full-suite verification, and independent correctness/migration/simplicity review before the next milestone begins.

## Test Strategy

### Execution and Recovery

- Exact task 447 race reproduction: an observer sees the source while TaskRunner is in `strm_ready`, but cannot move it or mutate workflow facts.
- External success followed by process crash before checkpoint.
- Claim loss before checkpoint.
- Duplicate command delivery.
- Duplicate operation preparation and recovery.
- Missing source with validated destination.
- Invalid-share observation during an active task.
- Two processes attempting to acquire the runner lease.
- Filesystem move interruption at every persisted boundary.

### Migration

- Metadata-only task links.
- Submission-only history.
- Task-only and non-share records.
- ID gaps and sequence reset.
- Empty share fields on non-share sources.
- Conflicting links and duplicate identities.
- Orphan events and operations.
- Malformed archived payloads.
- Idempotent repeated dry-runs.
- Exact row counts and checksum reconciliation.
- Synthetic historical tasks remain non-runnable.

### Product Behavior

- Archive hides but preserves a task.
- Explicit purge requires archived terminal state and creates an audit record.
- Web and Telegram history remain complete.
- Series update resolves the migrated canonical task.
- Quality and invalid-share actions create commands rather than side effects.
- Unified backup restores a consistent database.
- Health reports schema version, runner lease, migration state, and heartbeat.

### Verification Commands

Every milestone runs focused tests plus:

```sh
python3 -m unittest discover -s tests -p 'test*.py' -q
python3 -m compileall -q app bridge.py doctor.py tests
python3 -m unittest tests.test_secret_hygiene -q
git diff --check
```

Migration milestones additionally run the importer twice against copied production databases and compare normalized reports and logical table checksums. Binary SQLite file checksums are not compared because page layout and migration timestamps are not semantic state.

## Production Cutover

1. Publish a versioned multi-architecture image and verify its manifest.
2. Disable intake and all mutating interfaces.
3. Stop the application so Runner, quality, probes, maintenance, HDHive, and backup are no longer writing.
4. Snapshot both legacy databases with SQLite backup, run `quick_check`, and record hashes and sizes.
5. Run the migration into `cms-tg-ingest.new.db` in the same filesystem.
6. Run all migration validation and compare the recorded report to the approved dry-run expectations.
7. Atomically rename the new database to the configured unified path.
8. Start the new release with TaskRunner, intake, and mutating UI actions still closed.
9. Validate read-only Web/API/TG history, task counts, task 447 reconciliation, archive behavior, health, schema version, and unified backup/restore.
10. This is the lossless rollback gate.
11. If validation passes, acquire the singleton runner lease and enable TaskRunner.
12. Run a non-destructive workflow smoke test.
13. Enable intake and mutating Web/TG actions.
14. Record the cutover as forward-fix-only and retain legacy snapshots read-only for the configured retention window.

## Rollback

### Before Opening Writes

Rollback is lossless:

1. Stop the new process.
2. Retain the failed unified database and migration report for diagnosis.
3. Restore the previous image and configuration.
4. Restore the paired legacy snapshots.
5. Run `quick_check` on both.
6. Restart the old runtime.

### After Opening Writes

Do not reactivate old executors and do not copy unified rows piecemeal into legacy databases. Instead:

1. Freeze intake.
2. Back up the unified database.
3. Forward-fix the binary or migrate to a corrected unified schema.
4. Reconcile uncertain external effects through operation journals and observable postconditions.

No automatic reverse exporter is part of this design.

## Acceptance Criteria

- Production uses one application-owned SQLite file.
- All legacy rows are mapped or migration aborts with an explicit report.
- TaskRunner is the only workflow side-effect executor.
- No active legacy polling/status repair path remains.
- Every critical side effect has journaled recovery and claim-aware checkpointing.
- Task 447's exact failure mode cannot recur: a validated destination advances the task and an observer cannot move active task files.
- Ordinary delete archives; explicit purge is constrained and audited.
- Unified backup and restore are verified.
- Pre-write cutover validation supports lossless rollback.
- Full tests, migration dry-runs, secret hygiene, compilation, and diff checks pass.
- Unraid post-cutover health reports a compatible schema, one active runner, non-stale heartbeat, and no new TaskRunner/CMS/115 errors.
