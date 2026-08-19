# Telegram HDHive Search Result Titles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make HDHive search candidate names readable: full title in the message, name-only buttons, and the selected title on the resource page header.

**Architecture:** Keep CMS TMDB search and the existing session. Add a trailing-ellipsis helper and a shared candidate label helper in `app/telegram_ui.py`. Candidate buttons become `序号. 片名` (max 64 chars). The candidate message and `format_hdhive_resources` header both use `片名 (年) · 电影|剧集 · TMDB id`.

**Tech Stack:** Python 3.12, `unittest`, existing Telegram long-poll bridge.

**Spec:** `docs/superpowers/specs/2026-08-19-tg-search-result-titles-design.md`

---

## File map

| File | Responsibility |
| --- | --- |
| `app/telegram_ui.py` | `truncate_end`, `format_hdhive_candidate_label`, name-only `hdhive_candidate_keyboard` |
| `bridge.py` | Candidate result message; resource header from `session.candidates` |
| `tests/test_hdhive_bridge.py` | Keyboard, truncation, message, and header tests |

Do not change session schema, CMS search fields, resource-row buttons, or the `/help` reply keyboard.

Use Unicode ellipsis `…` (U+2026), not three dots. Run tests with `python3 -m unittest`.

---

### Task 1: Trailing-ellipsis helper

**Files:**
- Modify: `app/telegram_ui.py:236`
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write the failing tests**

Add `truncate_end` to the existing `app.telegram_ui` import in `tests/test_hdhive_bridge.py`, then add two methods on `HdhiveBridgeTests`:

```python
from app.telegram_ui import (
    format_hdhive_subscriptions,
    hdhive_candidate_keyboard,
    hdhive_resource_keyboard,
    truncate_end,
)
```

```python
    def test_truncate_end_keeps_the_readable_prefix(self):
        self.assertEqual(truncate_end("abcdefghij", 8), "abcdefg…")
        self.assertEqual(truncate_end("短名", 8), "短名")

    def test_truncate_end_fits_a_single_ellipsis_when_limit_is_one(self):
        self.assertEqual(truncate_end("abc", 1), "…")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_truncate_end_keeps_the_readable_prefix tests.test_hdhive_bridge.HdhiveBridgeTests.test_truncate_end_fits_a_single_ellipsis_when_limit_is_one`

Expected: FAIL with `ImportError` or `AttributeError` because `truncate_end` does not exist. `truncate_text("abcdefghij", 8)` would be `ab...hij`, which is the wrong algorithm.

- [ ] **Step 3: Add `truncate_end` next to `truncate_text` in `app/telegram_ui.py`**

Insert immediately after `truncate_text`:

```python
def truncate_end(text: str, limit: int) -> str:
    value = str(text or "")
    width = max(1, int(limit))
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"
```

Do not change `truncate_text`. Path names and error snippets still need the head-and-tail cut.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_truncate_end_keeps_the_readable_prefix tests.test_hdhive_bridge.HdhiveBridgeTests.test_truncate_end_fits_a_single_ellipsis_when_limit_is_one`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/telegram_ui.py tests/test_hdhive_bridge.py
git commit -m "feat: add trailing-ellipsis helper for Telegram buttons"
```

---

### Task 2: Name-only candidate buttons

**Files:**
- Modify: `app/telegram_ui.py:289-300`
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write the failing tests**

Add these methods on `HdhiveBridgeTests`. Keep `test_candidate_keyboard_only_offers_subscription_for_tv` and extend it with label assertions:

```python
    def test_candidate_keyboard_only_offers_subscription_for_tv(self):
        keyboard = hdhive_candidate_keyboard(
            "session",
            [
                {"media_type": "movie", "title": "电影", "year": "2026"},
                {"media_type": "tv", "title": "剧集", "year": "2026"},
            ],
        )
        rows = keyboard["inline_keyboard"]
        self.assertEqual(rows[0][0]["text"], "1. 电影")
        self.assertEqual(rows[1][0]["text"], "2. 剧集")
        self.assertEqual(rows[1][1]["text"], "订阅此剧")
        self.assertEqual(rows[-1][0]["text"], "取消搜索")
        callbacks = [
            button["callback_data"]
            for row in rows
            for button in row
            if "callback_data" in button
        ]
        self.assertNotIn("hive:subscribe:session:0", callbacks)
        self.assertIn("hive:subscribe:session:1", callbacks)
        self.assertNotIn("2026", rows[0][0]["text"])
        self.assertNotIn("[电影]", rows[0][0]["text"])
        self.assertNotIn("[剧集]", rows[1][0]["text"])

    def test_candidate_keyboard_uses_untitled_fallback(self):
        keyboard = hdhive_candidate_keyboard("session", [{"media_type": "movie", "title": "", "year": "2026"}])
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "1. 未命名")

    def test_candidate_keyboard_truncates_long_titles_at_the_end(self):
        title = "龙" * 80
        keyboard = hdhive_candidate_keyboard("session", [{"media_type": "movie", "title": title, "year": "2026"}])
        label = keyboard["inline_keyboard"][0][0]["text"]
        self.assertTrue(label.startswith("1. 龙"))
        self.assertTrue(label.endswith("…"))
        self.assertEqual(len(label), 64)
        self.assertNotIn("2026", label)
        self.assertNotIn("...", label)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_keyboard_only_offers_subscription_for_tv tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_keyboard_uses_untitled_fallback tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_keyboard_truncates_long_titles_at_the_end`

Expected: FAIL because current labels are `1. 电影 (2026) [电影]` and long titles are cut with `...` in the middle.

- [ ] **Step 3: Change `hdhive_candidate_keyboard`**

Replace the function body in `app/telegram_ui.py` with:

```python
def hdhive_candidate_keyboard(session_id: str, candidates: list[dict[str, str]]) -> dict[str, Any]:
    buttons = []
    for index, candidate in enumerate(candidates[:12]):
        title = str(candidate.get("title") or "未命名").strip() or "未命名"
        label = truncate_end(f"{index + 1}. {title}", 64)
        row = [{"text": label, "callback_data": f"hive:candidate:{session_id}:{index}"}]
        if candidate.get("media_type") == "tv":
            row.append({"text": "订阅此剧", "callback_data": f"hive:subscribe:{session_id}:{index}"})
        buttons.append(row)
    buttons.append([{"text": "取消搜索", "callback_data": f"hive:cancel:{session_id}"}])
    return {"inline_keyboard": buttons}
```

Leave subscribe and cancel callback data unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_keyboard_only_offers_subscription_for_tv tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_keyboard_uses_untitled_fallback tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_keyboard_truncates_long_titles_at_the_end`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/telegram_ui.py tests/test_hdhive_bridge.py
git commit -m "feat: show only the movie name on HDHive search buttons"
```

---

### Task 3: Shared candidate label

**Files:**
- Modify: `app/telegram_ui.py` (after `truncate_end`)
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write the failing tests**

Add to the `app.telegram_ui` import:

```python
from app.telegram_ui import (
    format_hdhive_candidate_label,
    format_hdhive_subscriptions,
    hdhive_candidate_keyboard,
    hdhive_resource_keyboard,
    truncate_end,
)
```

```python
    def test_candidate_label_uses_full_title_and_chinese_type(self):
        self.assertEqual(
            format_hdhive_candidate_label(
                {"title": "攻壳机动队 SAC_2045", "year": "2020", "media_type": "tv", "tmdb_id": "80986"}
            ),
            "攻壳机动队 SAC_2045 (2020) · 剧集 · TMDB 80986",
        )
        self.assertEqual(
            format_hdhive_candidate_label(
                {"title": "搏击俱乐部", "year": "1999", "media_type": "movie", "tmdb_id": "550"}
            ),
            "搏击俱乐部 (1999) · 电影 · TMDB 550",
        )

    def test_candidate_label_fills_missing_title_and_year(self):
        self.assertEqual(
            format_hdhive_candidate_label({"media_type": "movie", "tmdb_id": "550"}),
            "未命名 (年份未知) · 电影 · TMDB 550",
        )
```

