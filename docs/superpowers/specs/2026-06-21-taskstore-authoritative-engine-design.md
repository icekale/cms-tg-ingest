# TaskStore Authoritative Engine Design

## Goal

Make `TaskStore` the execution authority for new 115 links so Telegram and Web can show one clear current stage for every link: received, organizing, recognizing, own-share creation, STRM generation, move, Emby confirmation, and cleanup.

## Assumptions

- This version is incremental. New links use the authoritative task engine; historical `SubmissionStore` rows remain readable and repairable.
- `SubmissionStore` stays as a compatibility/detail table for existing CMS, 115, move, Emby, and cleanup metadata during the migration.
- The self-share workflow remains the production path: receive the external 115 share, let CMS organize it, create our own permanent share, run CMS share-sync from our own share, move generated STRM, confirm Emby, then delete the 115 source file without cancelling the share.
- This change must not introduce CMS ordinary share-down submission for the self-share flow. The only CMS sync after our own share is `add_share115_sync_task()`.
- Cleanup remains guarded: never delete the 115 source until the own share exists, STRM has moved, and Emby is confirmed. If Emby confirmation is disabled, cleanup should stop as `needs_action` unless a separate explicit cleanup override is configured.

## Current State

The repository already has:

- `app/models.py`: stage/status vocabulary and retry decisions.
- `app/task_store.py`: `tasks` and `task_events` SQLite tables.
- `app/task_bridge.py`: sidecar projection from `SubmissionStore` rows into `TaskStore`.
- `app/web.py`: task list/detail pages and a retry POST endpoint.
- `bridge.py`: real runtime orchestration with `SubmissionStore`, `handle_update()`, and per-link `start_status_poll()` threads.

The explicit limitation in `README.md` is that `TaskStore` is still a sidecar timeline. The user-visible bug class comes from that split: a link can be blocked by stale `SubmissionStore` state while `TaskStore`, Telegram, and Web do not own the actual execution decision.

## Scope

In scope:

- Add an authoritative runner for new links.
- Make new link de-duplication and retry decisions come from `TaskStore`.
- Keep using proven helper functions for 115 receive, CMS organize, CMS share-sync, STRM move, Emby confirmation, and cleanup.
- Make Telegram and Web read the authoritative task state.
- Convert Web retry from "record a retry event" into "enqueue the task for real execution".
- Keep `SubmissionStore` updates for compatibility and historical diagnostics.

Out of scope:

- Full migration of all historical `SubmissionStore` rows.
- Removing `SubmissionStore`.
- Replacing CMS, 115, or Emby API clients.
- Changing configured folder IDs or media-library mappings.
- Cancelling permanent 115 shares during cleanup.

## Architecture

Add a focused task runner layer beside the existing bridge:

- `TaskStore`: authoritative state and event log for new links.
- `SubmissionStore`: compatibility metadata backing existing helper functions.
- `TaskRunner`: single-owner worker loop that claims runnable tasks and advances one stage at a time.
- `TaskHandlers`: small stage functions that reuse existing `bridge.py` helpers instead of duplicating 115/CMS/Emby logic.
- Telegram/Web adapters: create tasks, show task state, and enqueue retry/reprocess actions.

`handle_update()` should stop launching a new `start_status_poll()` thread for new self-share links. It should parse links, create or find tasks in `TaskStore`, enqueue them, and return a visible response immediately. The runner owns long-running execution.

## Stage Model

Use explicit internal stages for the user-visible workflow. The implementation should add missing enum values instead of hiding organizing or recognition behind older generic stages.

