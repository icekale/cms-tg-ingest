# Post-auto-organize Self-share Guard Design

## Goal

After cms-tg-ingest triggers CMS `auto_organize`, CMS consumes queued 115 life events during its async `auto_tidy` job. A stale `delete_file` event can delete a self-share STRM directory under `/media/share`. Today the recovery relies on the 300s maintenance loop (`restore_missing_self_share_library_folders`), so the deletion window can be minutes long and Emby can emit a misleading `library.deleted` notification.

This change adds a one-shot guard: right after a successful `auto_organize` trigger, schedule a delayed self-share directory validation that runs the existing restore path within seconds, instead of waiting for the next maintenance tick.

## Scope

In scope:
- Schedule a delayed restore guard after `BridgeSelfShareTaskWorkflow._trigger_cloud_auto_organize` successfully triggers CMS auto-organize.
- Reuse the existing `restore_missing_self_share_library_folders` function; no new restore logic.
- Add a small module-level dedupe so bursts of tasks do not spawn many redundant guard threads.
- Add unit tests with `delay_seconds=0` to verify the guard invokes the restore path.

Out of scope:
- No CMS-side changes (CMS PRO is PyArmor-obfuscated and exposes no exclusion switch).
- No change to the existing 300s maintenance loop.
- No change to how Emby webhooks are forwarded by CMS.

## Design

1. New helper `schedule_post_organize_restore_guard(store, cms, self_share_config, move_config, emby=None, delay_seconds=30, limit=50)` in `app/workflows/self_share.py`:
   - Dedupe with a module-level lock + last-scheduled timestamp; skip if a guard was scheduled within `delay_seconds`.
   - Spawn a daemon thread that sleeps `delay_seconds`, then calls `restore_missing_self_share_library_folders(store, cms, self_share_config, move_config, emby=emby, limit=limit)` inside a try/except.
   - Log at INFO when the guard runs and when it restores folders.

2. Call the helper from `_trigger_cloud_auto_organize` on both success paths (direct `run_auto_organize` without a prepared operation, and the prepared-started-completed path) just before returning `StageResult.complete`.

3. `delay_seconds` is derived from the workflow's `self_share_config.auto_organize_retry_seconds` (default 90) capped to `[15, 60]`, so the guard runs soon after CMS has consumed life events but far earlier than the maintenance loop.

## Error handling

- Thread exceptions are caught and logged; they never fail the task workflow.
- The guard is best-effort; the 300s maintenance loop remains the safety net.
- Dedupe timestamp is updated only after a thread is actually spawned.

## Testing

- Unit test: `schedule_post_organize_restore_guard` with `delay_seconds=0` invokes `restore_missing_self_share_library_folders` and returns the restore count.
- Unit test: second call within the dedupe window skips spawning another thread.
- Unit test: `_trigger_cloud_auto_organize` success path schedules the guard (restore called with `delay_seconds=0` after the stage returns).
- Run full local unittest suite.
