# HDHive Series Subscriptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent HDHive TV subscriptions that accept TMDB candidates or `hdhive.com/tv/<slug>` URLs, check daily at 01:30 Asia/Shanghai, unlock one best new 115 resource per episode, and reuse the existing ingest workflow.

**Architecture:** Add a small SQLite-backed subscription store in the existing task database, a subscription service for page resolution, resource selection, cost confirmation, and intake delegation, and one scheduler thread using the same local-time lease pattern as quality automation. Telegram and Web call the service; neither implements CMS, 115, STRM, or Emby behavior.

**Tech Stack:** Python 3, `urllib`/`HttpJson`, dataclasses, SQLite, existing Telegram Bot HTTP API, existing `HdhiveProxyClient`, `TaskStore`, `unittest`/`pytest`, Docker Compose.

---

## File Map

- Create: `app/hdhive_subscription_store.py` for SQLite schema, subscription/item/run models, deduplication, leases, and runtime settings.
- Create: `app/hdhive_subscriptions.py` for HDHive TV URL parsing, resource ranking, subscription service, and daily scheduler.
- Modify: `app/clients/hdhive.py` to resolve a public HDHive TV page into a TMDB TV ID and normalize episode metadata.
- Modify: `app/config.py` for subscription defaults and validation.
- Modify: `bridge.py` to recognize direct HDHive TV URLs, construct the service/scheduler, and delegate successful unlock URLs to the existing intake callback.
- Modify: `app/telegram_ui.py` for subscription list/status and action keyboards.
- Modify: `app/web.py` for the HDHive management page and subscription actions.
- Modify: `.env.example`, `README.md`, `PRODUCT.md`, and `docs/dockerhub-overview.md` for deployment and usage.
- Modify: `doctor.py` for subscription database/settings health checks.
- Create: `tests/test_hdhive_subscription_store.py` for persistence and lease behavior.
- Create: `tests/test_hdhive_subscriptions.py` for URL parsing, resource ranking, cost rules, and scheduler behavior.
- Modify: `tests/test_hdhive_client.py` for HDHive page resolution and episode field normalization.
- Modify: `tests/test_hdhive_bridge.py` for direct URL intake and Telegram subscription callbacks.
- Modify: `tests/test_web_admin.py` or create `tests/test_hdhive_web.py` for Web subscription actions.
- Modify: `tests/test_docs_v02.py` or create `tests/test_hdhive_subscription_docs.py` for configuration/documentation examples.

Do not modify or reset the existing unrelated worktree changes in `app/clients/p115.py`, `app/cms_cloud_index.py`, `app/workflows/self_share.py`, or their tests.

### Task 1: Add HDHive TV URL Resolution And Configuration

**Files:**
- Modify: `app/clients/hdhive.py`
- Modify: `app/config.py`
- Modify: `.env.example`
- Test: `tests/test_hdhive_client.py`
- Test: `tests/test_hdhive_subscriptions.py`

- [ ] **Step 1: Write failing URL and page metadata tests.**

Add tests for the exact accepted and rejected inputs:

```python
def test_parse_hdhive_tv_url_accepts_only_hdhive_tv_pages():
    assert parse_hdhive_tv_url(
        "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5"
    ).slug == "542a1c1fe6ac4a5aab152369079596b5"
    with pytest.raises(HdhiveUrlError):
        parse_hdhive_tv_url("https://hdhive.com/movie/abc")
    with pytest.raises(HdhiveUrlError):
        parse_hdhive_tv_url("https://evil.example/tv/abc")
```

Use an HTML fixture containing the server-rendered fields around the supplied example and assert that the parser returns `tmdb_id="255358"`, title `攻壳机动队`, and year `2026`. Add a test proving a missing TMDB ID raises `HDHIVE_PAGE_UNRESOLVED`.

- [ ] **Step 2: Run the focused tests and verify the intended failures.**

Run:

```bash
pytest -q tests/test_hdhive_client.py tests/test_hdhive_subscriptions.py -k 'url or page'
```

Expected: import or attribute failures for `parse_hdhive_tv_url`, `HdhiveUrlError`, and page resolution because the API is not implemented.

- [ ] **Step 3: Implement URL parsing and page resolution.**

Add these public types and methods:

```python
@dataclass(frozen=True)
class HdhiveTvUrl:
    slug: str
    url: str

@dataclass(frozen=True)
class HdhiveTvPage:
    slug: str
    tmdb_id: str
    title: str
    year: str
    url: str

def parse_hdhive_tv_url(url: str) -> HdhiveTvUrl:
    """Validate the URL shape without fetching the page."""

class HdhiveProxyClient:
    def resolve_tv_page(self, url: str) -> HdhiveTvPage:
        """Fetch public server-rendered HTML and extract the page TV metadata."""
```

Use `urllib.parse.urlsplit` and require host `hdhive.com` or `www.hdhive.com`, path exactly `/tv/<slug>`, and an ASCII slug of 8-96 alphanumeric characters. Inject a `page_fetcher` callable in tests; production uses one GET with the existing HTTP timeout and a browser-like `Accept` header. Extract only the serialized page object associated with the validated slug, then validate a numeric TMDB ID. Do not log page HTML or auth tokens.

Extend `HdhiveResource` normalization to retain optional `season_number`, `episode_number`, and `episode_key` fields when the proxy provides them. Existing constructor call sites remain valid by giving the new fields defaults.

- [ ] **Step 4: Add configuration defaults.**

Add to `Config` and `Config.from_env()`:

```python
hdhive_subscription_auto_enabled: bool = True
hdhive_subscription_time: str = "01:30"
hdhive_subscription_timezone: str = "Asia/Shanghai"
```

Validate the time as `HH:MM` and the timezone with `ZoneInfo`, using the same validation style as `QUALITY_AUTO_TIME` and `QUALITY_AUTO_TIMEZONE`. Add these variables, disabled HDHive by default, to `.env.example`:

```env
HDHIVE_SUBSCRIPTION_AUTO_ENABLED=true
HDHIVE_SUBSCRIPTION_TIME=01:30
HDHIVE_SUBSCRIPTION_TIMEZONE=Asia/Shanghai
```

- [ ] **Step 5: Run the client/config tests.**

Run:

```bash
pytest -q tests/test_hdhive_client.py tests/test_hdhive_subscriptions.py -k 'url or page or config'
```

Expected: PASS, including rejection of non-HD Hive hosts and malformed times.

- [ ] **Step 6: Commit the page/config slice.**

```bash
git add app/clients/hdhive.py app/config.py .env.example tests/test_hdhive_client.py tests/test_hdhive_subscriptions.py
git commit -m "feat: resolve HDHive TV subscription URLs"
```

### Task 2: Add Persistent Subscription Storage

**Files:**
- Create: `app/hdhive_subscription_store.py`
- Test: `tests/test_hdhive_subscription_store.py`

- [ ] **Step 1: Write failing persistence and uniqueness tests.**

Cover the required behaviors:

```python
def test_create_from_same_chat_and_source_is_idempotent(tmp_path):
    store = HdhiveSubscriptionStore(tmp_path / "tasks.db")
    first = store.create_subscription("464100862", "hdhive_tv", "slug-1", "剧集", "255358")
    second = store.create_subscription("464100862", "hdhive_tv", "slug-1", "剧集", "255358")
    assert first.id == second.id
    assert len(store.list_subscriptions("464100862")) == 1

def test_item_state_and_task_id_survive_new_store_instance(tmp_path):
    path = tmp_path / "tasks.db"
    store = HdhiveSubscriptionStore(path)
    subscription = store.create_subscription("464100862", "tmdb_tv", "255358", "剧集", "255358")
    item = store.upsert_item(subscription.id, "s01e01", "resource-1", "valid", 1080, 8)
    store.mark_item_enqueued(item.id, 42)
    reopened = HdhiveSubscriptionStore(path)
    assert reopened.get_item(item.id).status == "enqueued"
    assert reopened.get_item(item.id).task_id == 42
```

Add tests for pause/resume/delete, pending confirmation records, and atomic `claim_daily_run("2026-07-25", ...)` returning true once and false for a second caller.

- [ ] **Step 2: Run the store tests and verify they fail.**

Run `pytest -q tests/test_hdhive_subscription_store.py` and confirm the missing store class/method failures.

- [ ] **Step 3: Create the SQLite schema and typed models.**

Define frozen dataclasses `HdhiveSubscription`, `HdhiveSubscriptionItem`, and `HdhiveSubscriptionRun`. On initialization create:

