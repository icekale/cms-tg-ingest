# HDHive Smart Episode Judgment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe multi-season recognition, subscription-level episode filters, Emby existing-episode skipping, and TMDB-based completion detection to HDHive TV subscriptions.

**Architecture:** Keep episode parsing in a pure rule module, extend the existing TMDB and Emby clients with narrowly scoped detail queries, persist filter/skip/completion state in the existing HDHive SQLite store, and let `HdhiveSubscriptionService` orchestrate the rules before delegating successful 115 links to the unchanged intake workflow. Existing Telegram, legacy Web, and Vue Web surfaces expose the same subscription controls.

**Tech Stack:** Python 3, dataclasses, SQLite, existing `HttpJson` clients, Telegram Bot callbacks, Vue 3/Naive UI, unittest/pytest, npm/Vite.

---

## File Map

- Create: `app/series_rules.py` for immutable season/episode parsing, filter parsing, special-episode rules, and completion predicates.
- Create: `tests/test_series_rules.py` for pure rule tests.
- Modify: `app/media/classify.py` for shared episode-key parsing compatibility used by TMDB/Emby metadata.
- Modify: `app/clients/emby.py` for Series lookup and existing episode-key queries.
- Modify: `tests/test_emby_client.py` for Emby query paths, fields, and failure behavior.
- Modify: `app/media/classify.py` and `tests/test_tmdb_resolver.py` for normalized TV status/season data.
- Modify: `app/hdhive_subscription_store.py` for schema migration, filter persistence, skip statuses, summaries, and `completed` subscriptions.
- Modify: `tests/test_hdhive_subscription_store.py` for migration and state persistence.
- Modify: `app/hdhive_subscriptions.py` for smart judgment orchestration and result summaries.
- Modify: `tests/test_hdhive_subscriptions.py` for multi-season, filtering, Emby skip, and completion behavior.
- Modify: `bridge.py` and `app/telegram_ui.py` for subscription filter callbacks, one-time filter input, status labels, and notifications.
- Modify: `tests/test_hdhive_bridge.py` for callback/input behavior and status output.
- Modify: `app/web.py`, `app/web_api.py`, and `tests/test_hdhive_web.py` for legacy Web filter/status actions.
- Modify: `frontend/src/api.js`, `frontend/src/views/Hdhive.vue`, and `tests/test_frontend.py` for Vue filter/status controls.
- Modify: `README.md`, `PRODUCT.md`, and `docs/dockerhub-overview.md` for user-facing rules and syntax.

Do not change the normal 115 intake, CMS organize, STRM mode, cleanup, or TaskRunner behavior except where the existing HDHive subscription callback already hands off a successful 115 URL.

### Task 1: Add Pure Episode and Filter Rules

**Files:**
- Create: `app/series_rules.py`
- Create: `tests/test_series_rules.py`

- [x] **Step 1: Write the failing tests.**

