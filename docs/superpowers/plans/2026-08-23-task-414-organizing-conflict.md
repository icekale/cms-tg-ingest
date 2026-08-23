# Task 414 Organizing Conflict Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make organizing continue collecting all received-file hits so an intake split across multiple CMS media folders reaches the existing conflict/manual-action path instead of retrying forever.

**Architecture:** Keep the existing `dest_id_from_file_hits` and `CONFLICT` semantics. Change only `_resolve_intake_dest_folder` so a partial single-folder match does not return early; it continues accumulating file and folder hits until all candidate IDs are processed or a real conflict is found. Add one workflow-level regression test using the existing `FakeP115` and workflow fixture.

**Tech Stack:** Python 3, unittest, SQLite-backed test stores, existing `BridgeSelfShareTaskWorkflow`.

---

### Task 1: Add the failing regression test

**Files:**
- Modify: `tests/test_bridge_task_engine.py` near the organizing-stage tests

- [ ] **Step 1: Add a test for split CMS destinations**

Add this method to `BridgeSelfShareTaskWorkflowTests`:

```python
    def test_resolve_intake_dest_folder_collects_all_hits_before_declaring_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.search_hits = {
                "episode-a.mkv": [
                    {"fid": "episode-a", "cid": "season-a", "n": "episode-a.mkv"},
                ],
                "episode-b.mkv": [
                    {"fid": "episode-b", "cid": "season-b", "n": "episode-b.mkv"},
                ],
            }
            workflow.p115.folder_paths = {
                "season-a": [
                    {"cid": "season-a", "n": "Season 01", "pid": "dest-a"},
                    {"cid": "dest-a", "n": "Show A", "pid": "tv-parent"},
                ],
                "season-b": [
                    {"cid": "season-b", "n": "Season 02", "pid": "dest-b"},
                    {"cid": "dest-b", "n": "Show B", "pid": "tv-parent"},
                ],
            }
            task_metadata = {
                "intake_identity": {
                    "files": [
                        {"id": "episode-a", "name": "episode-a.mkv"},
                        {"id": "episode-b", "name": "episode-b.mkv"},
                    ],
                    "root_ids": ["received-root"],
                }
            }

            status, folder, identity = workflow._resolve_intake_dest_folder(
                task_metadata,
                {},
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "conflict")
            self.assertIsNone(folder)
            self.assertIsNone(identity)
```

- [ ] **Step 2: Run the focused test and verify it fails for the current bug**

Run:

```bash
python -m pytest tests/test_bridge_task_engine.py -k resolve_intake_dest_folder_collects_all_hits_before_declaring_incomplete -q
```

Expected: FAIL because the current implementation returns `incomplete` after the first partial destination instead of continuing to the second file.

### Task 2: Implement the minimal accumulation fix

**Files:**
- Modify: `app/workflows/self_share.py:1730-1741`
- Modify: `app/workflows/self_share.py:1764-1775`

- [ ] **Step 1: Replace early incomplete returns with continued accumulation**

In both search loops, change the branch from:

```python
                if dest != INCOMPLETE:
                    if self._intake_expected_files_located(dest, expected_ids, file_hits):
                        break
                    return INCOMPLETE, None, None
```

to:

```python
                if dest != INCOMPLETE:
                    if self._intake_expected_files_located(dest, expected_ids, file_hits):
                        break
                    dest = INCOMPLETE
```

This preserves the fast path when one destination contains every expected file, while allowing later searches to reveal a second destination.

- [ ] **Step 2: Run the focused regression test**

Run:

```bash
python -m pytest tests/test_bridge_task_engine.py -k resolve_intake_dest_folder_collects_all_hits_before_declaring_incomplete -q
```

Expected: PASS.

- [ ] **Step 3: Run related intake and workflow tests**

Run:

```bash
python -m pytest tests/test_intake_identity.py tests/test_bridge_task_engine.py -q
```

Expected: PASS with no failures.

- [ ] **Step 4: Review the diff and commit the code fix**

Run:

```bash
git diff --check
git diff -- app/workflows/self_share.py tests/test_bridge_task_engine.py
git add app/workflows/self_share.py tests/test_bridge_task_engine.py
git commit -m "fix: detect split organizing destinations"
```

### Task 3: Release v0.4.20 and roll out safely

**Files:**
- Modify: `app/__init__.py` version `0.4.19` → `0.4.20`
- Modify: `CHANGELOG.md` with the organizing conflict fix
- Modify: `README.md` and `docs/dockerhub-overview.md` fixed production image references `0.4.19` → `0.4.20`

- [ ] **Step 1: Run the release preflight**

Run:

```bash
git status --short --branch
git fetch origin
python3 -m compileall -q app bridge.py doctor.py
python3 -m unittest discover -s tests -p 'test*.py' -q
git diff --check
```

Expected: clean pre-existing worktree, origin up to date, compile succeeds, zero unittest failures, and no whitespace errors.

- [ ] **Step 2: Update the release metadata**

Set `app/__init__.py` to `__version__ = "0.4.20"`, add the fix to the top of `CHANGELOG.md`, and update only the public fixed image examples in `README.md` and `docs/dockerhub-overview.md` to `0.4.20`.

- [ ] **Step 3: Commit, push, tag, and wait for the image workflow**

Run:

```bash
git add app/__init__.py CHANGELOG.md README.md docs/dockerhub-overview.md
git commit -m "release: publish v0.4.20"
git push origin main
git tag -a v0.4.20 -m "release v0.4.20"
git push origin v0.4.20
run_id=$(gh run list --workflow release-images.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$run_id"
```

Expected: the tagged `release-images.yml` run completes successfully before production rollout.

- [ ] **Step 4: Verify the published multi-architecture image**

Run:

```bash
docker buildx imagetools inspect icekale/cms-tg-ingest:0.4.20
docker buildx imagetools inspect icekale/cms-tg-ingest:latest
```

Expected: both inspect successfully and the version image lists `linux/amd64` and `linux/arm64`.

- [ ] **Step 5: Back up only remote deployment metadata and update the container**

Over SSH to `root@192.168.5.28`, in the existing compose directory, run the configured deployment commands without touching `.env`, `/data`, or media mounts:

```bash
cp -a data "backups/data-before-v0.4.20-$(date +%Y%m%d-%H%M%S)"
cp .env "backups/env-before-v0.4.20-$(date +%Y%m%d-%H%M%S)"
# Change only the cms-tg-ingest image tag to icekale/cms-tg-ingest:0.4.20.
docker compose pull cms-tg-ingest
docker compose up -d --no-build cms-tg-ingest
docker compose ps
```

- [ ] **Step 6: Verify task 414 and container health remotely**

Run:

```bash
docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' cms-tg-ingest
docker exec cms-tg-ingest python /app/doctor.py --quiet
curl -fsS http://127.0.0.1:8788/api/v1/health
```

Then query `/data/tasks.db` read-only. Confirm task 414 is `NEEDS_ACTION` with the existing conflict message, and confirm no new source-delete or share-create operation exists for task 414.
