# Quality Guardrails And Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent TMDB/STRM mismatches from entering the media library and add an operator-facing audit path for old bad records.

**Architecture:** Keep the current `bridge.py` workflow intact. Add narrow quality guard functions around expected TMDB selection, STRM source validation, and quality reporting. Extend `doctor.py` with an offline DB audit that reads `submissions.db` and reports mismatches without needing CMS, 115, Telegram, or Emby network access.

**Tech Stack:** Python standard library, SQLite, existing unittest suite, current `SubmissionStore`, `MovePlan`, and `app.quality` concepts.

---

### Task 1: Fix Expected TMDB Priority

**Files:**
- Modify: `bridge.py`
- Test: `tests/test_quality_checks.py`

- [ ] **Step 1: Write failing tests**

Add tests that prove `recognition_json.tmdb_id` is the authoritative task TMDB and that a conflicting moved folder is reported:

```python
class ExpectedTmdbPriorityTests(unittest.TestCase):
    def test_recognition_tmdb_wins_over_wrong_self_share_folder_marker(self):
        row = {
            "title": "Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv",
            "recognition_json": '{"title":"双喜","tmdb_id":"1570664","share_name":"Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv"}',
            "own_share_file_name": "D-得闲谨制-2025-[tmdb=1356454]",
            "dest_path": "/media/D-得闲谨制-2025-[tmdb=1356454]",
            "emby_path": "/media/D-得闲谨制-2025-[tmdb=1356454]/x.strm",
        }

        self.assertEqual(bridge.expected_task_tmdb_id(bridge.parse_recognition_json(row), row), "1570664")

    def test_quality_issue_flags_wrong_folder_against_recognition_tmdb(self):
        row = {
            "title": "Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv",
            "emby_status": "confirmed",
            "emby_title": "得闲谨制",
            "recognition_json": '{"title":"双喜","tmdb_id":"1570664","share_name":"Double.Happiness.2025.2160p.NF.WEB-DL.DDP5.1.H.265-HiveWeb.mkv"}',
            "own_share_file_name": "D-得闲谨制-2025-[tmdb=1356454]",
            "dest_path": "/media/D-得闲谨制-2025-[tmdb=1356454]",
            "emby_path": "/media/D-得闲谨制-2025-[tmdb=1356454]/x.strm",
        }

        issue = bridge.quality_issue_for_row(row)

        self.assertIn("任务 TMDB 1570664", issue)
        self.assertIn("路径 TMDB 1356454", issue)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_quality_checks.ExpectedTmdbPriorityTests -v`

Expected: first test fails because the old implementation picks `own_share_file_name` before `recognition_json.tmdb_id`.

- [ ] **Step 3: Implement minimal TMDB priority fix**

Change `expected_task_tmdb_id()` so explicit recognition TMDB wins first:

```python
def expected_task_tmdb_id(recognition: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    row = row or {}
    explicit = str(recognition.get("tmdb_id") or "").strip()
    if explicit:
        return explicit
    for value in (
        row.get("title"),
        recognition.get("share_name"),
        row.get("url"),
        row.get("own_share_file_name"),
        row.get("dest_path"),
        row.get("source_path"),
        row.get("emby_path"),
    ):
        tmdb_id = extract_tmdb_id_from_name(str(value or ""))
        if tmdb_id:
            return tmdb_id
    return ""
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_quality_checks.ExpectedTmdbPriorityTests tests.test_quality_checks.EmbyQualityMatchTests tests.test_quality_checks.StatusRepairTests -v`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add bridge.py tests/test_quality_checks.py
git commit -m "fix: prefer recognition tmdb in quality checks"
```

### Task 2: Block Bad Self-Share STRM Before Move

**Files:**
- Modify: `bridge.py`
- Test: `tests/test_self_share_workflow.py`

- [ ] **Step 1: Write failing tests**

Add tests that prove self-share moves reject direct STRM and unexpected share code before copying into the media library:

```python
def test_merge_self_share_folder_rejects_direct_strm_before_copy(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "share" / "Movie"
        dest = root / "library" / "Movie"
        source.mkdir(parents=True)
        dest.mkdir(parents=True)
        (source / "movie.strm").write_text("http://cms/d/direct.mkv", encoding="utf-8")
        store = bridge.SubmissionStore(root / "db.sqlite")
        row = store.upsert_submission(bridge.ShareKey("abc", "1234"), "https://115cdn.com/s/abc?password=1234", "received")
        row = store.update_self_share(row["id"], workflow_mode="self_share_sync", own_share_code="ownshare") or row
        plan = bridge.MovePlan("conflict", "ready", source, dest, "欧美电影")

        updated = bridge.merge_self_share_strm_folder(plan, store, row)

        self.assertEqual(updated["move_status"], "error")
        self.assertIn("发现直链 STRM", updated["move_error"])
        self.assertFalse((dest / "movie.strm").exists())


def test_merge_self_share_folder_rejects_unexpected_share_code_before_copy(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "share" / "Movie"
        dest = root / "library" / "Movie"
        source.mkdir(parents=True)
        dest.mkdir(parents=True)
        (source / "movie.strm").write_text("http://cms/s/othershare_1212_file.mkv", encoding="utf-8")
        store = bridge.SubmissionStore(root / "db.sqlite")
        row = store.upsert_submission(bridge.ShareKey("abc", "1234"), "https://115cdn.com/s/abc?password=1234", "received")
        row = store.update_self_share(row["id"], workflow_mode="self_share_sync", own_share_code="ownshare") or row
        plan = bridge.MovePlan("conflict", "ready", source, dest, "欧美电影")

        updated = bridge.merge_self_share_strm_folder(plan, store, row)

        self.assertEqual(updated["move_status"], "error")
        self.assertIn("STRM 不是预期的分享链接", updated["move_error"])
        self.assertFalse((dest / "movie.strm").exists())
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_self_share_workflow.SelfShareWorkflowTests.test_merge_self_share_folder_rejects_direct_strm_before_copy tests.test_self_share_workflow.SelfShareWorkflowTests.test_merge_self_share_folder_rejects_unexpected_share_code_before_copy -v`

Expected: both fail because current merge copies bad STRM.

- [ ] **Step 3: Implement minimal STRM validation**

Add `validate_self_share_strm_source()` and call it in `merge_self_share_strm_folder()` before copying:

```python
def validate_self_share_strm_source(source: Path, row: dict[str, Any]) -> str:
    if str(row.get("workflow_mode") or "") != "self_share_sync":
        return ""
    own_share_code = str(row.get("own_share_code") or "").strip()
    if not own_share_code:
        return "等待自有分享码，暂不移动 STRM"
    for path in sorted(source.rglob("*.strm")):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if "/d/" in text:
            return f"发现直链 STRM：{path}"
        expected_marker = f"/s/{own_share_code}_1212_"
        if expected_marker not in text:
            return f"STRM 不是预期的分享链接：{path}"
    return ""
```

In `merge_self_share_strm_folder()`:

```python
    issue = validate_self_share_strm_source(source, row)
    if issue:
        return store.update_move(
            int(row["id"]),
            "error",
            source_path=str(source),
            dest_path=str(dest),
            category_final=plan.category,
            error=issue,
        ) or row
```

- [ ] **Step 4: Run tests**

Run: `python3 -m unittest tests.test_self_share_workflow.SelfShareWorkflowTests.test_merge_self_share_folder_rejects_direct_strm_before_copy tests.test_self_share_workflow.SelfShareWorkflowTests.test_merge_self_share_folder_rejects_unexpected_share_code_before_copy -v`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add bridge.py tests/test_self_share_workflow.py
git commit -m "fix: block invalid self-share strm moves"
```

### Task 3: Add Offline DB Audit To doctor.py

**Files:**
- Modify: `doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write failing tests**

Add tests that create a temporary `submissions.db` with a wrong TMDB row and a direct STRM row, then verify `audit_submission_db()` reports both.

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_doctor.DoctorConfigTests.test_audit_db_reports_tmdb_mismatch_and_direct_strm -v`

Expected: fails because `audit_submission_db` does not exist.

- [ ] **Step 3: Implement `audit_submission_db(db_path)`**

Use only SQLite and filesystem reads. Report:
- `tmdb_mismatch` when `recognition_json.tmdb_id` differs from TMDB found in `dest_path`, `source_path`, or `emby_path`.
- `direct_strm` when any `*.strm` under `dest_path` contains `/d/`.
- `unexpected_strm` when `own_share_code` is set but STRM does not contain `/s/<own_share_code>_1212_`.

- [ ] **Step 4: Add CLI flag**

Add `doctor.py --audit-db /data/submissions.db`. It prints audit lines after normal checks. If audit finds issues, exit code is `1`.

- [ ] **Step 5: Run tests**

Run: `python3 -m unittest tests.test_doctor -v`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add doctor.py tests/test_doctor.py
git commit -m "feat: audit submission db quality"
```

### Task 4: Verify And Deploy

**Files:**
- No source changes beyond previous tasks.

- [ ] **Step 1: Run full local verification**

Run:

```bash
python3 -m py_compile bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 2: Deploy to Unraid**

Create a backup under `/mnt/user/appdata/cms-tg-ingest/backups/pre-quality-guardrails-<timestamp>.tgz`, rsync code excluding `.env`, `docker-compose.yml`, `data/`, `backups/`, then rebuild:

```bash
docker compose up -d --build
```

- [ ] **Step 3: Runtime verification**

Run on Unraid:

```bash
docker exec cms-tg-ingest python /app/doctor.py --quiet
docker exec cms-tg-ingest python /app/doctor.py --audit-db /data/submissions.db
```

Expected: doctor passes or reports only real historical quality issues.
