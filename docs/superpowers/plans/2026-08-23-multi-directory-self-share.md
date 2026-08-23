# Multi-Directory Self-Share Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一个自有分享任务安全处理多个 CMS 整理目录，每个目录独立识别、分享、同步和 STRM，全部完成后统一清理接收源，并可安全恢复任务 414 而不重复接收。

**Architecture:** 保留现有单目录任务和阶段枚举，在任务 metadata 中加入 `multi_target_version=1` 与 `organized_targets` 列表。整理阶段将所有接收文件按实际目标目录分组；后续阶段逐目标执行并以目标级 journal operation 保证幂等，阶段只有在所有目标完成后才推进。新增受保护的 `resume_organizing` 动作只恢复已有成功 `receive_share` 的任务，不改变 operation generation。

**Tech Stack:** Python 3 标准库、SQLite JSON metadata、现有 `TaskStore` operation journal、`unittest`、Docker/Unraid 发布流程。

---

## 文件范围

- Modify: `app/media/intake_identity.py` — 从文件命中结果构建单目录或多目录分组。
- Test: `tests/test_intake_identity.py` — 分组、缺失、重复归属和季目录测试。
- Modify: `app/workflows/self_share.py` — 整理、识别、分享、同步、STRM、移动、Emby 和统一清理的多目标编排。
- Test: `tests/test_bridge_task_engine.py` — 任务阶段的多目录回归、幂等恢复和全任务失败保护。
- Modify: `app/task_actions.py` — 添加受保护的 `resume_organizing` 动作。
- Test: `tests/test_task_actions.py` — 动作可见性、CAS 保护和不重复接收验证。
- Modify: `app/web_api.py` — 多目标摘要字段、动作状态和 serializer 的 TaskStore 传递。
- Test: `tests/test_web_api.py` — 多目标序列化与动作暴露。
- Modify: `app/telegram_ui.py` — 将 `resume_organizing` 加入现有任务动作按钮白名单并显示中文标签。
- Test: `tests/test_task_actions.py` 和 `tests/test_web_api.py` — Telegram/Web 动作入口兼容。
- Modify: `app/task_store.py` — 将多目标状态键纳入普通 reprocess 的清理边界，并保留 `resume_organizing` 所需 metadata。
- Test: `tests/test_task_store.py` — reprocess metadata 清理和恢复边界。
- Modify: `app/__init__.py`, `CHANGELOG.md`, `README.md`, `docs/dockerhub-overview.md` — 发布新版本说明。
- Create: `docs/superpowers/specs/2026-08-23-multi-directory-self-share-design.md` — 已完成并提交，作为实现依据。

不修改 `SelfShareWorkflow.prepare()` 的旧轮询路径；本功能只扩展任务引擎 `BridgeSelfShareTaskWorkflow`，避免把未经 journal 保护的旧路径改成多目标。

---

### Task 1: Add pure multi-directory grouping

**Files:**
- Modify: `app/media/intake_identity.py`
- Test: `tests/test_intake_identity.py`

- [ ] **Step 1: Write the failing tests**

在现有 intake identity 测试中加入以下断言，测试新函数 `dest_file_ids_from_hits(file_hits, folder_hits, expected_ids)`：

```python
def test_dest_file_ids_from_hits_groups_expected_files_by_destination(self):
    result = dest_file_ids_from_hits(
        file_hits=[
            {"fid": "episode-a", "cid": "season-a"},
            {"fid": "episode-b", "cid": "season-b"},
        ],
        folder_hits=[
            {"fid": "season-a", "cid": "dest-a", "n": "S01"},
            {"fid": "season-b", "cid": "dest-b", "n": "S02"},
            {"fid": "dest-a", "cid": "movie-root", "n": "片库 A"},
            {"fid": "dest-b", "cid": "movie-root", "n": "片库 B"},
        ],
        expected_ids=["episode-a", "episode-b"],
    )
    self.assertEqual(result, {"dest-a": ["episode-a"], "dest-b": ["episode-b"]})


def test_dest_file_ids_from_hits_returns_empty_until_every_expected_file_is_found(self):
    result = dest_file_ids_from_hits(
        file_hits=[{"fid": "episode-a", "cid": "dest-a"}],
        folder_hits=[{"fid": "dest-a", "n": "片库 A"}],
        expected_ids=["episode-a", "episode-b"],
    )
    self.assertEqual(result, {})


def test_dest_file_ids_from_hits_marks_one_file_seen_under_two_destinations_ambiguous(self):
    result = dest_file_ids_from_hits(
        file_hits=[
            {"fid": "episode-a", "cid": "dest-a"},
            {"fid": "episode-a", "cid": "dest-b"},
        ],
        folder_hits=[
            {"fid": "dest-a", "n": "片库 A"},
            {"fid": "dest-b", "n": "片库 B"},
        ],
        expected_ids=["episode-a"],
    )
    self.assertIsNone(result)
```

