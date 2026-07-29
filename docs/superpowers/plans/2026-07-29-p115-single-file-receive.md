# P115 Single-File Receive Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correctly identify real 115 ordinary-file records during receive snapshots, receive reconciliation, and explicit-TMDB historical recovery without changing folder behavior or production state.

**Architecture:** Keep the legacy `p115_file_id()` semantics unchanged and use the existing record-aware `p115_item_id()` / `p115_item_parent_id()` only at boundaries that consume mixed file/folder listings. Add one regression test per boundary using the real `{fid: file_id, cid: parent_id}` file shape, then run focused and full Python suites.

**Tech Stack:** Python 3.12, `unittest`, existing `P115WebClient` and `BridgeSelfShareTaskWorkflow`.

---

### Task 1: Verify the isolated baseline

**Files:**
- Read: `app/clients/p115.py`
- Read: `app/workflows/self_share.py`
- Test: `tests/test_http_clients.py`
- Test: `tests/test_self_share_workflow.py`

- [ ] **Step 1: Confirm worktree and branch isolation**

Run:

```bash
git status --short --branch
git diff --name-only main...feature/web-realtime-logging
```

Expected: current branch is `fix/p115-single-file-receive`, worktree has only the approved design/plan documentation, and the logging branch does not modify `app/clients/p115.py`, `app/workflows/self_share.py`, `tests/test_http_clients.py`, or `tests/test_p115_single_file_receive.py`.

- [ ] **Step 2: Run the focused baseline**

Run:

```bash
python3 -W error::ResourceWarning -m unittest -q \
  tests.test_http_clients \
  tests.test_self_share_workflow
```

Expected: exit 0 with no failures or resource warnings.

- [ ] **Step 3: Run the full baseline**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test*.py' -q
```

Expected: exit 0; this establishes that later failures come from the new regressions.

### Task 2: Snapshot real ordinary-file IDs

**Files:**
- Modify: `tests/test_http_clients.py`
- Modify: `app/clients/p115.py:725-737`

- [ ] **Step 1: Add the failing snapshot regression**

Add this method to `HttpClientTests` in `tests/test_http_clients.py`:

```python
def test_prepare_share_receive_snapshots_real_file_id_not_parent_cid(self):
    class FakeHttp:
        def request(self, url, method="GET", data=None, headers=None, params=None):
            if url.endswith("/share/snap"):
                return {
                    "state": True,
                    "data": {
                        "shareinfo": {"share_title": "123 (2026) {tmdb-1228710}"},
                        "list": [{"fid": "source-id", "n": "123 (2026) {tmdb-1228710}.mkv"}],
                    },
                }
            if url.endswith("/files"):
                return {
                    "state": True,
                    "data": [{
                        "fid": "old-local-id",
                        "cid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "fc": 1,
                    }],
                }
            raise AssertionError(url)

    client = P115WebClient("UID=1", http=FakeHttp(), timeout=3)

    intent = client.prepare_share_receive("abc", "1234", "pending-cid")

    self.assertEqual(intent["target_pre_call_file_ids"], ["old-local-id"])
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
python3 -m unittest -v tests.test_http_clients.HttpClientTests.test_prepare_share_receive_snapshots_real_file_id_not_parent_cid
```

Expected: FAIL because current code returns `pending-cid` instead of `old-local-id`.

- [ ] **Step 3: Use the mixed-item ID helper in the snapshot**

Change the snapshot comprehension in `P115WebClient._prepare_share_receive()` to:

```python
existing_file_ids = [
    file_id
    for file_id in (p115_item_id(item) for item in existing_items)
    if file_id
]
```

- [ ] **Step 4: Run the new test and verify GREEN**

Run:

```bash
python3 -m unittest -v tests.test_http_clients.HttpClientTests.test_prepare_share_receive_snapshots_real_file_id_not_parent_cid
```

Expected: PASS.

- [ ] **Step 5: Commit the snapshot fix**

```bash
git add app/clients/p115.py tests/test_http_clients.py
git commit -m "fix: snapshot real p115 file ids"
```

### Task 3: Normalize real ordinary-file receive records

**Files:**
- Modify: `tests/test_http_clients.py`
- Modify: `app/clients/p115.py:846-865`

- [ ] **Step 1: Add the failing reconciliation regression**

Add this method to `HttpClientTests`:

```python
def test_reconcile_prepared_share_receive_handles_real_file_records(self):
    class FakeHttp:
        def request(self, url, method="GET", data=None, headers=None, params=None):
            if url.endswith("/files"):
                return {
                    "state": True,
                    "data": [
                        {
                            "fid": "old-local-id",
                            "cid": "pending-cid",
                            "n": "123 (2026) {tmdb-1228710}.mkv",
                            "fc": 1,
                        },
                        {
                            "fid": "new-local-id",
                            "cid": "pending-cid",
                            "n": "123 (2026) {tmdb-1228710}.mkv",
                            "fc": 1,
                        },
                    ],
                }
            raise AssertionError(url)

    client = P115WebClient("UID=1", http=FakeHttp(), timeout=3)
    intent = {
        "share_code": "abc",
        "receive_code": "1234",
        "target_cid": "pending-cid",
        "source_file_ids": ["source-id"],
        "source_file_names": ["123 (2026) {tmdb-1228710}.mkv"],
        "title": "123 (2026) {tmdb-1228710}",
        "target_pre_call_file_ids": ["old-local-id"],
        "target_snapshot_complete": True,
    }

    result = client.reconcile_prepared_share_receive(intent)

    self.assertIsNotNone(result)
    self.assertEqual(result["received_items"], [{
        "file_id": "new-local-id",
        "file_name": "123 (2026) {tmdb-1228710}.mkv",
        "is_folder": False,
        "parent_id": "pending-cid",
        "received_item_verified": True,
    }])
    self.assertTrue(result["received_items_complete"])
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
python3 -m unittest -v tests.test_http_clients.HttpClientTests.test_reconcile_prepared_share_receive_handles_real_file_records
```

Expected: FAIL because reconciliation returns `None` after treating `cid=pending-cid` as the file ID.

- [ ] **Step 3: Use record-aware ID and parent helpers**

Change `_normalized_received_item()` to begin with:

```python
file_id = p115_item_id(item)
file_name = p115_file_name(item)
if not file_id or not file_name or file_id == str(target_cid or "").strip():
    return None
