# Intake Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind organize dest and cleanup targets from a per-task file-identity snapshot, not from title/TMDB search.

**Architecture:** Pure helpers live in `app/media/intake_identity.py`. Receive writes `intake_identity.root_ids` + `files`. Organizing locates each video fid and writes `dest_id`. Cleanup deletes only `root_ids` whose current parent is a source/inbox/redundant cid. `self_share.py` wires the helpers; it does not keep title-based ownership.

**Tech Stack:** Python 3, existing `P115WebClient` / `p115_item_id` / `p115_item_parent_id`, unittest.

**Spec:** `docs/superpowers/specs/2026-08-22-intake-identity-design.md`

**Files:**
- Create: `app/media/intake_identity.py`, `tests/test_intake_identity.py`
- Modify: `app/workflows/self_share.py` (receive snapshot, organize bind, cleanup)
- Modify: `app/task_store.py` (`REPROCESS_METADATA_DELETE_KEYS` adds `intake_identity`)
- Modify: `tests/test_bridge_task_engine.py` (FakeP115.search_files + three 115 graphs)
- Modify: `CHANGELOG.md`

Do not replace CMS auto_organize, split the stage machine, add workers, or hand-edit production #402.

---

### Task 1: Video / season predicates and file snapshot

**Files:**
- Create: `tests/test_intake_identity.py`
- Create: `app/media/intake_identity.py`

- [ ] **Step 1: Write the failing tests**

```python
import unittest

from app.media.intake_identity import is_season_folder_name, is_video_name, snapshot_files


class IntakeIdentitySnapshotTests(unittest.TestCase):
    def test_video_suffix_and_season_names(self):
        self.assertTrue(is_video_name("拆弹专家.2017.mkv"))
        self.assertFalse(is_video_name("拆弹专家.2017.chs.ass"))
        self.assertTrue(is_season_folder_name("Season 03"))
        self.assertTrue(is_season_folder_name("第3季"))
        self.assertFalse(is_season_folder_name("C-拆弹专家-2017-[tmdb=441531]"))

    def test_snapshot_lists_videos_two_levels_for_season_roots(self):
        listed = {
            "recv-folder": [
                {"fid": "share-should-ignore", "n": "poster.jpg"},
                {"cid": "season-1", "n": "Season 1", "pid": "recv-folder"},
                {"fid": "video-root", "n": "Extra.mkv", "cid": "recv-folder"},
            ],
            "season-1": [
                {"fid": "ep1", "n": "Show.S01E01.mkv", "cid": "season-1"},
                {"fid": "sub1", "n": "Show.S01E01.ass", "cid": "season-1"},
            ],
        }

        def list_files(parent_id, limit=500):
            return list(listed.get(str(parent_id), []))

        files = snapshot_files(
            [
                {"file_id": "recv-folder", "file_name": "Show", "is_folder": True},
            ],
            list_files,
        )
        self.assertEqual(
            {(item["id"], item["name"]) for item in files},
            {("video-root", "Extra.mkv"), ("ep1", "Show.S01E01.mkv")},
        )

    def test_snapshot_single_video_root(self):
        files = snapshot_files(
            [{"file_id": "lone-mkv", "file_name": "Movie.mkv", "is_folder": False}],
            lambda *_args, **_kwargs: [],
        )
        self.assertEqual(files, [{"id": "lone-mkv", "name": "Movie.mkv"}])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_intake_identity.IntakeIdentitySnapshotTests -q`

Expected: FAIL with `ModuleNotFoundError: app.media.intake_identity`

- [ ] **Step 3: Write minimal implementation**

In `app/media/intake_identity.py`:

```python
from __future__ import annotations

import re
from typing import Any, Callable

from app.clients.p115 import p115_file_name, p115_is_folder, p115_item_id, p115_item_parent_id

VIDEO_SUFFIXES = (".mkv", ".mp4", ".ts", ".iso", ".avi", ".mov", ".wmv", ".m2ts")
_SEASON_NAME = re.compile(r"(?i)^(season\s*\d+|第.+季)$")

ListFiles = Callable[..., list[dict[str, Any]]]


def is_video_name(name: str) -> bool:
    return str(name or "").strip().lower().endswith(VIDEO_SUFFIXES)


def is_season_folder_name(name: str) -> bool:
    return bool(_SEASON_NAME.match(str(name or "").strip()))


def snapshot_files(roots: list[dict[str, Any]], list_files: ListFiles) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(file_id: str, name: str) -> None:
        file_id = str(file_id or "").strip()
        name = str(name or "").strip()
        if not file_id or not is_video_name(name) or file_id in seen:
            return
        seen.add(file_id)
        files.append({"id": file_id, "name": name})

    for root in roots:
        file_id = str(root.get("file_id") or "").strip()
        name = str(root.get("file_name") or "").strip()
        if not file_id:
            continue
        if not root.get("is_folder"):
            add(file_id, name)
            continue
        try:
            children = list_files(file_id, limit=500)
        except Exception:
            continue
        for item in children:
            child_id = p115_item_id(item)
            child_name = p115_file_name(item)
            if p115_is_folder(item) and is_season_folder_name(child_name):
                try:
                    episodes = list_files(child_id, limit=500)
                except Exception:
                    continue
                for episode in episodes:
                    add(p115_item_id(episode), p115_file_name(episode))
                continue
            add(child_id, child_name)
    return files
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_intake_identity.IntakeIdentitySnapshotTests -q`

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add tests/test_intake_identity.py app/media/intake_identity.py
git commit -m "feat: snapshot intake video file ids"
```

---

### Task 2: Resolve dest from file search hits

**Files:**
- Modify: `tests/test_intake_identity.py`
- Modify: `app/media/intake_identity.py`

- [ ] **Step 1: Write the failing tests**

```python
from app.media.intake_identity import dest_id_from_file_hits