Keep the existing `dest_id_from_file_hits()` API as a compatibility wrapper: it returns `INCOMPLETE` for `{}`, `CONFLICT` for `None` or more than one destination, and the only destination ID for a one-entry mapping.

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
python3 -m unittest tests.test_intake_identity -q
```

Expected: FAIL because `dest_file_ids_from_hits` does not exist.

- [ ] **Step 3: Implement the minimal grouping helper**

Add `dest_file_ids_from_hits()` beside `dest_id_from_file_hits()` and reuse the existing season-parent rule:

```python
def dest_file_ids_from_hits(
    *,
    file_hits: list[dict[str, Any]],
    folder_hits: list[dict[str, Any]],
    expected_ids: list[str],
) -> dict[str, list[str]] | None:
    expected = {str(value) for value in expected_ids if str(value)}
    folders = {p115_item_id(item): item for item in folder_hits if p115_item_id(item)}
    by_file: dict[str, set[str]] = {}
    for item in file_hits:
        file_id = p115_item_id(item)
        if file_id not in expected:
            continue
        parent_id = p115_item_parent_id(item)
        parent = folders.get(parent_id) or {}
        dest_id = p115_item_parent_id(parent) if is_season_folder_name(p115_file_name(parent)) else parent_id
        if dest_id:
            by_file.setdefault(file_id, set()).add(dest_id)
    if set(by_file) != expected:
        return {}
    if any(len(destinations) != 1 for destinations in by_file.values()):
        return None
    grouped: dict[str, list[str]] = {}
    for file_id, destinations in by_file.items():
        dest_id = next(iter(destinations))
        grouped.setdefault(dest_id, []).append(file_id)
    return {dest_id: sorted(file_ids) for dest_id, file_ids in sorted(grouped.items())}
```

Update `dest_id_from_file_hits()` to call this helper and preserve the old return values. Do not change `collect_file_ids_under_dest()` or cleanup semantics in this task.

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
python3 -m unittest tests.test_intake_identity -q
```

Expected: all intake identity tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/media/intake_identity.py tests/test_intake_identity.py
git commit -m "feat: group intake files by multiple destinations"
```

---

### Task 2: Persist multiple targets during organizing

**Files:**
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Write the failing organizing test**

Add a task-engine test using the existing `FakeP115` fixtures. Configure two complete destination trees and assert that organizing completes with two target records rather than returning `CONFLICT` or deferring:

```python
def test_organizing_persists_two_complete_destinations(self):
    with tempfile.TemporaryDirectory() as tmp:
        workflow = self._workflow(tmp, receive_cid="receive-root")
        workflow.p115.search_hits = {
            "episode-a": [{"fid": "episode-a", "cid": "season-a", "n": "01.mkv"}],
            "episode-b": [{"fid": "episode-b", "cid": "season-b", "n": "02.mkv"}],
        }
        workflow.p115.folder_paths = {
            "season-a": [
                {"fid": "season-a", "cid": "dest-a", "n": "S01", "fc": 1},
                {"fid": "dest-a", "cid": "movie-root", "n": "Show A", "fc": 1},
            ],
            "season-b": [
                {"fid": "season-b", "cid": "dest-b", "n": "S02", "fc": 1},
                {"fid": "dest-b", "cid": "movie-root", "n": "Show B", "fc": 1},
            ],
        }
        workflow.p115.files_by_parent = {
            "dest-a": [{"fid": "season-a", "cid": "dest-a", "n": "S01", "fc": 1}],
            "dest-b": [{"fid": "season-b", "cid": "dest-b", "n": "S02", "fc": 1}],
            "season-a": [{"fid": "episode-a", "cid": "season-a", "n": "01.mkv"}],
            "season-b": [{"fid": "episode-b", "cid": "season-b", "n": "02.mkv"}],
        }
        task = self._claim_task(
            "split-share", "1234", TaskStage.ORGANIZING,
            metadata={
                "receive_target_cid": "receive-root",
                "intake_identity": {
                    "root_ids": ["received-root"],
                    "files": [{"id": "episode-a", "name": "01.mkv"}, {"id": "episode-b", "name": "02.mkv"}],
                },
            },
        )

        result = workflow.run_stage(task)

        self.assertEqual(result.outcome, StageOutcome.COMPLETE)
        targets = result.metadata["organized_targets"]
        self.assertEqual([target["target_id"] for target in targets], ["dest-a", "dest-b"])
        self.assertEqual(targets[0]["file_ids"], ["episode-a"])
        self.assertEqual(targets[1]["file_ids"], ["episode-b"])
