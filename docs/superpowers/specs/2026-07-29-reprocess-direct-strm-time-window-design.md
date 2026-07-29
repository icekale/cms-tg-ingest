# Reprocess Direct STRM Time Window Design

## Goal

Prevent a reprocessed series-update task from adopting a stale, unrelated
direct STRM directory that predates the current update run, while preserving
the existing behavior for ordinary intake tasks and exact TMDB folders.

The production target is task `#338`, which is an update child of task `#328`
with TMDB `273114`, type `tv`, and category `国产电视`.

## Root Cause

Task `#338` reused an existing SubmissionStore row. At the start of the repair,
that row was about 61 hours old. `find_recent_direct_library_strm_source_dir()`
derived its scan lower bound from the row's original `created_at`, so an
unrelated `欧美电影` direct STRM from about 53.5 hours earlier remained eligible.

At the first organizing attempt, that stale directory was the only eligible
candidate. It had neither an exact TMDB marker nor a unique title-token match,
but the existing single-candidate compatibility fallback selected it. The
submission was therefore changed to `cms_direct_strm_resolved` with category
`欧美电影` while retaining the canonical task TMDB `273114`.

The cross-task folder-owner guard then detected a different TMDB owner and
stopped the task at `organizing/needs_action` before any self-share was created.
The correct `国产电视` direct STRM with exact TMDB `273114` appeared later, after
the task had already stopped.

## Baseline

Implementation starts from local `main` commit `b12a4ef`, which combines:

- released series-update protection through `v0.2.44`;
- runtime CMS mutation journaling;
- complete background credential redaction;
- ambiguous same-title 115 share recovery protection.

The isolated branch is `fix/reprocess-direct-strm-time-window`. Its baseline
passes 1,132 Python tests with `ResourceWarning` promoted to an error.

## Finder Contract

Extend the direct STRM finder with one optional keyword-only argument:

```python
def find_recent_direct_library_strm_source_dir(
    config: MoveConfig,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str = "",
    *,
    min_update_time: float = 0,
) -> tuple[Path, str] | None:
```

The existing row-derived lower bound remains unchanged. When
`min_update_time > 0`, the effective lower bound becomes the later of:

- the existing submission-based lower bound; and
- `min_update_time`.

Invalid or missing values normalize to zero. Callers that omit the argument
retain current behavior.

The existing exact-TMDB exception remains intact: a media root whose folder
name contains the expected TMDB ID may be reused even when its mtime predates
the current run. This is required for a new episode entering an established
series directory.

## Workflow Integration

`BridgeSelfShareTaskWorkflow._stage_organizing()` already reads
`update_started_at` and `reprocess_started_at` to constrain 115 folder scans.
It will derive a separate direct STRM cutoff:

```text
max(update_started_at - 5, reprocess_started_at - 5)
```

The five-second tolerance matches the existing organized-folder scan behavior.
The cutoff is passed to both direct STRM finder calls in the organizing stage:

- the CMS cloud-index-assisted path; and
- the normal path used when no organized folder is known yet.

Ordinary intake tasks have neither timestamp, pass zero, and preserve the
current 60-second submission tolerance and single-candidate compatibility
fallback.

## Resulting Behavior

For a reprocessed task:

1. A stale non-matching direct STRM from before the current run is ignored.
2. If no safe candidate exists yet, the task continues with the existing
   `等待 CMS 整理完成` defer behavior.
3. A newly generated direct STRM after the cutoff is eligible.
4. An established exact-TMDB series folder remains eligible regardless of its
   earlier mtime.
5. Existing TMDB mismatch and cross-task folder-owner guards still run before
   folder persistence or share creation.

No submission creation timestamp is rewritten. No STRM, media folder, 115
folder, database row, or share is deleted by this change.

## Error Handling

The new argument is advisory filtering state, not a new failure mode. A malformed
value is treated as zero so existing callers remain compatible. Filesystem read
errors continue to skip the affected candidate. An empty result continues to
defer rather than fail or force a retry.

The change does not weaken `needs_action` handling for ambiguous folder owners,
unknown CMS mutation outcomes, or ambiguous same-title 115 share recovery.

## Tests

Automated coverage will prove:

- the finder still returns an existing sole candidate when no explicit cutoff
  is supplied;
- the finder ignores a stale sole non-matching candidate when
  `min_update_time` is newer;
- an exact-TMDB series directory remains eligible across the cutoff;
- the organizing stage passes its update/reprocess cutoff to the finder;
- a reused old submission is not rewritten from `国产电视` to `欧美电影` by a
  stale direct STRM;
- ordinary organizing behavior remains compatible.

Focused finder and bridge workflow tests run first. The final gate includes the
complete Python suite, compilation, whitespace checks, release tests, frontend
tests, and frontend production build.

## Production Recovery

After publishing a pinned `0.2.45` image that includes both the runtime fixes
and this correction:

1. Back up the Unraid Compose file, `.env`, `tasks.db`, and `submissions.db`.
2. Verify both SQLite backups with `PRAGMA quick_check`.
3. Deploy only the pinned image tag and verify container health, doctor, the
   filtered API health response, runner heartbeat, and logs.
4. Stop the service and invoke `start_series_update_from_link()` for `#338` and
   parent `#328`; do not edit SQLite manually.
5. Verify canonical TMDB/type/category, no self-share code, no foreign folder,
   and no active claim before restarting.
6. Monitor without manual wakeups until a safe terminal result.
7. Confirm task `#341`, its folder fingerprint, and its share state remain
   unchanged.

The already existing exact-TMDB `国产电视` STRM may be reused after reprocessing,
because exact-TMDB folders intentionally bypass the non-matching stale cutoff.

## Alternatives Rejected

Resetting `submissions.created_at` would make the current heuristic appear
fresh but would destroy the row's audit meaning and could change unrelated
timeout behavior.

Removing the sole-candidate fallback whenever a TMDB ID is known would be
stricter, but it would also change legacy workflows whose CMS directories lack
TMDB markers and title-token matches. The approved cutoff is narrower and
targets only explicit update/reprocess runs.

## Out Of Scope

- Reclassifying or deleting existing stale STRM directories.
- Automatically retrying every existing `needs_action` task.
- Changing ordinary direct or source-share CMS submission behavior.
- Weakening folder-owner, share-recovery, or remote-operation safety guards.
- Redesigning CMS organization timing or 115 folder discovery.
