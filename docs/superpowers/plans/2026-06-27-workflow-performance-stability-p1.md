# Workflow Performance Stability P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce unnecessary 115 API pressure and add safer global cooldown behavior when 115 reports rate-limit/risk-control errors.

**Architecture:** Keep the existing TaskStore authoritative workflow unchanged. Add focused helpers inside the 115 client for conservative early-stop search, bounded organized-folder scans, and reusable 115 risk-control detection; add TaskRunner handling that turns those errors into NEEDS_ACTION with lock release instead of repeated retries.

**Tech Stack:** Python stdlib, unittest, existing app clients/workflows/task runner.

---

### Task 1: 115 Organized-Folder Search Early Stop

**Files:**
- Modify: `app/clients/p115.py`
- Test: `tests/test_self_share_workflow.py`

- [ ] Add a failing test showing `find_organized_folder()` stops after a high-confidence TMDB search hit and does not keep searching later title/year tokens.
- [ ] Implement minimal early-stop behavior: search one token, run `select_organized_115_folder()`, return immediately when selected.
- [ ] Run the targeted unittest and full suite.

### Task 2: Bounded Organized-Folder Tree Scan

**Files:**
- Modify: `app/clients/p115.py`
- Test: `tests/test_self_share_workflow.py`

- [ ] Add a failing test showing `scan_organized_folders()` respects a max listed-folder budget and stops before walking a large tree.
- [ ] Implement a conservative default budget that does not change normal small-tree behavior.
- [ ] Run targeted unittest and full suite.

### Task 3: Global 115 Risk-Control Handling

**Files:**
- Modify: `app/clients/p115.py`
- Modify: `app/workflows/self_share.py`
- Modify: `app/task_runner.py`
- Test: `tests/test_self_share_workflow.py`
- Test: `tests/test_task_runner.py`

- [ ] Add failing tests for reusable 115 risk-control detection from failed API responses and TaskRunner converting those exceptions to NEEDS_ACTION with lock metadata cleared.
- [ ] Implement `P115RiskControlError` and raise it from `_ensure_state()` for known 115 risk-control messages.
- [ ] Reuse the helper in workflow receive handling.
- [ ] Catch `P115RiskControlError` in TaskRunner and record NEEDS_ACTION instead of FAILED/retry.
- [ ] Run targeted tests and full suite.

### Task 4: Documentation

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] Document that 115 queries now use conservative early-stop and bounded scans.
- [ ] Document operational behavior when 115 risk control is detected.
- [ ] Run docs-related tests and full suite.
