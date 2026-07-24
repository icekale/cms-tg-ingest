# HDHive Search And Unlock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Telegram button-driven HDHive TMDB search, cloud-drive filtering, single/batch unlock, and automatic intake of unlocked 115 links through the CMS-owned OAuth proxy.

**Architecture:** The add-on reads only the CMS OAuth access token from a read-only mounted JSON file and calls the CMS authorization proxy for account, resource, and unlock operations. CMS remains responsible for refresh-token rotation. A short-lived in-memory Telegram session stores search candidates and selected resource indexes; successful 115 links are passed into the existing TaskStore intake path.

**Tech Stack:** Python 3, `urllib`-based `HttpJson`, dataclasses, Telegram Bot HTTP API, SQLite TaskStore, `unittest`/`pytest`, Docker Compose.

---

## File Map

- Create: `app/clients/hdhive.py` for token loading, proxy HTTP calls, response normalization, and account/resource/unlock models.
- Create: `app/hdhive.py` for short-lived search sessions, TMDB candidate/resource selection, cost rules, and callback identifiers.
- Modify: `app/config.py` for HDHive settings.
- Modify: `app/clients/cms.py` for CMS HDHive status refresh delegation and TMDB search methods.
- Modify: `app/telegram_ui.py` for the persistent HDHive menu button and compact result keyboards.
- Modify: `bridge.py` for HDHive text/callback dispatch and reuse of existing 115 intake.
- Modify: `docker-compose.yml` and `.env.example` for the read-only CMS token mount and settings.
- Modify: `doctor.py` only if the existing health output needs an HDHive check hook.
- Create: `tests/test_hdhive_client.py` for proxy, token, response, and retry behavior.
- Create: `tests/test_hdhive_workflow.py` for session, filter, cost, batch, and intake behavior.
- Modify: `tests/test_bridge_task_engine.py` only for shared intake regressions if the refactor touches its helper.

The following existing user changes remain untouched unless a test demonstrates a direct conflict: `app/clients/p115.py`, `app/cms_cloud_index.py`, `app/workflows/self_share.py`, and their related tests.

## Task 1: Add Configuration And Proxy Client

**Files:**
- Modify: `app/config.py`
- Create: `app/clients/hdhive.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Test: `tests/test_hdhive_client.py`

- [ ] **Step 1: Write failing tests for token loading and proxy request shapes.**

Add tests that create a temporary JSON token file with an access token and refresh token, inject a fake HTTP transport, and verify that the request body contains only the access token plus the documented fields:

```python
def test_resources_posts_media_type_and_tmdb_id_without_logging_token(tmp_path):
    token_path = tmp_path / "hdhive-openapi.json"
    token_path.write_text('{"access_token":"access-1","refresh_token":"refresh-1"}', encoding="utf-8")
    http = FakeHttp([{"success": True, "data": [], "code": "200"}])
    client = HdhiveProxyClient("https://proxy.test", token_path, http=http)

    client.resources("movie", "550")

    assert http.calls == [
        ("POST", "https://proxy.test/api/hdhive/resources", {
            "resource_type": "movie",
            "tmdb_id": "550",
            "access_token": "access-1",
        })
    ]
    assert "refresh-1" not in client.last_log_text
```

Add matching tests for `/api/hdhive/resources/unlock` with `{"slug": "slug-1"}` and `{"slugs": ["slug-1", "slug-2"]}`.

- [ ] **Step 2: Run the focused tests and verify they fail because the client does not exist.**

Run:

```bash
pytest -q tests/test_hdhive_client.py
```

Expected: collection or import failures for the missing `HdhiveProxyClient` and models.

- [ ] **Step 3: Implement the minimal client and typed response models.**

Implement `app/clients/hdhive.py` with these public types and methods:

```python
@dataclass(frozen=True)
class HdhiveAccount:
    nickname: str
    points: int
    weekly_free_quota_remaining: int
    weekly_free_quota_unlimited: bool
    level: str
    is_blocked: bool

@dataclass(frozen=True)
class HdhiveResource:
    slug: str
    title: str
    pan_type: str
    share_size: str
    video_resolution: tuple[str, ...]
    source: tuple[str, ...]
    subtitle_language: tuple[str, ...]
    subtitle_type: tuple[str, ...]
    unlock_points: int | None
    validate_status: str
    validate_message: str
    is_unlocked: bool

@dataclass(frozen=True)
class HdhiveUnlockItem:
    slug: str
    success: bool
    full_url: str
    message: str
    error_code: str
    already_owned: bool

