# Explicit New-Link Series Update Design

## Goal

Make a new 115 share link update a specific completed series without guessing the
historical title or silently treating the link as a new movie intake. Repair
production task `#338` as an update of task `#328` while leaving task `#341` and
its 115 folder/share untouched.

## Root Cause

The existing `追更 <URL>` command only finds history by the URL's normalized
share code and receive code. A new URL cannot match the completed series task,
so the command falls through to ordinary intake. Task `#338` therefore lost the
stable identity of task `#328` (`tmdb_id=273114`, television, `国产电视`) and
selected a folder already used by task `#341` (`tmdb_id=9533`). The subsequent
share recovery correctly refused to reuse an older share, leaving `#338` in an
unbounded pending loop.

## Command Contract

The explicit command for a new link is:

```text
追更 #<completed-task-id> <115-share-url>
```

For example, repairing the affected series uses task `#328` as the target. The
target task must:

- exist and be unclaimed;
- be `cleaned/succeeded`;
- be a self-share television task in `国产电视`, `外国电视`, or `番剧`;
- have a persisted submission, an explicit TMDB ID, and television recognition.

`追更 <URL>` remains supported when the URL exactly matches an eligible
completed series task. When it does not match, it returns an instruction to add
the historical task ID and does not create an ordinary intake task.

Movie, unfinished, missing, ambiguous, or invalid targets are rejected without
mutating either task. Multiple URLs with one target are not supported in this
change; the user submits one explicit target and one URL per command.

## Update Child Model

A new share URL keeps its own TaskStore task and SubmissionStore row. It becomes
an update child of the completed target instead of replacing the target's share
key or reusing the target's submission row.

The child inherits only stable media identity:

- TMDB ID;
- media type;
- category;
- normalized recognition and title identity;
- organized parent/category identity needed by the existing workflow.

It does not inherit runtime output such as organized folder IDs, self-share
codes, aliases, STRM paths, Emby state, cleanup state, defer counters, or claims.
The task records `series_update_parent_task_id`,
`series_update_parent_submission_id`, `update_requested_run`,
`update_received_run`, and `update_started_at` for audit and receive-stage
behavior.

This preserves the completed target as history while allowing the normal
receive, organize, share, STRM, Emby, and cleanup stages to process the new
source link under the target series identity.

## State Transition And Concurrency

Preparation uses compare-and-set checks against the source task's stage,
status, claim, and `updated_at` value. A claimed source is rejected. An existing
source already linked to a different parent is rejected.

TaskStore and SubmissionStore are separate SQLite databases, so preparation is
ordered to avoid a runner observing half-prepared state:

1. Freeze the source task at `received/pending` with `next_run_at=-1`, clear its
   attempt-local metadata, and record the parent relation.
2. Reset or create the source submission and copy the validated stable identity.
3. Enqueue the source task with `next_run_at=0` only after submission preparation
   succeeds.
4. If submission preparation fails, leave the source unscheduled and record
   `needs_action`; do not let the runner continue with partial identity.

The transition is idempotent for the same source/parent pair. It does not delete
115 content or shares.

## Cross-Task Folder Guard

Before persisting an organized folder candidate, the workflow checks TaskStore
for other tasks that already record the same `own_share_file_id`.

- An owner with the same explicit TMDB ID is allowed, which supports legitimate
  updates of one series.
- An owner with a different explicit TMDB ID causes `needs_action` before share
  creation.
- If either side lacks enough identity to prove equality, reuse is rejected as
  ambiguous.

This guard catches the `#338`/`#341` collision independently of command parsing
and prevents the wrong folder from reaching share creation.

## Production Repair

After tests and patch release publication:

1. Back up `/data/tasks.db` and `/data/submissions.db` with timestamped names.
2. Deploy the pinned patch image to Unraid and verify container, doctor, API
   health, runner heartbeat, and logs.
3. Re-read tasks `#328`, `#338`, and `#341` and require `#338` to be unclaimed.
4. Prepare existing source task `#338` as an update child of `#328` through the
   same guarded helper used by the command.
5. Verify that `#338` has TMDB `273114`, television recognition, category
   `国产电视`, and no reference to file ID `3481694900213253783` before it runs.
6. Monitor the task through the correct TMDB-tagged series folder to a terminal
   result. Confirm task `#341` and its existing 115 share were not changed.

No database row, media folder, STRM tree, or 115 share is manually deleted by
this repair.

## Tests

Automated coverage will prove:

- `追更 #<id> <new URL>` creates or reuses a child with the target's stable TV
  identity and queues it from `received`;
- unmatched `追更 <new URL>` returns guidance without creating a task;
- exact-link update behavior remains compatible;
- missing, movie, unfinished, non-self-share, or identity-less targets are
  rejected;
- claimed and concurrently changed source tasks are not overwritten;
- one source cannot be linked to two different parents;
- attempt-local state is removed while stable target identity is retained;
- a folder owned by a different TMDB task becomes `needs_action` before share
  creation;
- a folder owned by the same explicit TMDB remains valid for a series update.

Full Python tests, compilation, whitespace checks, release workflow checks, and
post-deployment health checks remain required.

## Alternatives Rejected

Automatic title or TMDB matching after intake remains unsafe because it starts
processing before the update target is known and ambiguous titles can still
cross-link media. A one-off database repair would recover `#338` but leave the
same command behavior available for future failures. Neither approach meets the
prevention requirement.

## Out Of Scope

- Automatically guessing a historical task from a new link.
- Deleting the wrong task's 115 folder or existing share.
- General redesign of share-creation polling or pending-time UI.
- Supporting one command that maps several new URLs to one target.
