# HDHive LLM Episode Parse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When HDHive subscription regex cannot parse season/episode but the resource text has episode clues, call the existing OpenAI classifier to infer the season (from title/TMDB) and extract episodes from the text; auto-enqueue only at high confidence when the season is in the TMDB list, otherwise mark `pending_confirmation`.

**Architecture:** Keep `episode_keys()` deterministic. Put clue detection, JSON validation, and auto/pending/reject rules in `app/hdhive_episode_llm.py` (no HTTP). Add `OpenAIClassifier.parse_hdhive_episode` with schema + cache. `HdhiveSubscriptionService.check()` calls the parser before grouping, then reuses the existing filter / Emby / unlock path.

**Tech Stack:** Python 3, unittest, existing `OpenAIClassifier` (`/responses` + json_schema), `HdhiveSubscriptionService`, TMDB lookup already performed in `check()`.

**Spec:** `docs/superpowers/specs/2026-09-09-hdhive-llm-episode-parse-design.md`

## File map

| File | Responsibility |
| --- | --- |
| Create `app/hdhive_episode_llm.py` | Clue regex, payload → `EpisodeKey`s, decide auto/pending/reject, pending reason text, TMDB season set |
| Create `tests/test_hdhive_episode_llm.py` | Pure-function tests, no network |
| Modify `app/clients/http.py` | Optional per-request `timeout` on `HttpJson.request` |
| Modify `bridge.py` | `OpenAIClassifier.parse_hdhive_episode`; pass classifier into subscription factory |
| Modify `tests/test_openai_fallback.py` | Classifier HTTP, cache, disabled, payload hygiene |
| Modify `app/hdhive_subscriptions.py` | Optional `episode_parser`; call it in `check()` before grouping |
| Modify `tests/test_hdhive_subscriptions.py` | Fake parser integration |
| Modify `tests/test_hdhive_bridge.py` | Factory forwards `openai_classifier` |
| Modify `CHANGELOG.md`, `PRODUCT.md` | User-visible note |

Do not change `episode_keys()`, database schema, or HDHive Vue layout.

Tests in this repo use unittest:

```sh
python3 -m unittest tests.test_hdhive_episode_llm -q
```

---

### Task 1: Pure episode-LLM helpers

**Files:**
- Create: `tests/test_hdhive_episode_llm.py`
- Create: `app/hdhive_episode_llm.py`

- [ ] **Step 1: Write the failing tests**

