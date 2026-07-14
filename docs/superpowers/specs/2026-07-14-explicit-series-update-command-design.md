# Explicit Series Update Command Design

## Goal

Allow the authorized Telegram user to send `追更 <115 share URL>` and trigger the existing self-share update workflow without locating the historical task button.

## Command Behavior

- Parse the text only when it starts with the Chinese `追更` prefix followed by one or more valid 115 share URLs.
- For each URL, locate the TaskStore task by normalized share code and receive code.
- When the matched task is a completed self-share series (`国产电视`, `外国电视`, or `番剧`), start the same update flow used by the existing `追更 #任务号` button.
- When no eligible completed series task exists, pass the URL to the ordinary intake flow. Existing duplicate, active-task, and failed-task handling remains unchanged.
- The command does not create a background poller and does not add any 115 API call before the normal task runner receives the task.

## Implementation

- Extract the existing `task_update` callback body into one reusable helper that accepts a task ID and a command source label.
- The Telegram callback and the text command both call that helper, preserving the same checks, metadata reset, queue transition, and status messages.
- Keep authorization at the existing `handle_update` boundary.

## Error Handling

- Invalid or missing URLs after the `追更` prefix return the normal help-free no-op behavior.
- Non-series and unfinished matching tasks do not force an update; they fall through to ordinary intake behavior.
- A matching completed series whose submission record is missing reports the existing update-preparation error and does not enqueue work.

## Tests

- `追更 <URL>` queues a completed foreign-TV self-share task at `received` and increments its update run metadata.
- `追更 <URL>` with no historical task performs ordinary intake.
- A completed movie using `追更 <URL>` follows ordinary intake rather than resetting the movie task.
- The existing callback test continues to cover the button path.