```sql
CREATE TABLE IF NOT EXISTS hdhive_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_value TEXT NOT NULL,
  source_url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  tmdb_id TEXT NOT NULL,
  media_type TEXT NOT NULL DEFAULT 'tv',
  pan_type TEXT NOT NULL DEFAULT '115',
  status TEXT NOT NULL DEFAULT 'active',
  last_checked_at REAL NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(chat_id, source_type, source_value)
);
CREATE TABLE IF NOT EXISTS hdhive_subscription_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id INTEGER NOT NULL,
  episode_key TEXT NOT NULL,
  resource_slug TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  validate_status TEXT NOT NULL DEFAULT '',
  resolution_score INTEGER NOT NULL DEFAULT 0,
  unlock_points INTEGER,
  status TEXT NOT NULL DEFAULT 'discovered',
  task_id INTEGER,
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(subscription_id, episode_key, resource_slug),
  FOREIGN KEY(subscription_id) REFERENCES hdhive_subscriptions(id)
);
CREATE TABLE IF NOT EXISTS hdhive_subscription_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  run_date TEXT NOT NULL,
  status TEXT NOT NULL,
  summary_json TEXT NOT NULL DEFAULT '{}',
  started_at REAL NOT NULL,
  finished_at REAL,
  UNIQUE(run_date)
);
```

Use a connection-local `busy_timeout`, a process lock around writes, and `INSERT ... ON CONFLICT` for idempotency. Store one item per subscription/episode/resource candidate so a later check can compare a new higher-quality resource against an older pending candidate. `claim_daily_run` inserts one global `run_date` lease; per-subscription results are kept in `summary_json`. Add methods for create/list/get, status changes, settings, item upsert/claim/mark, pending confirmations, and run lease. Do not store OAuth tokens or duplicate unlocked URLs in subscription tables.

- [ ] **Step 4: Implement the minimum store methods and make the tests pass.**

The public store surface must include:

```python
create_subscription(chat_id, source_type, source_value, title, tmdb_id, source_url="")
list_subscriptions(chat_id: str | None = None, include_deleted: bool = False)
get_subscription(subscription_id: int)
set_status(subscription_id: int, status: str)
upsert_item(subscription_id, episode_key, resource_slug, validate_status, resolution_score, unlock_points, title="")
list_items(subscription_id: int)
get_item(item_id: int)
mark_item_pending(item_id: int, error: str = "")
mark_item_enqueued(item_id: int, task_id: int)
mark_item_failed(item_id: int, error: str)
claim_daily_run(run_date: str, run_id: str, now: float) -> bool
```

- [ ] **Step 5: Run all focused storage tests and commit.**

Run `pytest -q tests/test_hdhive_subscription_store.py`. Expected: PASS.

```bash
git add app/hdhive_subscription_store.py tests/test_hdhive_subscription_store.py
git commit -m "feat: persist HDHive subscriptions in TaskStore database"
```

### Task 3: Implement Resource Selection And Subscription Service

**Files:**
- Create: `app/hdhive_subscriptions.py`
- Modify: `app/clients/hdhive.py`
- Test: `tests/test_hdhive_subscriptions.py`

- [ ] **Step 1: Write failing policy and service tests.**

Use fake proxy, fake page resolver, fake subscription store, and an intake callback. Cover:

```python
def test_select_best_resource_uses_validity_then_resolution_then_cost():
    selected = select_best_resource([
        resource("invalid-2160", status="invalid", resolution="2160P", points=0),
        resource("unknown-2160", status="", resolution="2160P", points=1),
        resource("valid-1080-expensive", status="valid", resolution="1080P", points=20),
        resource("valid-720-cheap", status="valid", resolution="720P", points=1),
    ])
    assert selected.slug == "valid-1080-expensive"

def test_new_low_cost_resource_unlocks_and_enters_existing_intake_once():
    result = service.check_subscription(subscription_id)
    assert result.enqueued == 1
    assert intake_calls == [["https://115cdn.com/s/new?password=abcd"]]

def test_high_cost_resource_is_pending_without_unlock_call():
    result = service.check_subscription(subscription_id)
    assert result.pending_confirmation == 1
    assert proxy.unlock_calls == []
```

Also test the `20` boundary, already-owned resources, explicit invalid resources, episode key fallback, and one subscription failure not stopping the scheduler's next subscription.

