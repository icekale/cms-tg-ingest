# Unified Web Task Display Title Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every task-bearing Web UI surface show the organized 115 folder name when available, while preserving the existing API `title` field and all task data.

**Architecture:** Centralize the existing legacy display-title precedence in `app/web_api.py`, expose the derived value as `display_title`, and import the helper from `app/web.py`. Vue views use a small compatibility helper that prefers `display_title` and falls back to `title`, so old API responses remain renderable.

**Tech Stack:** Python 3.12 standard library, SQLite-backed `TaskStore`, Python `unittest`, Vue 3, Naive UI, Node.js built-in test runner, Vite.

---

## File Map

- Modify: `app/web_api.py` — shared display-title helper; `display_title` in task, health, and quality payloads.
- Modify: `app/web.py` — import the shared helper and remove the duplicate legacy implementation.
- Modify: `tests/test_web_api.py` — folder-name precedence, fallback, and quality payload tests.
- Modify: `frontend/src/taskView.js` — compatibility display-title selector.
- Modify: `frontend/src/views/Overview.vue` — queue title.
- Modify: `frontend/src/views/Tasks.vue` — task table title.
- Modify: `frontend/src/views/TaskDetail.vue` — detail heading.
- Modify: `frontend/src/views/Quality.vue` — quality issue title.
- Modify: `frontend/src/views/Health.vue` — latest-problem title.
- Modify: `frontend/test/taskView.test.js` — selector tests.

No database schema, migration, Dockerfile, Compose, Telegram, 115 client, or
production data changes are part of this implementation.

### Task 1: Add failing API regression tests

**Files:**
- Modify: `tests/test_web_api.py` near the existing serializer tests.

- [ ] **Step 1: Write the failing folder-name and fallback tests**

Add these methods to `WebApiTests`:

```python
    def test_serialize_task_exposes_organized_folder_as_display_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task(
                "folder-display",
                "",
                "https://115cdn.com/s/folder-display?password=secret",
            )
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                title="https://115cdn.com/s/folder-display?password=secret",
                metadata_patch={
                    "organized_folder": {
                        "file_name": "H-黑金-2011-[tmdb=77221]",
                    },
                },
            )

            payload = serialize_task(store.find_task(task.id))

        self.assertEqual(payload["title"], "https://115cdn.com/s/folder-display?password=secret")
        self.assertEqual(payload["display_title"], "H-黑金-2011-[tmdb=77221]")

    def test_serialize_task_display_title_falls_back_without_folder_metadata(self):
        task = type(
            "Task",
            (),
            {
                "id": 2,
                "title": "原始电影标题",
                "share_code": "fallback-share",
                "source_type": "share",
                "current_stage": TaskStage.RECEIVED,
                "status": TaskStatus.PENDING,
                "strm_mode": "shared",
                "category": "",
                "tmdb_id": "",
                "url": "https://115cdn.com/s/fallback-share",
                "error_type": "",
                "error_summary": "",
                "retry_count": 0,
                "next_run_at": 0,
                "claimed_by": "",
                "metadata": {},
                "created_at": 0,
                "updated_at": 0,
            },
        )()

        payload = serialize_task(task)

        self.assertEqual(payload["display_title"], "原始电影标题")
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run:

```bash
python3 -m unittest tests.test_web_api.WebApiTests.test_serialize_task_exposes_organized_folder_as_display_title tests.test_web_api.WebApiTests.test_serialize_task_display_title_falls_back_without_folder_metadata -v
```

Expected: FAIL with `KeyError: 'display_title'`, proving the tests cover the missing API field rather than an unrelated error.

### Task 2: Centralize the backend display title and expose it in API payloads

**Files:**
- Modify: `app/web_api.py` near `_enum_value()` and `serialize_task()`.
- Modify: `app/web.py` import block and the existing `task_display_title()` definition.
- Modify: `tests/test_web_api.py` quality API test.

- [ ] **Step 1: Move the existing helper to `app/web_api.py`**

Add this helper after `_enum_value()` in `app/web_api.py`; it is the existing
legacy precedence with no filesystem or network access:

```python
def task_display_title(task: Any) -> str:
    metadata = getattr(task, "metadata", {}) or {}
    organized = metadata.get("organized_folder")
    if isinstance(organized, dict):
        folder_name = str(organized.get("file_name") or "").strip()
        if folder_name:
            return folder_name
    for key in ("own_share_file_name", "dest_path", "source_path", "emby_path"):
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue
        if key.endswith("_path"):
            name = Path(value).name
            if name:
                return name
        return value
    title = str(getattr(task, "title", "") or "").strip()
    if title and not title.startswith(("http://", "https://")):
        return title
    return str(getattr(task, "share_code", "") or title or "-")
