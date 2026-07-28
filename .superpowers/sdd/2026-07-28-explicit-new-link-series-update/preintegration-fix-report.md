# Pre-Integration Fix Report: Explicit New-Link Series Update

Date: 2026-07-28

## Scope

Completed the pre-integration fixes for the explicit new-link series update branch without resetting or discarding the inherited uncommitted work. The runtime-side-effect-stability worktree was not inspected or modified.

## Implementation

- Kept `TaskStore.get_or_create_share_task()` as the source-creation API. It uses the store lock and one SQLite `BEGIN IMMEDIATE` transaction, inserts with `ON CONFLICT DO NOTHING`, then returns the selected row. Existing rows therefore retain their URL, chat ID, metadata, stage, status, claims, schedule, and `updated_at` unchanged. Ordinary `upsert_task()` was not changed.
- Made same-parent unscheduled active tasks recoverable only when they are the exact preparation checkpoint shape: `received`, `pending` or `running`, unclaimed, and `next_run_at < 0`.
- Parked an exact checkpoint through snapshot-guarded CAS as `needs_action/needs_action`, with `next_run_at=-1`, returning `failed`. Scheduled same-parent tasks still return `already_started`.
- Routed interruption exceptions from child-submission preparation through the existing failure/parking transition, including interruptions before preparation and after the child submission transaction has committed.
- Kept activation CAS-loss parking guarded by the frozen snapshot's `updated_at`; a changed concurrent snapshot is returned without being overwritten.

## Coverage

- TaskStore create-once coverage now asserts the exact returned snapshot keeps URL, chat ID, metadata, stage/status, claims, schedule, and `updated_at`.
- The concurrent source-creation test uses two real `TaskStore` instances on the same SQLite database. A test-only wrapper pauses only the losing call to the atomic API with `Event` coordination; it uses no sleeps and proves the loser cannot change the winner's URL, chat ID, `updated_at`, parent metadata, stage/status, or checkpoint schedule.
- Interruption coverage proves immediate parking before preparation and after child-submission commit, then confirms an explicit retry starts a new guarded run.
- Activation coverage proves both exact-checkpoint parking and that a concurrent changed snapshot is not overwritten.
- A non-`received` unscheduled same-parent task remains unchanged, proving checkpoint parking is restricted to the deliberate preparation state.

## TDD Evidence

No prior agent report existed, so no prior RED result is claimed.

1. Initial inherited focused checks:

   `python3 -m unittest` with the inherited create-once, race, and three checkpoint tests

   Result: 5 tests passed. This showed the inherited interruption tests only parked on a later request and did not expose the required immediate recovery behavior.

2. RED after correcting the regression tests:

   `python3 -m unittest` with the create-once, real-SQLite race, three checkpoint/activation, changed-snapshot, and non-checkpoint cases

   Result: 7 tests ran; 3 failures. The two interruption paths escaped `KeyboardInterrupt` without parking, and an `organizing/pending/next_run_at=-1` same-parent task was incorrectly parked.

3. GREEN after the minimal bridge changes:

   Same 7-test command.

   Result: 7 tests passed.

## Verification

- `python3 -m compileall -q app bridge.py doctor.py`
  - Exit 0.
- `python3 -m unittest tests.test_task_store tests.test_bridge_v02_integration`
  - Exit 0; 161 tests passed. The command emitted expected logged traces from simulated failure fixtures.
- `python3 -W error::ResourceWarning -m unittest discover -s tests -v`
  - Exit 0; 1044 tests passed.
- `python3 -W error::ResourceWarning -m unittest tests.test_doctor -v`
  - Exit 0; 18 tests passed.
- `git diff --check`
  - Exit 0 before final report creation; rerun before commit.

## Files

- `app/task_store.py`
- `bridge.py`
- `tests/test_task_store.py`
- `tests/test_bridge_v02_integration.py`
- `.superpowers/sdd/2026-07-28-explicit-new-link-series-update/preintegration-fix-report.md`

## Self-Review

- `get_or_create_share_task()` never executes an update on conflict and selects the canonical task inside its transaction.
- The explicit helper invokes the atomic API rather than a lookup followed by ordinary upsert.
- Parking requires the frozen `received` state, unclaimed snapshot fields, an unscheduled timestamp, and matching `updated_at`; a concurrent change cannot be overwritten.
- Child preparation failure, interruption, and activation CAS loss all result in either a visible `needs_action` record or the actual concurrent snapshot, never an unobserved frozen checkpoint owned by this helper.
- `upsert_task()` remains unchanged; its existing mode/idempotency/claim tests run in the required TaskStore module verification.

## Concerns

The full warning-policy suite exited successfully with 1044 passing tests, but after its `OK` summary Python printed two `ResourceWarning: unclosed database` finalizer messages. Running `tests.test_doctor` alone with the same warning policy did not reproduce them. This fix does not touch that unrelated cleanup path, so the observation is retained for follow-up rather than hidden by a broad unrelated change.
