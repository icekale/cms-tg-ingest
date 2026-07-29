# Reprocess Direct STRM Time Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent series-update reprocessing from adopting stale unmatched direct STRM directories, publish the combined runtime and series fixes as `0.2.45`, and safely recover production task `#338` under parent `#328`.

**Architecture:** Add an optional keyword-only direct STRM cutoff while preserving the finder default and exact-TMDB exception. The self-share organizing stage derives that cutoff only from the current update/reprocess timestamps, so ordinary tasks remain unchanged. Release from the local combined baseline, deploy a pinned image, and recover `#338` only through the existing guarded series-update helper.

**Tech Stack:** Python 3.14-compatible standard library, `unittest`, SQLite, Git/GitHub Actions, Docker Buildx, Docker Hub, Unraid Compose, Vue/Vite frontend.

## Global Constraints

- Work only in `/Users/kale/Documents/openclaw/cms-tg-ingest-release/.worktrees/reprocess-direct-strm-time-window` until the verified feature branch is ready to integrate.
- Baseline is local `main@b12a4ef`; it already combines `v0.2.44` series protection and the final runtime side-effect fixes.
- Preserve ordinary finder behavior when no explicit cutoff is supplied.
- Preserve the exact-TMDB folder exception across the new cutoff.
- Do not rewrite `submissions.created_at`.
- Do not delete or rename existing STRM, media, 115 folders, database rows, or shares.
- Do not loosen cross-TMDB owner, ambiguous-share recovery, claim, lease, CAS, or operation-journal guards.
- Do not manually update production SQLite.
- Leave task `#328` as completed history and task `#341`, its folder, and its share unchanged.
- Do not print private URLs, share codes, receive codes, cookies, tokens, credentials, or complete unfiltered health payloads.
- Release only an unused `v0.2.45` tag; production Compose must pin `icekale/cms-tg-ingest:0.2.45`.
- Run the complete Python suite before `npm ci`, because secret-hygiene tests scan the repository tree.

---

### Task 1: Bound Reprocess Direct STRM Discovery

**Files:**
- Modify: `app/media/strm.py:453-555`
- Modify: `app/workflows/self_share.py:1300-1405`
- Test: `tests/test_direct_workflow.py:1-410`
- Test: `tests/test_bridge_task_engine.py:4200-4300`

**Interfaces:**
- Consumes: existing row-derived finder time window, `update_started_at`, `reprocess_started_at`, and the exact-TMDB exception.
- Produces: `find_recent_direct_library_strm_source_dir(..., *, min_update_time: float = 0) -> tuple[Path, str] | None`.
- Produces: organizing-stage calls that pass `max(update_started_at - 5, reprocess_started_at - 5)` without changing ordinary intake calls.

- [ ] **Step 1: Add the failing finder cutoff tests**

Add `import inspect` to `tests/test_direct_workflow.py`. Add this test next to `test_recent_direct_library_lookup_allows_old_exact_tmdb_folder`:

```python
def test_recent_direct_library_lookup_ignores_stale_unmatched_folder_after_cutoff(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        library = root / "library"
        media_root = library / "unmatched-old-folder"
        media_root.mkdir(parents=True)
        strm = media_root / "movie.strm"
        strm.write_text("https://115.com/d/file-id/movie.mkv", encoding="utf-8")
        os.utime(strm, (100, 100))
        os.utime(media_root, (100, 100))
        config = MoveConfig(source_roots=[], library_roots={"欧美电影": library}, stable_seconds=0)
        row = {"created_at": 1, "title": "悬案 (2026)"}

        self.assertIn(
            "min_update_time",
            inspect.signature(find_recent_direct_library_strm_source_dir).parameters,
        )
        found = find_recent_direct_library_strm_source_dir(
            config,
            row,
            {"tmdb_id": "273114", "title": "悬案", "type": "tv"},
            share_name="悬案 (2026)",
            min_update_time=200,
        )

    self.assertIsNone(found)
```