- [ ] **Step 2: Run policy/service tests and confirm the intended failures.**

Run `pytest -q tests/test_hdhive_subscriptions.py` and confirm the missing policy/service symbols fail before production implementation.

- [ ] **Step 3: Implement deterministic resource ranking.**

Add:

```python
def episode_key(resource: HdhiveResource) -> str:
    """Prefer structured season/episode fields, then parse SxxEyy, then slug."""

def resolution_score(resource: HdhiveResource) -> int:
    """Return the maximum numeric score from 8K/4K/2160P/1080P/... values."""

def select_best_resource(resources: list[HdhiveResource]) -> HdhiveResource | None:
    """Sort valid status first, resolution descending, points ascending."""
```

Exclude status values explicitly equal to `invalid`, `expired`, or `unavailable`. Sort the remaining resources by `(is_valid_status, resolution_score, -known_cost)` with stable slug tie-breaking. Keep already-owned resources eligible even when their cost is absent.

- [ ] **Step 4: Implement subscription creation and checking.**

Define `HdhiveSubscriptionService` with this surface:

```python
class HdhiveSubscriptionService:
    def create_from_url(self, chat_id: str, url: str) -> HdhiveSubscription: ...
    def create_from_tmdb(self, chat_id: str, tmdb_id: str, title: str) -> HdhiveSubscription: ...
    def list(self, chat_id: str | None = None) -> list[HdhiveSubscription]: ...
    def check(self, subscription_id: int, confirmed_item_id: int | None = None) -> SubscriptionCheckResult: ...
    def pause(self, subscription_id: int) -> HdhiveSubscription: ...
    def resume(self, subscription_id: int) -> HdhiveSubscription: ...
    def delete(self, subscription_id: int) -> HdhiveSubscription: ...
```

`create_from_url` calls `resolve_tv_page`, saves `source_type="hdhive_tv"`, the page slug, source URL, title, and TMDB ID. `create_from_tmdb` saves `source_type="tmdb_tv"`. `check` queries only `tv + tmdb_id`, filters to `pan_type="115"`, groups by `episode_key`, upserts every candidate, and processes one best unprocessed item per episode.

For a low-cost or already-owned item, call `proxy.unlock([slug])`; for a successful item with a 115 URL, call `enqueue_links([url], chat_id)`, then mark the item `enqueued` with the resulting TaskStore task ID when available. If intake returns no task ID, retain `enqueued` plus an error-free dedupe marker so a transient Telegram notification failure cannot unlock twice. For high/unknown cost, mark `pending_confirmation` and do not call unlock. Confirming an item rechecks the current resource and then unlocks the stored slug once.

- [ ] **Step 5: Run service tests and commit.**

Run `pytest -q tests/test_hdhive_subscriptions.py`. Expected: PASS for ranking, dedupe, cost boundaries, and intake delegation.

```bash
git add app/hdhive_subscriptions.py app/clients/hdhive.py tests/test_hdhive_subscriptions.py
git commit -m "feat: automate HDHive subscription resource selection"
```

### Task 4: Add Daily Scheduler And Runtime Wiring

**Files:**
- Modify: `app/hdhive_subscriptions.py`
- Modify: `bridge.py`
- Modify: `app/config.py`
- Test: `tests/test_hdhive_subscriptions.py`
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write failing scheduler tests.**

Use an injected clock and assert:

```python
def test_next_run_defaults_to_0130_shanghai():
    scheduler = HdhiveSubscriptionScheduler(..., run_time="01:30", timezone_name="Asia/Shanghai")
    now = datetime(2026, 7, 25, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert scheduler.next_run_at(now).hour == 1
    assert scheduler.next_run_at(now).minute == 30

def test_daily_lease_allows_one_run_per_local_date():
    assert scheduler.run_if_due(at_0130) is not None
    assert scheduler.run_if_due(at_0130_plus_minute) is None
```

Add a wiring test that `create_hdhive_subscription_service` is `None` when HDHive is disabled and uses the existing `enqueue_hdhive_links` callback when enabled.

- [ ] **Step 2: Implement scheduler lease and settings.**

Add `HdhiveSubscriptionScheduler` with `next_run_at`, `run_if_due`, `run_now`, `start`, and `stop`. Use `HdhiveSubscriptionStore.claim_daily_run` before the scheduled run. A manual run uses a unique run ID but still acquires a running lease. Process subscriptions in ascending ID order, catch/log each subscription exception, and persist a summary after every run. Expose `status_snapshot()` with enabled, time, timezone, current status, last summary, and next run.

