# Runtime Side-Effect Stability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已复现的重复执行、外部副作用重放、后台线程失控、SQLite 连接泄漏和备份覆盖问题，使任务在进程崩溃、双 Runner、重复点击和数据库瞬时失败后仍可安全恢复。

**Architecture:** 保留单机 Python、SQLite、现有 `TaskStore` 和分阶段工作流，不引入消息队列或新数据库。Runner 使用唯一 Worker 身份、不可变 claim token 和可续期租约；有成本或不可逆的远端写操作使用持久化操作日志，按“记录意图 -> 标记调用开始 -> 调用远端 -> 保存结果 -> 对账完成”执行。Web 和 Telegram 共用一个单 Worker、有界、防重的后台任务协调器。

**Tech Stack:** Python 3.12、stdlib `sqlite3`/`unittest`/`concurrent.futures`、现有 115/CMS/HDHive/Emby 客户端、SQLite、Vue 3/Naive UI、Docker/Unraid。

## Global Constraints

- 保留现有任务阶段、STRM 三种模式、CMS 整理、115 风控冷却和 Emby 入库行为。
- 不提高 TaskRunner 并发度；后台管理任务固定 `max_workers=1`，总在途任务数上限为 8。
- 不自动重放结果未知且可能重复扣分、重复同步或重复创建资源的远端写操作。
- 可对账的操作自动恢复；不可对账的操作进入明确的 `needs_action`，由用户显式重跑。
- 115 接收、创建分享、CMS 分享同步、115 删除和 HDHive 解锁都必须先持久化操作意图。
- claim 续租只更新 `claim_heartbeat_at`，不能修改 `updated_at`，以免破坏现有 stage result CAS。
- 数据库迁移必须兼容现有 `tasks.db`、`submissions.db` 和 HDHive 表，不删除已有行。
- 不修改 CMS 容器数据库内容；`CmsCloudDataIndex` 继续只读访问。
- 每个行为修复先写失败测试，再写最小实现；每个任务独立提交。
- 发布前必须通过 `ResourceWarning` 作为错误的全量测试、双 Runner 测试、故障注入测试、前端构建和 Docker 构建。

---

## Confirmed Failure Matrix

| Severity | Failure | Current evidence | Required result |
|---|---|---|---|
| P1 | 外部接口成功后本地落库失败会重放 | 115 接收、创建分享、CMS 分享同步、删除、HDHive 解锁均已故障注入复现两次调用 | 重启后先读取操作日志并对账；远端 mutation 不得自动调用第二次 |
| P1 | 两个 Runner 重复执行同一任务 | 固定 `worker_id="task-runner"`，第二个 Runner 启动会清除第一个 Runner claim | 两个 Runner 重叠时同一 claim 只执行一次；活跃租约不可被启动逻辑清除 |
| P2 | Web 重复点击无限创建 daemon thread | 连续 20 次 HDHive 检查创建 20 个线程和 20 次调用 | 同 key 只接受一次；总在途最多 8；只有 1 个执行线程 |
| P2 | TG HDHive 检查阻塞轮询 | callback 中同步调用 `service.check()` | callback 立即返回，结果异步通知，异常可见 |
| P2 | SQLite 连接未关闭 | `with sqlite3.connect()` 离开时只提交/回滚，不关闭，测试出现 `ResourceWarning` | 全量测试在 `-W error::ResourceWarning` 下通过 |
| P2 | 同名数据库备份覆盖 | 两个不同目录的 `state.db` 使用相同目标名 | 每个逻辑数据库生成唯一文件，冲突在写入前失败 |
| P3 | 非法/超大 `Content-Length` 导致无响应或无界读取 | Handler 直接 `int()` 并按声明长度读取 | 非法长度返回 400，超过 64 KiB 返回 413 |

## File Map

- Create `app/sqlite_utils.py`: 统一打开、提交/回滚并关闭 SQLite 连接；提供只读连接和 `quick_check`。
- Create `app/background_jobs.py`: 单 Worker、有界、防重的后台任务协调器和可查询状态。
- Modify `app/models.py`: 增加 claim token/heartbeat 和持久化操作快照模型。
- Modify `app/task_store.py`: claim 租约、操作日志表、CAS token 校验、清理逻辑。
- Modify `app/task_runner.py`: 唯一 Worker ID、活跃 claim 续租、移除启动清 claim。
- Modify `app/quality_automation.py`: 质量任务 claim 传递 token，并保持现有 CAS。
- Modify `app/clients/p115.py`: 将接收和建分享拆成可准备、执行、恢复的步骤；提供删除前存在性对账。
- Modify `app/workflows/self_share.py`: 所有已确认的 115/CMS 写操作接入操作日志。
- Modify `app/hdhive_subscription_store.py`: 持久保存 HDHive 解锁结果和未知结果状态。
- Modify `app/hdhive_subscriptions.py`: 解锁和入队拆成两个可恢复步骤。
- Modify `app/cms_cloud_index.py`, `app/hdhive_cards.py`, `app/backup.py`: 使用正确关闭的 SQLite 上下文。
- Modify `app/web.py`, `app/web_api.py`, `bridge.py`: Web/TG 共用后台协调器；限制请求体；展示后台任务结果。
- Modify focused test modules listed by each task.
- Modify `README.md`, `CHANGELOG.md`, `.github/workflows/ci.yml` only in the final gate task.