```

Import `task_display_title` from `.web_api` in `app/web.py` and delete the
duplicate function there. Do not change the legacy call sites.

- [ ] **Step 2: Add `display_title` without changing `title`**

In `serialize_task()`, preserve the current title expression and add the
derived field:

```python
"title": task.title or task.share_code,
"display_title": task_display_title(task),
```

In `quality_items()`, compute the same value for a found task and expose it
next to the existing issue `title`:

```python
display_title = task_display_title(task) if task is not None else issue.title
```

Include both fields as follows:

```python
"title": issue.title or (task.title if task is not None else ""),
"display_title": display_title,
```

- [ ] **Step 3: Extend the quality API regression assertion**

In `test_quality_api_exposes_rule_state_and_aggregates`, add this metadata:

```python
"organized_folder": {"file_name": "Q-质量 API 任务-2026-[tmdb=123]"},
```

Then assert that the returned row preserves the old title and exposes the
folder display title:

```python
self.assertEqual(item["display_title"], "Q-质量 API 任务-2026-[tmdb=123]")
```

- [ ] **Step 4: Run the focused Python tests**

Run:

```bash
python3 -m unittest tests.test_web_api -v
```

Expected: all `test_web_api` tests pass, including the serializer tests and the
quality display-title assertion.

- [ ] **Step 5: Commit the backend change**

```bash
git add app/web_api.py app/web.py tests/test_web_api.py
git commit -m "feat: expose organized folder names in web task APIs"
```

### Task 3: Add the Vue display-title compatibility helper with a failing test

**Files:**
- Modify: `frontend/test/taskView.test.js`.
- Modify: `frontend/src/taskView.js`.

- [ ] **Step 1: Write the failing frontend tests**

Extend the import and add this test:

```javascript
import { displayTaskTitle, taskLifecycleState, taskStatusLabel } from '../src/taskView.js'

test('prefers backend display title and falls back to legacy title', () => {
  assert.equal(
    displayTaskTitle({ id: 328, display_title: 'H-后天-2024-[tmdb=435]', title: 'swso9jn3wul' }),
    'H-后天-2024-[tmdb=435]',
  )
  assert.equal(displayTaskTitle({ id: 329, title: '旧版任务标题' }), '旧版任务标题')
  assert.equal(displayTaskTitle({ id: 330 }), '任务 #330')
})
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
cd frontend && node --test test/taskView.test.js
```

Expected: FAIL because `displayTaskTitle` is not exported yet.

- [ ] **Step 3: Implement the minimal helper**

Add to `frontend/src/taskView.js`:

```javascript
export function displayTaskTitle(task = {}) {
  const displayTitle = typeof task.display_title === 'string' ? task.display_title.trim() : ''
  if (displayTitle) return displayTitle
  const title = typeof task.title === 'string' ? task.title.trim() : ''
  if (title) return title
  const id = task.id ?? task.task_id
  return id === undefined || id === null ? '-' : `任务 #${id}`
}
```

- [ ] **Step 4: Run the focused frontend test**

Run:

```bash
cd frontend && node --test test/taskView.test.js
```

Expected: all task-view tests pass.

- [ ] **Step 5: Commit the helper and test**

```bash
git add frontend/src/taskView.js frontend/test/taskView.test.js
git commit -m "feat: add compatible web task title selector"
```

### Task 4: Use the derived title on every task-bearing Vue surface

**Files:**
- Modify: `frontend/src/views/Overview.vue`.
- Modify: `frontend/src/views/Tasks.vue`.
- Modify: `frontend/src/views/TaskDetail.vue`.
- Modify: `frontend/src/views/Quality.vue`.
- Modify: `frontend/src/views/Health.vue`.

- [ ] **Step 1: Import the helper in each view**

Add this existing-module import to each `<script setup>` block:

```javascript
import { displayTaskTitle } from '../taskView'
```

- [ ] **Step 2: Replace each task title rendering site**

Use these exact replacements:

```vue
<!-- Overview.vue -->
#{{ task.id }} {{ displayTaskTitle(task) }}
```

```javascript
// Tasks.vue columns

default: () => `#${row.id} ${displayTaskTitle(row)}`
```

```vue
<!-- TaskDetail.vue -->
<h1>{{ displayTaskTitle(task) }}</h1>
```

```javascript
// Quality.vue taskCell
const title = displayTaskTitle(row)
```

```vue
<!-- Health.vue latest problem -->
#{{ health.latest_problem.id }} {{ displayTaskTitle(health.latest_problem) }}
```

Keep all links, task IDs, actions, status labels, and existing `title` fields
unchanged. Only the visible name changes.

- [ ] **Step 3: Run frontend tests and build**

Run:

```bash
cd frontend && npm test && npm run build
```

Expected: all frontend tests pass and Vite writes `frontend/dist` without
errors.

- [ ] **Step 4: Commit the Vue integration**

```bash
git add frontend/src/views/Overview.vue frontend/src/views/Tasks.vue frontend/src/views/TaskDetail.vue frontend/src/views/Quality.vue frontend/src/views/Health.vue
git commit -m "feat: show organized folder names across web UI"
```

### Task 5: Run the complete verification gate

**Files:**
- No source changes; verify the committed implementation.

- [ ] **Step 1: Check the repository and compile Python**

Run:

```bash
git status --short --branch
python3 -m compileall -q app bridge.py doctor.py
git diff --check
```

Expected: clean working tree apart from no generated tracked changes, no
compile errors, and no whitespace errors.

- [ ] **Step 2: Run the complete Python suite**

Run:

```bash
python3 -m unittest discover -s tests -p 'test*.py' -q
```

Expected: the full suite ends with `OK`.

- [ ] **Step 3: Run the complete frontend suite and production build**

Run:

```bash
cd frontend && npm test && npm run build
```

Expected: all Node tests pass and the production build succeeds.

- [ ] **Step 4: Verify the API contract locally**

Run the focused API tests again:

```bash
python3 -m unittest tests.test_web_api.WebApiTests.test_serialize_task_exposes_organized_folder_as_display_title tests.test_web_api.WebApiTests.test_serialize_task_display_title_falls_back_without_folder_metadata -v
```

Expected: both tests pass, with `title` unchanged and `display_title` equal to
the organized folder name when metadata is present.

- [ ] **Step 5: Keep release operations separate**

The implementation commits from Tasks 2-4 are the final code commits. Do not
create an empty release commit, bump the version, publish Docker Hub, or deploy
Unraid as part of this feature unless the user separately requests release
operations.