- [ ] **Step 3: Wire service construction after the intake callback exists.**

Keep the existing `create_hdhive_workflow` for interactive search. Extract the current nested `enqueue_hdhive_links` closure in `run_forever` into a callback that is defined before starting the subscription scheduler. Construct `HdhiveSubscriptionStore(config.task_db_path)` and `HdhiveSubscriptionService` only when `hdhive_workflow` exists. Start the scheduler after the callback is defined and stop/join it in the existing shutdown path. Do not start it for disabled HDHive or missing OAuth token files.

- [ ] **Step 4: Run scheduler/wiring tests and commit.**

Run:

```bash
pytest -q tests/test_hdhive_subscriptions.py tests/test_hdhive_bridge.py
```

Expected: PASS, including the 01:30 lease and no duplicate scheduler startup.

```bash
git add app/hdhive_subscriptions.py app/config.py bridge.py tests/test_hdhive_subscriptions.py tests/test_hdhive_bridge.py
git commit -m "feat: schedule daily HDHive subscription checks"
```

### Task 5: Add Telegram Direct URL And Subscription Controls

**Files:**
- Modify: `bridge.py`
- Modify: `app/telegram_ui.py`
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write failing Telegram behavior tests.**

Test that a message containing the supplied HDHive URL calls `create_from_url`, does not call normal 115/CMS intake, and returns the created subscription title. Test callbacks for `pause`, `resume`, `delete`, `check`, and `confirm`. Test the search candidate keyboard exposes `订阅此剧` only for TV candidates.

- [ ] **Step 2: Add subscription keyboards and formatting.**

Add `hdhive_subscription_keyboard(subscription)` and `format_hdhive_subscriptions(subscriptions, scheduler_snapshot)` using short callback data such as `hsub:pause:<id>`, `hsub:check:<id>`, and `hsub:confirm:<item_id>`. Keep callback payloads under Telegram's 64-byte limit. Add `HDHive 订阅` to the persistent menu and help text.

- [ ] **Step 3: Route direct HDHive TV links before normal link handling.**

Add a strict extractor that returns only `/tv/<slug>` URLs from `hdhive.com`. In `handle_update`, when HDHive is enabled and the message contains one or more such links, create each subscription and send one compact result per URL. Do not pass these URLs to `extract_share_links`, CMS `add_share_down`, or 115 cloud download handling. A malformed HDHive URL produces a clear error and leaves other valid URLs untouched.

- [ ] **Step 4: Add callback dispatch.**

Handle `hsub:` callbacks in `handle_callback_query`, enforce the existing allowed chat ID, answer every callback query, call the service method, and refresh the subscription list message. For a high-cost confirmation, show the current resource, maximum points, and the explicit confirmation button. After a manual check, report new/pending/failed counts rather than raw secret links.

- [ ] **Step 5: Run Telegram tests and commit.**

Run `pytest -q tests/test_hdhive_bridge.py tests/test_telegram_client.py`. Expected: PASS.

```bash
git add bridge.py app/telegram_ui.py tests/test_hdhive_bridge.py
git commit -m "feat: manage HDHive subscriptions from Telegram"
```

### Task 6: Add Web HDHive Management

**Files:**
- Modify: `app/web.py`
- Modify: `bridge.py`
- Test: `tests/test_hdhive_web.py` or `tests/test_web_admin.py`

- [ ] **Step 1: Write failing Web route tests.**

Using the existing `ThreadingHTTPServer` handler harness, assert:

- `GET /hdhive` renders account status, schedule, subscriptions, and pending confirmations;
- `POST /hdhive/subscriptions/<id>/pause`, `/resume`, `/delete`, and `/check` call the service;
- `POST /hdhive/settings` accepts `01:30` and `Asia/Shanghai` and rejects `25:00`;
- disabled HDHive returns a clear 409/disabled page rather than a traceback.

- [ ] **Step 2: Implement the page and safe action routes.**

Extend `maybe_start_web_server` and `start_web_server` with optional `hdhive_service` and `hdhive_scheduler` parameters. Add `render_hdhive_page` using the existing `_page`/`_navigation` styles and forms. Every action must redirect back to `/hdhive`, preserve Web auth headers, and use integer IDs validated by the service. Use `Thread` only for long-running immediate checks, matching the existing quality manual-run behavior.

