# Web Task Display Folder Name Design

## Goal

Make every new Web UI task surface show the organized 115 folder name when it
is available. The current legacy UI already derives a display name from task
metadata, but the Vue UI serializes and renders the original share title, so
pages such as `/app/tasks/328` show a share code or URL instead of the
organized folder name.

## Scope

Apply the same display rule to:

- the Vue task list;
- the Vue overview queue;
- the Vue task detail page;
- the Vue quality queue;
- the Vue health page's latest-problem link;
- the API payloads consumed by those pages;
- the existing legacy UI through the shared helper.

No database migration, 115 request, task state change, file move, or metadata
rewrite is required.

## Display Contract

Keep the existing `title` API field unchanged for compatibility. Add a
`display_title` field to task and quality payloads. The value uses this order:

1. `metadata.organized_folder.file_name`;
2. `metadata.own_share_file_name`;
3. the basename of `metadata.dest_path`, `source_path`, or `emby_path`;
4. the existing task title when it is not an HTTP URL;
5. the task share code or a `-` fallback.

The helper is shared by `app/web_api.py` and `app/web.py`, so the legacy and
Vue UIs cannot drift on title selection. Sensitive metadata continues to pass
through the existing redaction layer; `display_title` is only a derived
display string.

## Data Flow

`serialize_task()` computes `display_title` from the in-memory
`TaskSnapshot`. `api_tasks()`, `api_task_detail()`, and health payloads inherit
the field through that serializer. `quality_items()` computes the same field
for the task associated with each issue, while preserving its existing
`title` field.

The Vue views render `display_title || title`, which keeps compatibility with
older API responses and with tasks that have no organized folder yet. This
includes the latest-problem task link on the health page.

## Error Handling

Missing, empty, malformed, or non-dict organized-folder metadata falls through
to the next candidate. Path parsing remains local and does not access the
filesystem. Existing URL and sensitive-value redaction remains unchanged.

## Tests

Add API regression coverage for:

- an organized folder name taking precedence over a URL task title;
- fallback to the original title when no folder metadata exists;
- quality rows exposing the same display title.

Add or update frontend tests to assert that all task-bearing Vue surfaces
prefer `display_title`. Run the focused tests first, then the complete Python
and frontend test suites, compilation, build, and `git diff --check`.

## Alternatives Rejected

Changing `title` itself would be simpler for the Vue code, but it would change
the meaning of an existing API field and could surprise external consumers.

Deriving the folder name independently in each Vue view would duplicate the
metadata precedence rules and recreate the legacy/new UI divergence.

Persisting the folder name into the task record would add unnecessary writes
and migration concerns for a value that is already available in metadata.

## Out Of Scope

- renaming 115 folders or local media directories;
- changing CMS recognition, organization, or STRM behavior;
- changing Telegram messages;
- changing task identity or stored task titles.