```

Do not add a new database column: TaskStore already persists arbitrary metadata JSON. Verify persistence through the later action/reprocess tests in Task 6; this task only changes the workflow metadata payload.

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_persists_two_complete_destinations -q
```

Expected: the organizing test fails because the current workflow returns `NEEDS_ACTION`/`CONFLICT` and no `organized_targets` list.

- [ ] **Step 3: Add target normalization and multi-target resolution**

In `app/workflows/self_share.py` add small private helpers near `_resolve_intake_dest_folder`:

```python
_MULTI_TARGET_VERSION = 1

def _normalized_target(self, dest_id, file_ids, folder, recognition=None):
    folder = dict(folder or {})
    return {
        "target_id": str(dest_id).strip(),
        "file_ids": sorted({str(value).strip() for value in file_ids if str(value).strip()}),
        "folder": {
            "file_id": str(folder.get("file_id") or dest_id).strip(),
            "file_name": str(folder.get("file_name") or dest_id).strip(),
            "parent_id": str(folder.get("parent_id") or "").strip(),
        },
        "recognition": dict(recognition or {}),
        "share": {"file_id": str(dest_id).strip(), "status": "pending"},
        "strm": {"status": "pending", "move_status": "pending", "emby_status": "pending"},
    }
```

Refactor the current resolver into `_resolve_intake_dest_folders()` returning `(status, targets, identity)`. Reuse existing search, `file_info`, folder-path enrichment, receive-root checks, and completeness checks. Once all expected file IDs are found, call `dest_file_ids_from_hits()` and build one normalized target per destination. Return `CONFLICT` only for ambiguous file ownership; return `INCOMPLETE` when expected IDs are still missing. Keep `_resolve_intake_dest_folder()` as a single-target compatibility wrapper for old callers.

Change `_stage_organizing()` to call the plural resolver. For a non-empty target list, call a new `_complete_organized_targets()` which:

```python
def _complete_organized_targets(self, task, row, targets, recognition, stage_metadata, hint_metadata):
    metadata = {
        "submission_id": int(row["id"]),
        "multi_target_version": 1,
        "organized_targets": targets,
        "organized_scan_cursor": {},
        **hint_metadata,
    }
    identity = stage_metadata.get("intake_identity")
    if isinstance(identity, dict):
        metadata["intake_identity"] = {**identity, "dest_id": targets[0]["target_id"]}
    return StageResult.complete("已找到 CMS 整理后的多个 115 文件夹", metadata)
```

Retain `_complete_organized_folder()` unchanged for exactly one target so legacy metadata stays byte-compatible where possible. Normal reprocess must clear target state because it starts a new receive generation; the metadata deletion and protected resume behavior are covered in Task 6.

- [ ] **Step 4: Run the organizing and legacy tests**

Run:

```bash
python3 -m unittest \
  tests.test_intake_identity \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests -q
```

Expected: the new split-destination test and all existing organizing tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "feat: persist multiple organizing targets"
```

---

### Task 3: Process independent recognition and share creation

**Files:**
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_bridge_task_engine.py`

- [ ] **Step 1: Write failing tests**

Add a test-only `_seed_targets_with_shares()` fixture in the existing test class that creates one claimed task at the requested stage with two normalized target records and two target-specific folder names. Add tests that seed different TMDB hints and assert:

```python
def test_recognizing_keeps_target_specific_tmdb_and_category(self):
    task = self._claim_task(
        "split-share", "1234", TaskStage.RECOGNIZING,
        metadata={"organized_targets": self._seed_targets_with_shares()},
    )
    result = workflow.run_stage(task)
    targets = result.metadata["organized_targets"]
    self.assertEqual(targets[0]["recognition"]["tmdb_id"], "259231")
    self.assertEqual(targets[1]["recognition"]["tmdb_id"], "326917")
    self.assertEqual(targets[0]["recognition"]["category"], "外国电视")
    self.assertEqual(targets[1]["recognition"]["category"], "外国电视")


def test_own_share_created_uses_one_journal_operation_per_target(self):
    task = self._claim_task(
        "split-share", "1234", TaskStage.OWN_SHARE_CREATED,
        metadata={"organized_targets": self._seed_targets_with_shares()},
    )
    workflow._journaled_create_share(task, "dest-a", "Show A", "1212")
    workflow._journaled_create_share(task, "dest-b", "Show B", "1212")
    workflow._journaled_create_share(task, "dest-a", "Show A", "1212")
    self.assertEqual(workflow.p115.created_shares, ["dest-a", "dest-b"])
    self.assertEqual(
        len([op for op in self.tasks.list_operations(task.id) if op.operation_type == "create_share"]),
        2,
    )
```

Add a partial failure test using the same fixture, configure the fake client to raise `P115SharePendingError` only for `dest-b`, and assert the aggregate stage result defers/needs action while the completed `dest-a` operation remains `succeeded` and is not called again.

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_recognizing_keeps_target_specific_tmdb_and_category tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_own_share_created_uses_one_journal_operation_per_target -q
```

Expected: FAIL because the current stages read only `task.metadata["organized_folder"]` and one submission row share.

- [ ] **Step 3: Implement target iterators and recognition persistence**

Add private helpers in `BridgeSelfShareTaskWorkflow`:

```python
def _organized_targets(self, task):
    raw = task.metadata.get("organized_targets")
    if isinstance(raw, list) and raw:
        return [dict(item) for item in raw if isinstance(item, dict) and str(item.get("target_id") or "").strip()]
    folder = task.metadata.get("organized_folder")
    if isinstance(folder, dict) and folder.get("file_id"):
        return [{
            "target_id": str(folder["file_id"]),
            "file_ids": [],
            "folder": dict(folder),
            "recognition": {},
            "share": {"file_id": str(folder["file_id"]), "status": "pending"},
            "strm": {"status": "pending", "move_status": "pending", "emby_status": "pending"},
        }]
    return []

def _is_multi_target(self, task):
    return int(task.metadata.get("multi_target_version") or 0) == 1 and bool(task.metadata.get("organized_targets"))
```

Refactor `_stage_recognizing()` so multi-target tasks loop through targets, construct a target-local folder/recognition, run the existing TMDB/category resolution per target, and write the enriched target back into a copied list. If any target needs action, return before creating any share. Keep the current single-target branch untouched.

Update `_conflicting_folder_owner()` to accept the target-local TMDB identity. A target may have a different TMDB ID from another target in the same task, but it must still reject an owner task whose identity is missing or different.

Refactor `_stage_share_alias_prepared()` and `_stage_own_share_created()` to loop through targets. Store share data under `target["share"]` and call `_journaled_create_share()` with `target_id` in the operation key and request metadata. The operation key must remain stable across retries within the same generation:

```python
operation_key = f"{operation_scope(task)}:create_share:{target_id}"
```

Mirror the first target into the legacy submission columns only for compatibility display; never use the legacy columns to decide whether another target needs a share.

- [ ] **Step 4: Run recognition/share tests and legacy tests**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests \
  tests.test_task_bridge \
  tests.test_runtime_recovery -q
```

Expected: all tests pass, with exactly one create operation for each multi-target destination and unchanged single-target recovery behavior.

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "feat: create self shares per organizing target"
```

---

### Task 4: Validate and submit CMS sync per target

**Files:**
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_runtime_recovery.py`

- [ ] **Step 1: Write failing tests**

Add tests asserting:

```python
def test_share_validation_requires_every_target(self):
    workflow.p115.share_statuses = [
        {"available": True, "share_state": "0", "have_vio_file": False},
        {"available": False, "share_state": "2", "have_vio_file": False},
    ]
    task = self._claim_task(
        "split-share", "1234", TaskStage.SHARE_VALIDATED,
        metadata={"organized_targets": self._seed_targets_with_shares()},
    )
    result = workflow.run_stage(task)
    self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
    targets = result.metadata["organized_targets"]
    self.assertEqual(targets[0]["share"]["validation_status"], "valid")
    self.assertEqual(targets[1]["share"]["validation_status"], "invalid")


def test_cms_sync_submits_once_for_each_target(self):
    task = self._claim_task(
        "split-share", "1234", TaskStage.SHARE_SYNC_SUBMITTED,
        metadata={"organized_targets": self._seed_targets_with_shares()},
    )
    workflow._stage_share_sync_submitted(task)
    workflow._stage_share_sync_submitted(task)
    self.assertEqual(len(workflow.cms.share_sync_calls), 2)
    self.assertEqual(
        len([op for op in self.tasks.list_operations(task.id) if op.operation_type == "cms_share_sync"]),
        2,
    )
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_share_validation_requires_every_target tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_cms_sync_submits_once_for_each_target -q
```

Expected: FAIL because validation and CMS sync currently read one row share and one task-level share code.

- [ ] **Step 3: Implement aggregate validation and CMS sync**

Make `_stage_share_validated()` iterate target shares, persist `validation_status`, `validation_error`, and review metadata in each target, and return a single aggregate result:

- all valid: `StageResult.complete()`;
- temporary unknown: `StageResult.defer()`;
- invalid or risk-marked: `StageResult.needs_action()` with target ID and reason.

Make `_stage_share_sync_submitted()` use the same loop and create a journal operation per target:

```python
operation_key = f"{operation_scope(task)}:cms_share_sync:{target_id}:{share_code}"
```

Call `cms.add_share115_sync_task()` only when that target operation is new/prepared. If a previous operation is `succeeded`, mark that target submitted without calling CMS again. If any operation is `uncertain`, keep the complete target state and return the existing manual safety message for the task.

Keep the existing single-target branch or route it through a one-element adapter, then run the old tests to ensure scalar metadata remains unchanged.

- [ ] **Step 4: Run focused recovery tests**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_task_engine \
  tests.test_runtime_recovery \
  tests.test_taskstore_workflow_events -q
```

Expected: all tests pass and no target receives a duplicate external call after a successful journal operation.

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py tests/test_runtime_recovery.py
git commit -m "feat: validate and sync each self-share target"
```

---

### Task 5: Process independent STRM, move, Emby and unified cleanup

**Files:**
- Modify: `app/workflows/self_share.py`
- No planned change: `app/media/strm.py` existing pure STRM helpers are called with target-local row views.
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_task_quality.py`
- Test: `tests/test_quality_automation.py`

- [ ] **Step 1: Write failing tests**

Add three test-only helpers in the existing test class before the tests below:

- `_seed_targets_with_strm(same_dest=False, invalid_target="")` creates a claimed task with two target records, two share codes, independent STRM source directories, a shared `intake_identity` receive root, and optional destination collision or invalid review status.
- `_persist_stage_result(task, result, next_stage)` applies the result metadata through the existing TaskStore compare-and-set test path and returns a fresh claimed snapshot.
- `_run_until_stage(task, target_stage)` invokes the real workflow stages and `_persist_stage_result()` until the requested stage result is returned; it never calls external clients except the configured fakes.

Add tests for:

```python
def test_strm_and_move_record_independent_paths_for_each_target(self):
    task = self._seed_targets_with_strm()
    result = self._run_until_stage(task, TaskStage.MOVED)
    self.assertEqual(result.outcome, StageOutcome.COMPLETE)
    current = self.tasks.find_task(task.id)
    targets = current.metadata["organized_targets"]
    self.assertNotEqual(targets[0]["strm"]["dest_path"], targets[1]["strm"]["dest_path"])
    self.assertEqual({target["strm"]["move_status"] for target in targets}, {"moved"})


def test_overlapping_multi_target_destinations_need_action(self):
    task = self._seed_targets_with_strm(same_dest=True)
    result = self._run_until_stage(task, TaskStage.MOVED)
    self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
    self.assertFalse(self.cleanup_client.deleted)