```python
import unittest

from app.series_rules import (
    EpisodeKey,
    is_special_episode,
    parse_episode_filter,
    parse_episode_key,
)


class SeriesRuleTests(unittest.TestCase):
    def test_normalizes_short_and_padded_episode_tokens(self):
        self.assertEqual(parse_episode_key("Show.S1E2.2160p"), EpisodeKey(1, 2))
        self.assertEqual(parse_episode_key("S01E02"), EpisodeKey(1, 2))
        self.assertEqual(parse_episode_key("S01"), None)

    def test_default_filter_excludes_specials_but_explicit_s00_allows_them(self):
        self.assertTrue(is_special_episode(EpisodeKey(0, 1)))
        self.assertFalse(parse_episode_filter("").matches(EpisodeKey(0, 1)))
        self.assertTrue(parse_episode_filter("S00").matches(EpisodeKey(0, 1)))

    def test_filter_supports_single_episode_range_season_and_union(self):
        episode_filter = parse_episode_filter("S01E01-S01E03,S02")
        self.assertTrue(episode_filter.matches(EpisodeKey(1, 2)))
        self.assertFalse(episode_filter.matches(EpisodeKey(1, 4)))
        self.assertTrue(episode_filter.matches(EpisodeKey(2, 99)))

    def test_rejects_cross_season_ranges_and_bad_tokens(self):
        with self.assertRaises(ValueError):
            parse_episode_filter("S01E01-S02E02")
        with self.assertRaises(ValueError):
            parse_episode_filter("S01E")


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run the focused test and verify it fails for the missing module.**

Run: `python -m pytest -q tests/test_series_rules.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'app.series_rules'`.

- [x] **Step 3: Implement the minimal pure rule module.**

Define an ordered frozen `EpisodeKey(season: int, episode: int)` with a `normalized` property returning `S{season:02d}E{episode:02d}`. Define `EpisodeFilter` with exact keys, season numbers, and same-season inclusive ranges. Implement:

```python
def parse_episode_key(value: str | None) -> EpisodeKey | None:
    raise NotImplementedError

def parse_episode_filter(value: str | None) -> EpisodeFilter:
    raise NotImplementedError

def is_special_episode(key: EpisodeKey) -> bool:
    raise NotImplementedError

def normalize_episode_key(value: str | None) -> str:
    raise NotImplementedError

def completion_state(tmdb_status: str, expected: set[EpisodeKey], terminal: set[EpisodeKey], blocked: set[EpisodeKey]) -> str:
    raise NotImplementedError
```

`parse_episode_key` accepts only a bounded `S<season>E<episode>` token with positive season/episode values, plus `S00E..` for explicit special handling. Empty filters match normal episodes only; explicit `S00` matches special episodes. `completion_state` returns `completed` only for TMDB `ended`/`canceled`, a non-empty expected set, no blocked expected keys, and full terminal coverage; otherwise it returns `active`.

- [x] **Step 4: Run the focused tests and the existing classifier tests.**

Run: `python -m pytest -q tests/test_series_rules.py tests/test_openai_fallback.py`

Expected: all tests pass and existing title/TMDB matching behavior remains unchanged.

- [x] **Step 5: Commit the rule module.**

```bash
git add app/series_rules.py tests/test_series_rules.py
git commit -m "feat: add episode filtering rules"
```

### Task 2: Expose TMDB TV Completion Metadata

**Files:**
- Modify: `app/media/classify.py`
- Create: `tests/test_tmdb_resolver.py`

- [x] **Step 1: Write the failing normalization test.**

```python
import unittest

from app.media.classify import TmdbApiResolver


class TmdbTvDetailsTests(unittest.TestCase):
    def test_normalizes_status_and_season_episode_counts(self):
        result = TmdbApiResolver._normalize_details(
            {
                "id": 1416,
                "name": "Grey's Anatomy",
                "original_language": "en",
                "origin_country": ["US"],
                "genres": [],
                "status": "Ended",
                "number_of_seasons": 2,
                "number_of_episodes": 4,
                "seasons": [
                    {"season_number": 1, "episode_count": 2},
                    {"season_number": 2, "episode_count": 2},
                ],
            },
            "tv",
        )

        self.assertEqual(result["status"], "Ended")
        self.assertEqual(result["number_of_seasons"], 2)
        self.assertEqual(result["seasons"][1]["episode_count"], 2)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run the test and verify the new fields are absent.**

Run: `python -m pytest -q tests/test_tmdb_resolver.py`

Expected: FAIL with a missing `status` key.

- [x] **Step 3: Add normalized TV metadata without changing movie output.**

Extend `TmdbApiResolver._normalize_details` only for `media_type == "tv"`. Copy `status`, `number_of_seasons`, `number_of_episodes`, and a list of dictionaries containing `season_number`, `episode_count`, and `air_date` when present. Convert numeric fields to `int` only when valid and omit malformed season entries. Keep the current fallback behavior and existing category fields.