class IntakeIdentityDestTests(unittest.TestCase):
    def test_movie_parent_is_dest(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-c-441531", "n": "C-拆弹专家-2017-[tmdb=441531]", "pid": "movie-parent"},
                {"cid": "recv-folder-402", "n": "拆弹专家 (2017) [tmdb=441531]", "pid": "redundant-cid"},
            ],
            expected_ids=["video-mkv-402"],
        )
        self.assertEqual(dest, "dest-c-441531")

    def test_season_parent_walks_up_to_show_root(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
            ],
            folder_hits=[
                {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
                {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
            ],
            expected_ids=["ep-s3-e1"],
        )
        self.assertEqual(dest, "dest-108978")

    def test_missing_file_is_incomplete(self):
        dest = dest_id_from_file_hits(
            file_hits=[],
            folder_hits=[],
            expected_ids=["video-mkv-402"],
        )
        self.assertEqual(dest, "incomplete")

    def test_two_library_roots_conflict(self):
        dest = dest_id_from_file_hits(
            file_hits=[
                {"fid": "ep-a", "cid": "dest-a", "n": "A.mkv"},
                {"fid": "ep-b", "cid": "dest-b", "n": "B.mkv"},
            ],
            folder_hits=[
                {"cid": "dest-a", "n": "Show A", "pid": "tv-parent"},
                {"cid": "dest-b", "n": "Show B", "pid": "tv-parent"},
            ],
            expected_ids=["ep-a", "ep-b"],
        )
        self.assertEqual(dest, "conflict")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_intake_identity.IntakeIdentityDestTests -q`

Expected: FAIL with `dest_id_from_file_hits` missing

- [ ] **Step 3: Write minimal implementation**

Append to `app/media/intake_identity.py`:

```python
INCOMPLETE = "incomplete"
CONFLICT = "conflict"


def dest_id_from_file_hits(
    *,
    file_hits: list[dict[str, Any]],
    folder_hits: list[dict[str, Any]],
    expected_ids: list[str],
) -> str:
    folders = {p115_item_id(item): item for item in folder_hits if p115_item_id(item)}
    found: dict[str, str] = {}
    for item in file_hits:
        file_id = p115_item_id(item)
        if file_id not in {str(value) for value in expected_ids}:
            continue
        parent_id = p115_item_parent_id(item)
        parent = folders.get(parent_id) or {}
        parent_name = p115_file_name(parent)
        if is_season_folder_name(parent_name):
            dest_id = str(parent.get("pid") or parent.get("parent_id") or "").strip()
        else:
            dest_id = parent_id
        if dest_id:
            found[file_id] = dest_id
    expected = [str(value) for value in expected_ids if str(value)]
    if not expected or any(file_id not in found for file_id in expected):
        return INCOMPLETE
    dests = set(found.values())
    if len(dests) != 1:
        return CONFLICT
    return dests.pop()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_intake_identity.IntakeIdentityDestTests -q`

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add tests/test_intake_identity.py app/media/intake_identity.py
git commit -m "feat: resolve dest from video file hits"
```

---

### Task 3: Cleanup eligibility

**Files:**
- Modify: `tests/test_intake_identity.py`
- Modify: `app/media/intake_identity.py`

- [ ] **Step 1: Write the failing tests**

```python
from app.media.intake_identity import cleanup_root_action


class IntakeIdentityCleanupTests(unittest.TestCase):
    def test_delete_empty_root_in_redundant(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="redundant-cid",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "delete",
        )

    def test_skip_when_root_is_dest(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="dest-c-441531",
                parent_id="movie-parent",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "skip",
        )

    def test_skip_when_root_already_gone(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid"},
            ),
            "skip",
        )

    def test_needs_action_when_root_sits_in_library(self):
        self.assertEqual(
            cleanup_root_action(
                root_id="recv-folder-402",
                parent_id="movie-parent",
                dest_id="dest-c-441531",
                cleanup_parents={"pending-cid", "redundant-cid"},
            ),
            "needs_action",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_intake_identity.IntakeIdentityCleanupTests -q`

Expected: FAIL with `cleanup_root_action` missing

- [ ] **Step 3: Write minimal implementation**

```python
def cleanup_root_action(
    *,
    root_id: str,
    parent_id: str,
    dest_id: str,
    cleanup_parents: set[str],
) -> str:
    root_id = str(root_id or "").strip()
    parent_id = str(parent_id or "").strip()
    dest_id = str(dest_id or "").strip()
    if not root_id or root_id == dest_id or not parent_id:
        return "skip"
    if parent_id in {str(value) for value in cleanup_parents if str(value)}:
        return "delete"
    return "needs_action"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_intake_identity.IntakeIdentityCleanupTests -q`

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add tests/test_intake_identity.py app/media/intake_identity.py
git commit -m "feat: decide cleanup from intake roots"
```

---

### Task 4: Write intake_identity on receive

**Files:**
- Modify: `tests/test_bridge_task_engine.py`
- Modify: `app/workflows/self_share.py`
- Modify: `app/task_store.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_bridge_task_engine.py`, add `search_files` to `FakeP115` (needed later; receive only uses `list_files`):

```python
    def __init__(self):
        ...
        self.search_hits = {}

    def search_files(self, search_value, limit=20):
        return list(self.search_hits.get(str(search_value), []))
```

Add this test on `BridgeSelfShareTaskWorkflowTests`:

```python
    def test_received_stage_snapshots_distinct_video_file_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115.files_by_parent["received-a"] = [
                {"fid": "video-a", "cid": "received-a", "n": "Movie.A.mkv"},
                {"fid": "sub-a", "cid": "received-a", "n": "Movie.A.ass"},
            ]
            self.p115.files_by_parent["received-b"] = [
                {"fid": "video-b", "cid": "received-b", "n": "Movie.B.mkv"},
            ]
            row = self._row()
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED, {"submission_id": row["id"]}, row["id"])
            result = workflow.run_stage(task)
            identity = result.metadata.get("intake_identity") or {}
            self.assertEqual(identity.get("root_ids"), ["received-a", "received-b"])
            self.assertEqual(
                {(item["id"], item["name"]) for item in identity.get("files") or []},
                {("video-a", "Movie.A.mkv"), ("video-b", "Movie.B.mkv")},
            )
            self.assertNotIn("file-a", {item["id"] for item in identity.get("files") or []})
            self.assertEqual(result.metadata.get("received_file_ids"), ["file-a", "file-b"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_received_stage_snapshots_distinct_video_file_ids -q`

Expected: FAIL because `intake_identity` is missing

- [ ] **Step 3: Write minimal implementation**

In `app/task_store.py`, add `"intake_identity"` to `REPROCESS_METADATA_DELETE_KEYS` (after `received_snapshot_complete`).

In `app/workflows/self_share.py` `_stage_received` metadata block, after `received_items` are known:

```python
from app.media.intake_identity import snapshot_files

roots = received.get("received_items") or []
try:
    files = snapshot_files(roots, self.p115.list_files)
except Exception:
    files = []
metadata["intake_identity"] = {
    "root_ids": [str(item.get("file_id") or "").strip() for item in roots if str(item.get("file_id") or "").strip()],
    "files": files,
}
```

If `list_files` is missing, pass `lambda *_args, **_kwargs: []`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_received_stage_snapshots_distinct_video_file_ids -q`

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py app/task_store.py tests/test_bridge_task_engine.py
git commit -m "feat: persist intake identity after receive"
```

---

### Task 5: Organize movie graph — bind C- dest, not leftover

**Files:**
- Modify: `tests/test_bridge_task_engine.py`
- Modify: `app/workflows/self_share.py`

IDs in this test must all differ: `share-fid-402`, `recv-folder-402`, `dest-c-441531`, `video-mkv-402`.

- [ ] **Step 1: Write the failing test**

```python
    def test_organizing_binds_movie_dest_from_moved_video_fid(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
                ],
                "441531": [
                    {
                        "cid": "dest-c-441531",
                        "n": "C-拆弹专家-2017-[tmdb=441531]",
                        "pid": "movie-parent",
                    },
                    {
                        "cid": "recv-folder-402",
                        "n": "拆弹专家 (2017) [tmdb=441531]",
                        "pid": "redundant-cid",
                    },
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-c-441531")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-c-441531")
            self.assertEqual(stored["own_share_file_id"], "dest-c-441531")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_binds_movie_dest_from_moved_video_fid -q`

Expected: FAIL — current containment rejects `dest-c-441531` or never binds it

- [ ] **Step 3: Write minimal implementation**

In organizing, after CMS auto_organize, if `intake_identity.files` is empty → `StageResult.defer("等待 CMS 整理完成", ...)`.

Otherwise search each `files[].name` plus any known TMDB id. Call `dest_id_from_file_hits`. Then:

- `incomplete` → defer「等待 CMS 整理完成」
- `conflict` → `needs_action`「接收文件落到多个片库目录，已停止自动绑定」
- dest id ∈ `root_ids` → treat as incomplete (still inbox/leftover)
- else write `intake_identity.dest_id`, `update_self_share(..., own_share_file_id=dest_id, own_share_file_name=folder_name)` and complete organize as today

Replace `_folder_contains_received_items` / `_reject_if_unrelated` as the ownership check. Do not bind from `find_organized_folder` unless `dest_id_from_file_hits` returned that folder.

`is_unverified_received_source` must stop treating `received_file_ids` (share fids) as unverified. Check `intake_identity.root_ids` still parented by `receive_cid` instead.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_binds_movie_dest_from_moved_video_fid -q`

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "feat: bind organize dest from video fid"
```

---

### Task 6: TV merge, 追更, old dest, missing files

**Files:**
- Modify: `tests/test_bridge_task_engine.py`
- Modify: `app/workflows/self_share.py` only if Task 5 helpers need another branch

- [ ] **Step 1: Write the failing tests**

```python
    def test_organizing_merges_season_files_into_existing_show_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {
                "Reacher.S03E01.mkv": [
                    {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
                ],
                "108978": [
                    {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
                    {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                    {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-399"],
                    "received_items": [
                        {"file_id": "recv-s3", "file_name": "Season 3", "is_folder": True},
                    ],
                    "received_items_complete": True,
                    "intake_identity": {
                        "root_ids": ["recv-s3"],
                        "files": [{"id": "ep-s3-e1", "name": "Reacher.S03E01.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-108978")

    def test_organizing_second_task_can_reuse_existing_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {
                "Reacher.S03E02.mkv": [
                    {"fid": "ep-s3-e2", "cid": "season-3", "n": "Reacher.S03E02.mkv"},
                ],
                "108978": [
                    {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
                    {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                ],
            }
            row = self._row("def", "5678")
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "def",
                "5678",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "intake_identity": {
                        "root_ids": ["recv-s3-task-b"],
                        "files": [{"id": "ep-s3-e2", "name": "Reacher.S03E02.mkv"}],
                    },
                    "received_items_complete": True,
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(stored["own_share_file_id"], "dest-108978")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-108978")

    def test_organizing_ignores_same_tmdb_dest_without_these_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {"108978": [
                {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
            ]}
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "intake_identity": {
                        "root_ids": ["recv-s3"],
                        "files": [{"id": "ep-s3-e1", "name": "Reacher.S03E01.mkv"}],
                    },
                    "received_items_complete": True,
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIsNone(stored["own_share_file_id"])

    def test_organizing_defers_when_intake_files_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {"submission_id": row["id"], "intake_identity": {"root_ids": ["recv-folder-402"], "files": []}},
                row["id"],
            )
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_merges_season_files_into_existing_show_dest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_second_task_can_reuse_existing_dest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_ignores_same_tmdb_dest_without_these_files tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_defers_when_intake_files_missing -q`

Expected: at least one FAIL if Task 5 only handled the movie happy path

- [ ] **Step 3: Write minimal implementation**

Keep using `dest_id_from_file_hits`. Search both video names and `tmdb_id`. If `find_organized_folder` returns a folder that is not the resolved dest, ignore it. Empty `files` defers. Missing hits defer. Do not bind `old-dest-108978`.

- [ ] **Step 4: Run tests to verify they pass**

Run the same command as Step 2.

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "feat: bind tv dest and skip unmatched tmdb folders"
```

---

### Task 7: Cleanup deletes only intake roots

**Files:**
- Modify: `tests/test_bridge_task_engine.py`
- Modify: `app/workflows/self_share.py`

- [ ] **Step 1: Write the failing tests**

```python
    def test_cleaned_stage_deletes_redundant_receive_root_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            cleanup.parents["recv-folder-402"] = "redundant-cid"
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            workflow.self_share_config.source_cleanup_parent_ids = {"redundant-cid"}
            row = self._self_share_row(title="C-拆弹专家-2017-[tmdb=441531]", tmdb_id="441531")
            row = self.submissions.update_self_share(int(row["id"]), own_share_file_id="dest-c-441531") or row
            dest = Path(tmp) / "library" / "C-拆弹专家-2017-[tmdb=441531]"
            self._write_strm(dest)
            row = self.submissions.update_move(int(row["id"]), "moved", dest_path=str(dest), category_final="华语电影") or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                        "dest_id": "dest-c-441531",
                    },
                },
                row["id"],
            )
            workflow._now = lambda: 101.0
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, ["recv-folder-402"])
            self.assertNotIn("dest-c-441531", cleanup.deleted)

    def test_cleaned_stage_needs_action_when_root_parent_is_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            cleanup.parents["recv-folder-402"] = "movie-parent"
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(int(row["id"]), "moved", dest_path=str(dest), category_final="华语电影") or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "Movie.mkv"}],
                        "dest_id": "folder-id",
                    },
                },
                row["id"],
            )
            workflow._now = lambda: 101.0
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(cleanup.deleted, [])
```

`FakeCleanupClient` currently only has `delete_file`. Extend it:

```python
class FakeCleanupClient:
    def __init__(self):
        self.deleted = []
        self.parents = {}

    def delete_file(self, file_id):
        self.deleted.append(file_id)

    def file_parent_id(self, file_id):
        return str(self.parents.get(str(file_id), "") or "")
```

In `_stage_cleaned`, resolve parent as:

```python
parent_id = ""
if hasattr(self.cleanup_client, "file_parent_id"):
    parent_id = str(self.cleanup_client.file_parent_id(root_id) or "").strip()
if not parent_id:
    parent_id = source_delete_parent_id(task, row, root_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_cleaned_stage_deletes_redundant_receive_root_only tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_cleaned_stage_needs_action_when_root_parent_is_library -q`

Expected: FAIL — cleaned still keys off `own_share_file_id`

- [ ] **Step 3: Write minimal implementation**

In `_stage_cleaned`, build:

```python
identity = task.metadata.get("intake_identity") or {}
cleanup_parents = set(self.self_share_config.source_cleanup_parent_ids or set())
receive_cid = self._task_receive_cid(task)
if receive_cid:
    cleanup_parents.add(receive_cid)
if hasattr(self.cms, "auto_organize_excluded_parent_ids"):
    cleanup_parents.update(self.cms.auto_organize_excluded_parent_ids() or set())
```

For each `root_id` in `identity["root_ids"]`, resolve parent, call `cleanup_root_action`. `delete` → existing `_journaled_delete`. `skip` → continue. `needs_action` → stop. Never delete `own_share_file_id` / `dest_id`. Residue scan excluded set = `{dest_id} ∪ files[].id`.

Remove `_is_library_dest_cleanup_target` once no callers remain.

- [ ] **Step 4: Run tests to verify they pass**

Run the same command as Step 2, plus:

`python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_cleaned_stage_does_not_delete_library_dest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_cleaned_stage_deletes_source_after_emby_confirmed_and_own_share_exists -q`

Expected: OK

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "feat: delete intake roots only during cleanup"
```

---

### Task 8: Update leftover 0.4.14 organize tests and changelog

**Files:**
- Modify: `tests/test_bridge_task_engine.py` (`test_organizing_stage_uses_season_folder_child_video_to_find_dest`, `test_organizing_stage_rejects_dest_that_does_not_contain_received_seasons`)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Rewrite the two 0.4.14 tests onto distinct IDs and `intake_identity`**

`uses_season_folder_child_video` must seed `intake_identity.files=[{id: ep1, name: Reacher.S01E01...mkv}]` and `search_hits` so dest is `dest-108978`, not because the season root id sits inside dest.

`rejects_dest_that_does_not_contain_received_seasons` becomes the same assertion as `test_organizing_ignores_same_tmdb_dest_without_these_files` if that makes it redundant — delete the old test instead of keeping two copies.

- [ ] **Step 2: Run the organize + cleanup + unit suites**

Run:

```bash
python3 -m unittest tests.test_intake_identity tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests tests.test_self_share_workflow -q
```

Expected: OK

- [ ] **Step 3: Add changelog under a new Unreleased / next-patch section**

```markdown
- **整理和清理改为跟踪接收到的视频 fid**：不再用分享 ID 或标题搜索当所有权；电影整夹会绑 CMS 新建的 C- 目录，清理只删待整理/冗余里的接收根。
```

Do not bump `__version__` in this task. Version bump belongs to the release commit.

- [ ] **Step 4: Commit**

```bash
git add tests/test_bridge_task_engine.py CHANGELOG.md
git commit -m "test: require distinct 115 ids in organize fixtures"
```

---

## Self-review

| Spec requirement | Task |
|---|---|
| `intake_identity` shape | 1, 4 |
| Snapshot two-level videos, skip subs | 1, 4 |
| Bind dest from file hits; season walks up | 2, 5, 6 |
| Empty/missing files defer; two dests needs_action | 2, 6 |
| Same TMDB without these files ignored | 6 |
| Movie C- graph | 5 |
| TV merge + 追更 | 6 |
| Cleanup roots only; dest never deleted | 3, 7 |
| Reprocess clears identity | 4 (`REPROCESS_METADATA_DELETE_KEYS`) |
| Distinct IDs in fixtures | 5–8 |
| No CMS replace / no #402 hand bind | stated in header |

No TBD placeholders. Names stay `intake_identity`, `snapshot_files`, `dest_id_from_file_hits`, `cleanup_root_action` across tasks.