```python
import unittest

from app.clients.hdhive import HdhiveResource
from app.hdhive_episode_llm import (
    DECISION_AUTO,
    DECISION_PENDING,
    DECISION_REJECT,
    decide_llm_episode,
    format_llm_pending_reason,
    has_episode_clue,
    keys_from_llm_payload,
    resource_clue_text,
    tmdb_season_numbers,
)
from app.series_rules import EpisodeKey


class HasEpisodeClueTests(unittest.TestCase):
    def test_clue_patterns(self):
        for text in (
            "更新至07集",
            "第12集",
            "第十二集",
            "全08集",
            "EP07",
            "E01-E08",
            "Show.S02.2160p",
            "Season 2 extra",
        ):
            with self.subTest(text=text):
                self.assertTrue(has_episode_clue(text))

    def test_season_word_without_episode_is_not_a_clue(self):
        self.assertFalse(has_episode_clue("最后生还者 第二季"))
        self.assertFalse(has_episode_clue("The Last of Us"))
        self.assertFalse(has_episode_clue("4K 2160P WEB-DL"))

    def test_resource_clue_text_joins_title_remark_and_episode_key(self):
        resource = HdhiveResource(
            slug="pack",
            title="最后生还者",
            pan_type="115",
            share_size="",
            video_resolution=(),
            source=(),
            subtitle_language=(),
            subtitle_type=(),
            unlock_points=8,
            validate_status="valid",
            validate_message="",
            is_unlocked=False,
            episode_key="",
            remark="更新至07集",
        )
        self.assertIn("更新至07集", resource_clue_text(resource))
        self.assertTrue(has_episode_clue(resource_clue_text(resource)))


class KeysFromLlmPayloadTests(unittest.TestCase):
    def test_valid_range_requires_evidence_substring(self):
        source = "最后生还者 第二季 更新至07集"
        parsed = keys_from_llm_payload(
            {
                "season": 2,
                "episode_start": 1,
                "episode_end": 7,
                "confidence": 0.9,
                "reason": "title season plus updated-through",
                "evidence": "更新至07集",
            },
            source,
        )
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.keys[0], EpisodeKey(2, 1))
        self.assertEqual(parsed.keys[-1], EpisodeKey(2, 7))
        self.assertEqual(parsed.confidence, 0.9)

    def test_missing_or_foreign_evidence_is_rejected(self):
        source = "更新至07集"
        self.assertFalse(keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.9, "evidence": ""},
            source,
        ).ok)
        self.assertFalse(keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.9, "evidence": "S02E10"},
            source,
        ).ok)

    def test_invalid_range_is_rejected(self):
        source = "更新至07集"
        payload = {
            "season": 2,
            "episode_start": 1,
            "episode_end": 300,
            "confidence": 0.9,
            "evidence": "更新至07集",
        }
        self.assertFalse(keys_from_llm_payload(payload, source).ok)


class DecideLlmEpisodeTests(unittest.TestCase):
    def test_high_confidence_in_tmdb_is_auto(self):
        parsed = keys_from_llm_payload(
            {
                "season": 2,
                "episode_start": 1,
                "episode_end": 7,
                "confidence": 0.8,
                "evidence": "更新至07集",
            },
            "更新至07集",
        )
        self.assertEqual(decide_llm_episode(parsed, {1, 2}, 0.75, 0.45), DECISION_AUTO)

    def test_high_confidence_missing_tmdb_season_is_pending(self):
        parsed = keys_from_llm_payload(
            {
                "season": 9,
                "episode_start": 1,
                "episode_end": 1,
                "confidence": 0.95,
                "evidence": "第1集",
            },
            "第1集",
        )
        self.assertEqual(decide_llm_episode(parsed, {1, 2}, 0.75, 0.45), DECISION_PENDING)

    def test_medium_confidence_is_pending_and_low_is_reject(self):
        source = "更新至07集"
        medium = keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.5, "evidence": "更新至07集"},
            source,
        )
        low = keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.2, "evidence": "更新至07集"},
            source,
        )
        self.assertEqual(decide_llm_episode(medium, {2}, 0.75, 0.45), DECISION_PENDING)
        self.assertEqual(decide_llm_episode(low, {2}, 0.75, 0.45), DECISION_REJECT)

    def test_empty_tmdb_seasons_cannot_auto(self):
        parsed = keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.99, "evidence": "更新至07集"},
            "更新至07集",
        )
        self.assertEqual(decide_llm_episode(parsed, set(), 0.75, 0.45), DECISION_PENDING)

    def test_pending_reason_and_tmdb_season_numbers(self):
        keys = (EpisodeKey(2, 1), EpisodeKey(2, 7))
        self.assertEqual(format_llm_pending_reason(keys), "LLM 识别待确认：S02E01-S02E07")
        self.assertEqual(
            tmdb_season_numbers({"seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": "x"}]}),
            {0, 1},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_hdhive_episode_llm -q`

Expected: FAIL with `ModuleNotFoundError: app.hdhive_episode_llm`

- [ ] **Step 3: Write the module**

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from app.clients.hdhive import HdhiveResource
from app.series_rules import EpisodeKey


DECISION_AUTO = "auto"
DECISION_PENDING = "pending"
DECISION_REJECT = "reject"
MAX_LLM_CALLS_PER_CHECK = 8
LLM_EPISODE_PENDING_PREFIX = "LLM 识别待确认："