---

### Task 1: Close Every Short-Lived SQLite Connection

**Files:**
- Create: `app/sqlite_utils.py`
- Modify: `app/cms_cloud_index.py`
- Modify: `app/hdhive_cards.py`
- Modify: `app/backup.py`
- Test: `tests/test_cms_cloud_index.py`
- Test: `tests/test_hdhive_cards.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- Produces: `sqlite_connection(database, *, uri=False, read_only=False, timeout=30, row_factory=None)` context manager.
- Produces: `sqlite_quick_check(database: str | Path) -> None`, raising `sqlite3.DatabaseError` unless `PRAGMA quick_check` returns exactly `ok`.
- Does not replace the existing private connection helpers in `TaskStore` or `HdhiveSubscriptionStore`; they already close correctly.

- [ ] **Step 1: Add failing close and warning tests**

Patch each module's `sqlite3.connect` with a tracking connection and assert `close()` is called after success and exception paths. Add an integration test that repeatedly calls `CmsCloudDataIndex`, `TmdbDetailCache`, and `backup_sqlite_databases`, then forces collection:

```python
with warnings.catch_warnings():
    warnings.simplefilter("error", ResourceWarning)
    for _ in range(25):
        index.has_file_id("missing")
        cache.get("tv", "1416", lambda: {"id": 1416})
    gc.collect()
```

- [ ] **Step 2: Run the tests and verify the current implementation fails**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tests.test_cms_cloud_index tests.test_hdhive_cards tests.test_backup -v
```

Expected: FAIL with an unclosed SQLite connection or a failed `close()` assertion.

- [ ] **Step 3: Implement the shared context manager**

Use one actual connection object and close it in `finally`:

```python
@contextmanager
def sqlite_connection(database, *, uri=False, read_only=False, timeout=30, row_factory=None):
    connection = sqlite3.connect(database, uri=uri, timeout=timeout)
    if row_factory is not None:
        connection.row_factory = row_factory
    try:
        yield connection
        if not read_only:
            connection.commit()
    except Exception:
        if not read_only:
            connection.rollback()
        raise
    finally:
        connection.close()
```

`sqlite_quick_check()` must open read-only, execute `PRAGMA quick_check`, and raise with the returned diagnostic if it is not `ok`.

- [ ] **Step 4: Replace only the leaking call sites**

Use `read_only=True` for all three `CmsCloudDataIndex` reads. Use normal transactional mode for `TmdbDetailCache` schema/write calls and read-only mode for its lookup. In `backup.py`, ensure both source and temporary target close even when `Connection.backup()` raises.

- [ ] **Step 5: Run focused tests**

Run the Step 2 command again.

Expected: PASS with no `ResourceWarning`.

- [ ] **Step 6: Commit**

```bash
git add app/sqlite_utils.py app/cms_cloud_index.py app/hdhive_cards.py app/backup.py \
  tests/test_cms_cloud_index.py tests/test_hdhive_cards.py tests/test_backup.py
git commit -m "fix: close short-lived sqlite connections"
```

---

### Task 2: Make SQLite Backups Collision-Proof and Verifiable

**Files:**
- Modify: `app/backup.py`
- Modify: `bridge.py`
- Test: `tests/test_backup.py`

**Interfaces:**
- `backup_sqlite_databases()` accepts either `Mapping[str, str | Path]` or the existing iterable of paths.
- Mapping keys are stable logical names. Production uses `submissions` and `tasks`.
- Iterable compatibility derives names from `Path.stem`, but duplicate derived names are rejected before any file is written.
- Every temporary backup must pass `sqlite_quick_check()` before `os.replace()`.

- [ ] **Step 1: Add a same-stem regression test**

Create `one/state.db` and `two/state.db` with different values. Call the iterable compatibility API and assert status is `failed`, `files == []`, both sources appear in the error, and no `state-*.db` exists.

- [ ] **Step 2: Add stable-name and corruption tests**

Call with:

```python
{"submissions": one / "state.db", "tasks": two / "state.db"}
```

Assert two files exist, their names start with `submissions-` and `tasks-`, and each contains the correct source value. Patch `sqlite_quick_check` to raise and assert the target is not published and the `.tmp` file is removed.

- [ ] **Step 3: Run tests and verify same-stem behavior fails**

Run: `python3 -m unittest tests.test_backup -v`

Expected: FAIL because the current implementation reports two successes while only one target survives.

- [ ] **Step 4: Normalize and validate backup sources before writing**