Add the same signature assertion and `min_update_time=2000` argument to `test_recent_direct_library_lookup_allows_old_exact_tmdb_folder`. Its expected exact folder result remains unchanged.

- [ ] **Step 2: Add the failing workflow regression**

Add this test to `BridgeSelfShareTaskWorkflowTests` near the existing organizing direct-STRM tests:

```python
def test_organizing_reprocess_ignores_direct_strm_older_than_current_run(self):
    with tempfile.TemporaryDirectory() as tmp:
        western_root = Path(tmp) / "library" / "western"
        workflow = self._workflow(
            tmp,
            move_config=bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": western_root}),
        )
        row = self._row()
        row = self.submissions.update_status(int(row["id"]), "received", title="悬案 (2026)") or row
        row = self.submissions.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="auto_organize_submitted",
        ) or row
        recognition = {
            "ok": True,
            "title": "悬案",
            "share_name": "悬案 (2026)",
            "tmdb_id": "273114",
            "type": "tv",
            "category": "国产电视",
            "category_status": "self_share_resolved",
        }
        row = self.submissions.update_recognition(
            int(row["id"]), recognition, "self_share_resolved"
        ) or row
        row = self.submissions.update_category(int(row["id"]), "国产电视", "selected") or row
        stale_dir = western_root / "unmatched-old-folder"
        self._write_strm(stale_dir, content="http://cms/d/stale/movie.mkv")
        stale_time = float(row["created_at"]) + 10
        os.utime(stale_dir / "movie.strm", (stale_time, stale_time))
        os.utime(stale_dir, (stale_time, stale_time))
        update_started_at = stale_time + 120
        task = self._claim_task(
            "abc",
            "1234",
            TaskStage.ORGANIZING,
            {
                "submission_id": row["id"],
                "recognition": recognition,
                "update_started_at": update_started_at,
                "reprocess_started_at": update_started_at,
            },
            row["id"],
        )

        result = workflow.run_stage(task)
        stored = self.submissions.find_by_id(int(row["id"]))
        stored_recognition = bridge.parse_recognition_json(stored)

    self.assertEqual(result.outcome, StageOutcome.DEFER)
    self.assertEqual(stored["category_choice"], "国产电视")
    self.assertEqual(stored["category_status"], "selected")
    self.assertEqual(stored_recognition["category"], "国产电视")
    self.assertEqual(stored_recognition["category_status"], "self_share_resolved")
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
python3 -m unittest -v \
  tests.test_direct_workflow.DirectWorkflowTests.test_recent_direct_library_lookup_ignores_stale_unmatched_folder_after_cutoff \
  tests.test_direct_workflow.DirectWorkflowTests.test_recent_direct_library_lookup_allows_old_exact_tmdb_folder \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_organizing_reprocess_ignores_direct_strm_older_than_current_run
```

Expected: finder tests fail because `min_update_time` is absent; the workflow test fails because the stale sole candidate changes the submission category to `欧美电影`.

- [ ] **Step 4: Implement the optional finder cutoff**

Change the finder signature and normalize its cutoff:

```python
def find_recent_direct_library_strm_source_dir(
    config: MoveConfig,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str = "",
    *,
    min_update_time: float = 0,
) -> tuple[Path, str] | None:
    try:
        since = float(row.get("created_at") or row.get("updated_at") or 0) - 60
    except (TypeError, ValueError):
        since = 0
    try:
        explicit_since = float(min_update_time or 0)
    except (TypeError, ValueError):
        explicit_since = 0
    if explicit_since > 0:
        since = max(since, explicit_since)
```

Leave both existing `and not exact_tmdb_folder` conditions unchanged.

- [ ] **Step 5: Pass the current-run cutoff from organizing**

After parsing both stage timestamps, add:

```python
direct_min_update_time = max(
    update_started_at - 5 if update_started_at else 0,
    reprocess_started_at - 5 if reprocess_started_at else 0,
)
```

