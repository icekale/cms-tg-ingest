# 115 自有分享异步审核防损失 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保持共享 STRM 和 Emby 入库速度，将 115 源文件清理延后到异步审核观察期通过，并停止对已标记违规分享的自动改名重建。

**Architecture:** TaskRunner 仍按现有阶段推进；`SHARE_VALIDATED` 只负责前置状态判断和记录分享创建时间，`CLEANED` 阶段负责审核检查点和最终清理。P115 客户端新增带短缓存的“我的分享列表”状态映射，多个任务复用一次列表请求。历史失效分享巡检改用批量状态，遇到确认违规只记录并保留源文件。

**Tech Stack:** Python 3、`unittest`、SQLite Store、现有 `P115WebClient`/`TaskRunner`/`StageResult`。

---

### Task 1: Add review-window configuration

**Files:**
- Modify: `app/config.py:20-55,95-115,220-245`
- Modify: `app/.env.example` equivalent root file `.env.example:95-120`
- Test: `tests/test_review_config.py`

- [x] **Step 1: Write the failing tests**

Add tests for the parser and environment wiring:

```python
class ReviewConfigTests(unittest.TestCase):
    def test_review_checkpoints_require_strict_order_and_end_at_grace(self):
        self.assertEqual(parse_review_checkpoints("600,3600,21600,86400", 86400), (600, 3600, 21600, 86400))
        with self.assertRaises(ValueError):
            parse_review_checkpoints("600,600,86400", 86400)
        with self.assertRaises(ValueError):
            parse_review_checkpoints("600,3600", 86400)

    def test_self_share_config_carries_review_window_values(self):
        config = SimpleNamespace(
            workflow_mode="self_share_sync",
            self_share_review_grace_seconds=86400,
            self_share_review_checkpoints_seconds=(600, 3600, 21600, 86400),
            self_share_review_list_cache_seconds=300,
            # existing SelfShareConfig.from_config fields are supplied by the fixture
        )
        self.assertEqual(SelfShareConfig.from_config(config).review_grace_seconds, 86400)
```

- [x] **Step 2: Run the focused tests and verify the expected failure**

Run: `python3 -m unittest tests.test_review_config -v`

Expected: FAIL because `parse_review_checkpoints` and the new configuration fields do not exist.

- [x] **Step 3: Implement the minimal configuration support**

Add `parse_review_checkpoints(value, grace_seconds)` with strict positive integer ordering and require the last checkpoint to equal the grace period. Add the three `Config` and `SelfShareConfig` fields, parse them in `Config.from_env`, and document defaults:

```text
SELF_SHARE_REVIEW_GRACE_SECONDS=86400
SELF_SHARE_REVIEW_CHECKPOINTS_SECONDS=600,3600,21600,86400
SELF_SHARE_REVIEW_LIST_CACHE_SECONDS=300
```

- [x] **Step 4: Run the focused tests and verify they pass**

Run: `python3 -m unittest tests.test_review_config -v`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/config.py .env.example tests/test_review_config.py
git commit -m "feat: configure self-share review window"
```

### Task 2: Make P115 share creation and status lookup conservative

**Files:**
- Modify: `app/clients/p115.py:330-370,716-740`
- Test: `tests/test_self_share_workflow.py:200-310`

- [x] **Step 1: Write the failing tests**

Extend the fake HTTP tests to assert `share/send` receives `ignore_warn=0`, and add a batch status test:

```python
def test_create_long_share_does_not_ignore_115_warning(self):
    client = P115WebClient("UID=1", http=FakeHttp())
    client.create_long_share("folder-id")
    self.assertEqual(fake_http.calls[0][2]["ignore_warn"], 0)

def test_list_own_share_states_returns_state_and_violation_flags(self):
    client = P115WebClient("UID=1", http=FakeShareListHttp(), share_list_cache_ttl_seconds=300)
    first = client.list_own_share_states()
    second = client.list_own_share_states()
    self.assertEqual(first["share-a"], {"share_state": "1", "have_vio_file": False})
    self.assertEqual(fake_http.calls, 1)
    self.assertEqual(first, second)
```

- [x] **Step 2: Run the focused tests and verify the expected failure**

Run: `python3 -m unittest tests.test_self_share_workflow.P115WebClientTests.test_create_long_share_does_not_ignore_115_warning tests.test_self_share_workflow.P115WebClientTests.test_list_own_share_states_returns_state_and_violation_flags -v`

Expected: FAIL because creation currently sends `ignore_warn=1` and no cached list method exists.

- [x] **Step 3: Implement the minimal client changes**

Change the share creation payload to `ignore_warn: 0`. Add a short-lived in-memory cache keyed by the list request parameters. Parse only `share_code`, `share_state`, `have_vio_file`, and `create_time`; retain 115 risk-control exceptions. Add a helper that treats `share_state` outside `0/1/true` as unavailable without turning network errors into invalid status.

- [x] **Step 4: Run the focused tests and the P115 regression tests**

Run: `python3 -m unittest tests.test_self_share_workflow.P115WebClientTests -v`

Expected: PASS, including existing share duration, cache, and risk-control tests.

- [x] **Step 5: Commit**

```bash
git add app/clients/p115.py tests/test_self_share_workflow.py
git commit -m "fix: respect 115 share warnings and batch status reads"
```

### Task 3: Defer source deletion until review checkpoints

**Files:**
- Modify: `app/workflows/self_share.py:900-1060,1290-1335`
- Modify: `app/task_bridge.py:175-230`
- Test: `tests/test_bridge_task_engine.py:1220-1315` and `tests/test_self_share_workflow.py`

- [x] **Step 1: Write failing workflow tests**

Add tests covering the new lifecycle:

```python
def test_valid_share_does_not_delete_source_before_review_window(self):
    task = self._claim_task("abc", "1234", TaskStage.SHARE_VALIDATED, {"submission_id": row_id}, row_id)
    result = workflow.run_stage(task)
    self.assertEqual(result.outcome, StageOutcome.COMPLETE)
    self.assertEqual(cleanup.deleted, [])
    self.assertEqual(result.metadata["share_review_status"], "pending")