class HdhiveProxyClient:
    def __init__(self, base_url: str, token_path: Path, http: HttpJson, refresh_via_cms: Callable[[], None] | None = None) -> None:
        raise NotImplementedError

    def account(self) -> HdhiveAccount:
        raise NotImplementedError

    def resources(self, media_type: str, tmdb_id: str) -> list[HdhiveResource]:
        raise NotImplementedError

    def unlock(self, slugs: list[str]) -> list[HdhiveUnlockItem]:
        raise NotImplementedError

    def healthcheck(self) -> bool:
        raise NotImplementedError
```

Use `HttpJson` for POST requests, `HDHIVE_PROXY_BASE_URL` for the base URL, and `/api/hdhive/resources`, `/api/hdhive/resources/unlock`, `/api/hdhive/me` for the proxy paths. Read the JSON file on each request so CMS token refreshes become visible without restarting the bot. Never include access tokens, refresh tokens, or unlocked URLs in log messages.

When the proxy response indicates an expired access token, call a supplied `refresh_via_cms` callback once, reload the token file, and retry exactly once. Raise `HdhiveProxyError` with a stable `error_code` for all other API failures.

- [ ] **Step 4: Add configuration parsing and the read-only mount.**

Add these `Config` fields and defaults:

```python
hdhive_enabled: bool = False
hdhive_proxy_base_url: str = "https://authx.771885.xyz"
hdhive_token_config_path: str = "/config/hdhive-openapi.json"
hdhive_search_session_ttl_seconds: int = 900
hdhive_auto_unlock_max_points: int = 20
```

Parse `HDHIVE_ENABLED`, `HDHIVE_PROXY_BASE_URL`, `HDHIVE_TOKEN_CONFIG_PATH`, `HDHIVE_SEARCH_SESSION_TTL_SECONDS`, and `HDHIVE_AUTO_UNLOCK_MAX_POINTS`. Add the same variables, disabled by default, to `.env.example`. Add the CMS token file as a read-only Compose volume:

```yaml
- /mnt/user/appdata/cloud-media-sync/config/hdhive-openapi.json:/config/hdhive-openapi.json:ro
```

- [ ] **Step 5: Run the client and config tests.**

Run:

```bash
pytest -q tests/test_hdhive_client.py tests/test_http_clients.py
```

Expected: all focused tests pass, including redaction and one-retry behavior.

- [ ] **Step 6: Commit the isolated client/config work.**

```bash
git add app/clients/hdhive.py app/config.py .env.example docker-compose.yml tests/test_hdhive_client.py
git commit -m "feat: add CMS-backed HDHive proxy client"
```

## Task 2: Add CMS Status Refresh And TMDB Search Adapters

**Files:**
- Modify: `app/clients/cms.py`
- Modify: `app/clients/hdhive.py`
- Test: `tests/test_hdhive_client.py`

- [ ] **Step 1: Add failing tests for CMS request paths and search result normalization.**

Verify that `CmsClient.get_hdhive_info()` calls `GET /api/hdhive/info`, and that `search_movie()` and `search_tv()` call the existing CMS endpoints with `keyword`, `page`, and `page_size`. Include a response with `data.results` and verify title, year, TMDB ID, poster, and media type normalization.

- [ ] **Step 2: Implement CMS adapters.**

Add:

```python
def get_hdhive_info(self) -> dict:
    return self._authorized("/api/hdhive/info", method="GET")

def search_movie(self, keyword: str, page: int = 1, page_size: int = 8) -> dict:
    return self._authorized(
        "/api/tmdb/search_movie",
        method="GET",
        params={"keyword": keyword, "page": page, "page_size": page_size},
    )

def search_tv(self, keyword: str, page: int = 1, page_size: int = 8) -> dict:
    return self._authorized(
        "/api/tmdb/search_tv",
        method="GET",
        params={"keyword": keyword, "page": page, "page_size": page_size},
    )
```

The HDHive client receives a callback that invokes `get_hdhive_info()` when its proxy returns the documented token-expired code. The callback must not expose the CMS response or token in a Telegram message.

- [ ] **Step 3: Run the adapter tests.**

Run:

```bash
pytest -q tests/test_hdhive_client.py
```

Expected: PASS.

- [ ] **Step 4: Commit the CMS adapter work.**

```bash
git add app/clients/cms.py app/clients/hdhive.py tests/test_hdhive_client.py
git commit -m "feat: expose CMS TMDB and HDHive refresh adapters"
```

## Task 3: Add Search Sessions And Unlock Rules

**Files:**
- Create: `app/hdhive.py`
- Test: `tests/test_hdhive_workflow.py`

- [ ] **Step 1: Write failing tests for filtering, session expiry, cost confirmation, and batch limits.**

Cover these exact cases:

```python
def test_default_filter_is_115_and_invalid_resources_are_not_selectable():
    assert visible_pan_types == ["115"]
    assert selectable_indexes == [0]