_CLUE_RE = re.compile(
    r"更新至|"
    r"第[0-9一二三四五六七八九十百]+集|"
    r"全\d+集|"
    r"(?<![A-Za-z0-9])EP?\d+(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])S\d+(?![A-Za-z0-9])|"
    r"(?<![A-Za-z])Season\s*\d+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LlmEpisodeParse:
    ok: bool
    keys: tuple[EpisodeKey, ...] = ()
    season: int | None = None
    confidence: float = 0.0
    reason: str = ""
    evidence: str = ""


def has_episode_clue(text: str) -> bool:
    return bool(_CLUE_RE.search(str(text or "")))


def resource_clue_text(resource: HdhiveResource) -> str:
    parts = (
        getattr(resource, "title", ""),
        getattr(resource, "remark", ""),
        getattr(resource, "episode_key", ""),
        getattr(resource, "episode_code", ""),
    )
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def tmdb_season_numbers(details: dict | None) -> set[int]:
    seasons = (details or {}).get("seasons")
    if not isinstance(seasons, list):
        return set()
    numbers: set[int] = set()
    for season in seasons:
        if not isinstance(season, dict):
            continue
        raw = season.get("season_number")
        if isinstance(raw, bool):
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            numbers.add(number)
    return numbers


def keys_from_llm_payload(payload: dict | None, source_text: str) -> LlmEpisodeParse:
    data = payload if isinstance(payload, dict) else {}
    evidence = str(data.get("evidence") or "").strip()
    reason = str(data.get("reason") or "").strip()
    source = str(source_text or "")
    try:
        season = int(data.get("season"))
        start = int(data.get("episode_start"))
        end = int(data.get("episode_end"))
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        return LlmEpisodeParse(ok=False, reason=reason, evidence=evidence)
    if evidence == "" or evidence not in source:
        return LlmEpisodeParse(ok=False, confidence=confidence, reason=reason, evidence=evidence)
    if season < 0 or start <= 0 or start > end or end - start > 200:
        return LlmEpisodeParse(ok=False, confidence=confidence, reason=reason, evidence=evidence)
    keys = tuple(EpisodeKey(season, number) for number in range(start, end + 1))
    return LlmEpisodeParse(
        ok=True,
        keys=keys,
        season=season,
        confidence=max(0.0, min(1.0, confidence)),
        reason=reason,
        evidence=evidence,
    )


def decide_llm_episode(
    parsed: LlmEpisodeParse,
    tmdb_seasons: set[int],
    high_confidence: float,
    suggest_confidence: float,
) -> str:
    if not parsed.ok or not parsed.keys or parsed.season is None:
        return DECISION_REJECT
    if parsed.confidence < float(suggest_confidence):
        return DECISION_REJECT
    if parsed.confidence >= float(high_confidence) and parsed.season in tmdb_seasons:
        return DECISION_AUTO
    return DECISION_PENDING


def format_llm_pending_reason(keys: tuple[EpisodeKey, ...]) -> str:
    if not keys:
        return f"{LLM_EPISODE_PENDING_PREFIX}未知"
    if len(keys) == 1:
        span = keys[0].normalized
    else:
        span = f"{keys[0].normalized}-{keys[-1].normalized}"
    return f"{LLM_EPISODE_PENDING_PREFIX}{span}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_hdhive_episode_llm -q`

Expected: PASS, silent.

- [ ] **Step 5: Commit**

```bash
git add app/hdhive_episode_llm.py tests/test_hdhive_episode_llm.py
git commit -m "$(cat <<'EOF'
feat: add HDHive LLM episode parse helpers

Keep clue detection and auto/pending/reject rules out of HTTP and regex parsing.
EOF
)"
```

---

### Task 2: Classifier method, cache, 20s timeout

**Files:**
- Modify: `app/clients/http.py` (`HttpJson.request`)
- Modify: `bridge.py` (`OpenAIClassifier`)
- Modify: `tests/test_openai_fallback.py`
- Modify: `tests/test_http_clients.py` only if an existing HttpJson test needs the new kwarg (keep it compiling; add one small assertion if easy)

- [ ] **Step 1: Write failing classifier tests** at the end of `tests/test_openai_fallback.py`

```python
class HdhiveEpisodeParseTests(unittest.TestCase):
    def _classifier(self, http):
        class FakeConfig:
            http_timeout = 60
            openai_high_confidence = 0.75
            openai_suggest_confidence = 0.45
            openai_classify_enabled = True
            openai_api_key = "test-key"
            openai_base_url = "https://open.sub2api.top/v1"
            openai_model = "gpt-test"

        return bridge.OpenAIClassifier(FakeConfig(), http=http)

    def test_parse_hdhive_episode_sends_schema_without_secrets(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, url, method="GET", payload=None, headers=None, timeout=None):
                self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
                return {
                    "output_text": (
                        '{"season":2,"episode_start":1,"episode_end":7,'
                        '"confidence":0.9,"reason":"title","evidence":"更新至07集"}'
                    )
                }

        http = FakeHttp()
        result = self._classifier(http).parse_hdhive_episode(
            tmdb_id="100088",
            resource_slug="pack",
            title="最后生还者 第二季",
            remark="更新至07集",
            show_title="最后生还者",
            tmdb_seasons=[{"season_number": 1, "name": "Season 1"}, {"season_number": 2, "name": "Season 2"}],
        )
        self.assertEqual(result["season"], 2)
        self.assertEqual(result["episode_end"], 7)
        self.assertEqual(http.calls[0]["timeout"], 20)
        dumped = str(http.calls[0]["payload"])
        self.assertNotIn("test-key", dumped)
        self.assertNotIn("115cdn.com", dumped)
        self.assertIn("更新至07集", dumped)
        self.assertIn("json_schema", dumped)

    def test_parse_hdhive_episode_caches_success_and_failure(self):
        class FakeHttp:
            def __init__(self):
                self.calls = 0

            def request(self, url, method="GET", payload=None, headers=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("slow")
                return {
                    "output_text": (
                        '{"season":1,"episode_start":1,"episode_end":1,'
                        '"confidence":0.9,"reason":"ok","evidence":"第1集"}'
                    )
                }

        http = FakeHttp()
        classifier = self._classifier(http)
        first = classifier.parse_hdhive_episode(
            tmdb_id="1", resource_slug="a", title="剧", remark="第1集", show_title="剧"
        )
        second = classifier.parse_hdhive_episode(
            tmdb_id="1", resource_slug="a", title="剧", remark="第1集", show_title="剧"
        )
        self.assertFalse(first.get("ok"))
        self.assertEqual(first, second)
        self.assertEqual(http.calls, 1)

    def test_disabled_classifier_does_not_call_http(self):
        class FakeHttp:
            def request(self, *args, **kwargs):
                raise AssertionError("http should not run")

        class FakeConfig:
            http_timeout = 60
            openai_high_confidence = 0.75
            openai_suggest_confidence = 0.45
            openai_classify_enabled = False
            openai_api_key = ""
            openai_base_url = "https://open.sub2api.top/v1"
            openai_model = "gpt-test"

        result = bridge.OpenAIClassifier(FakeConfig(), http=FakeHttp()).parse_hdhive_episode(
            tmdb_id="1", resource_slug="a", title="剧", remark="第1集", show_title="剧"
        )
        self.assertFalse(result.get("ok"))
```

- [ ] **Step 2: Run the new tests**

Run: `python3 -m unittest tests.test_openai_fallback.HdhiveEpisodeParseTests -q`

Expected: FAIL with `AttributeError: parse_hdhive_episode`

- [ ] **Step 3: Add optional timeout to `HttpJson.request`**

In `app/clients/http.py`, change `request` to accept `timeout: int | None = None` and pass `int(timeout) if timeout is not None else self.timeout` into `_read_response`. Do not change other clients.

- [ ] **Step 4: Implement `OpenAIClassifier.parse_hdhive_episode`**

Add next to `identify_media` in `bridge.py`:

```python
_EPISODE_CACHE_OK_TTL_SECONDS = 6 * 3600
_EPISODE_CACHE_FAIL_TTL_SECONDS = 30 * 60
_EPISODE_CACHE_MAX_ENTRIES = 256
_EPISODE_HTTP_TIMEOUT_SECONDS = 20
```

In `__init__`, add:

```python
self._episode_cache: dict[str, tuple[float, float, dict[str, Any]]] = {}
self._episode_cache_lock = threading.Lock()
```

Method behavior:

1. If `not self.enabled`, return `{"ok": False, "season": 0, "episode_start": 0, "episode_end": 0, "confidence": 0.0, "reason": "disabled", "evidence": ""}` without HTTP.
2. Cache key = sha256 of `tmdb_id|resource_slug|title|remark|episode_key|episode_code` (UTF-8).
3. On hit within the stored TTL, return a copy of the cached dict.
4. POST `{base_url}/responses` with json_schema name `hdhive_episode` and required fields `season`, `episode_start`, `episode_end`, `confidence`, `reason`, `evidence`. Call `self.http.request(..., timeout=20)` (or `_EPISODE_HTTP_TIMEOUT_SECONDS`).
5. System prompt (exact intent): 你是剧集季集抽取器。集数必须来自资源标题/备注原文；备注「更新至07集」则 episode_end=7，不得改成 TMDB 总集数或当前正播集。文本缺季号时可用剧名和 TMDB 季列表推断 season。给不出合法季和集时仍填字段，由调用方校验。evidence 必须是原文片段。
6. User payload keys only: `show_title`, `tmdb_id`, `tmdb_seasons`, `title`, `remark`, `episode_key`, `episode_code`. Never send API keys, unlock URLs, cookies.
7. On timeout/HTTP/JSON/schema error: `ok=False`, `reason` is a short class/name (not the raw URL). Cache failure for 30 minutes.
8. On success: include `ok: True` plus the schema fields. Cache 6 hours.
9. Evict oldest entry when cache exceeds 256 keys.

`_extract_json` already exists; reuse it.

- [ ] **Step 5: Run classifier tests**

Run: `python3 -m unittest tests.test_openai_fallback.HdhiveEpisodeParseTests tests.test_http_clients -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/clients/http.py bridge.py tests/test_openai_fallback.py tests/test_http_clients.py
git commit -m "$(cat <<'EOF'
feat: parse HDHive episodes through OpenAI classifier

Reuse the existing model settings with a short timeout and cache so subscription checks do not hammer the API.
EOF
)"
```

---

### Task 3: Call the parser inside subscription check

**Files:**
- Modify: `app/hdhive_subscriptions.py`
- Modify: `tests/test_hdhive_subscriptions.py`

- [ ] **Step 1: Extend `make_service` and add failing service tests**

In `HdhiveSubscriptionServiceTests.make_service`, add `episode_parser=None` and pass it into `HdhiveSubscriptionService(...)`.

Add this fake near the other fakes:

```python
class FakeEpisodeParser:
    def __init__(self, result=None, error=None, *, enabled=True):
        self.enabled = enabled
        self.result = result or {}
        self.error = error
        self.calls = []
        self.high_confidence = 0.75
        self.suggest_confidence = 0.45

    def parse_hdhive_episode(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return dict(self.result)
```

Add tests (same class):

```python
def test_llm_high_confidence_enqueues_seasonless_updated_through(self):
    tmdb = FakeTmdbResolver(
        {
            "ok": True,
            "status": "Returning Series",
            "seasons": [
                {"season_number": 1, "episode_count": 10},
                {"season_number": 2, "episode_count": 8},
            ],
        }
    )
    parser = FakeEpisodeParser(
        {
            "ok": True,
            "season": 2,
            "episode_start": 1,
            "episode_end": 7,
            "confidence": 0.91,
            "reason": "current season",
            "evidence": "更新至07集",
        }
    )
    unlock_items = [HdhiveUnlockItem("pack", True, "https://115cdn.com/s/pack?password=abcd", "", "", False)]
    directory, store, subscription, proxy, service, intake_calls = self.make_service(
        [resource("pack", episode_key="", title="最后生还者", remark="更新至07集")],
        unlock_items,
        tmdb_resolver=tmdb,
        episode_parser=parser,
    )
    try:
        result = service.check(subscription.id)
        item = store.list_items(subscription.id)[0]
    finally:
        directory.cleanup()
    self.assertEqual(len(parser.calls), 1)
    self.assertNotIn("password", str(parser.calls[0]))
    self.assertEqual(result.enqueued, 1)
    self.assertEqual(result.summary["unparsed"], 0)
    self.assertEqual(item.status, "enqueued")
    self.assertEqual(item.normalized_episode_key, "S02E01-S02E07")
    self.assertEqual(proxy.unlock_calls, [["pack"]])
    self.assertEqual(intake_calls, [(["https://115cdn.com/s/pack?password=abcd"], "464100862")])

def test_llm_medium_confidence_is_pending_without_unlock(self):
    tmdb = FakeTmdbResolver({"ok": True, "seasons": [{"season_number": 2, "episode_count": 8}]})
    parser = FakeEpisodeParser(
        {
            "ok": True,
            "season": 2,
            "episode_start": 1,
            "episode_end": 7,
            "confidence": 0.5,
            "reason": "maybe",
            "evidence": "更新至07集",
        }
    )
    directory, store, subscription, proxy, service, intake_calls = self.make_service(
        [resource("pack", episode_key="", title="最后生还者", remark="更新至07集")],
        [HdhiveUnlockItem("pack", True, "https://115cdn.com/s/pack?password=abcd", "", "", False)],
        tmdb_resolver=tmdb,
        episode_parser=parser,
    )
    try:
        result = service.check(subscription.id)
        item = store.list_items(subscription.id)[0]
    finally:
        directory.cleanup()
    self.assertEqual(result.enqueued, 0)
    self.assertEqual(item.status, "pending_confirmation")
    self.assertEqual(item.normalized_episode_key, "S02E01-S02E07")
    self.assertIn("LLM 识别待确认：S02E01-S02E07", item.last_error)
    self.assertEqual(proxy.unlock_calls, [])
    self.assertEqual(intake_calls, [])

def test_llm_is_not_called_without_clue_or_when_regex_parses(self):
    tmdb = FakeTmdbResolver({"ok": True, "seasons": [{"season_number": 1}, {"season_number": 2}]})
    parser = FakeEpisodeParser({"ok": True, "season": 2, "episode_start": 1, "episode_end": 1, "confidence": 0.9, "evidence": "x"})
    directory, store, subscription, proxy, service, _intake = self.make_service(
        [
            resource("bare", episode_key="", title="最后生还者 第二季", remark=""),
            resource("parsed", episode_key="", title="Show", remark="S01E02"),
        ],
        tmdb_resolver=tmdb,
        episode_parser=parser,
    )
    try:
        service.check(subscription.id)
        items = {item.resource_slug: item for item in store.list_items(subscription.id)}
    finally:
        directory.cleanup()
    self.assertEqual(parser.calls, [])
    self.assertEqual(items["bare"].status, "unparsed")
    self.assertEqual(items["parsed"].normalized_episode_key, "S01E02")
    self.assertEqual(proxy.unlock_calls, [])

def test_llm_error_or_disabled_leaves_unparsed(self):
    tmdb = FakeTmdbResolver({"ok": True, "seasons": [{"season_number": 1}, {"season_number": 2}]})
    for parser in (
        FakeEpisodeParser(enabled=False),
        FakeEpisodeParser(error=TimeoutError("slow")),
        FakeEpisodeParser({"ok": False, "reason": "nope"}),
    ):
        directory, store, subscription, proxy, service, _intake = self.make_service(
            [resource("pack", episode_key="", title="剧", remark="更新至07集")],
            tmdb_resolver=tmdb,
            episode_parser=parser,
        )
        try:
            result = service.check(subscription.id)
            item = store.list_items(subscription.id)[0]
        finally:
            directory.cleanup()
        self.assertEqual(result.enqueued, 0)
        self.assertEqual(item.status, "unparsed")
        self.assertEqual(proxy.unlock_calls, [])

def test_llm_call_cap_leaves_remaining_unparsed(self):
    tmdb = FakeTmdbResolver(
        {
            "ok": True,
            "seasons": [
                {"season_number": 1, "episode_count": 20},
                {"season_number": 2, "episode_count": 20},
            ],
        }
    )
    parser = FakeEpisodeParser(
        {"ok": True, "season": 1, "episode_start": 1, "episode_end": 1, "confidence": 0.9, "evidence": "第1集"}
    )
    resources = [
        resource(f"ep{index}", episode_key="", title="剧", remark="第1集", points=0)
        for index in range(9)
    ]
    directory, store, subscription, proxy, service, _intake = self.make_service(
        resources,
        [HdhiveUnlockItem(item.slug, True, f"https://115cdn.com/s/{item.slug}?password=abcd", "", "", False) for item in resources],
        tmdb_resolver=tmdb,
        episode_parser=parser,
    )
    try:
        service.check(subscription.id)
        items = store.list_items(subscription.id)
    finally:
        directory.cleanup()
    self.assertEqual(len(parser.calls), 8)
    self.assertEqual(sum(1 for item in items if item.status == "unparsed"), 1)

def test_llm_special_episode_still_skipped_by_default(self):
    tmdb = FakeTmdbResolver({"ok": True, "seasons": [{"season_number": 0}, {"season_number": 1}]})
    parser = FakeEpisodeParser(
        {"ok": True, "season": 0, "episode_start": 1, "episode_end": 1, "confidence": 0.9, "evidence": "第1集"}
    )
    directory, store, subscription, proxy, service, _intake = self.make_service(
        [resource("special", episode_key="", title="剧 番外", remark="第1集")],
        tmdb_resolver=tmdb,
        episode_parser=parser,
    )
    try:
        service.check(subscription.id)
        item = store.list_items(subscription.id)[0]
    finally:
        directory.cleanup()
    self.assertEqual(item.status, "filtered")
    self.assertEqual(item.skip_reason, "特殊集默认跳过")
    self.assertEqual(proxy.unlock_calls, [])
```

Keep `test_multi_season_tmdb_does_not_guess_seasonless_updated_through` unchanged (no parser).

- [ ] **Step 2: Run the new service tests**

Run: `python3 -m unittest tests.HdhiveSubscriptionServiceTests.test_llm_high_confidence_enqueues_seasonless_updated_through tests.HdhiveSubscriptionServiceTests.test_llm_medium_confidence_is_pending_without_unlock -q`

Use the full class path:

```sh
python3 -m unittest tests.test_hdhive_subscriptions.HdhiveSubscriptionServiceTests.test_llm_high_confidence_enqueues_seasonless_updated_through -q
```

Expected: FAIL (`episode_parser` unexpected kwarg, or parser never called).

- [ ] **Step 3: Wire `check()`**

In `HdhiveSubscriptionService.__init__`, add `episode_parser: Any | None = None` and `self.episode_parser = episode_parser`.

In `check()`, after `default_season = _default_season_from_tmdb(tmdb_details)` and **before** the resource loop:

```python
from app.hdhive_episode_llm import (
    DECISION_AUTO,
    DECISION_PENDING,
    MAX_LLM_CALLS_PER_CHECK,
    decide_llm_episode,
    format_llm_pending_reason,
    has_episode_clue,
    keys_from_llm_payload,
    resource_clue_text,
    tmdb_season_numbers,
)
```

Prefer a module-level import at the top of `app/hdhive_subscriptions.py` instead of inline.

Then:

```python
season_numbers = tmdb_season_numbers(tmdb_details)
parser = self.episode_parser
parser_enabled = bool(
    parser is not None
    and getattr(parser, "enabled", False)
    and hasattr(parser, "parse_hdhive_episode")
)
high_confidence = float(getattr(parser, "high_confidence", 0.75) or 0.75)
suggest_confidence = float(getattr(parser, "suggest_confidence", 0.45) or 0.45)
llm_calls = 0
llm_decision_by_resource: dict[int, str] = {}
```

Inside the 115 resource loop, replace the current `parsed_keys` / `key` assignment with:

```python
parsed_keys = episode_keys(resource, default_season=default_season)
if (
    not parsed_keys
    and parser_enabled
    and llm_calls < MAX_LLM_CALLS_PER_CHECK
):
    clue_text = resource_clue_text(resource)
    if has_episode_clue(clue_text):
        llm_calls += 1
        try:
            raw = parser.parse_hdhive_episode(
                tmdb_id=str(subscription.tmdb_id or ""),
                resource_slug=str(resource.slug or ""),
                title=str(resource.title or ""),
                remark=str(resource.remark or ""),
                episode_key=str(getattr(resource, "episode_key", "") or ""),
                episode_code=str(getattr(resource, "episode_code", "") or ""),
                show_title=str(subscription.title or ""),
                tmdb_seasons=[
                    season
                    for season in (tmdb_details.get("seasons") or [])
                    if isinstance(season, dict)
                ],
            )
        except Exception:
            LOG.warning(
                "HDHive LLM episode parse failed subscription_id=%s resource_slug=%s",
                subscription.id,
                resource.slug,
                exc_info=True,
            )
            raw = {}
        parsed = keys_from_llm_payload(raw, clue_text)
        decision = decide_llm_episode(parsed, season_numbers, high_confidence, suggest_confidence)
        llm_decision_by_resource[id(resource)] = decision
        if decision in {DECISION_AUTO, DECISION_PENDING} and parsed.keys:
            parsed_keys = parsed.keys
            LOG.info(
                "HDHive LLM episode parsed subscription_id=%s resource_slug=%s decision=%s key=%s confidence=%s",
                subscription.id,
                resource.slug,
                decision,
                _format_episode_keys(parsed_keys),
                parsed.confidence,
            )
key = _format_episode_keys(parsed_keys) if parsed_keys else episode_key(resource, default_season=default_season)
```

Do not log title, remark, unlock URL, or tokens.

In the loop that currently marks `unparsed` / `filtered` (`for key, candidates in grouped.items():` around the `if not parsed_keys` block), after a successful filter match (the `for item in items: if item.status == "filtered": reset` branch) and before falling through, handle pending:

```python
if llm_decision_by_resource.get(id(candidates[0])) == DECISION_PENDING:
    for item in items:
        if item.status != "enqueued" and not protects_unlock_outcome(item):
            self.store.mark_item_pending(item.id, format_llm_pending_reason(parsed_keys))
    continue
```

Place this **after** the filter skip (`if not any(episode_filter.matches...)`) so `S00` still becomes `filtered` instead of pending.

Do not change `episode_keys()`.

- [ ] **Step 4: Run subscription tests**

```sh
python3 -m unittest tests.test_hdhive_subscriptions -q
```

Expected: PASS, including `test_multi_season_tmdb_does_not_guess_seasonless_updated_through`.

- [ ] **Step 5: Commit**

```bash
git add app/hdhive_subscriptions.py tests/test_hdhive_subscriptions.py
git commit -m "$(cat <<'EOF'
feat: use LLM episode fallback during HDHive checks

Call the parser only after regex fails and a clue exists, then reuse the existing unlock path.
EOF
)"
```

---

### Task 4: Factory wiring and docs

**Files:**
- Modify: `bridge.py` (`create_hdhive_subscription_service` and `run_forever` call site)
- Modify: `tests/test_hdhive_bridge.py`
- Modify: `CHANGELOG.md`
- Modify: `PRODUCT.md`

- [ ] **Step 1: Write the failing factory test**

In `tests/test_hdhive_bridge.py`, next to `test_subscription_service_factory_passes_tmdb_and_emby_clients`:

```python
def test_subscription_service_factory_passes_openai_classifier(self):
    with tempfile.TemporaryDirectory() as directory:
        config = SimpleNamespace(
            hdhive_enabled=True,
            database_path=str(Path(directory) / "tasks.db"),
            hdhive_auto_unlock_max_points=20,
        )
        classifier = object()
        service = bridge.create_hdhive_subscription_service(
            config,
            SimpleNamespace(proxy=object()),
            lambda _urls, _chat: None,
            openai_classifier=classifier,
        )
        self.assertIs(service.episode_parser, classifier)
```

- [ ] **Step 2: Run it**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_subscription_service_factory_passes_openai_classifier -q`

Expected: FAIL (`unexpected keyword argument 'openai_classifier'`).

- [ ] **Step 3: Wire the factory**

Add `openai_classifier: Any | None = None` to `create_hdhive_subscription_service` and pass `episode_parser=openai_classifier` into `HdhiveSubscriptionService`.

In `run_forever`, the `openai_classifier = OpenAIClassifier(config)` instance already exists above the subscription factory. Pass `openai_classifier=openai_classifier` into that `create_hdhive_subscription_service(...)` call.

- [ ] **Step 4: Docs**

`CHANGELOG.md` — add at the top (new version heading only if you are also bumping `app.__version__`; otherwise an `## Unreleased` section):

```markdown
- **HDHive 订阅 LLM 季集兜底**：正则无法识别且标题/备注有集数线索时，复用现有 OpenAI 推断季号。高置信且季号在 TMDB 季列表才自动入队，否则进入待确认。
```

`PRODUCT.md` HDHive paragraph — append one sentence: 打开 OpenAI 分类后，无法用正则识别的资源会再尝试模型识别，低把握时仍待确认。

Do not add a new env var.

- [ ] **Step 5: Run targeted tests**

```sh
python3 -m unittest tests.test_hdhive_bridge tests.test_hdhive_subscriptions tests.test_hdhive_episode_llm tests.test_openai_fallback.HdhiveEpisodeParseTests -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bridge.py tests/test_hdhive_bridge.py CHANGELOG.md PRODUCT.md
git commit -m "$(cat <<'EOF'
feat: wire HDHive episode parser to OpenAI settings

Subscription checks reuse the existing classifier; docs mention the fallback.
EOF
)"
```

---

### Task 5: Full regression

- [ ] **Step 1: Run the HDHive + OpenAI + HTTP suites**

```sh
python3 -m unittest tests.test_hdhive_episode_llm tests.test_hdhive_subscriptions tests.test_hdhive_bridge tests.test_openai_fallback tests.test_http_clients tests.test_hdhive_web -q
```

Expected: PASS.

- [ ] **Step 2: If the full suite is cheap enough, run it**

```sh
python3 -m unittest discover -s tests -p 'test*.py' -q
```

Expected: PASS. The known CI flake `Directory not empty` in `test_run_forever_starts_invalid_self_share_probe_only_when_explicitly_enabled` is unrelated; do not “fix” it in this change.

- [ ] **Step 3: Spec checklist (no extra commit unless something failed)**

Confirm by test name:

1. High confidence + TMDB season → enqueue — `test_llm_high_confidence_enqueues_seasonless_updated_through`
2. Medium confidence → pending — `test_llm_medium_confidence_is_pending_without_unlock`
3. No clue → no call — `test_llm_is_not_called_without_clue_or_when_regex_parses`
4. Regex success → no call — same test
5. Disabled / timeout / bad payload → unparsed — `test_llm_error_or_disabled_leaves_unparsed`
6. Cache — `test_parse_hdhive_episode_caches_success_and_failure`
7. Cap 8 — `test_llm_call_cap_leaves_remaining_unparsed`
8. Specials — `test_llm_special_episode_still_skipped_by_default`
9. Guess guard without LLM — `test_multi_season_tmdb_does_not_guess_seasonless_updated_through`
10. No secrets in payload — classifier + service tests

---

## Self-review

**Spec coverage**

| Spec item | Task |
| --- | --- |
| `episode_keys()` unchanged | Task 3 (do not edit that function) |
| `has_episode_clue` / decide / keys | Task 1 |
| OpenAI reuse, schema, cache, 20s | Task 2 |
| Call before grouping, auto vs pending | Task 3 |
| Max 8 calls / errors do not abort check | Task 3 |
| Factory + existing OpenAI flag | Task 4 |
| No new schema / no Vue rewrite | (omitted on purpose) |
| Tests 1–10 | Task 3 + 2 + 5 |

**Type names used everywhere:** `LlmEpisodeParse`, `DECISION_AUTO` / `DECISION_PENDING` / `DECISION_REJECT`, `parse_hdhive_episode`, `episode_parser`, `MAX_LLM_CALLS_PER_CHECK`.