Implement a private normalizer that returns ordered `(logical_name, Path)` pairs, requires names matching `[A-Za-z0-9._-]+`, and rejects duplicate logical names and duplicate final target paths before creating temporary files.

- [ ] **Step 5: Verify each snapshot before atomic publication**

After `source_connection.backup(target_connection)` and both connections close, call `sqlite_quick_check(temporary)`. Only then run `chmod`, `os.replace`, and append to `files`.

- [ ] **Step 6: Wire production names**

Change `create_backup_scheduler()` to pass:

```python
{
    "submissions": Path(config.db_path),
    "tasks": Path(config.task_db_path),
}
```

Keep `BackupScheduler.sources` readable for diagnostics and add `named_sources` if necessary rather than breaking existing callers.

- [ ] **Step 7: Run focused tests and commit**

Run: `python3 -m unittest tests.test_backup tests.test_bridge_v02_integration -v`

Expected: PASS; two same-named source files can no longer overwrite one another.

```bash
git add app/backup.py bridge.py tests/test_backup.py tests/test_bridge_v02_integration.py
git commit -m "fix: prevent sqlite backup name collisions"
```

---

### Task 3: Replace Startup Claim Clearing With Renewable Worker Leases

**Files:**
- Modify: `app/models.py`
- Modify: `app/task_store.py`
- Modify: `app/task_runner.py`
- Modify: `app/quality_automation.py`
- Modify: `app/workflows/self_share.py`
- Modify: `bridge.py`
- Test: `tests/test_task_store.py`
- Test: `tests/test_task_runner.py`
- Test: `tests/test_quality_automation.py`
- Test: `tests/test_bridge_task_engine.py`

**Interfaces:**
- `TaskSnapshot.claim_token: str` is immutable for one claim ownership period.
- `TaskSnapshot.claim_heartbeat_at: float` is renewed while work is active.
- `TaskStore.renew_claim(task_id, expected_claimed_by, expected_claim_token, *, now=None) -> bool` updates only `claim_heartbeat_at`.
- All claimed-write CAS methods receive `expected_claim_token` in addition to the existing stage, worker and version fields.
- Default Worker ID format: `<hostname>:<pid>:<12-char-random>`.

- [ ] **Step 1: Add migration and claim-token tests**

Cover:

- an existing database gains `claim_token` and `claim_heartbeat_at` without losing rows;
- two claims by the same worker at the same timestamp receive different tokens;
- a stale result carrying the first token cannot commit after the second claim;
- renewing a claim changes `claim_heartbeat_at` but not `claimed_at`, `claim_token`, or `updated_at`;
- a claim cannot be stolen while heartbeat is fresh and can be recovered after lease expiry.

- [ ] **Step 2: Add the overlapping Runner regression test**

Use a blocking workflow and two `TaskRunner` instances against the same database. Start Runner A, wait until it enters `run_stage`, then start Runner B and release A. Assert the workflow call count is exactly 1 and the task advances once.

- [ ] **Step 3: Verify current code fails**

Run:

```bash
python3 -m unittest \
  tests.test_task_store.TaskStoreTests.test_claim_heartbeat_prevents_live_claim_recovery \
  tests.test_task_runner.TaskRunnerTests.test_second_runner_does_not_clear_live_claim -v
```

Expected: FAIL because there is no heartbeat/token and startup clears the fixed worker claim.

- [ ] **Step 4: Migrate the task schema**

Add columns with safe defaults:

```sql
claim_token TEXT NOT NULL DEFAULT '';
claim_heartbeat_at REAL NOT NULL DEFAULT 0;
```

For an active legacy claim, set `claim_heartbeat_at=claimed_at` and a deterministic one-time legacy token. Change stale predicates to use `COALESCE(NULLIF(claim_heartbeat_at, 0), claimed_at)`. Every claim-clear path must reset worker, start time, token, and heartbeat together.

- [ ] **Step 5: Add token-aware CAS and renewal**

Generate a new UUID token inside the same `BEGIN IMMEDIATE` transaction that wins the claim. `_claim_matches()` must require the token. Thread the token through `complete_claimed_stage`, `record_event`, `patch_claimed_metadata`, quality reservation completion, and self-share metadata persistence.

- [ ] **Step 6: Make Runner IDs unique and remove startup clearing**

Change the constructor to `worker_id: str | None = None`, generate a unique ID when absent, delete `_startup_claims_cleared` and `_clear_startup_claims_once()`, and do not call `clear_worker_claims()` from `run_once()` or `start()`.

- [ ] **Step 7: Renew the currently active claim**

Keep an `_active_claim` guarded by a lock. The existing heartbeat loop calls `renew_claim()` every 15 seconds while `run_stage()` is active. If renewal fails, log once and let the existing token-aware result CAS discard the stale result.

- [ ] **Step 8: Update production wiring and all claim users**

Create the production runner with a unique ID explicitly. Update quality automation and workflow calls to pass `reserved.claim_token` or `task.claim_token`. Do not weaken token checking for tests or legacy callers.