def test_session_expires_after_configured_ttl():
    assert session_store.get(session_id, now=created_at + 901) is None

def test_single_unlock_at_20_points_is_immediate():
    assert workflow.unlock_preview(session_id).requires_confirmation is False

def test_single_unlock_above_20_points_requires_confirmation():
    assert workflow.unlock_preview(session_id).requires_confirmation is True

def test_batch_limit_uses_account_level():
    assert workflow.validate_selection(session_id, [0, 1]) == "batch_limit_exceeded"

def test_batch_result_preserves_partial_failures():
    assert result.items[0].success is True
    assert result.items[1].error_code == "INSUFFICIENT_POINTS"
```

Use fake `HdhiveResource` objects for `115`, `quark`, and `aliPan`, plus valid, invalid, and unknown validation states.

- [ ] **Step 2: Implement the session store and workflow service.**

Implement:

```python
@dataclass(frozen=True)
class HdhiveSession:
    session_id: str
    chat_id: str
    query: str
    candidates: tuple[dict, ...]
    media_type: str
    tmdb_id: str
    resources: tuple[HdhiveResource, ...]
    pan_type: str
    selected_indexes: tuple[int, ...]
    created_at: float

@dataclass(frozen=True)
class UnlockPreview:
    selected_slugs: tuple[str, ...]
    maximum_points: int
    requires_confirmation: bool
    account: HdhiveAccount

class HdhiveSessionStore:
    def begin(self, chat_id: str, query: str) -> str:
        raise NotImplementedError

    def set_candidates(self, session_id: str, candidates: list[dict]) -> None:
        raise NotImplementedError

    def set_resources(self, session_id: str, media_type: str, tmdb_id: str, resources: list[HdhiveResource]) -> None:
        raise NotImplementedError

    def get(self, session_id: str) -> HdhiveSession | None:
        raise NotImplementedError

    def remove(self, session_id: str) -> None:
        raise NotImplementedError

class HdhiveWorkflow:
    def search_candidates(self, query: str) -> list[dict]:
        raise NotImplementedError

    def load_resources(self, session_id: str, media_type: str, tmdb_id: str) -> list[HdhiveResource]:
        raise NotImplementedError

    def visible_resources(self, session_id: str, pan_type: str = "115") -> list[HdhiveResource]:
        raise NotImplementedError

    def toggle_selection(self, session_id: str, index: int) -> list[int]:
        raise NotImplementedError

    def unlock_preview(self, session_id: str) -> UnlockPreview:
        raise NotImplementedError

    def unlock(self, session_id: str, confirmed: bool = False) -> list[HdhiveUnlockItem]:
        raise NotImplementedError
```

Use a 15-minute TTL, a per-chat active session, and a lock around mutations. Sort resource output with valid resources first and leave invalid resources visible but disabled. Enforce account batch limits before calling the proxy. Do not split oversized batches.

- [ ] **Step 3: Run workflow tests.**

Run:

```bash
pytest -q tests/test_hdhive_workflow.py
```

Expected: PASS.

- [ ] **Step 4: Commit the workflow work.**

```bash
git add app/hdhive.py tests/test_hdhive_workflow.py
git commit -m "feat: add HDHive search sessions and unlock rules"
```

## Task 4: Add Telegram Buttons And 115 Intake Reuse

**Files:**
- Modify: `app/telegram_ui.py`
- Modify: `bridge.py`
- Test: `tests/test_hdhive_workflow.py`
- Test: `tests/test_bridge_task_engine.py` if shared intake changes require regression coverage.

- [ ] **Step 1: Write failing tests for the Telegram state transitions.**

Verify the following sequence with fake Telegram, CMS, HDHive, and TaskStore objects:

```text
HDHive 搜索 -> prompt for query
query -> movie/tv candidate buttons
candidate -> resource list with 115 default filter
filter -> resource list for selected pan_type
toggle -> selected count changes
unlock <= 20 -> one proxy call and one 115 TaskStore intake
unlock > 20 -> confirmation message and no proxy call before confirmation
batch -> one proxy call with slugs in selection order
non-115 success -> result is sent but TaskStore intake is not called
```

- [ ] **Step 2: Add compact keyboard and formatting helpers.**

Add `HDHive 搜索` to `menu_keyboard()` and `MENU_BUTTONS`. Add helpers that render candidate, filter, resource, confirmation, and result keyboards. Callback data must use only short session IDs and integer indexes, never raw slugs or URLs.

- [ ] **Step 3: Add a shared unlocked-link intake callback.**

Extract the existing 115 source submission portion of `handle_update()` into a callback with this contract:

```python
def enqueue_unlocked_115_links(urls: list[str], chat_id: str) -> list[str]:
    """Submit successful HDHive 115 URLs through the existing TaskStore workflow."""