parent_id = p115_item_parent_id(item) or str(target_cid or "").strip()
```

Keep the remaining validation and return structure unchanged.

- [ ] **Step 4: Run receive regressions and verify GREEN**

Run:

```bash
python3 -m unittest -v \
  tests.test_http_clients.HttpClientTests.test_prepare_share_receive_snapshots_real_file_id_not_parent_cid \
  tests.test_http_clients.HttpClientTests.test_reconcile_prepared_share_receive_handles_real_file_records \
  tests.test_http_clients.HttpClientTests.test_reconcile_prepared_share_receive_excludes_pre_call_same_name_items
```

Expected: all three tests PASS, including the existing folder-record case.

- [ ] **Step 5: Commit the receive normalization fix**

```bash
git add app/clients/p115.py tests/test_http_clients.py
git commit -m "fix: normalize p115 received file records"
```

### Task 4: Recover historical explicit-TMDB ordinary files

**Files:**
- Create: `tests/test_p115_single_file_receive.py`
- Modify: `app/workflows/self_share.py:14-31`
- Modify: `app/workflows/self_share.py:1072-1118`

- [ ] **Step 1: Add a focused failing historical-recovery test**

Create `tests/test_p115_single_file_receive.py`:

```python
import unittest
from types import SimpleNamespace

from app.workflows.self_share import BridgeSelfShareTaskWorkflow


class P115SingleFileReceiveTests(unittest.TestCase):
    def test_hint_recovery_uses_real_file_id_and_parent_cid(self):
        class FakeP115:
            def list_files(self, parent_id, limit=500):
                self.call = (parent_id, limit)
                return [
                    {
                        "fid": "old-local-id",
                        "cid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "fc": 1,
                    },
                    {
                        "fid": "new-local-id",
                        "cid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "fc": 1,
                    },
                ]

        workflow = object.__new__(BridgeSelfShareTaskWorkflow)
        workflow.p115 = FakeP115()
        task = SimpleNamespace(
            source_type="share",
            metadata={
                "receive_target_cid": "pending-cid",
                "received_existing_file_ids": ["old-local-id"],
                "received_snapshot_complete": True,
            },
        )

        result = workflow._recover_received_items_for_hint(task, "1228710", 1)

        self.assertEqual(workflow.p115.call, ("pending-cid", 500))
        self.assertEqual(result, [{
            "file_id": "new-local-id",
            "file_name": "123 (2026) {tmdb-1228710}.mkv",
            "is_folder": False,
            "parent_id": "pending-cid",
            "received_item_verified": True,
        }])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the historical-recovery test and verify RED**

