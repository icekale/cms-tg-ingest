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

## Fix Round 2/5

### Root Cause

`start_series_update_from_link()` deliberately freezes a source row as an unclaimed `received/pending` checkpoint with `next_run_at=-1` before preparing the child submission. A concurrent duplicate using another `TaskStore` for the same SQLite database could read that exact in-flight snapshot and call `_park_series_update_checkpoint()`. The earlier race test released its losing invocation only after the winner returned, so it never overlapped this preparation window.

### Implementation

- Serialized the complete explicit new-link operation with a process-local lock keyed by the resolved `TaskStore` database path plus share code and receive code.
- Shared the lock registry across separate `TaskStore` instances and stored locks in a `WeakValueDictionary`, so unrelated databases do not contend and unused registry entries can be reclaimed.
- Kept the existing negative-checkpoint recovery branches unchanged. A process restart clears process-local ownership, allowing a genuinely stale checkpoint to be parked and explicitly retried rather than becoming permanently `already_started`.
- Kept `get_or_create_share_task()`, source claim/CAS checks, URL/chat/metadata/`updated_at` preservation, and ordinary `upsert_task()` unchanged.

### TDD Evidence

1. RED against production code at `1e1c67d` after adding the deterministic regression:

   `python3 -m unittest tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests.test_explicit_new_link_series_update_duplicate_does_not_park_active_preparation_checkpoint -v`

   Result: 1 test failed. While the first invocation was event-blocked in child-submission preparation, the duplicate changed the checkpoint from `TaskStage.RECEIVED` to `TaskStage.NEEDS_ACTION`.

2. GREEN after adding canonical per-database/share process-local serialization:

   Same command.

   Result: 1 test passed. The refined test coordinates two real `TaskStore` instances, signals when the duplicate enters database-identity resolution, verifies the frozen checkpoint state and `updated_at` remain unchanged before releasing preparation, then verifies the duplicate returns `already_started` after activation.

### Verification

- `python3 -m unittest -v -k series_update tests.test_bridge_v02_integration.BridgeTaskStoreHandleUpdateTests`
  - Initial run exposed an obsolete test-harness ordering assumption: the deliberately paused loser now held the outer operation lock. The test was re-coordinated so the intended winner enters first.
  - Final result: exit 0; 31 tests passed.
- `python3 -m unittest tests.test_bridge_v02_integration -v`
  - Exit 0; 81 tests passed. Logged exception traces are expected simulated failure fixtures.
- `git diff --check`
  - Exit 0 before this report update; rerun before commit.

### Files

- `bridge.py`
- `tests/test_bridge_v02_integration.py`
- `.superpowers/sdd/2026-07-28-explicit-new-link-series-update/preintegration-fix-report.md`

### Concerns

No new concerns for the current single-process bridge execution model. Coordination is intentionally process-local so process restart naturally releases ownership and permits stale-checkpoint recovery; a future multi-process bridge architecture would require a persistent ownership mechanism.