def test_cleaned_stage_defers_until_all_review_checkpoints_pass(self):
    task = self._claim_task("abc", "1234", TaskStage.CLEANED, {"submission_id": row_id, "share_created_at": 0}, row_id)
    result = workflow.run_stage(task)
    self.assertEqual(result.outcome, StageOutcome.DEFER)
    self.assertEqual(cleanup.deleted, [])

def test_review_state_6_keeps_source_and_stops_without_rebuilding(self):
    p115.share_list = {"owncode": {"share_state": "6", "have_vio_file": True}}
    result = workflow.run_stage(cleaned_task)
    self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
    self.assertEqual(cleanup.deleted, [])
    self.assertEqual(p115.renamed, [])
```

Update the old tests that expected cleanup during `SHARE_VALIDATED`; cleanup should now be asserted only after the final review checkpoint.

- [x] **Step 2: Run the focused tests and verify the expected failure**

Run: `python3 -m unittest tests.test_bridge_task_engine tests.test_self_share_workflow -v`

Expected: FAIL on the new assertions and on old immediate-cleanup expectations.

- [x] **Step 3: Implement review metadata and stage behavior**

Add helpers that calculate the next checkpoint from `share_created_at`, call the cached batch status map, and return one of `pending`, `passed`, `invalid`, or `unknown`. Record review metadata in TaskStore. On immediate `share_state=6` or `have_vio_file`, return `NEEDS_ACTION` and do not call the level-two alias path. At `CLEANED`, defer with a user-facing message until the next checkpoint; only invoke `cleanup_own_share_source` after every checkpoint passes. Preserve source files for invalid and unknown outcomes.

Remove the automatic recursive video rename/re-share branch from the invalid-share path. Keep the first-level `asset-*` directory alias only for STRM path restoration and task isolation.

- [x] **Step 4: Run the focused tests and verify they pass**

Run: `python3 -m unittest tests.test_bridge_task_engine tests.test_self_share_workflow -v`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/workflows/self_share.py app/task_bridge.py tests/test_bridge_task_engine.py tests/test_self_share_workflow.py
git commit -m "fix: delay self-share source cleanup until review"
```

### Task 4: Use batch status checks for historical invalid-share probing

**Files:**
- Modify: `app/self_share_health.py:25-125`
- Modify: `bridge.py:1010-1035`
- Test: `tests/test_invalid_share_cleanup.py`

- [x] **Step 1: Write the failing tests**

Add a fake client with `list_own_share_states()` and assert three candidates are covered by one list request; assert a `share_state=6` candidate is passed to the existing safe cleanup path and risk control stops the run without further candidates.

- [x] **Step 2: Run the focused tests and verify the expected failure**

Run: `python3 -m unittest tests.test_invalid_share_cleanup -v`

Expected: FAIL because the probe currently calls `share_snap` once per row.

- [x] **Step 3: Implement the batch probe**

Read one status map per probe invocation, match rows by `own_share_code`, and use `inspect_share` only for a missing map entry. Keep the existing `limit` as a limit on local rows, not API requests. Prioritize recently created or never-checked rows before historical rows so new failures are discovered without increasing request frequency.

- [x] **Step 4: Run the focused tests and verify they pass**

Run: `python3 -m unittest tests.test_invalid_share_cleanup -v`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/self_share_health.py bridge.py tests/test_invalid_share_cleanup.py
git commit -m "perf: batch invalid self-share probes"
```

### Task 5: Document deployment defaults and run the full verification

**Files:**
- Modify: `README.md: self-share environment section`
- Modify: `CHANGELOG.md`
- Test: `tests/test_docs_v02.py` or a new documentation assertion in `tests/test_review_config.py`

- [x] **Step 1: Write the failing documentation assertions**

Assert the README contains the three review variables and explains that source deletion is delayed until review passes.

- [x] **Step 2: Run the focused documentation test and verify failure**

Run: `python3 -m unittest tests.test_docs_v02 -v`

Expected: FAIL until the new defaults and behavior are documented.

- [x] **Step 3: Update documentation**

Document the defaults, the expected temporary 115 storage increase, the `needs_action` behavior for confirmed invalid shares, and the fact that no automatic filename obfuscation or repeated re-sharing is performed.

- [x] **Step 4: Run all tests and static checks**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

Expected: all tests pass and `git diff --check` produces no output.

- [x] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md tests/test_docs_v02.py
git commit -m "docs: explain self-share review window"
```