Run:

```bash
python3 -m unittest -v tests.test_p115_single_file_receive
```

Expected: FAIL because both ordinary files are assigned the parent `cid` and recovery returns an empty list.

- [ ] **Step 3: Use record-aware helpers in historical recovery**

Replace the `p115_file_id` / `p115_parent_id` imports in `app/workflows/self_share.py` with `p115_item_id` / `p115_item_parent_id`, then change the loop to:

```python
file_id = p115_item_id(item)
file_name = p115_file_name(item)
parent_id = p115_item_parent_id(item)
```

Keep all existing completeness, snapshot, parent, TMDB, and count guards unchanged.

- [ ] **Step 4: Run historical and workflow tests and verify GREEN**

Run:

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_p115_single_file_receive \
  tests.test_bridge_task_engine \
  tests.test_self_share_workflow
```

Expected: all tests PASS with no resource warnings.

- [ ] **Step 5: Add a failing legacy-snapshot safety regression**

Add `test_hint_recovery_rejects_legacy_snapshot_containing_target_cid` to `P115SingleFileReceiveTests`. Use a real ordinary-file listing, set `received_existing_file_ids` to `["pending-cid"]`, call `_recover_received_items_for_hint()`, and assert that the result is `[]`.

- [ ] **Step 6: Run the legacy-snapshot test and verify RED**

```bash
python3 -m unittest -v tests.test_p115_single_file_receive.P115SingleFileReceiveTests.test_hint_recovery_rejects_legacy_snapshot_containing_target_cid
```

Expected: FAIL because the ordinary file is currently selected even though the legacy baseline is ambiguous.

- [ ] **Step 7: Fail closed on an invalid legacy baseline**

Immediately after building `existing_ids` in `_recover_received_items_for_hint()`, add:

```python
if receive_cid in existing_ids:
    # Older snapshots stored a regular file's parent cid as its item
    # id. That baseline cannot safely distinguish old and new files.
    return []
```

- [ ] **Step 8: Run both historical tests and verify GREEN**

```bash
python3 -m unittest -v tests.test_p115_single_file_receive
```

Expected: both tests PASS.

- [ ] **Step 9: Commit the historical recovery fix**

```bash
git add app/workflows/self_share.py tests/test_p115_single_file_receive.py
git commit -m "fix: recover p115 ordinary files by fid"
```

### Task 5: Verify and review the complete branch

**Files:**
- Verify: `app/clients/p115.py`
- Verify: `app/workflows/self_share.py`
- Verify: `tests/test_http_clients.py`
- Verify: `tests/test_p115_single_file_receive.py`

- [ ] **Step 1: Run syntax and focused verification**

```bash
python3 -m compileall -q app tests
python3 -W error::ResourceWarning -m unittest -q \
  tests.test_http_clients \
  tests.test_p115_single_file_receive \
  tests.test_bridge_task_engine \
  tests.test_self_share_workflow
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the full Python suite**

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test*.py' -q
```

Expected: exit 0 with no failures or resource warnings.

- [ ] **Step 3: Check scope and whitespace**

```bash
git diff --check main...HEAD
git diff --stat main...HEAD
git status --short --branch
```

Expected: no whitespace errors; only the approved design, plan, two production files, existing HTTP client test file, and new focused test file differ from `main`; worktree is clean.

- [ ] **Step 4: Review the branch against the design**

Confirm all of the following from the diff and test output:

- `p115_file_id()` remains unchanged.
- Folder records still use their `cid` and `pid` through the existing mixed-item helpers.
- Ordinary files use `fid` as item ID and `cid` as parent ID.
- No retry, database migration, network call, deployment, or production task action was added.
- The Web realtime logging branch has no overlapping modified files.

- [ ] **Step 5: Record final verification if review requires no code changes**

No additional commit is needed when the worktree is already clean. Report branch HEAD, exact test counts, and integration order; do not merge, push, deploy, or requeue `#339` in this plan.