Pass `min_update_time=direct_min_update_time` to both organizing-stage calls to `find_recent_direct_library_strm_source_dir()`. Do not change the direct workflow caller in `app/workflows/direct.py`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the RED command again, then run:

```bash
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_direct_workflow \
  tests.test_bridge_task_engine
```

Expected: all focused tests pass, including the existing exact-TMDB and ordinary organizing cases.

- [ ] **Step 7: Review and commit the behavioral fix**

Run:

```bash
git diff --check
git diff -- app/media/strm.py app/workflows/self_share.py tests/test_direct_workflow.py tests/test_bridge_task_engine.py
```

Commit only these four reviewed files:

```bash
git add app/media/strm.py app/workflows/self_share.py tests/test_direct_workflow.py tests/test_bridge_task_engine.py
git commit -m "fix: bound reprocess direct strm lookup"
```

---

### Task 2: Prepare The Combined 0.2.45 Release

**Files:**
- Modify: `app/__init__.py`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `docs/dockerhub-overview.md`
- Modify: `tests/test_release_workflows.py`

**Interfaces:**
- Consumes: verified runtime hardening plus Task 1.
- Produces: application version `0.2.45` and matching public release examples/tests.

- [ ] **Step 1: Verify the release tag is unused**

Run:

```bash
git fetch origin --tags
git tag --list v0.2.45
git ls-remote --tags origin refs/tags/v0.2.45
```

Expected: both tag queries are empty. Stop if either finds `v0.2.45`.

- [ ] **Step 2: Make release tests fail for the new version**

Update `tests/test_release_workflows.py` to require:

```python
self.assertIn("git tag v0.2.45", readme)
self.assertIn(
    "docker pull icekale/cms-tg-ingest:0.2.44\n# 将 compose 的 image 改为 0.2.44",
    readme,
)
```

and require `image: icekale/cms-tg-ingest:0.2.45` in the Docker Hub overview test.

Run:

```bash
python3 -m unittest -v tests.test_release_workflows
```

Expected: failures reference stale `0.2.44` release examples and stale `0.2.43` rollback guidance.

- [ ] **Step 3: Update release metadata**

- Set `app.__version__` to `0.2.45`.
- Add `## 0.2.45 - 2026-07-29` at the top of `CHANGELOG.md` describing:
  - durable CMS mutation recovery for direct/source-share modes;
  - complete background credential redaction and ambiguous same-title 115 recovery protection;
  - the reprocess direct STRM current-run cutoff.
- Change active README tag/pull examples to `0.2.45` and rollback guidance to `0.2.44`.
- Change the Docker Hub overview image and pull examples to `0.2.45`.

- [ ] **Step 4: Verify release metadata and commit**

Run:

```bash
python3 -m unittest -v tests.test_release_workflows
python3 -m unittest -v tests.test_docs_v02 tests.test_docs_task_engine
git diff --check
```

Commit only the five release files:

```bash
git add app/__init__.py CHANGELOG.md README.md docs/dockerhub-overview.md tests/test_release_workflows.py
git commit -m "release: publish v0.2.45"
```

---

### Task 3: Run The Combined Release Gate

**Files:**
- Verify: all tracked application, test, frontend, workflow, and documentation files.

**Interfaces:**
- Consumes: Tasks 1-2 and any newer local `main` commits that must be merged before release.
- Produces: one clean, fully verified feature-branch HEAD safe to fast-forward into local `main`.

- [ ] **Step 1: Reconcile concurrent local work without force operations**

Run:

```bash
git fetch origin
git log -5 --oneline --decorate main
git merge-base --is-ancestor main HEAD
```

If the ancestry check fails because local `main` advanced, merge `main` into this branch normally, inspect every conflict, and rerun all following gates. Never force-push or discard another worktree's commits.

- [ ] **Step 2: Run Python checks before installing frontend dependencies**