- [ ] **Step 9: Run focused tests and commit**

Run:

```bash
python3 -m unittest tests.test_task_store tests.test_task_runner \
  tests.test_quality_automation tests.test_bridge_task_engine -v
```

Expected: PASS, including two overlapping Runners and lease expiry recovery.

```bash
git add app/models.py app/task_store.py app/task_runner.py app/quality_automation.py \
  app/workflows/self_share.py bridge.py tests/test_task_store.py tests/test_task_runner.py \
  tests/test_quality_automation.py tests/test_bridge_task_engine.py
git commit -m "fix: use renewable task worker leases"
```

---

### Task 4: Add a Durable Task Operation Journal

**Files:**
- Modify: `app/models.py`
- Modify: `app/task_store.py`
- Test: `tests/test_task_store.py`

**Interfaces:**
- Produces `TaskOperation` with `task_id`, `operation_key`, `operation_type`, `status`, `request`, `result`, `attempt_count`, `last_error`, and timestamps.
- Produces `operation_scope(task) -> str` using `operation_generation` and `update_requested_run`.
- Produces `prepare_operation()`, `start_operation()`, `complete_operation()`, `mark_operation_uncertain()`, `mark_operation_failed()`, and `find_operation()`.
- Valid statuses: `prepared`, `started`, `succeeded`, `uncertain`, `failed`.

- [ ] **Step 1: Add operation lifecycle tests**

Verify:

- prepare is idempotent for `(task_id, operation_key)` and never overwrites the original request;
- only `prepared -> started` increments `attempt_count`;
- completion stores JSON result and survives reopening the database;
- a second start of `started`, `uncertain`, or `succeeded` does not authorize another external call;
- clearing finished task history removes its operations;
- a reprocess increments `operation_generation`, while a series update's `update_requested_run` creates a distinct scope.

- [ ] **Step 2: Run tests and verify missing API failures**

Run: `python3 -m unittest tests.test_task_store -v`

Expected: FAIL because `task_operations` and the lifecycle methods do not exist.

- [ ] **Step 3: Create the operation table**

```sql
CREATE TABLE IF NOT EXISTS task_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    operation_key TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE(task_id, operation_key),
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);
```

Add an index on `(task_id, operation_type, status)` and delete operation rows explicitly from `clear_finished_tasks()` because existing task connections do not rely on SQLite cascade behavior.

- [ ] **Step 4: Implement transactional state transitions**

Each method uses `BEGIN IMMEDIATE`, validates the expected current status in SQL, and returns the persisted row. `prepare_operation()` returns the existing row on conflict only when type and request JSON match; a mismatch raises `ValueError` instead of silently changing the intent.

- [ ] **Step 5: Define operation scopes**

```python
def operation_scope(task: TaskSnapshot) -> str:
    generation = max(0, int(task.metadata.get("operation_generation") or 0))
    update_run = max(0, int(task.metadata.get("update_requested_run") or 0))
    return f"g{generation}:u{update_run}"
```

`build_reprocess_metadata()` increments `operation_generation`; existing series update logic already increments `update_requested_run`.

- [ ] **Step 6: Run tests and commit**

Run: `python3 -m unittest tests.test_task_store tests.test_task_actions -v`

Expected: PASS and existing reprocess behavior remains unchanged except for the new generation metadata.

```bash
git add app/models.py app/task_store.py tests/test_task_store.py tests/test_task_actions.py
git commit -m "feat: persist task side-effect operations"
```

---

### Task 5: Make 115 Share Receive Resumable Without Re-Receiving

**Files:**
- Modify: `app/clients/p115.py`
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_http_clients.py`
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_cloud_workflow.py`

**Interfaces:**
- Produces `P115WebClient.prepare_share_receive(share_code, receive_code, target_cid) -> dict` with source root IDs, names, title, target CID, target pre-call IDs, and snapshot completeness.
- Produces `execute_prepared_share_receive(intent: dict) -> dict`.
- Produces `reconcile_prepared_share_receive(intent: dict) -> dict | None` without calling `/share/receive`.
- Keeps `receive_share_to_cid()` as a compatibility wrapper for non-TaskRunner callers and existing focused client tests.
- Operation key: `<scope>:receive_share:<source share_code>:<target_cid>`.

- [ ] **Step 1: Add client preparation/reconciliation tests**

Assert preparation makes only read calls, the prepared payload is JSON-safe, execution performs exactly one `/share/receive`, and reconciliation finds only items absent from the pre-call target snapshot. Include same-name old files and multi-root shares.

- [ ] **Step 2: Add crash-boundary workflow tests**

Cover these boundaries:

1. intent saved, process stops before the POST;
2. POST succeeds, saving result raises once;
3. operation result saves, TaskRunner stage result is discarded;
4. process restarts from `started` and target reconciliation succeeds.

