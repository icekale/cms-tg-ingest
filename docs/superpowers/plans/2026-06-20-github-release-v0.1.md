# GitHub Release v0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current working `cms-tg-ingest` deployment into a safe GitHub-ready Docker project.

**Architecture:** Keep the runtime as a standard-library Python service. Add offline diagnostics, Docker packaging, example configuration, documentation, and repository hygiene around the existing bridge without changing the proven runtime workflow.

**Tech Stack:** Python 3.12 standard library, Docker Compose, unittest, shell scripts.

---

### Task 1: Export Current Runtime

**Files:**
- Create: `bridge.py`
- Create: `tests/test_openai_fallback.py`
- Create: `tests/test_quality_checks.py`
- Create: `tests/test_self_share_workflow.py`

- [x] Copy deployed `bridge.py` from Unraid.
- [x] Copy deployed test files from Unraid.
- [x] Run `python3 -W error::ResourceWarning -m unittest discover -s tests -v` and confirm the baseline passes.

### Task 2: Add Offline Doctor

**Files:**
- Create: `doctor.py`
- Create: `tests/test_doctor.py`

- [x] Write failing tests for missing required env, valid self-share config, and CLI exit status.
- [x] Run `python3 -m unittest tests/test_doctor.py -v` and confirm failure because `doctor.py` is missing.
- [x] Implement `doctor.run_checks()` and `doctor.main()`.
- [x] Run `python3 -m unittest tests/test_doctor.py -v` and confirm pass.

### Task 3: Add Packaging and Docs

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `.dockerignore`
- Create: `README.md`
- Create: `LICENSE`
- Create: `scripts/diagnostics.sh`
- Create: `.github/ISSUE_TEMPLATE/bug_report.md`

- [ ] Add Docker packaging that runs `bridge.py` by default and supports `python /app/doctor.py` for diagnostics.
- [ ] Add a compose example with all sensitive values sourced from `.env`.
- [ ] Document Unraid mount examples and the self-share workflow.
- [ ] Add a diagnostics script that redacts token/key/password/cookie values.

### Task 4: Verify Release Candidate

**Commands:**
- `python3 -m py_compile bridge.py doctor.py`
- `python3 -W error::ResourceWarning -m unittest discover -s tests -v`
- `grep -R` secret pattern scan over tracked text files

- [ ] Confirm Python files compile.
- [ ] Confirm all tests pass.
- [ ] Confirm no obvious real secrets are present.