Run:

```bash
python3 -m compileall -q app bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test*.py' -q
python3 -m unittest -v tests.test_release_workflows
git diff --check
```

Expected: all tests pass with zero failures and `git diff --check` has no output.

- [ ] **Step 3: Run the recovery matrix five consecutive times**

Run this command five times and require every run to pass:

```bash
python3 -W error::ResourceWarning -m unittest -q tests.test_runtime_recovery
```

- [ ] **Step 4: Run frontend clean verification**

Run from `frontend/`:

```bash
npm ci
npm test
npm run build
```

Expected: 2 frontend tests pass and the production build exits zero. The existing Vite chunk-size advisory is non-blocking.

- [ ] **Step 5: Verify the exact branch payload**

Run:

```bash
git status --short --branch
git log --oneline --decorate main..HEAD
git diff --stat main..HEAD
git diff --check main..HEAD
```

Expected: only the approved design/plan, Task 1 fix/tests, and `0.2.45` release metadata are new relative to the combined local `main`.

---

### Task 4: Integrate And Publish Once

**Files:**
- Update: local `main` Git history.
- Publish: remote `main` and annotated tag `v0.2.45`.

**Interfaces:**
- Consumes: clean verified feature branch from Task 3.
- Produces: one GitHub mainline containing both previously unpushed runtime fixes and the new STRM fix, plus a multi-architecture release image.

- [ ] **Step 1: Fast-forward local main**

In `/Users/kale/Documents/openclaw/cms-tg-ingest-release`, require a clean worktree, fetch once more, and run:

```bash
git merge --ff-only fix/reprocess-direct-strm-time-window
```

If `--ff-only` fails, stop and reconcile concurrent main changes on the feature branch; do not create an unreviewed merge at release time.

- [ ] **Step 2: Recheck the integrated HEAD**

Run on local `main`:

```bash
python3 -m compileall -q app bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test*.py' -q
git diff --check origin/main..main
git status --short --branch
```

- [ ] **Step 3: Push main and create the release tag**

Run:

```bash
git push origin main
git tag -a v0.2.45 -m "release v0.2.45"
git push origin v0.2.45
```

Do not treat the push as image publication.

- [ ] **Step 4: Wait for GitHub Actions and inspect Docker Hub**

Run:

```bash
gh run list --workflow release-images.yml --limit 1
release_run_id=$(gh run list --workflow release-images.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$release_run_id"
docker buildx imagetools inspect icekale/cms-tg-ingest:0.2.45
docker buildx imagetools inspect icekale/cms-tg-ingest:latest
```

Require the workflow to succeed and both tags to expose active `linux/amd64` and `linux/arm64` manifests. Record the shared digest.

---

### Task 5: Back Up And Deploy Unraid

**Files:**
- Back up: `/mnt/user/appdata/cms-tg-ingest/docker-compose.yml`
- Back up: `/mnt/user/appdata/cms-tg-ingest/.env`
- Back up: `/mnt/user/appdata/cms-tg-ingest/data/tasks.db`
- Back up: `/mnt/user/appdata/cms-tg-ingest/data/submissions.db`
- Modify: only the Compose image tag, from `0.2.44` to `0.2.45`.

**Interfaces:**
- Consumes: verified Docker Hub image from Task 4.
- Produces: healthy Unraid container pinned to `0.2.45`, with recoverable pre-deploy state.

- [ ] **Step 1: Capture a timestamped backup and safe task snapshots**

Use one timestamp under `/mnt/user/appdata/cms-tg-ingest/data/backups/`. Copy the two databases, `.env`, and Compose file without printing their contents. Generate redacted JSON snapshots for `#328`, `#338`, and `#341` containing only task/submission IDs, stage/status, TMDB/type/category, parent IDs, booleans for share/folder presence, and SHA-256 short fingerprints for folder IDs.

- [ ] **Step 2: Verify both SQLite backups**