| User stage | Internal stage | Meaning |
| --- | --- | --- |
| 接收 | `received` | Telegram accepted the link and, for self-share mode, received it into the configured pending 115 folder. |
| 整理 | `organizing` | CMS auto-organize has been triggered or is being waited on. |
| 识别 | `recognizing` | CMS recognition, TMDB extraction, parent-folder category mapping, and OpenAI fallback are being resolved. |
| 建分享 | `own_share_created` | The organized 115 folder has a permanent own-share link. |
| 生成 STRM | `share_sync_submitted` then `strm_ready` | CMS share115 sync has been submitted from our own share and STRM source has appeared. |
| 移动 | `moved` | The generated share-link STRM folder has moved or merged into the media library. |
| Emby | `emby_confirmed` | Emby sees the item and returns title, path, and parent/library label. |
| 清理 | `cleaned` | The 115 source file/folder has been deleted while the permanent own share remains. |

`organizing` and `recognizing` are required because they are the stages most likely to get stuck. They must be visible in both Telegram and Web.

## Task Data Requirements

`TaskStore` needs enough metadata to execute without relying on stale submission status:

- Link identity: `share_code`, `receive_code`, `url`.
- Execution status: current stage, status, retry count, next-run timestamp, claimed-by token, claim timestamp.
- Telegram context: chat ID for progress notifications.
- Compatibility pointer: `submission_id` after the row is created.
- Own share metadata: own share code, receive code, URL, file ID, file name.
- Move/Emby metadata: category, TMDB ID, source path, destination path, Emby item ID, Emby title, Emby parent/library.
- Error fields: type, summary, detail, recoverability.

The existing `tasks` table can be migrated additively. Do not rewrite historical rows destructively.

## Execution Flow

### 1. Receive Link

Telegram input creates or finds a task by `(share_code, receive_code)`.

- If no task exists, create it as `received/pending`.
- If task is running, return current stage and latest event.
- If task succeeded, return completed summary and media path.
- If task failed or needs action, show retry/reprocess buttons.

For self-share mode, the runner receives the external share into `SELF_SHARE_RECEIVE_CID`. This is part of the `received` stage, not a separate hidden poll thread.

### 2. Organize

The runner triggers CMS auto-organize using the existing `CmsClient.run_auto_organize()`.

It then searches 115 for the organized folder using existing folder-selection logic. This must respect excluded CMS folders and the task creation time to avoid selecting old unrelated folders.

If the folder is not found yet, the task remains in `organizing/running` with a next-run delay. It should not fail immediately unless the configured timeout is exceeded.

### 3. Recognize

Recognition order:

1. Use CMS/organized folder naming and TMDB markers.
2. Use parent-folder category mapping when CMS has already organized into a known category folder.
3. Use TMDB ID extraction from folder names like `[tmdb=123]`, `[tmdbid=123]`, or `{tmdb-123}`.
4. Call OpenAI only when CMS/category signals are missing or conflicting.
5. If still ambiguous, set `needs_action` and ask the user via Telegram buttons.

The runner must prefer CMS organization over LLM guesses. LLM output can suggest, but should not override an explicit CMS parent category or an exact TMDB-marked folder.

### 4. Create Own Share

Once the organized folder is identified, create a permanent own-share link through the existing 115 client.

This stage is idempotent:

- If `own_share_code` already exists, reuse it.
- Never cancel existing shares.
- Never proceed to cleanup unless `own_share_code` and `own_share_file_id` are both present.

### 5. Generate STRM

Submit CMS share115 sync from our own share only:

- `share_code = own_share_code`
- `receive_code = own_share_receive_code`
- `cid = SELF_SHARE_CMS_CID`
- `local_path = SELF_SHARE_CMS_LOCAL_PATH`

The runner waits for STRM source under `SELF_SHARE_STRM_ROOT`. It must validate that STRM files are share-link STRM, not direct `/d/` STRM, before allowing move.

### 6. Move

Move or merge the generated STRM folder into the mapped media library root.

Keep the existing quality guards:

- Block direct-link STRM.
- Block wrong own-share markers.
- Block TMDB mismatch between source folder, recognition, and destination.
- Do not overwrite unrelated destination folders.

On conflict, the task should stop at `moved/failed` or `needs_action` with a precise reason instead of silently reporting "already exists".

### 7. Emby

Refresh/confirm Emby using existing Emby helpers.

On success, store:

- Emby item ID.
- Emby title.
- Emby path.
- Emby parent/library label.

Telegram should report the media library label when available.

### 8. Cleanup

After own-share exists, STRM has moved, and Emby is confirmed, delete only the 115 source file/folder that was created by this task.

Cleanup must:

- Use the stored `own_share_file_id` or the specific organized source file ID.
- Never delete by broad title search alone.
- Never cancel the permanent share.
- Record `cleaned/succeeded` only after 115 delete succeeds.

## Retry and Reprocess Semantics

Retry current stage:

- Allowed for failed runnable stages.
- Reuses stored metadata when safe.
- Increments attempt count and appends a task event.

Force reprocess:

- Creates a new attempt for the same task or resets to `received` with an explicit event.
- Must not rely on stale `SubmissionStore.status`.
- Should keep historical events visible.

Duplicate link behavior:

- Succeeded task: show completed summary.
- Running task: show current stage and latest event.
- Failed task: show failed stage and retry buttons.
- Needs action: show the exact pending user choice.

## Web and Telegram UX

Telegram:

- On link submission, immediately reply with task ID and stage.
- Provide buttons for status, retry, force reprocess, and category selection when needed.
- `/status` and history should prefer `TaskStore` for new tasks.

Web:

- Task list shows stage, status, title, category, destination, and last error.
- Task detail shows chronological events.
- Retry POST enqueues real work, not only an event.
- Failed tasks clearly show which stage failed and why.

## Migration Strategy

Phase 1:

- Add runner and claim/enqueue fields to `TaskStore`.
- Route new self-share links through `TaskStore` runner.
- Keep old `SubmissionStore` execution available as a fallback behind configuration if needed.

Phase 2:

- Move TG/Web status commands to `TaskStore` first, with `SubmissionStore` fallback for older rows.
- Add lazy backfill for historical rows when a task detail page is opened.

Phase 3:

- Retire `start_status_poll()` for new tasks after production verification.
- Keep repair/audit tools compatible with both stores.

## Failure Handling

Every stage should convert exceptions into task state:

- Transient external errors: keep current stage as failed/retryable with detail.
- Missing organized folder before timeout: stay running with next-run timestamp.
- Recognition uncertainty: `needs_action`, not failed.
- Quality guard failure: failed or needs action with exact source/destination reason.
- Emby timeout: failed/retryable unless Emby is disabled.

No stage should fail silently. TG and Web must be able to show the last event for that task.

## Testing Strategy

Unit tests:

- Task claim/enqueue behavior.
- Duplicate link state decisions.
- Stage transition rules.
- Retry and force-reprocess rules.
- Recognition precedence: CMS/TMDB/category beats OpenAI guess.

Integration tests with fakes:

- New TG link creates a TaskStore task and does not call CMS ordinary share-down in self-share mode.
- Runner advances received -> organizing -> recognizing -> own share -> share sync.
- STRM move rejects direct-link STRM and wrong own-share STRM.
- Emby confirmation records parent/library label.
- Cleanup deletes source only after own share and move success.
- Duplicate running/succeeded/failed links return correct Telegram messages.

Regression tests:

- Existing `SubmissionStore` rows remain readable.
- Existing audit/doctor checks still run.
- Existing quality guards remain enforced.

## Success Criteria

- Sending a new 115 link creates one authoritative `TaskStore` task.
- Telegram responds immediately with task ID/current stage.
- Web and Telegram show the same current stage.
- New self-share flow does not call CMS ordinary share-down.
- A stuck task shows the exact stuck stage and latest reason.
- Retry from Web/TG actually re-enqueues work.
- Own-share STRM is generated and moved; direct-link STRM remains blocked.
- After successful Emby confirmation, cleanup deletes only the 115 source and preserves the permanent share.

## Rollback

Keep a feature flag for the authoritative runner during the first production rollout. If disabled, the app can fall back to the existing `SubmissionStore + start_status_poll()` path while preserving already-created `TaskStore` records for diagnostics.
