# Fix Audit Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three audited bugs: TMDB-resolved category authority, malformed Web task routes, and non-1212 self-share STRM quality checks.

**Architecture:** Keep changes surgical. Add regression tests first, then minimally adjust the existing helper functions and route parsing without changing the wider task engine. Preserve the current CMS-first/self-share workflow and do not trigger real 115/CMS operations during verification.

**Tech Stack:** Python 3 stdlib, `unittest`, SQLite-backed `TaskStore`, existing `bridge.py` workflow helpers, existing Web admin `WebApp` request handler.

---

## Files To Modify

- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/bridge.py`
  - Update `has_authoritative_category()` so TMDB-resolved categories are protected from later library-match overrides.
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/app/web.py`
  - Add a small safe task-id parser for `/task/<id>` routes and return `404` for malformed task paths.
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/app/quality.py`
  - Let `inspect_task_files()` accept `own_share_receive_code`, and let `scan_task_quality()` read it from task metadata.
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/tests/test_bridge_task_engine.py`
  - Add a regression test proving `tmdb_resolved` category cannot be overridden by an existing same-TMDB library direct STRM.
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/tests/test_web_admin.py`
  - Add malformed `/task/...` route tests.
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/tests/test_task_quality.py`
  - Add non-1212 own-share receive-code quality tests.

---

### Task 1: Protect TMDB-Resolved Categories During Move

**Files:**
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/bridge.py:1050`
- Test: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/tests/test_bridge_task_engine.py`

- [ ] **Step 1: Add the failing regression test**

Add this test near `test_moved_stage_keeps_authoritative_cms_category_even_when_same_tmdb_exists_elsewhere`:

```python
def test_moved_stage_keeps_tmdb_resolved_category_even_when_same_tmdb_exists_elsewhere(self):
    with tempfile.TemporaryDirectory() as tmp:
        tv_root = Path(tmp) / "library" / "tv"
        western_root = Path(tmp) / "library" / "western"
        workflow = self._workflow(
            tmp,
            move_config=bridge.MoveConfig(
                source_roots=[],
                library_roots={"外国电视": tv_root, "欧美电影": western_root},
            ),
        )
        row = self._self_share_row(title="W-无耻之徒-2011-[tmdb=34307]", category="外国电视", tmdb_id="34307")
        recognition = bridge.parse_recognition_json(row)
        recognition["category_status"] = "tmdb_resolved"
        row = self.submissions.update_recognition(int(row["id"]), recognition, "tmdb_resolved") or row
        source = self.config.strm_root / row["own_share_file_name"]
        tv_dest = tv_root / row["own_share_file_name"]
        western_dest = western_root / row["own_share_file_name"]
        self._write_strm(source, content="http://cms/s/owncode_ownpwd_1.mkv")
        self._write_strm(western_dest, content="http://cms/d/direct-link/movie.mkv")
        task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

        result = workflow.run_stage(task)
        moved = self.submissions.find_by_id(int(row["id"]))

        self.assertEqual(result.outcome, StageOutcome.COMPLETE)
        self.assertFalse(source.exists())
        self.assertTrue(tv_dest.exists())
        self.assertTrue(western_dest.exists())
        self.assertIn("/s/owncode_ownpwd_", (tv_dest / "movie.strm").read_text(encoding="utf-8"))
        self.assertIn("/d/", (western_dest / "movie.strm").read_text(encoding="utf-8"))
        self.assertEqual(moved["category_final"], "外国电视")
        self.assertEqual(result.metadata["category"], "外国电视")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_moved_stage_keeps_tmdb_resolved_category_even_when_same_tmdb_exists_elsewhere -v
```

Expected: FAIL because `bridge.has_authoritative_category()` returns `False` for `tmdb_resolved`, so `category_from_existing_library_match()` can override to `欧美电影`.

- [ ] **Step 3: Implement minimal fix**

Replace `has_authoritative_category()` with:

```python
def has_authoritative_category(row: dict[str, Any], recognition: dict[str, Any]) -> bool:
    if str(row.get("category_status") or "").strip() == "selected" and str(row.get("category_choice") or "").strip():
        return True
    status = str(recognition.get("category_status") or "").strip()
    category = str(recognition.get("category") or "").strip()
    if not category:
        return False
    if status == "self_share_resolved":
        return bool(
            str(recognition.get("organized_parent_id") or "").strip()
            or str(recognition.get("parent_id") or "").strip()
        )
    if status in {"tmdb_resolved", "tmdb_search_resolved"}:
        return bool(str(recognition.get("tmdb_id") or "").strip())
    return False
```

Why this shape: keep existing manual `selected` behavior; keep existing CMS parent/self-share behavior; add only TMDB statuses that carry a TMDB id and category.

- [ ] **Step 4: Run targeted tests**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_moved_stage_keeps_authoritative_cms_category_even_when_same_tmdb_exists_elsewhere tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_moved_stage_keeps_tmdb_resolved_category_even_when_same_tmdb_exists_elsewhere tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_recognizing_stage_uses_tmdb_hint_when_parent_unmapped -v
```

Expected: all selected tests PASS.

---

### Task 2: Make Web Task Routes Robust To Malformed IDs

**Files:**
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/app/web.py:213`
- Test: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/tests/test_web_admin.py`

- [ ] **Step 1: Add failing Web route tests**

Add this test near the other `WebAdminTests` endpoint tests:

```python
def test_task_routes_return_404_for_malformed_task_ids(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(Path(tmp) / "tasks.db")
        app = WebApp(store, web_token="")

        cases = [
            ("GET", "/task/"),
            ("GET", "/task/not-a-number"),
            ("POST", "/task/not-a-number/retry"),
            ("POST", "/task/not-a-number/emby"),
            ("POST", "/task/not-a-number/restore"),
            ("POST", "/task/not-a-number/reprocess"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, headers, body = app.handle_request(method, path, {}, b"")
                self.assertEqual(status, 404)
                self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
                self.assertEqual(body, b"not found")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_web_admin.WebAdminTests.test_task_routes_return_404_for_malformed_task_ids -v
```

Expected: ERROR from `ValueError` or `IndexError` in `int(parsed.path.split("/")[2])`.

- [ ] **Step 3: Implement safe parser and route guards**

In `/Users/kale/Documents/openclaw/cms-tg-ingest-release/app/web.py`, add this helper near the top-level render/helper functions:

```python
def parse_task_id_from_path(path: str) -> int | None:
    parts = str(path or "").strip("/").split("/")
    if len(parts) < 2 or parts[0] != "task":
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None
```

Then replace each `task_id = int(parsed.path.split("/")[2])` route block with the guarded form. Example for task detail:

```python
if method == "GET" and parsed.path.startswith("/task/"):
    task_id = parse_task_id_from_path(parsed.path)
    if task_id is None:
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
    return 200, {"Content-Type": "text/html; charset=utf-8"}, render_task_detail(self.store, task_id, self.submission_store).encode("utf-8")
```

Apply the same guard to `/emby`, `/restore`, `/reprocess`, and `/retry` POST route blocks before using `task_id`.

- [ ] **Step 4: Run targeted Web tests**

Run:

```bash
python3 -m unittest tests.test_web_admin -v
```

Expected: all Web admin tests PASS.

---

### Task 3: Use Actual Own-Share Receive Code In STRM Quality Checks

**Files:**
- Modify: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/app/quality.py:20`
- Test: `/Users/kale/Documents/openclaw/cms-tg-ingest-release/tests/test_task_quality.py`

- [ ] **Step 1: Add failing quality tests**

Add this test after `test_accepts_self_share_strm_url`:

```python
def test_accepts_self_share_strm_url_with_custom_receive_code(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dest = root / "Movie"
        dest.mkdir()
        (dest / "movie.strm").write_text("http://cms/s/ownshare_abcd_fileid.mkv", encoding="utf-8")
        store = TaskStore(root / "tasks.db")
        task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

        issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare", own_share_receive_code="abcd")

        self.assertEqual(issues, [])
```

Also extend `test_scan_task_quality_flags_local_taskstore_file_issues` by adding a custom-code successful task before `issues = scan_task_quality(store)`:

```python
custom_dest = root / "custom-dest"
custom_dest.mkdir()
(custom_dest / "movie.strm").write_text("https://115.com/s/owncustom_abcd_file.mkv", encoding="utf-8")
custom = store.upsert_task("custom", "", "https://115cdn.com/s/custom")
store.record_event(
    custom.id,
    TaskStage.MOVED,
    TaskStatus.SUCCEEDED,
    "moved",
    title="自定义提取码电影",
    metadata_patch={
        "dest_path": str(custom_dest),
        "own_share_code": "owncustom",
        "own_share_receive_code": "abcd",
    },
)
```

Keep the expected issue codes unchanged:

```python
self.assertEqual([issue.code for issue in issues], ["direct_strm", "missing_dest"])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_task_quality -v
```

Expected: FAIL because `inspect_task_files()` does not accept `own_share_receive_code`, and `scan_task_quality()` assumes `1212`.

- [ ] **Step 3: Implement minimal fix**

Change the function signature and expected marker in `/Users/kale/Documents/openclaw/cms-tg-ingest-release/app/quality.py`:

```python
def inspect_task_files(
    task: TaskSnapshot,
    *,
    dest_path: str | Path,
    own_share_code: str = "",
    own_share_receive_code: str = "1212",
) -> list[QualityIssue]:
    del task
    dest = Path(dest_path)
    if not dest.exists():
        return [QualityIssue("missing_dest", "目标目录不存在", str(dest))]
    files = sorted(path for path in dest.rglob("*.strm") if path.is_file())
    if not files:
        return [QualityIssue("missing_strm", "目标目录没有 STRM 文件", str(dest))]
    issues: list[QualityIssue] = []
    receive_code = str(own_share_receive_code or "1212").strip() or "1212"
    expected_marker = f"/s/{own_share_code}_{receive_code}_" if own_share_code else "/s/"
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if "/d/" in text:
            issues.append(QualityIssue("direct_strm", "发现直链 STRM", str(path)))
        elif expected_marker not in text:
            issues.append(QualityIssue("unexpected_strm", "STRM 不是预期的分享链接", str(path)))
    return issues
```

Update `scan_task_quality()` to pass the receive code:

```python
own_share_code = str(task.metadata.get("own_share_code") or "").strip()
own_share_receive_code = str(task.metadata.get("own_share_receive_code") or "1212").strip() or "1212"
title = task.title or str(task.metadata.get("received_title") or "") or task.share_code
for issue in inspect_task_files(
    task,
    dest_path=dest_path,
    own_share_code=own_share_code,
    own_share_receive_code=own_share_receive_code,
):
    issues.append(replace(issue, task_id=task.id, title=title))
```

- [ ] **Step 4: Run targeted quality tests**

Run:

```bash
python3 -m unittest tests.test_task_quality -v
```

Expected: all quality tests PASS.

---

### Task 4: Full Verification

**Files:**
- Verify only; no code changes.

- [ ] **Step 1: Run syntax check**

Run:

```bash
python3 -m py_compile bridge.py doctor.py app/*.py
```

Expected: no output and exit code 0.

- [ ] **Step 2: Run full unit suite**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests PASS. Current baseline before these changes is `Ran 264 tests ... OK`; after adding the new tests the count should increase.

- [ ] **Step 3: Manual no-network sanity check**

Run this local-only assertion:

```bash
python3 - <<'PY'
import bridge
row = {'category_status': 'tmdb_resolved', 'category_choice': '外国电视'}
recognition = {'category_status': 'tmdb_resolved', 'category': '外国电视', 'tmdb_id': '34307'}
assert bridge.has_authoritative_category(row, recognition) is True
print('tmdb_resolved authoritative: OK')
PY
```

Expected output:

```text
tmdb_resolved authoritative: OK
```

---

## Self-Review

- Spec coverage: all three audit findings are covered by one dedicated task each, plus full verification.
- Placeholder scan: no `TBD`, no vague “add tests”, no unspecified implementation steps.
- Type consistency: test snippets use existing `unittest`, `TaskStore`, `TaskStage`, `TaskStatus`, `StageOutcome`, `bridge.MoveConfig`, and existing helper methods from current test files.