def test_cleanup_waits_for_all_targets_and_deletes_receive_roots_once(self):
    task = self._seed_targets_with_strm()
    result = self._run_until_stage(task, TaskStage.CLEANED)
    self.assertEqual(result.outcome, StageOutcome.COMPLETE)
    self.assertEqual(sorted(self.cleanup_client.deleted), ["received-root"])


def test_cleanup_does_not_delete_when_one_target_review_is_invalid(self):
    task = self._seed_targets_with_strm(invalid_target="dest-b")
    result = self._run_until_stage(task, TaskStage.CLEANED)
    self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
    self.assertEqual(self.cleanup_client.deleted, [])
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_task_engine \
  tests.test_task_quality \
  tests.test_quality_automation -q
```

Expected: FAIL because STRM/move/Emby/cleanup currently use one submission row path and one share code.

- [ ] **Step 3: Implement target-local STRM and aggregate stages**

Add a target-local row view helper that overlays a target’s share and folder fields on the existing submission row without mutating the database row before the target operation succeeds:

```python
def _row_for_target(self, row, target):
    share = target.get("share") or {}
    folder = target.get("folder") or {}
    return {
        **row,
        "own_share_file_id": share.get("file_id") or folder.get("file_id"),
        "own_share_file_name": folder.get("file_name") or row.get("own_share_file_name"),
        "own_share_code": share.get("code") or "",
        "own_share_receive_code": share.get("receive_code") or "",
        "own_share_url": share.get("url") or "",
        "category_final": (target.get("recognition") or {}).get("category") or row.get("category_final"),
        "source_path": (target.get("strm") or {}).get("source_path") or "",
        "dest_path": (target.get("strm") or {}).get("dest_path") or "",
        "move_status": (target.get("strm") or {}).get("move_status") or "",
        "emby_status": (target.get("strm") or {}).get("emby_status") or "",
    }
```

Use the view only for pure path/planning calls. Persist target fields back into `organized_targets` after each successful operation. Do not call `update_move`, `update_emby`, or `update_cleanup` as if one target represented the whole task; set the legacy row status to the aggregate value only after every target is complete.

Refactor `_stage_strm_ready()`, `_stage_moved()`, and `_stage_emby_confirmed()` to:

1. skip targets whose target-local status is already complete;
2. find/validate/move/confirm each remaining target;
3. return defer or needs-action immediately on the first unsafe target;
4. complete only when all target statuses are complete.

Before moving, compute all planned destination paths and reject duplicate/overlapping paths with `NEEDS_ACTION`.

Refactor `_stage_cleaned()` to require all target review statuses, move statuses, and Emby statuses to be complete. Then run the existing `_cleanup_intake_roots()` once using the union of all target destination IDs as protected IDs. Keep residue cleanup and journal delete recovery unchanged. Only mirror `cleanup_status=deleted` after the unified cleanup succeeds.

- [ ] **Step 4: Run focused tests and full workflow tests**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_task_engine \
  tests.test_task_quality \
  tests.test_quality_automation \
  tests.test_quality_checks \
  tests.test_runtime_recovery -q
```

Expected: all tests pass; any target failure leaves all source files untouched.

- [ ] **Step 5: Commit**

```bash
git add app/workflows/self_share.py tests/test_bridge_task_engine.py tests/test_task_quality.py tests/test_quality_automation.py
git commit -m "feat: complete multi-target STRM and cleanup flow"
```

---

### Task 6: Add safe resume-organizing action and expose target state

**Files:**
- Modify: `app/task_actions.py`
- Modify: `app/web_api.py`
- Modify: `app/telegram_ui.py` — add the new task action to the existing visible-action list and label map
- Modify: `app/task_store.py`
- Test: `tests/test_task_actions.py`
- Test: `tests/test_web_api.py`
- Test: `tests/test_task_store.py`

- [ ] **Step 1: Write failing action and API tests**

Add an action test with a task in `NEEDS_ACTION` and metadata `_defer_stage="organizing"`, an existing `intake_identity`, and one succeeded `receive_share` operation:

```python
def test_resume_organizing_requeues_without_new_receive_generation(self):
    operations_before = self.store.list_operations(task.id)
    self.assertFalse(any(op.operation_type == "create_share" for op in operations_before))
    result = apply_task_action(self.store, task.id, "resume_organizing", max_retries=3, actor="Web")
    self.assertTrue(result.applied)
    resumed = self.store.find_task(task.id)
    self.assertEqual(resumed.current_stage, TaskStage.ORGANIZING)
    self.assertEqual(resumed.status, TaskStatus.PENDING)
    self.assertEqual(resumed.metadata["operation_generation"], 0)
    self.assertEqual(
        len([op for op in self.store.list_operations(task.id) if op.operation_type == "receive_share"]),
        1,
    )
```