Run `PRAGMA quick_check` against the copied `tasks.db` and `submissions.db` using the pinned currently deployed image. Require both results to be `ok` before changing Compose.

- [ ] **Step 3: Change only the image tag and deploy**

In `/mnt/user/appdata/cms-tg-ingest`:

```bash
docker compose pull cms-tg-ingest
docker compose up -d --no-build cms-tg-ingest
docker compose ps
```

Inspect the Compose diff against its backup and require the image line to be the only change.

- [ ] **Step 4: Verify the deployed application**

Run:

```bash
docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' cms-tg-ingest
docker exec cms-tg-ingest python /app/doctor.py --quiet
```

Call `/api/v1/health` but filter it with `jq` to output only HTTP-safe health/count/runner heartbeat fields. Require a 2xx response, `runner_heartbeat_stale=false`, and no new TaskRunner/CMS/115 exception in the latest 200 logs.

---

### Task 6: Recover And Monitor Production Task #338

**Files:**
- Read/write through application APIs: production `tasks.db` and `submissions.db`.
- Preserve: tasks `#328` and `#341`, all media, 115 folders, and shares.

**Interfaces:**
- Consumes: deployed `0.2.45`, healthy runner, verified backups, and `start_series_update_from_link()`.
- Produces: `#338` safely requeued under parent `#328`, then a safe terminal result with no cross-TMDB share.

- [ ] **Step 1: Stop the service and revalidate invariants**

Stop `cms-tg-ingest` so the runner cannot race preparation. Read all three tasks and their submissions through `TaskStore`/`SubmissionStore`. Require:

```text
#328 = cleaned/succeeded, TMDB 273114, type tv, category 国产电视
#338 = unclaimed, parent task 328, no own share code, no persisted foreign folder
#341 = byte-for-byte-equivalent safe snapshot fields to the pre-deploy snapshot
```

Stop without mutation if any invariant fails.

- [ ] **Step 2: Invoke the formal series-update helper**

Use a one-off Compose Python process that reads the existing child key/link from task `#338` and calls:

```python
updated, result = bridge.start_series_update_from_link(
    parent,
    bridge.ShareKey(child.share_code, child.receive_code),
    child.url,
    child.chat_id,
    submission_store,
    task_store,
    source="生产修复 v0.2.45",
)
print(result, updated.id if updated else "", updated.current_stage.value if updated else "", updated.status.value if updated else "")
```

Expected output begins `started 338 received pending`. Any `source_busy`, `source_conflict`, `not_eligible`, or `failed` result stops recovery.

- [ ] **Step 3: Assert prepared identity before restart**

Require task/submission TMDB `273114`, type `tv`, category `国产电视`, parent task `328`, parent submission `314`, no own share code/file ID, no `share_create_status=pending`, and no claim. Confirm `#341` still matches the saved snapshot.

- [ ] **Step 4: Restart and monitor without manual wakeups**

Start only the existing Compose service. Poll `#338` at its scheduled cadence through read-only TaskStore/API checks. Do not invoke retry, reprocess, manual wake, or SQLite updates while it is running.

Acceptable outcomes:

- preferred: `cleaned/succeeded` under TMDB `273114` with healthy Emby confirmation;
- safe stop: a specific `needs_action` with no self-share code, no foreign folder, and no destructive operation.

Investigate any safe stop before deciding on another recovery action.

- [ ] **Step 5: Run final production verification**

Re-run container health, doctor, filtered API health, runner heartbeat, and latest logs. Verify no repeated CMS POST recovery, no same-title share ambiguity adoption, no credential leakage, and no new state change for `#341`.

- [ ] **Step 6: Record completion evidence**

Update the local SDD progress ledger with commit hashes, test counts, GitHub Actions run ID, Docker digest/platforms, Unraid backup paths, deployed image, redacted `#338` terminal state, and `#341` invariance. Do not include credentials, URLs, share codes, receive codes, or raw 115 folder IDs.