- [x] **Step 4: Run TMDB and existing classification tests.**

Run: `python -m pytest -q tests/test_tmdb_resolver.py tests/test_openai_fallback.py tests/test_bridge_task_engine.py`

Expected: PASS with no change to current CMS-first category behavior.

- [x] **Step 5: Commit the TMDB slice.**

```bash
git add app/media/classify.py tests/test_tmdb_resolver.py
git commit -m "feat: expose TMDB TV completion metadata"
```

### Task 3: Add Emby Existing-Episode Queries

**Files:**
- Modify: `app/clients/emby.py`
- Modify: `tests/test_emby_client.py`

- [x] **Step 1: Write failing client tests.**

Add a response-driven fake HTTP client and assert these behaviors:

```python
def test_existing_episode_keys_are_loaded_from_tmdb_series(self):
    http = QueueHttp([
        {"Items": [{"Id": "series-1", "ProviderIds": {"Tmdb": "1416"}}]},
        {"Items": [
            {"ParentIndexNumber": 1, "IndexNumber": 2},
            {"ParentIndexNumber": 2, "IndexNumber": 1},
        ]},
    ])
    client = EmbyClient("http://emby.test", "secret-key", user_id="user-1", http=http)

    self.assertEqual(client.existing_episode_keys_by_tmdb("1416"), {"S01E02", "S02E01"})
    self.assertNotIn("secret-key", http.calls[0][0])
```

Also test that an item without both season and episode indexes is ignored and that a non-2xx/HTTP error propagates to the caller instead of returning an empty set.

- [x] **Step 2: Run the focused tests and verify the methods are missing.**

Run: `python -m pytest -q tests/test_emby_client.py -k episode`

Expected: FAIL with `AttributeError` for `existing_episode_keys_by_tmdb`.

- [x] **Step 3: Implement the three narrow Emby methods.**

Add:

```python
def find_series_by_tmdb(self, tmdb_id: str) -> dict | None:
    raise NotImplementedError

def episode_keys_for_series(self, series_id: str) -> set[str]:
    raise NotImplementedError

def existing_episode_keys_by_tmdb(self, tmdb_id: str) -> set[str]:
    raise NotImplementedError
```

Use `/Users/{user_id}/Items` with `AnyProviderIdEquals=tmdb.<id>`, `IncludeItemTypes=Series`, and a small limit; then `/Shows/{series_id}/Episodes` with the current user ID and `Fields=ParentIndexNumber,IndexNumber`. URL-quote the Series ID. Reuse `item_tmdb_id` for exact provider matching and `parse_episode_key` only for formatting valid positive integer indexes. Do not catch HTTP failures in the client; the subscription service will record the unavailable reason.

- [x] **Step 4: Run Emby tests and all HTTP client tests.**

Run: `python -m pytest -q tests/test_emby_client.py tests/test_http_clients.py`

Expected: PASS, including API-key redaction tests.

- [x] **Step 5: Commit the Emby slice.**

```bash
git add app/clients/emby.py tests/test_emby_client.py
git commit -m "feat: query existing Emby TV episodes"
```

### Task 4: Persist Filters, Skip Reasons, and Completion Summaries

**Files:**
- Modify: `app/hdhive_subscription_store.py`
- Modify: `tests/test_hdhive_subscription_store.py`

- [x] **Step 1: Write migration and state tests.**

Add tests that create a database with the pre-feature schema, reopen it through `HdhiveSubscriptionStore`, and assert `episode_filter` and `last_summary_json` default safely. Add tests for:

```python
subscription = store.create_subscription("1", "tmdb_tv", "1416", "剧集", "1416")
store.update_episode_filter(subscription.id, "S01E01-S01E03")
store.record_check(subscription.id, "", summary={"emby_exists": 2})
saved = store.get_subscription(subscription.id)
self.assertEqual(saved.episode_filter, "S01E01-S01E03")
self.assertEqual(json.loads(saved.last_summary_json)["emby_exists"], 2)

item = store.upsert_item(subscription.id, "S01E02", "resource", "valid", 1080, 8, normalized_episode_key="S01E02")
store.mark_item_skipped(item.id, "emby_exists", "Emby 已存在")
self.assertEqual(store.get_item(item.id).skip_reason, "Emby 已存在")
```

Verify `completed` is accepted by `set_status`, deleted subscriptions remain filtered from normal lists, and old records still reopen.

- [x] **Step 2: Run the storage tests and verify the new attributes/methods fail.**

Run: `python -m pytest -q tests/test_hdhive_subscription_store.py`

Expected: FAIL because the old dataclasses and schema do not expose the new fields or methods.

- [x] **Step 3: Add additive SQLite migration and store APIs.**

Extend the frozen dataclasses with `episode_filter`, `last_summary_json`, `normalized_episode_key`, and `skip_reason`. In `_init_db`, add an `_ensure_columns` pass that executes only missing `ALTER TABLE` statements. Extend `upsert_item` with an optional `normalized_episode_key` keyword and preserve existing status unless a new discovery update explicitly resets a prior `filtered` or `emby_exists` row.

Add:

```python
def update_episode_filter(self, subscription_id: int, value: str) -> HdhiveSubscription:
    raise NotImplementedError

def record_check(self, subscription_id: int, error: str = "", checked_at: float | None = None, summary: dict | None = None) -> None:
    raise NotImplementedError

def mark_item_skipped(self, item_id: int, status: str, reason: str) -> HdhiveSubscriptionItem:
    raise NotImplementedError

def reset_item_for_check(self, item_id: int, expected_status: str) -> HdhiveSubscriptionItem:
    raise NotImplementedError
```

Allow only `filtered`, `emby_exists`, and `unparsed` in `mark_item_skipped`; reject arbitrary status values. `reset_item_for_check` changes only a matching skip status back to `discovered`, allowing a changed filter or removed Emby episode to be reconsidered. Store summaries as compact JSON and accept `completed` in `set_status`.

- [x] **Step 4: Run storage tests and the existing doctor tests.**

Run: `python -m pytest -q tests/test_hdhive_subscription_store.py tests/test_doctor.py`

Expected: PASS with the old schema migration path covered.

- [x] **Step 5: Commit the storage slice.**

```bash
git add app/hdhive_subscription_store.py tests/test_hdhive_subscription_store.py
git commit -m "feat: persist HDHive smart judgment state"
```

### Task 5: Integrate Smart Judgment Into Subscription Checks

**Files:**
- Modify: `app/hdhive_subscriptions.py`
- Modify: `tests/test_hdhive_subscriptions.py`

- [x] **Step 1: Write failing service tests with real store state.**

Add fakes for a TMDB resolver and Emby client, then cover these cases:

```python
def test_check_skips_emby_existing_episode_and_handles_multiple_seasons(self):
    service, store, subscription, proxy, intake, _ = self.make_service(
        [resource("s1e1", episode_key="s01e01"), resource("s2e1", episode_key="s02e01")],
        tmdb={"status": "Returning Series", "seasons": []},
        existing_emby={"S01E01"},
    )
    result = service.check(subscription.id)
    self.assertEqual(result.skipped, 1)
    self.assertEqual(result.enqueued, 1)
    self.assertEqual(store.list_items(subscription.id)[0].status, "emby_exists")
    self.assertEqual(intake, [["https://115cdn.com/s/new?password=abcd"]])

def test_check_applies_episode_filter_and_does_not_unlock_s00_by_default(self):
    subscription = self.store.update_episode_filter(self.subscription.id, "S01E02-S01E03")
    result = self.service.check(subscription.id)
    self.assertEqual(result.summary["filtered"], 2)
    self.assertNotIn("S00E01", self.proxy.unlock_calls)

def test_ended_series_becomes_completed_only_after_expected_episodes_are_terminal(self):
    result = self.service.check(self.subscription.id)
    self.assertEqual(result.subscription_status, "completed")
```