```

The extracted function must preserve duplicate detection, TaskStore stage initialization, self-share configuration, and the existing result formatting. Do not call the regular CMS plain-submit path for an unlocked HDHive 115 URL when the self-share TaskStore workflow is enabled.

- [ ] **Step 4: Dispatch HDHive text and callbacks from `handle_update()`.**

Add an `HdhiveWorkflow | None` parameter to `handle_update()` and `handle_callback_query()`. The `/hdhive_search` menu action starts a session. When a non-link text message is pending for the allowed chat, route it to candidate search. Route callback prefixes `hive:` to the workflow, answer every callback query, and send the resulting message.

The existing URL, command, category, cleanup, and quality paths must remain unchanged when HDHive is disabled or no HDHive session is active.

- [ ] **Step 5: Wire the workflow in `run_forever()`.**

When `HDHIVE_ENABLED=true`, construct `HdhiveProxyClient` with the configured token file and a callback that calls `CmsClient.get_hdhive_info()`. Construct `HdhiveSessionStore` and `HdhiveWorkflow`, and pass them into the Telegram update loop. When disabled, pass `None` and do not mount or read the token file at runtime.

- [ ] **Step 6: Run bridge and workflow tests.**

Run:

```bash
pytest -q tests/test_hdhive_workflow.py tests/test_bridge_task_engine.py tests/test_telegram_client.py
```

Expected: PASS, with existing bridge behavior unchanged.

- [ ] **Step 7: Commit the Telegram integration.**

```bash
git add app/telegram_ui.py bridge.py tests/test_hdhive_workflow.py tests/test_bridge_task_engine.py
git commit -m "feat: add Telegram HDHive search and unlock flow"
```

## Task 5: Health, Documentation, And Container Verification

**Files:**
- Modify: `doctor.py` for disabled/missing-token reporting when the existing health output has no suitable extension point.
- Modify: `README.md`
- Modify: `PRODUCT.md`
- Modify: `docs/dockerhub-overview.md`
- Test: `tests/test_dockerfile.py`
- Test: `tests/test_docs_v02.py` or a new `tests/test_hdhive_docs.py`

- [ ] **Step 1: Add health and documentation tests.**

Verify that the feature reports `disabled`, `not authorized`, or `ready` without printing token contents, and that the Compose example contains the read-only token mount and disabled-by-default settings.

- [ ] **Step 2: Add health output and Chinese usage documentation.**

Document the one-time CMS setup:

1. Open CMS `转存下载 -> 影巢账号`.
2. Complete HDHive OAuth authorization.
3. Enable `HDHIVE_ENABLED=true` in the bot environment.
4. Keep the read-only token mount in place.
5. Use the Telegram `HDHive 搜索` button.

Document that only the CMS-bound HDHive account is supported in this version, that non-115 unlocks are returned without automatic ingest, and that high-cost resources require confirmation.

- [ ] **Step 3: Run the complete local suite and build the image.**

Run:

```bash
pytest -q
docker build -t cms-tg-ingest:hdhive-test .
```

Expected: all tests pass and the image builds successfully.

- [ ] **Step 4: Perform safe live verification.**

Use the mounted CMS token and perform only:

```text
HDHive account status query
TMDB search for a known title
HDHive resource query with the 115 filter
```

Do not call unlock until the Telegram confirmation message shows the exact selected resource, account quota, and maximum point cost. Then perform one real unlock only if the user explicitly confirms that live cost is acceptable, and verify that exactly one 115 TaskStore task is created.

- [ ] **Step 5: Commit documentation and verification changes.**

```bash
git add doctor.py README.md PRODUCT.md docs/dockerhub-overview.md tests/test_dockerfile.py tests/test_docs_v02.py tests/test_hdhive_docs.py
git commit -m "docs: document HDHive Telegram integration"
```

## Final Review

- [ ] Run `git status --short` and confirm only intentional HDHive changes remain in addition to the user's pre-existing modified files.
- [ ] Run `git diff --check`.
- [ ] Run `pytest -q`.
- [ ] Confirm no test fixture, log assertion, README, or Docker file contains an access token, refresh token, or real unlocked URL.
- [ ] Confirm the final report names the exact image/build verification result and states whether live unlock was performed.
