# Self-share Final-state Guard Design

## Goal

Keep completed self-share tasks stable after CMS ordinary sync events. A completed task must end with media-library STRM files generated from the user's own permanent 115 share link, not CMS direct-link STRM.

## Scope

This is a small stability guard, not a performance refactor.

In scope:
- Inspect local TaskStore/SubmissionStore state and local STRM files only.
- Guard completed `self_share_sync` submissions with `move_status=moved`, `emby_status=confirmed`, and `cleanup_status=deleted` or equivalent final state.
- Remove duplicate direct-link `.strm` files that match the same TMDB identity as a completed self-share task.
- Restore the expected self-share media folder if CMS ordinary sync deletes it after source cleanup.
- Re-submit CMS share sync once when the share STRM source folder is missing.

Out of scope:
- No broad 115 scanning.
- No high-concurrency changes.
- No database replacement or architecture rewrite.
- No deletion of images, NFO, subtitles, or media metadata unless they are inside a replaced self-share source folder already handled by the existing move/merge logic.

## Current Problem

After the app creates a permanent 115 share, generates share STRM, moves it into the media library, confirms Emby, and deletes the 115 transfer source, CMS ordinary incremental sync may receive a delete/create event. That can delete the just-restored self-share media folder and recreate a CMS direct-link STRM folder for the same media.

The observed symptom is a task that looks completed while the media library contains a direct `/d/` STRM or a duplicate direct STRM folder for the same TMDB ID.

## Design

Add a local final-state guard to the existing maintenance path:

1. Select recent completed self-share submissions.
2. Resolve the task identity from `recognition_json`, `own_share_file_name`, `dest_path`, or related row fields.
3. Check the configured media-library roots for `.strm` files under folders with the same TMDB ID.
4. If a matching STRM contains `/d/`, delete only that `.strm` file.
5. If the expected destination folder is missing but the self-share source folder exists under `SELF_SHARE_STRM_ROOT`, move/merge it back into the media library.
6. If both destination and self-share source are missing, submit CMS share sync once and wait for the next maintenance pass.

The guard must be conservative: if TMDB identity is absent or ambiguous, do not scan broadly and do not delete.

## Error Handling

- File read/delete errors are ignored per file so one bad path does not stop maintenance.
- CMS share-sync submission errors bubble to the existing maintenance loop logging.
- The guard does not mark a task failed; it only repairs local STRM state or schedules regeneration.

## Testing

Add regression tests for:
- A completed self-share task whose expected destination was deleted and share STRM source is available: it restores share STRM.
- A completed self-share task with a duplicate same-TMDB direct STRM folder: it removes only the direct `.strm` and keeps the share STRM.
- A destination that already exists with share STRM plus a duplicate direct STRM: it removes the duplicate direct STRM without moving anything.

## Deployment

Use the established Unraid-safe path:
- Run the full local unittest suite.
- Rsync code to `/mnt/user/appdata/cms-tg-ingest` excluding `.env`, data, backups, and compose overrides.
- Rebuild/recreate `cms-tg-ingest`.
- Verify container health, Python compile, and a local STRM direct/share count for the affected task.