- [ ] **Step 3: Wire the page into `run_forever` and test it.**

Pass the service/scheduler through the existing compatibility inspection in `call_maybe_start_web_server`, then run `pytest -q tests/test_hdhive_web.py tests/test_web_admin.py`. Expected: PASS with the pre-existing Web tests unchanged.

```bash
git add app/web.py bridge.py tests/test_hdhive_web.py
git commit -m "feat: add HDHive subscription Web management"
```

### Task 7: Add Doctor Checks, Documentation, And Integration Coverage

**Files:**
- Modify: `doctor.py`
- Modify: `README.md`
- Modify: `PRODUCT.md`
- Modify: `docs/dockerhub-overview.md`
- Test: `tests/test_doctor.py`
- Test: `tests/test_hdhive_subscription_docs.py`
- Test: `tests/test_bridge_v02_integration.py`

- [ ] **Step 1: Add failing documentation and doctor tests.**

Assert that the docs include the three subscription environment variables, the direct `hdhive.com/tv/<slug>` example, default `01:30`, and the warning that high/unknown costs require confirmation. Assert that `doctor.py` reports the subscription database/settings as healthy when HDHive is enabled and warns clearly when the OAuth token file is missing.

- [ ] **Step 2: Implement health output and user documentation.**

Extend the existing health report with subscription scheduler status, next run, active subscription count, and pending confirmation count. Add setup instructions showing both:

```text
https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5
```

and the Telegram search button flow. Explain that direct HDHive URLs are subscriptions, not immediate unlock commands.

- [ ] **Step 3: Add end-to-end fake integration coverage.**

Create a fake proxy response containing two episodes, duplicate resources, one high-cost resource, and one successful 115 unlock. Assert the scheduler calls the existing intake callback exactly once for the low-cost best resource, stores the high-cost item as pending, and leaves the other episode available for the next run. Do not perform a real HDHive unlock in tests.

- [ ] **Step 4: Run focused checks and commit.**

Run:

```bash
pytest -q tests/test_doctor.py tests/test_hdhive_subscription_docs.py tests/test_bridge_v02_integration.py
```

Expected: PASS.

```bash
git add doctor.py README.md PRODUCT.md docs/dockerhub-overview.md tests/test_doctor.py tests/test_hdhive_subscription_docs.py tests/test_bridge_v02_integration.py
git commit -m "docs: document HDHive series subscriptions"
```

### Task 8: Full Verification And Release Readiness

**Files:**
- Modify: `CHANGELOG.md` only if the repository release convention requires an entry.
- Test: all existing tests.

- [ ] **Step 1: Run syntax and focused subscription checks.**

```bash
python3 -m py_compile bridge.py doctor.py app/hdhive_subscription_store.py app/hdhive_subscriptions.py
pytest -q tests/test_hdhive_client.py tests/test_hdhive_subscription_store.py tests/test_hdhive_subscriptions.py tests/test_hdhive_bridge.py tests/test_hdhive_web.py
```

Expected: all focused tests pass and no secret-hygiene test sees the sample URL as a credential.

- [ ] **Step 2: Run the complete test suite and Docker build.**

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
docker build -t cms-tg-ingest:hdhive-subscriptions .
```

Expected: the complete suite passes, Docker build succeeds, and the image contains no `.env`, OAuth token, or test database.

- [ ] **Step 3: Perform safe runtime validation.**

With `HDHIVE_ENABLED=true` but without confirming any paid unlock:

1. Open `/hdhive` and verify account status plus `01:30 Asia/Shanghai`.
2. Send the supplied HDHive TV URL and verify one persistent subscription is created.
3. Click “立即检查” and verify page resolution/resource listing without unlocking a high-cost item.
4. Restart the container and verify the subscription and item dedupe state remain.
5. Confirm that the existing 115 direct-link intake tests and Web task pages still work.

- [ ] **Step 4: Review the final diff before any remote push/deployment.**

```bash
git status --short
git diff origin/main...HEAD --stat
git diff --check
```

Verify that only subscription implementation/docs/tests and the existing user-owned worktree changes are present; do not reset or stage the user-owned changes accidentally.