Also test Emby unavailable (no skip and an explicit summary reason), TMDB unknown (never completed), high-cost confirmation blocking completion, unparsed episodes blocking completion, and a later filter change resetting `filtered` items.

- [x] **Step 2: Run the service tests and verify they fail before integration.**

Run: `python -m pytest -q tests/test_hdhive_subscriptions.py -k 'smart or episode or completed'`

Expected: FAIL because the service does not accept the new dependencies, result fields, or skip logic.

- [x] **Step 3: Extend the service constructor and normalize each resource once.**

Add optional `tmdb_resolver` and `emby` dependencies to `HdhiveSubscriptionService`. Normalize every resource with `episode_key(resource)` followed by `parse_episode_key`; use the normalized string for grouping and pass it to `upsert_item`. A missing key is stored as `unparsed` and is never sent to `_unlock_one`.

- [x] **Step 4: Apply filters, Emby skips, and best-resource selection in order.**

Inside `check`, parse `subscription.episode_filter` before contacting HDHive. For each normalized episode:

1. Mark all candidates `filtered` when the rule does not match or when the key is special and not explicitly allowed.
2. If Emby is enabled, call `existing_episode_keys_by_tmdb` once per subscription check. Mark candidates `emby_exists` for matching keys; reset stale `emby_exists` rows if the key is no longer returned.
3. Select one candidate with the existing `select_best_resource` ranking.
4. Preserve the existing `enqueued`, `pending_confirmation`, `unlocking`, and `failed` behavior.
5. Never call `unlock` for filtered, Emby-existing, unparsed, or already-enqueued episode groups.

Catch only Emby query exceptions around the optional lookup, set `emby_skip_unavailable` in the summary, and continue without pretending the episode exists. Keep HDHive/115 exceptions on the current per-subscription error path.

- [x] **Step 5: Add completion calculation and structured result data.**

Resolve TMDB TV details once when a resolver is enabled. Build expected keys from `seasons[].season_number` and `episode_count`; remove keys excluded by an explicit filter. Treat `enqueued`, `emby_exists`, and `filtered` as terminal; treat missing expected keys, `pending_confirmation`, `failed`, `unlocking`, and `unparsed` as blocking. Call `completion_state` and update the subscription to `completed` only when it returns `completed`; otherwise leave it `active` unless the existing call failed.

Extend `SubscriptionCheckResult` with a `summary: dict[str, Any]` and `subscription_status: str`, while preserving the existing integer fields. Save the summary with `record_check` and include counts for `enqueued`, `pending_confirmation`, `failed`, `emby_exists`, `filtered`, `unparsed`, `blocked`, `expected`, `tmdb_status`, and `emby_skip_unavailable`.

- [x] **Step 6: Run all HDHive service tests and the bridge integration tests.**

Run: `python -m pytest -q tests/test_hdhive_subscriptions.py tests/test_hdhive_bridge.py tests/test_task_bridge.py`

Expected: PASS, including the existing cost threshold and unlock deduplication tests.

- [x] **Step 7: Commit the service slice.**

```bash
git add app/hdhive_subscriptions.py tests/test_hdhive_subscriptions.py
git commit -m "feat: add HDHive smart episode judgment"
```

### Task 6: Wire TMDB/Emby Dependencies And Telegram Controls

**Files:**
- Modify: `bridge.py`
- Modify: `app/telegram_ui.py`
- Modify: `tests/test_hdhive_bridge.py`

- [x] **Step 1: Write failing callback and notification tests.**

Cover the `hsub:filter:<id>` callback, invalid filter input, clearing a filter with an empty message, and `completed` rendering:

```python
def test_completed_subscription_renders_status_and_filter(self):
    subscription = store.create_subscription("464100862", "tmdb_tv", "1416", "剧集", "1416")
    store.update_episode_filter(subscription.id, "S01E01-S01E03")
    store.set_status(subscription.id, "completed")
    text = format_hdhive_subscriptions(store.list_subscriptions("464100862"))
    self.assertIn("已完结", text)
    self.assertIn("S01E01-S01E03", text)
```

- [x] **Step 2: Run the focused tests and verify the new callback is unhandled.**

Run: `python -m pytest -q tests/test_hdhive_bridge.py -k 'filter or completed'`

Expected: FAIL because the callback parser and pending-filter state do not exist.

- [x] **Step 3: Pass existing clients into the subscription service.**

At the existing service construction in `bridge.py`, pass the configured TMDB resolver and Emby client. Keep them optional so current deployments without either credential continue to work. Do not create a second client or a second polling thread.

- [x] **Step 4: Add a bounded Telegram filter-input state.**

Extend the existing HDHive session/input state with one pending subscription ID per allowed chat. Add callback data `hsub:filter:<subscription_id>` and a button labeled `设置集数过滤`. On callback, verify ownership through `subscription.chat_id`, store the pending ID, and send:

```text
请发送集数过滤，例如 S01E01-S01E10,S02；发送“清除”恢复全部正常集。
```

Before normal HDHive search/link handling, consume a pending filter message for the same chat. Validate with `parse_episode_filter`; on error send the examples and keep the pending state. On success call `update_episode_filter`, clear the pending state, and render the subscription list. This state is memory-only and safe to lose on restart because the stored filter remains unchanged.

- [x] **Step 5: Update TG formatting and callback handling.**

Add `completed` labels, filter text, and the latest summary counters to `format_hdhive_subscriptions`. Make the toggle button for a completed subscription say `恢复` and route to the existing resume action. Include skip/completion details in the existing `on_item_enqueued`/check notification without printing full share URLs.

- [x] **Step 6: Run Telegram and bridge tests.**

Run: `python -m pytest -q tests/test_hdhive_bridge.py tests/test_telegram_client.py tests/test_task_bridge.py`

Expected: PASS with current `/订阅` and `/搜索` behavior unchanged.

- [x] **Step 7: Commit the Telegram/wiring slice.**

```bash
git add bridge.py app/telegram_ui.py tests/test_hdhive_bridge.py
git commit -m "feat: add Telegram HDHive episode controls"
```

### Task 7: Make Vue UI Primary While Preserving Legacy Web Compatibility