Do not truncate the title in this helper. The message and resource header always show the full name.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_label_uses_full_title_and_chinese_type tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_label_fills_missing_title_and_year`

Expected: FAIL with `ImportError` because `format_hdhive_candidate_label` does not exist.

- [ ] **Step 3: Add the helper in `app/telegram_ui.py`**

Place it after `truncate_end` and before `format_taskstore_status`:

```python
def format_hdhive_candidate_label(candidate: dict[str, str] | None) -> str:
    item = candidate if isinstance(candidate, dict) else {}
    title = str(item.get("title") or "未命名").strip() or "未命名"
    year = str(item.get("year") or "").strip() or "年份未知"
    media_type = "电影" if item.get("media_type") == "movie" else "剧集"
    tmdb_id = str(item.get("tmdb_id") or "").strip() or "-"
    return f"{title} ({year}) · {media_type} · TMDB {tmdb_id}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_label_uses_full_title_and_chinese_type tests.test_hdhive_bridge.HdhiveBridgeTests.test_candidate_label_fills_missing_title_and_year`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/telegram_ui.py tests/test_hdhive_bridge.py
git commit -m "feat: format HDHive candidate titles for Telegram copy"
```

---

### Task 4: Candidate search message

**Files:**
- Modify: `bridge.py:164-188` (import) and `bridge.py:4368-4372`
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_search_query_lists_full_titles_and_name_only_buttons(self):
        telegram = FakeTelegram()
        cms = SimpleNamespace(
            search_movie=lambda keyword, page=1, page_size=8: {
                "code": 200,
                "data": {"results": [{"id": 550, "title": "搏击俱乐部", "release_date": "1999-10-15"}]},
            },
            search_tv=lambda keyword, page=1, page_size=8: {
                "code": 200,
                "data": {"results": [{"id": 80986, "name": "攻壳机动队 SAC_2045", "first_air_date": "2020-04-23"}]},
            },
        )
        workflow = HdhiveWorkflow(cms, FakeProxy(), HdhiveSessionStore())
        allowed = "464100862"
        update = {
            "message": {
                "chat": {"id": allowed},
                "from": {"id": allowed},
                "text": "/搜索",
            }
        }
        bridge.handle_update(
            update, object(), telegram, allowed, object(), poll_status=False, hdhive_workflow=workflow
        )
        update["message"]["text"] = "攻壳"
        bridge.handle_update(
            update, object(), telegram, allowed, object(), poll_status=False, hdhive_workflow=workflow
        )
        text = telegram.messages[-1][1]
        keyboard = telegram.messages[-1][2]
        self.assertIn("1. 搏击俱乐部 (1999) · 电影 · TMDB 550", text)
        self.assertIn("2. 攻壳机动队 SAC_2045 (2020) · 剧集 · TMDB 80986", text)
        self.assertNotIn("[电影]", text)
        self.assertNotIn("TMDB:", text)
        labels = [
            row[0]["text"]
            for row in keyboard["inline_keyboard"]
            if row[0]["callback_data"].startswith("hive:candidate:")
        ]
        self.assertEqual(labels, ["1. 搏击俱乐部", "2. 攻壳机动队 SAC_2045"])
```

`search_candidates` returns movies first, then TV. `search_candidates` already maps `name` → `title` and `first_air_date` / `release_date` → `year`. Do not add CMS fields.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_search_query_lists_full_titles_and_name_only_buttons`

Expected: FAIL because the message still uses `1. 搏击俱乐部 (1999) [电影] TMDB:550`.

- [ ] **Step 3: Use the shared label in `handle_update`**

Add `format_hdhive_candidate_label` to the `app.telegram_ui` import in `bridge.py`.

Replace the candidate list loop:

```python
                lines = ["请选择要查询的 TMDB 媒体："]
                for index, candidate in enumerate(candidates, 1):
                    lines.append(f"{index}. {format_hdhive_candidate_label(candidate)}")