For cases 2-4 assert `/share/receive` mutation count remains exactly 1. For case 1 assert no automatic POST occurs from an ambiguous `started` operation; it defers and then enters `needs_action` after the bounded recovery window.

- [ ] **Step 3: Verify current workflow repeats the mutation**

Run:

```bash
python3 -m unittest \
  tests.test_bridge_task_engine.BridgeSelfShareTaskWorkflowTests.test_receive_crash_reconciles_without_second_receive -v
```

Expected: FAIL with two receive calls or missing operation methods.

- [ ] **Step 4: Split the client transaction**

Move the share snap and target-directory snapshot into `prepare_share_receive()`. Make execution consume the exact saved source IDs and target CID. Make reconciliation reuse `_resolve_received_root_items()` against a fresh target listing, excluding the saved pre-call IDs.

- [ ] **Step 5: Integrate the operation journal in `_stage_received()`**

The stage sequence is:

```text
prepare remote read data -> persist prepared operation -> mark started
-> call receive once -> persist operation result -> update SubmissionStore
-> return StageResult.complete
```

On `succeeded`, rebuild metadata from the saved result. On `started` or `uncertain`, call reconciliation only; never invoke receive again automatically. Persist `receive_target_cid` in both operation request and task metadata.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
python3 -m unittest tests.test_http_clients tests.test_bridge_task_engine \
  tests.test_cloud_workflow -v
```

Expected: PASS; receive fault-injection tests report one mutation.

```bash
git add app/clients/p115.py app/workflows/self_share.py tests/test_http_clients.py \
  tests/test_bridge_task_engine.py tests/test_cloud_workflow.py
git commit -m "fix: reconcile interrupted 115 share receives"
```

---

### Task 6: Journal Share Creation, CMS Share Sync, and 115 Cleanup

**Files:**
- Modify: `app/clients/p115.py`
- Modify: `app/workflows/self_share.py`
- Test: `tests/test_http_clients.py`
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_self_share_workflow.py`

**Interfaces:**
- Produces `P115WebClient.create_share(file_id) -> dict` for `/share/send` only.
- Produces `P115WebClient.ensure_share_settings(share_code, receive_code) -> dict` for idempotent unlimited-duration/password configuration.
- Keeps `create_long_share()` as `create_share()` plus `ensure_share_settings()` compatibility behavior.
- Produces `P115WebClient.file_exists_in_parent(file_id, parent_id) -> bool` for deletion reconciliation.
- Operation types: `create_share`, `cms_share_sync`, `delete_source`, `delete_residue`.

- [ ] **Step 1: Add create-share crash tests**

Inject a failure after `/share/send` succeeds but before local operation result persistence. On restart, `find_own_share_by_title()` must recover the code using the persisted title and minimum request timestamp; `/share/send` remains one call; `ensure_share_settings()` configures password and unlimited duration.

Add a second test where the first source file is confirmed gone and the direct-file fallback uses a distinct operation key ending in its file ID.

- [ ] **Step 2: Add CMS sync unknown-outcome tests**

Persist `started`, call `add_share115_sync_task()`, then discard the local result. On restart assert CMS POST count remains 1. The task advances to STRM polling with `cms_share_sync_outcome="unknown"`; if expected STRM appears it continues normally, otherwise the existing bounded STRM wait ends in `needs_action` rather than resubmitting.

- [ ] **Step 3: Add deletion reconciliation tests**

After a successful `delete_file()`, fail local completion once. On restart, list the saved parent CID, observe that the file ID is absent, and mark cleanup succeeded without a second delete. If the listing fails, defer; if the file remains after the recovery deadline, enter `needs_action`. Apply the same rule to receive-stage residue files.

- [ ] **Step 4: Run tests and verify current duplicate calls**

Run:

```bash
python3 -m unittest tests.test_bridge_task_engine tests.test_self_share_workflow \
  -k "crash or operation or reconcile" -v
```

Expected: FAIL because the current methods call the remote mutation before durable intent/result storage.

- [ ] **Step 5: Split share send from share configuration**

Persist the send intent with `file_id`, exact share title, receive code, and `requested_at`. Mark started immediately before `/share/send`. Save the returned share code before calling the idempotent settings endpoint. A `started` operation only performs title/time reconciliation; it never sends a second create request.

- [ ] **Step 6: Make CMS submission at-most-once**

Persist and start the operation before `add_share115_sync_task()`. A saved `succeeded` operation reuses its response. A `started` operation after restart is marked `uncertain` and moves to output reconciliation; no automatic second CMS POST is allowed because CMS provides no idempotency key or queryable share-sync task ID.

- [ ] **Step 7: Make cleanup absence-aware**

Persist `file_id` and `parent_id` before deletion. `succeeded` is immediately reusable. For `started`, confirm absence from the parent; absence is success, presence is unknown and must not trigger an automatic second delete. Known API “file not found” responses also count as success.

- [ ] **Step 8: Run focused tests and commit**

Run:

```bash
python3 -m unittest tests.test_http_clients tests.test_bridge_task_engine \
  tests.test_self_share_workflow tests.test_invalid_share_cleanup -v
```

Expected: PASS; each injected mutation counter is 1.

```bash
git add app/clients/p115.py app/workflows/self_share.py tests/test_http_clients.py \
  tests/test_bridge_task_engine.py tests/test_self_share_workflow.py \
  tests/test_invalid_share_cleanup.py
git commit -m "fix: make self-share side effects recoverable"
```

---

### Task 7: Persist HDHive Unlock Results Before Intake

**Files:**
- Modify: `app/hdhive_subscription_store.py`
- Modify: `app/hdhive_subscriptions.py`
- Test: `tests/test_hdhive_subscription_store.py`
- Test: `tests/test_hdhive_subscriptions.py`

**Interfaces:**
- Adds item fields `unlocked_url`, `unlock_state`, `unlock_requested_at`, and `enqueue_started_at`.
- Adds status `unlocked` between `unlocking` and `enqueued`.
- Produces `mark_item_unlocked(item_id, full_url, points_spent, points_source, unlocked_at) -> HdhiveSubscriptionItem`.
- A stale `unlocking` item without a saved URL becomes `pending_confirmation` with `skip_reason="unlock_outcome_unknown"`; it is not automatically charged again.

- [ ] **Step 1: Add schema migration and result persistence tests**

Open a legacy HDHive database, verify all new columns appear, save an unlocked URL/cost/time, reopen the store, and assert the values survive. Verify URLs are not exposed by `repr()` or normal log messages.

- [ ] **Step 2: Replace the stale-unlock replay regression**

Change the existing `test_stale_unlocking_item_is_retried` expectation. A stale item with no persisted result must not call `proxy.unlock`; it becomes pending confirmation with an explicit “解锁结果未知，禁止自动重复扣分” error.

- [ ] **Step 3: Add unlock-then-enqueue crash tests**

Cover:

- unlock result saves, then `enqueue_links` raises;
- process restarts with status `unlocked`;
- the saved URL is enqueued without a second unlock call;
- intake deduplication returns the same TaskStore task ID;
- points and unlock time remain the original values.

- [ ] **Step 4: Run tests and verify current behavior repeats unlock**

Run:

```bash
python3 -m unittest tests.test_hdhive_subscription_store \
  tests.test_hdhive_subscriptions -v
```

Expected: FAIL because URL/result state is currently saved only after intake succeeds.

- [ ] **Step 5: Split subscription processing into unlock and intake phases**

After claiming an item, record `unlock_requested_at` before the proxy call. Validate the returned URL, compute actual/estimated points, then commit `status="unlocked"` and the full result. Only a later block calls `enqueue_links()` from the saved URL and marks `enqueued`.

- [ ] **Step 6: Define safe unknown-result behavior**

If the process restarts from `unlocking` without `unlocked_url`, do not invoke unlock automatically. Mark the item pending confirmation. A user-confirmed retry resets the item to a fresh unlock attempt; that explicit action is the authorization boundary for possible repeated cost.

- [ ] **Step 7: Run focused tests and commit**

Run:

```bash
python3 -m unittest tests.test_hdhive_subscription_store \
  tests.test_hdhive_subscriptions tests.test_hdhive_cards -v
```

Expected: PASS; injected intake failure produces one unlock call and preserves points/time.

```bash
git add app/hdhive_subscription_store.py app/hdhive_subscriptions.py \
  tests/test_hdhive_subscription_store.py tests/test_hdhive_subscriptions.py \
  tests/test_hdhive_cards.py
git commit -m "fix: persist hdhive unlocks before intake"
```

---

### Task 8: Use One Bounded Background Job Coordinator for Web and Telegram

**Files:**
- Create: `app/background_jobs.py`
- Create: `tests/test_background_jobs.py`
- Modify: `app/web.py`
- Modify: `app/web_api.py`
- Modify: `bridge.py`
- Test: `tests/test_web_admin.py`
- Test: `tests/test_web_api.py`
- Test: `tests/test_hdhive_web.py`
- Test: `tests/test_hdhive_bridge.py`
- Test: `tests/test_quality_telegram.py`

**Interfaces:**
- `BackgroundJobCoordinator(max_workers=1, max_in_flight=8, state_store=None)`.
- `submit(key, callable, *, description="", on_complete=None) -> JobSubmission`.
- `snapshot(key) -> BackgroundJobSnapshot | None` and `list_snapshots() -> tuple[...]`.
- Dedupe keys: `quality:run`, `hdhive:run`, `hdhive:subscription:<id>`, `hdhive:item:<id>`.
- Submission outcomes: `accepted`, `already_running`, `capacity_rejected`, `closed`.

- [ ] **Step 1: Add coordinator unit tests**

Create `tests/test_background_jobs.py` and verify:

- 20 submissions with one key execute the callable once;
- one blocking job plus 20 unique jobs never exceeds 8 in flight;
- executor thread count remains 1;
- exceptions produce `status="failed"`, preserve a short error, and invoke completion callback;
- `shutdown(wait=True)` rejects new work and joins the worker.

- [ ] **Step 2: Add Web duplicate-click tests**

Send 20 `/api/v1/hdhive/subscriptions/7/check` requests while the first service call blocks. Assert exactly one is accepted, the rest report already running, and `service.check_calls == [7]`. Repeat for quality run, all subscriptions, and item confirmation.

- [ ] **Step 3: Add non-blocking TG tests**

Use an Event-blocking `service.check()`. Call the TG callback handler and assert it acknowledges before the Event is released. On completion, verify a success/failure Telegram message is sent and the polling thread was never blocked by the service call.

- [ ] **Step 4: Verify current code fails and creates multiple threads**

Run:

```bash
python3 -m unittest tests.test_hdhive_web tests.test_hdhive_bridge \
  tests.test_quality_telegram -v
```

Expected: FAIL with 20 service calls or a blocked callback.

- [ ] **Step 5: Implement the bounded coordinator**

Use `ThreadPoolExecutor(max_workers=1)` plus a `BoundedSemaphore(8)`. Hold a lock while checking active keys and reserving capacity. The wrapper records queued/running/succeeded/failed timestamps, logs exceptions, persists a redacted JSON snapshot in `runtime_state` when a store is provided, releases key/capacity in `finally`, and calls `on_complete` outside the lock.

- [ ] **Step 6: Replace all per-request threads**

Replace direct `Thread(...)` calls at Web quality run, HDHive run, subscription check, and item confirm routes. API routes return 202 for accepted work, 409 for a duplicate, and 429 for capacity rejection. Legacy POST routes keep 303 redirects but render the latest coordinator state on the target page.

- [ ] **Step 7: Route TG through the same instance**

Create one coordinator in `bridge.run_forever()`, inject it into Web and TG handlers, and shut it down in the existing `finally` path. TG callbacks enqueue a job and answer immediately; completion callbacks send the formatted result.

- [ ] **Step 8: Expose useful status without a UI redesign**

Add the latest quality/HDHive background job status to existing API serialization and legacy page status bands. Show only description, state, start/end time, and redacted error; never include URLs, tokens, cookies, or full unlock payloads.

- [ ] **Step 9: Run focused tests and commit**

Run:

```bash
python3 -m unittest tests.test_background_jobs tests.test_web_admin tests.test_web_api \
  tests.test_hdhive_web tests.test_hdhive_bridge tests.test_quality_telegram -v
```

Expected: PASS; repeated requests are deduplicated and only one worker thread executes jobs.

```bash
git add app/background_jobs.py app/web.py app/web_api.py bridge.py \
  tests/test_background_jobs.py tests/test_web_admin.py tests/test_web_api.py \
  tests/test_hdhive_web.py tests/test_hdhive_bridge.py tests/test_quality_telegram.py
git commit -m "fix: bound and deduplicate background jobs"
```

---

### Task 9: Bound Web Request Bodies and Handle Invalid Lengths

**Files:**
- Modify: `app/web.py`
- Test: `tests/test_web_admin.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Constant `MAX_REQUEST_BODY_BYTES = 64 * 1024`.
- Invalid, negative, or non-decimal `Content-Length` returns HTTP 400.
- A declared or actual body over the limit returns HTTP 413.

- [ ] **Step 1: Add parser and integration tests**

Test missing, zero, negative, alphabetic, exactly 65536, and 65537-byte lengths. Use a raw local socket against `start_web_server(..., port=0)` for malformed headers so behavior is verified at the real Handler boundary.

- [ ] **Step 2: Run tests and verify malformed input currently breaks the request**

Run: `python3 -m unittest tests.test_web_admin tests.test_web_api -k "content_length or body_limit" -v`

Expected: FAIL because `int(Content-Length)` raises or the body is read without a bound.

- [ ] **Step 3: Add a small parsing helper and defense in depth**

```python
def parse_content_length(value: str | None, limit: int = MAX_REQUEST_BODY_BYTES) -> int:
    if value in (None, ""):
        return 0
    length = int(value, 10)
    if length < 0:
        raise ValueError("negative content length")
    if length > limit:
        raise RequestBodyTooLarge
    return length
```

The Handler converts errors to 400/413 before reading. `WebApp.handle_request()` independently rejects `len(body) > MAX_REQUEST_BODY_BYTES` so direct/internal callers have the same limit.

- [ ] **Step 4: Run focused tests and commit**

Run: `python3 -m unittest tests.test_web_admin tests.test_web_api -v`

Expected: PASS; malformed requests receive a deterministic response.

```bash
git add app/web.py tests/test_web_admin.py tests/test_web_api.py
git commit -m "fix: bound web request bodies"
```

---

### Task 10: Full Regression, Fault Injection, Documentation, and Release Gate

**Files:**
- Create: `tests/test_runtime_recovery.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: all Python and frontend tests