**Files:**
- Modify: `app/web.py`
- Modify: `app/web_api.py`
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/views/Hdhive.vue`
- Modify: `tests/test_hdhive_web.py`
- Modify: `tests/test_frontend.py`

- [x] **Step 1: Write failing Web/API tests.**

Add tests for `POST /api/v1/hdhive/subscriptions/<id>/episode-filter` with JSON `{"episode_filter":"S02"}`, invalid input returning HTTP 400 without changing the old filter, and the serialized payload containing `episode_filter`, `last_summary_json`, `completed`, and item skip reasons. Add a legacy form POST test for `/hdhive/subscriptions/<id>/episode-filter`.

- [x] **Step 2: Run the focused Web tests and verify the endpoint is missing.**

Run: `python -m pytest -q tests/test_hdhive_web.py -k filter`

Expected: FAIL with HTTP 404 or missing service method.

- [x] **Step 3: Implement shared backend actions and serialization.**

Add `HdhiveSubscriptionService.set_episode_filter(subscription_id, value)` that validates before calling the store. Keep the legacy form route for backward compatibility and add the JSON API route used by the Vue UI; return a safe serialized subscription. Extend `serialize_hdhive` with the typed subscription/item fields and a computed summary object, without exposing URLs, tokens, cookies, or API keys. Keep existing pause/resume/delete/check/confirm routes untouched.

- [x] **Step 4: Add the new Vue controls and status display.**

The new Vue UI is the primary surface. Add to `frontend/src/api.js`:

```javascript
hdhiveSubscriptionFilter: (id, episode_filter) => request(`hdhive/subscriptions/${id}/episode-filter`, {
  method: 'POST',
  body: JSON.stringify({ episode_filter }),
}),
```

In `Hdhive.vue`, render each subscription’s status, filter, `last_summary_json`, and skip counts. Add a compact text input with a save button, show `已完结` as a tag, and preserve pause/resume/check/delete and unlock confirmation. Display `emby_skip_unavailable` as a warning rather than silently treating all episodes as new.

- [x] **Step 5: Build the frontend and run Web/API tests.**

Run: `npm --prefix frontend run build && python -m pytest -q tests/test_hdhive_web.py tests/test_web_api.py tests/test_frontend.py`

Expected: Vite build succeeds and all legacy/new routes pass.

- [x] **Step 6: Commit the Web/Vue slice.**

```bash
git add app/web.py app/web_api.py frontend/src/api.js frontend/src/views/Hdhive.vue tests/test_hdhive_web.py tests/test_frontend.py
git commit -m "feat: expose HDHive smart judgment in Web UI"
```

### Task 8: Document, Run Full Regression, And Prepare Deployment

**Files:**
- Modify: `README.md`
- Modify: `PRODUCT.md`
- Modify: `docs/dockerhub-overview.md`
- Modify: `tests/test_hdhive_subscription_docs.py`

- [x] **Step 1: Write documentation assertions.**

Add tests that require the docs to explain `S01E01-S01E10`, `S02`, Emby existing-episode skipping, TMDB completion detection, and the rule that unparsed/high-cost episodes are not automatically unlocked.

- [x] **Step 2: Run documentation tests and confirm the new text is absent.**

Run: `python -m pytest -q tests/test_hdhive_subscription_docs.py`

Expected: FAIL for the missing smart judgment terms.

- [x] **Step 3: Update the Chinese user documentation.**

Document the default behavior, filter examples, `completed`/resume semantics, Emby failure fallback, and the recommended safe rollout: pause an existing subscription, run a manual check, inspect summaries, then resume. Do not add secrets or real account identifiers.

- [x] **Step 4: Run documentation and release checks.**

Run: `python -m pytest -q tests/test_hdhive_subscription_docs.py tests/test_secret_hygiene.py tests/test_dockerfile.py`

Expected: PASS with no secret-hygiene findings.

- [x] **Step 5: Run the complete regression suite and local build checks.**

Run:

```bash
python -m pytest -q
python -m compileall -q app bridge.py doctor.py
npm --prefix frontend run build
docker build -t cms-tg-ingest:smart-judgment .
```

Expected: all Python tests pass, Python compilation succeeds, the Vue build succeeds, and the Docker image builds without embedding secrets.

- [x] **Step 6: Commit documentation and final local verification.**

```bash
git add README.md PRODUCT.md docs/dockerhub-overview.md tests/test_hdhive_subscription_docs.py
git commit -m "docs: explain HDHive smart episode judgment"
git status --short
```

Expected: clean worktree except for explicitly unrelated user changes; do not reset or remove unrelated files.

## Final Verification Checklist

- [x] Existing HDHive subscription tests remain green.
- [x] No extra 115 scan or concurrency was introduced.
- [x] Emby API keys and HDHive tokens are absent from URLs, logs, API payloads, and rendered pages.
- [x] Old subscription databases migrate in place and retain existing unlocked/enqueued items.
- [x] Filtered and Emby-existing episodes never call HDHive unlock.
- [x] Unknown TMDB/Emby states do not produce false `completed` status.
- [x] Completed subscriptions can be resumed without deleting existing TaskStore tasks or media files.
