# Task 3 Report: Runner infrastructure recovery and truthful health

## Scope completed

- Added bounded supervision around `TaskRunner.run_once()`. Infrastructure exceptions now record `task_runner=error` and a truncated `task_runner_last_error`, back off, and retry without ending the worker.
- Runtime state now transitions to `running` after a successful iteration and `stopped` when the runner exits. The heartbeat refreshes the current state only; it never overwrites `error` with `running`, and it reports `error` if the execution thread is absent or dead.
- Added `runner_state` to the store aggregate, health summary/formatting, and `/api/v1/health` serialization. A fresh `error` state reports `TaskRunner心跳: error` rather than healthy/active.

## Changed files

- `app/task_runner.py`
- `app/task_store.py`
- `app/task_health.py`
- `app/web_api.py`
- `tests/test_task_runner.py`
- `tests/test_task_health.py`
- `tests/test_web_api.py`

## TDD evidence

1. RED: `python3 -m unittest tests.test_task_runner tests.test_task_health -v`
   - Failed as expected: the transient `sqlite3.OperationalError` ended the runner thread, and `TaskHealthSummary` had no `runner_state`.
2. RED: `python3 -m unittest tests.test_web_api.WebApiTests.test_health_api_exposes_runner_state -v`
   - Failed as expected with `KeyError: 'runner_state'` before API serialization was added.
3. GREEN: `python3 -m unittest tests.test_task_runner tests.test_task_health tests.test_web_api -v`
   - Passed: 55 tests, exit 0. The expected regression test logs the simulated transient store error while the worker recovers.
4. Patch hygiene: `git diff --check`
   - Passed: exit 0 with no whitespace errors.

## Self-review and concerns

- Confirmed stage-level workflow exception handling remains unchanged; this task only supervises infrastructure failures outside `workflow.run_stage`.
- Confirmed no retry, 115, or HDHive behavior was changed.
- Runtime-state writes are deliberately best-effort: a database outage may temporarily prevent status persistence, but it no longer terminates the worker. The next successful write refreshes the truthful state.