**Interfaces:**
- CI must fail on `ResourceWarning`.
- Runtime docs explain lease recovery, unknown external-operation states, bounded background jobs, and collision-proof backups.
- This task verifies release readiness; it does not push GitHub/Docker Hub or mutate Unraid unless the user separately authorizes deployment.

- [ ] **Step 1: Add one integrated fault-injection test module**

Create `tests/test_runtime_recovery.py` with table-driven cases for receive, create share, CMS sync, delete, and HDHive unlock. Each case injects failure at intent/start/result/stage-commit boundaries, reconstructs stores/services, resumes, and asserts the external mutation count is at most 1. Also include the two overlapping Runner scenario.

- [ ] **Step 2: Run the integrated recovery tests repeatedly**

Run:

```bash
for run in 1 2 3 4 5; do
  python3 -m unittest tests.test_runtime_recovery tests.test_task_runner -q || exit 1
done
```

Expected: all five runs pass without timing flakes, duplicate mutations, or leaked threads.

- [ ] **Step 3: Run the complete Python suite with warnings as errors**

Run:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Expected: at least the existing 979 tests plus all new tests PASS; zero `ResourceWarning`, unclosed database warnings, thread exceptions, or skipped recovery tests.

- [ ] **Step 4: Run static, frontend, and repository checks**

Run:

```bash
python3 -m compileall -q bridge.py app tests
npm test --prefix frontend
npm run build --prefix frontend
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Keep CI gates explicit**

Confirm `.github/workflows/ci.yml` runs the warning-as-error Python suite and Docker build. Add the frontend test/build commands only if they are not already present. Do not weaken or remove an existing gate to make the branch pass.

- [ ] **Step 6: Document operational behavior**

Update README troubleshooting with:

- `started/uncertain` means the remote result is unknown and is intentionally not replayed;
- how to inspect the task operation/event history before explicitly reprocessing;
- background manual jobs are single-worker, deduplicated, and capped at 8;
- backup filenames are `submissions-<UTC>.db` and `tasks-<UTC>.db` and are quick-checked;
- a second container must not be used to increase concurrency, although leases prevent duplicate ownership.

Add a concise CHANGELOG entry under the next unreleased version without including real credentials or private URLs.

- [ ] **Step 7: Build and smoke-test the image locally**

Run:

```bash
docker build --pull=false -t cms-tg-ingest:runtime-stability-check .
docker run --rm --entrypoint python cms-tg-ingest:runtime-stability-check /app/doctor.py --quiet
```

Expected: image builds and doctor exits 0.

- [ ] **Step 8: Validate migrations on copies of production databases**

Copy, do not move, the current Unraid `tasks.db` and `submissions.db` into a temporary validation directory. Start the new image against only those copies, run doctor, inspect `PRAGMA quick_check`, confirm task/subscription row counts match pre-migration values, and confirm `task_operations`, claim columns, and HDHive unlock columns exist.

Expected: no production mount is written during this validation.

- [ ] **Step 9: Define the Unraid deployment gate**

Before any later deployment:

1. stop only `cms-tg-ingest`;
2. take verified backups of both runtime databases;
3. deploy exactly one new container;
4. run `doctor.py --quiet`;
5. confirm one TaskRunner identity in logs and no startup claim-clearing message;
6. submit one non-destructive test link only with user approval;
7. verify one receive, one share create, one CMS share sync, one cleanup, and the expected Emby result;
8. retain the previous image tag and backups for rollback.

- [ ] **Step 10: Commit documentation and gates**

```bash
git add tests/test_runtime_recovery.py .github/workflows/ci.yml README.md CHANGELOG.md
git commit -m "test: gate runtime recovery and release"
```

---

## Final Acceptance Checklist

- [ ] Two overlapping TaskRunners execute one claimed task exactly once.
- [ ] A live claim remains owned while its heartbeat is fresh and is recoverable only after expiry.
- [ ] 115 receive, share creation, CMS sync, source deletion, residue deletion, and HDHive unlock each have a durable pre-call record.
- [ ] Every crash-boundary fault-injection test reports no more than one costly/irreversible mutation.
- [ ] Unknown CMS/HDHive outcomes stop safely and are visible instead of being silently replayed.
- [ ] 20 repeated Web requests execute one matching job and do not create 20 threads.
- [ ] TG polling remains responsive during a blocking HDHive check.
- [ ] Same-stem SQLite sources cannot overwrite one another during backup.
- [ ] Every published backup passes `PRAGMA quick_check`.
- [ ] Full Python suite passes with `ResourceWarning` promoted to error.
- [ ] Frontend tests/build, Docker build, doctor, migration-copy validation, and `git diff --check` pass.
- [ ] No secret, 115 cookie, TG token, API key, private share URL, or Unraid credential appears in code, fixtures, logs, docs, or commits.
