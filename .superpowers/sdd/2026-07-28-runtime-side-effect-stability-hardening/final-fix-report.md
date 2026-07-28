# Final Review Fix Wave Report

## Status

Complete. All three P1 findings are fixed at baseline
`51d475bc89b5b5ff1b985216f9807a224e4e2f59`. Focused and complete Python
verification passed with `ResourceWarning` promoted to an error. No push,
merge, deploy, production, or Unraid action was performed.

## Commits

- Implementation: `7ec163e4d3089a157d4d0101978bdc3bda2f824e` (`fix: harden runtime side-effect recovery`)
- This report is committed separately after generation so it can record the immutable implementation hash. The report commit is included in the final handoff.

## Files Changed

- `app/workflows/direct.py`: journal direct and source-share CMS mutations and stop unknown outcomes.
- `bridge.py`: inject the durable task store into both CMS workflows.
- `app/background_jobs.py`: redact Basic and quoted or escaped structured credentials.
- `app/web_api.py`: sanitize background-job descriptions at API serialization.
- `app/clients/p115.py`: return an explicit result for ambiguous same-title recovery.
- `app/workflows/self_share.py`: route ambiguous recovery to `needs_action` before settings mutation.
- `tests/test_direct_workflow.py`: direct crash-window, unknown-outcome, and compatibility coverage.
- `tests/test_source_share_workflow.py`: source-share crash-window and unknown-outcome coverage.
- `tests/test_background_jobs.py`: all-path credential redaction and diagnostic preservation coverage.
- `tests/test_self_share_workflow.py`: unique and ambiguous P115 title lookup coverage.
- `tests/test_bridge_task_engine.py`: interleaved two-task same-title recovery coverage.
- `.superpowers/sdd/2026-07-28-runtime-side-effect-stability-hardening/final-fix-report.md`: this report.

## RED Evidence

### Finding 1

Command:

```bash
python3 -m unittest -v tests.test_direct_workflow.DirectWorkflowTests.test_received_resumes_journaled_cms_result_after_submission_persistence_crash tests.test_direct_workflow.DirectWorkflowTests.test_received_started_cms_operation_requires_action_without_second_post tests.test_source_share_workflow.SourceShareTaskWorkflowTests.test_share_sync_resumes_journaled_result_after_submission_persistence_crash tests.test_source_share_workflow.SourceShareTaskWorkflowTests.test_share_sync_started_operation_requires_action_without_second_post
```

Observed: 4 failures. Both crash-window tests made two CMS calls instead of one,
and both pre-started operation tests returned `complete` instead of
`needs_action`.

The direct crash fixture was then tightened to leave a partial submission row:

```bash
python3 -m unittest -v tests.test_direct_workflow.DirectWorkflowTests.test_received_resumes_journaled_cms_result_after_submission_persistence_crash
```

Observed: 1 failure, `failed != complete`, because the legacy missing-task-ID
guard ignored the persisted successful operation result.

### Finding 2

Command:

```bash
python3 -m unittest -v tests.test_background_jobs.BackgroundJobCoordinatorTests.test_redacts_basic_and_quoted_credential_families_everywhere
```

Observed: 10 failing subtests. Basic payloads and quoted or escaped values for
authorization, token, access_token, secret, password, cookie, and set-cookie
remained visible in at least one log, snapshot, runtime-state, completion, or API
representation.

### Finding 3

Command:

```bash
python3 -m unittest -v tests.test_self_share_workflow.CmsPlaybackProbeTests.test_find_own_share_by_title_returns_explicit_ambiguity_for_multiple_eligible_matches
```

Observed: 1 failure. The client returned `task-b-share`, the newest matching
candidate, instead of an explicit ambiguous result.

Command:

```bash
python3 -m unittest -v tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_create_share_recovery_refuses_interleaved_same_title_candidates
```

Observed: 1 failure. Task A remained pending and adopted the truthy ambiguous
payload instead of entering `needs_action`; the unsafe path could reach share
settings mutation.

One initial targeted invocation used the wrong existing test class name for the
client test and produced a selector error. It was corrected to
`CmsPlaybackProbeTests` before production code changed.

## GREEN Verification

Exact CMS regressions:

```bash
python3 -m unittest -v tests.test_direct_workflow.DirectWorkflowTests.test_received_resumes_journaled_cms_result_after_submission_persistence_crash tests.test_direct_workflow.DirectWorkflowTests.test_received_started_cms_operation_requires_action_without_second_post tests.test_source_share_workflow.SourceShareTaskWorkflowTests.test_share_sync_resumes_journaled_result_after_submission_persistence_crash tests.test_source_share_workflow.SourceShareTaskWorkflowTests.test_share_sync_started_operation_requires_action_without_second_post
```

Result: 4 tests passed.

Exact background-job regressions:

```bash
python3 -m unittest -v tests.test_background_jobs.BackgroundJobCoordinatorTests.test_redacts_basic_and_quoted_credential_families_everywhere tests.test_background_jobs.BackgroundJobCoordinatorTests.test_redactor_preserves_ordinary_diagnostic_key_names
```

Result: 2 tests passed.

Exact P115 recovery regressions and retained single-candidate behavior:

```bash
python3 -m unittest -v tests.test_self_share_workflow.CmsPlaybackProbeTests.test_find_own_share_by_title_returns_explicit_ambiguity_for_multiple_eligible_matches tests.test_self_share_workflow.CmsPlaybackProbeTests.test_find_own_share_by_title_returns_the_latest_exact_match tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_create_share_recovery_refuses_interleaved_same_title_candidates tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_create_share_crash_recovers_by_saved_title_without_second_send
```

Result: 4 tests passed.

Direct partial-row and HTTP-200-without-task-ID verification:

```bash
python3 -m unittest -v tests.test_direct_workflow.DirectWorkflowTests.test_received_resumes_journaled_cms_result_after_submission_persistence_crash tests.test_direct_workflow.DirectWorkflowTests.test_received_started_cms_operation_requires_action_without_second_post tests.test_direct_workflow.DirectWorkflowTests.test_received_accepts_cms_200_without_task_id_and_does_not_resubmit
```

Result: 3 tests passed.

Focused suites:

```bash
python3 -W error::ResourceWarning -m unittest -v tests.test_direct_workflow tests.test_source_share_workflow tests.test_background_jobs tests.test_web_api tests.test_self_share_workflow tests.test_bridge_task_engine tests.test_runtime_recovery tests.test_task_store
```

Result: 416 tests passed in 5.333 seconds.

Complete Python suite:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Result: 1,084 tests passed in 14.079 seconds.

Whitespace verification:

```bash
git diff --check
```

Result: passed with no output before the implementation commit. It is rerun
after this report is added.

## Design Notes

### Finding 1: CMS Mutation Journaling

- `DirectTaskWorkflow` and `SourceShareTaskWorkflow` now receive the existing `TaskStore` used by the runner.
- Operation keys include operation generation, update run, operation type, workflow mode, and share code. Requests persist mode plus all endpoint arguments; `prepare_operation` enforces immutable request identity within the task.
- Each remote call is authorized only after `prepared -> started`, and its dictionary response is persisted as `succeeded` before any submission row or stage metadata is advanced.
- A succeeded operation replays only its persisted result. A started result may use the existing direct read-only share-key lookup; without a reliable result, started becomes uncertain and both modes return `needs_action` without another POST.
- Direct CMS HTTP 200 responses without a task ID remain successful and are now stored in the operation result before the existing `submitted_no_task_id` path advances.

### Finding 2: Credential-Safe Background Errors

- Authorization redaction consumes complete Basic and Bearer values rather than only the scheme.
- A structured key/value matcher handles quoted Python and JSON keys, escaped quotes, and the explicit authorization, token, access_token, api_key, secret, password, cookie, and set-cookie families.
- Existing Bearer, query/form, explicit key, URL, and mixed or empty Cookie handling remains in the same sanitizer pipeline.
- The coordinator sanitizes description and exception text before logging, snapshot replacement, runtime-state persistence, and completion callbacks. Web API serialization sanitizes both description and error again at exposure.
- Exact-key matching preserves ordinary diagnostics such as `token_count`, `password_policy`, `authorization_latency_ms`, and `cookie_jar_size`.

### Finding 3: Ambiguous 115 Recovery

- The currently consumed `share/slist` response has no proven stable source-file-to-share correlation in the repository contract, so recovery remains title/time based.
- Zero eligible matches returns pending, exactly one returns that share, and multiple eligible matches return `{recovery_status: ambiguous, match_count: N}` without choosing by age.
- Both legacy pending recovery and journaled create-share recovery convert ambiguity to `needs_action` before `ensure_share_settings` or submission persistence.
- Existing create-time boundary, cache, receive-code, review-window, violation, and single-candidate tests remain green.

## Remaining Concerns

- Same-title recovery intentionally requires manual intervention until 115 exposes a stable, verified source-file/share correlation in the API contract.
- A CMS mutation whose outcome cannot be recovered from a persisted result or the existing direct read-only lookup intentionally stops for manual inspection. It is never retried automatically.
- No production-like external CMS or 115 calls were made; verification used the repository's fake clients and complete local test suite.

## Scoped Re-review Adjudication

The scoped re-review confirmed the CMS replay and ambiguous 115 recovery findings
were closed. It found one valid remaining P1: a quoted structured credential that
contained `,`, `}`, or `]` was terminated at that delimiter, leaving the suffix
visible in logs, snapshots, runtime state, callbacks, and the Web API.

The controller treated this as a load-bearing part of the original credential
safety requirement and applied one surgical TDD correction in commit
`56f4975b` (`fix: redact complete quoted credentials`). No new review scope or
unrelated cleanup was opened.

RED command:

```bash
python3 -m unittest -v tests.test_background_jobs.BackgroundJobCoordinatorTests.test_redacts_basic_and_quoted_credential_families_everywhere
```

Result before the correction: three failing subtests for ordinary JSON, escaped
JSON, and a quoted secret containing closing delimiters.

GREEN evidence:

- Exact delimiter regressions and diagnostic-preservation test: 2 passed.
- `tests.test_background_jobs tests.test_web_api`: 47 passed with
  `ResourceWarning` promoted to an error.
- The reviewer reproduction now maps the complete quoted value to `[redacted]`
  for ordinary and escaped forms.

## Final Controller Gate

- Complete Python suite: 1,084/1,084 passed under
  `-W error::ResourceWarning`.
- Recovery matrix: `tests.test_runtime_recovery` passed five consecutive runs.
- `compileall`, `git diff --check`, and repository secret hygiene: passed.
- Frontend clean install: 57 packages, zero reported vulnerabilities.
- Frontend tests: 2/2 passed.
- Frontend production build: passed; the existing 647.86 kB Vite chunk advisory
  remains non-blocking.
- A final offline image was built by layering the frozen application sources on
  the previously verified `cms-tg-ingest:runtime-stability-check` image, and a
  synthetic non-secret `doctor.py --quiet` run exited 0.

The unchanged standard multi-stage Dockerfile was also invoked. Docker Hub
metadata resolution for `node:22-alpine` and `python:3.12-alpine` produced no
progress for about eight minutes and was canceled. This was an external registry
availability limitation, not a Dockerfile or build-step failure. The frontend
stage and final Python image content were verified independently as described
above. No production service, Unraid container, CMS, or 115 endpoint was touched.