```

Keep the existing `hdhive_candidate_keyboard(...)` `send_message` call and the `HdhiveSelectionError` / `HdhiveProxyError` handler.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_search_query_lists_full_titles_and_name_only_buttons`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bridge.py tests/test_hdhive_bridge.py
git commit -m "feat: list full HDHive search titles in the Telegram message"
```

---

### Task 5: Resource page header

**Files:**
- Modify: `bridge.py:2285-2291`
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write the failing tests**

```python
    def test_resource_header_uses_selected_candidate_title(self):
        workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "攻壳")
        workflow.set_candidates(
            session_id,
            [
                {
                    "media_type": "tv",
                    "tmdb_id": "80986",
                    "title": "攻壳机动队 SAC_2045",
                    "year": "2020",
                }
            ],
        )
        workflow.load_resources(session_id, "tv", "80986")
        text, _keyboard = bridge.format_hdhive_resources(workflow, session_id)
        self.assertTrue(text.startswith("HDHive 资源：攻壳机动队 SAC_2045 (2020) · 剧集 · TMDB 80986"))
        self.assertNotIn("tv / TMDB", text)

    def test_resource_header_falls_back_when_candidate_is_missing(self):
        workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
        session_id = workflow.sessions.begin("464100862", "Example")
        workflow.load_resources(session_id, "movie", "550")
        text, _keyboard = bridge.format_hdhive_resources(workflow, session_id)
        self.assertTrue(text.startswith("HDHive 资源：未命名 (年份未知) · 电影 · TMDB 550"))
```

`load_resources` sets `session.media_type` and `session.tmdb_id`. Do not add session fields. Match candidates by `media_type` and `tmdb_id`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_resource_header_uses_selected_candidate_title tests.test_hdhive_bridge.HdhiveBridgeTests.test_resource_header_falls_back_when_candidate_is_missing`

Expected: FAIL because the header is still `HDHive 资源：tv / TMDB 80986` or `HDHive 资源：movie / TMDB 550`.

- [ ] **Step 3: Resolve the selected candidate in `format_hdhive_resources`**

Replace the header line in `bridge.py`:

```python
    selected = next(
        (
            item
            for item in session.candidates
            if item.get("media_type") == session.media_type
            and str(item.get("tmdb_id") or "") == str(session.tmdb_id)
        ),
        {"media_type": session.media_type, "tmdb_id": session.tmdb_id},
    )
    lines = [f"HDHive 资源：{format_hdhive_candidate_label(selected)}", ""]
```

Leave the resource rows, filter buttons, unlock buttons, and keyboard call unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_resource_header_uses_selected_candidate_title tests.test_hdhive_bridge.HdhiveBridgeTests.test_resource_header_falls_back_when_candidate_is_missing`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bridge.py tests/test_hdhive_bridge.py
git commit -m "feat: show the selected title on the HDHive resource header"
```

---

### Task 6: Related suite

**Files:**
- Test: `tests/test_hdhive_bridge.py`, `tests/test_hdhive_workflow.py`

- [ ] **Step 1: Run the HDHive Telegram tests**

Run: `python3 -m unittest tests.test_hdhive_bridge tests.test_hdhive_workflow -q`

Expected: PASS, including `test_candidate_keyboard_only_offers_subscription_for_tv` and `test_search_candidates_merges_movie_and_tv_results`.

- [ ] **Step 2: No extra commit unless a test needed a fix**

If a test failed because of an accidental label change, fix it in the file that caused it and commit that fix:

```bash
git add tests/test_hdhive_bridge.py
git commit -m "test: keep HDHive search title assertions aligned"
```

If everything passed, do not create an empty commit.

---

## Spec coverage

| Spec requirement | Task |
| --- | --- |
| Candidate message: full title, year, Chinese type, TMDB ID | Task 3 + Task 4 |
| Title in the message is never truncated | Task 3 (`format_hdhive_candidate_label` has no cut) |
| Missing year → `年份未知` | Task 3 |
| Candidate buttons `序号. 片名` | Task 2 |
| Long button names end with `…` and stay ≤ 64 | Task 1 + Task 2 |
| Empty title → `未命名` on message and button | Task 2 + Task 3 |
| Subscribe / cancel unchanged | Task 2 |
| Resource header uses selected title | Task 5 |
| Missing candidate → `未命名` + type + TMDB ID | Task 5 |
| No new CMS fields / no session schema | Task 4 + Task 5 |
| Resource-row buttons unchanged | Task 5 leaves them alone |