Add negative tests for missing receive operation, missing intake snapshot, claimed task, and a task with an existing create-share operation. All must reject the action and leave the task unchanged.

Add API serialization assertions that `organized_targets` appears under a top-level sanitized summary and under sanitized metadata, and `resume_organizing` appears only for eligible tasks.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_task_actions tests.test_web_api tests.test_task_store -q
```

Expected: FAIL because the action is not in `TASK_ACTIONS` and no guard exists.

- [ ] **Step 3: Implement the guarded action**

Add `resume_organizing` to `TASK_ACTIONS`. Change `available_task_actions()` to accept an optional keyword-only `store: TaskStore | None = None`; pass the store from `serialize_task()` and pass it directly from `apply_task_action()`. Implement the strict eligibility helper:

```python
def can_resume_organizing(task: TaskSnapshot, store: TaskStore) -> bool:
    if task.status != TaskStatus.NEEDS_ACTION or task.claimed_by:
        return False
    if task.current_stage != TaskStage.NEEDS_ACTION:
        return False
    if str(task.metadata.get("_defer_stage") or "") != TaskStage.ORGANIZING.value:
        return False
    if not isinstance(task.metadata.get("intake_identity"), dict):
        return False
    if str(task.metadata.get("own_share_code") or "").strip():
        return False
    operations = store.list_operations(task.id)
    return any(
        operation.operation_type == "receive_share" and operation.status == "succeeded"
        for operation in operations
    )
```

Expose the action only when `store` is present and `can_resume_organizing()` is true; direct callers without a store keep the existing action set. Apply it with `compare_and_set_transition()` from `NEEDS_ACTION` to `ORGANIZING/PENDING`, patching only:

```python
{
    "resume_from_stage": TaskStage.NEEDS_ACTION.value,
    "resume_stage": TaskStage.ORGANIZING.value,
    "_defer_stage": "",
    "_defer_message": "",
    "_defer_count": 0,
}
```

Add `multi_target_version` and `organized_targets` to `REPROCESS_METADATA_DELETE_KEYS` so a normal full reprocess starts clean. Do not call `build_reprocess_metadata()` for `resume_organizing`, do not increment `operation_generation`, and do not delete `receive_share` metadata or operations. Add a user-facing message “继续整理已入队”.

Add `task_store: TaskStore | None = None` to `serialize_task()` and pass the store from `api_tasks()`, `api_task_detail()`, and health/latest-problem serializers. Use it to calculate available actions. Add a top-level `organized_targets` summary containing only `target_id`, folder name, TMDB ID, category, share status, sync status, STRM status, move status, and Emby status; keep full metadata under the existing sanitized metadata field. Update `telegram_ui.py` labels to show `继续整理` for `resume_organizing`.

- [ ] **Step 4: Run action/API tests**

Run:

```bash
python3 -m unittest tests.test_task_actions tests.test_web_api tests.test_task_store -q
```

Expected: all action guards, CAS behavior, and sanitized target metadata tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/task_actions.py app/web_api.py app/telegram_ui.py app/task_store.py tests/test_task_actions.py tests/test_web_api.py tests/test_task_store.py
git commit -m "feat: safely resume organizing without repeating receive"
```

---

### Task 7: Add release metadata and perform complete verification

**Files:**
- Modify: `app/__init__.py`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/dockerhub-overview.md`
- Test: `tests/test_release_workflows.py` — assert the new version and release-note text.

- [ ] **Step 1: Write/update release assertions**

Add a release test in `tests/test_release_workflows.py` that imports `app.__version__`, reads `CHANGELOG.md`, and asserts version `0.4.21`, the phrase `多目录`, and the phrase `继续整理`. Use the next patch version after `0.4.20`: `0.4.21`.

- [ ] **Step 2: Run release tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_release_workflows -q
```

Expected: FAIL until version and release documentation are updated.

- [ ] **Step 3: Update release metadata**

Set the version in `app/__init__.py` to `0.4.21`. Add a changelog entry describing:

- one task can process multiple CMS destinations;
- each destination has an independent share and STRM state;
- one target failure preserves all source files;
- `resume_organizing` avoids duplicate receive operations.

Update README and Docker Hub overview with the same externally observable behavior, without exposing deployment host details or credentials.

- [ ] **Step 4: Run the complete local suite**

Run:

```bash
git diff --check
python3 -m unittest discover -s tests -p 'test*.py' -q
```

Expected: exit code 0 and all tests pass. Expected fault-injection logs are acceptable only when the final unittest result is `OK`.

- [ ] **Step 5: Commit release metadata**

```bash
git add app/__init__.py CHANGELOG.md README.md docs/dockerhub-overview.md tests/test_release_workflows.py
git commit -m "release: publish v0.4.21"
```

---

### Task 8: Publish, deploy, and safely resume task 414

**Files:**
- No planned change: `.github/workflows/release-images.yml` already publishes version tags and multi-architecture images.
- Modify: remote Unraid compose outside the repository only after backup.

- [ ] **Step 1: Verify repository and tag state**

Run:

```bash
git status --short --branch
git log -5 --oneline --decorate
git tag --list 'v0.4.21'
```

Expected: clean release commit on `main`, no pre-existing conflicting tag.

- [ ] **Step 2: Push main and tag**

```bash
git push origin main
git tag v0.4.21
git push origin v0.4.21
```

Expected: GitHub Actions `release-images.yml` succeeds for `linux/amd64` and `linux/arm64`.

- [ ] **Step 3: Verify published images before deployment**

Confirm Docker Hub manifests for both `icekale/cms-tg-ingest:0.4.21` and `latest` contain both architectures. Do not deploy until the tagged image exists.

- [ ] **Step 4: Back up and deploy remotely**

On the remote Unraid host, back up the existing `data`, `.env`, and compose configuration, then change only the image tag to `icekale/cms-tg-ingest:0.4.21` and recreate the service. Do not delete the task database or 115 files.

- [ ] **Step 5: Verify runtime before touching task 414**

Run:

```bash
docker inspect -f '{{.Config.Image}} {{.State.Status}} {{.State.Health.Status}}' cms-tg-ingest
docker exec cms-tg-ingest python /app/doctor.py --quiet
```

Authenticate to `/api/v1/health` using the existing web login without printing credentials. Require HTTP 200, a non-stale TaskRunner heartbeat, installed STRM deletion guards, and image `0.4.21`.

- [ ] **Step 6: Resume task 414 through the guarded action**

Call the authenticated task-action API for `resume_organizing` only after confirming the action is exposed for task 414. Do not call `reprocess`. Capture the pre-action count of `receive_share` operations and verify it is unchanged after the action.

- [ ] **Step 7: Verify task 414 and external operations**

Poll read-only task state until it reaches a terminal state or needs action again. Verify:

- `organized_targets` contains both CMS destinations;
- each target has at most one successful `create_share` and one `cms_share_sync` operation;
- no new `receive_share` operation was created;
- if any target fails, task remains `NEEDS_ACTION` and source files are retained;
- if all targets succeed, each target has its own STRM destination and source cleanup occurs only after all target statuses pass.

- [ ] **Step 8: Commit any release-only workflow adjustment and report evidence**

If `.github/workflows/release-images.yml` required no change, leave it untouched. Record final image, health, test, task, and operation evidence in the completion report; do not remove backups or delete source files as part of cleanup.

---

## Plan self-review

- Spec data model is covered by Tasks 1-2; target-specific identity and legacy compatibility by Task 3.
- Per-target share creation, validation, CMS sync, STRM, Emby, unified cleanup, and all-target failure protection are covered by Tasks 3-5.
- Safe non-receiving recovery for task 414, API serialization, and Telegram action visibility are covered by Task 6.
- Release, image publication, remote backup/deployment, and operation-count verification are covered by Tasks 7-8.
- The plan uses the existing `TaskStore.list_operations(task_id)` API and filters by `operation_type` in Python; no unsupported keyword argument is used.
- Existing single-target branches and operation recovery tests are explicitly retained; no database migration or unrelated refactor is included.
- Searched for placeholders and unresolved symbols after review; test-only fixture helpers are explicitly named and defined in the tasks where they are used.
