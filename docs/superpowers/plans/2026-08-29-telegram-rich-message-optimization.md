# Telegram Rich Message Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Telegram Rich Message support to high-frequency operational, HDHive, task, and quality-result messages while preserving current keyboards, callbacks, state transitions, and plain-text fallback behavior.

**Architecture:** Keep `RichDocument` as the only internal rich-message model. Move formatting into existing UI/bridge formatter functions, then call `TelegramClient.send_rich_message()` at selected long-message boundaries; short callback feedback remains plain text. The Telegram client remains responsible only for API serialization and fallback to `sendMessage`/`editMessageText`.

**Tech Stack:** Python 3, existing `RichDocument` blocks, `unittest`, existing `HttpJson`, Telegram `sendRichMessage` API, no new dependencies.

---

## File Map

- Modify `app/telegram_ui.py`: add only reusable formatters for candidate lists, task intake summaries, and bounded operation results where the existing module already owns presentation logic.
- Modify `app/workflows/self_share.py`: remove share-code fallbacks from user-facing task labels.
- Modify `bridge.py`: convert HDHive resource/candidate/unlock flows and task detail/intake result call sites to RichDocument; keep business operations and callback data unchanged.
- Modify `tests/test_telegram_rich.py`: test new formatter shapes, bounds, and sensitive-link omission.
- Modify `tests/test_hdhive_bridge.py`: test HDHive rich messages, preserved keyboards, and unlock result summaries.
- Modify `tests/test_bridge_task_engine.py` and `tests/test_bridge_v02_integration.py`: update rich-message test doubles and task/intake expectations.
- Modify `tests/test_telegram_client.py` only if the existing fallback coverage needs a missing edge case; do not alter already passing transport behavior.

## Task 1: Add bounded formatter outputs

**Files:**
- Modify: `app/telegram_ui.py`
- Modify: `bridge.py`
- Test: `tests/test_telegram_rich.py`
- Test: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Write failing formatter tests**

Add tests that require the HDHive resource formatter to return a `RichDocument`, and add a candidate/result formatter test with bounded structured content. Use the existing `resource()`, `FakeProxy`, `HdhiveWorkflow`, and `HdhiveSessionStore` fixtures in `tests/test_hdhive_bridge.py`:

```python
from app.telegram_rich import RichDocument


def test_resource_view_is_rich_and_keeps_selection_summary(self):
    workflow = HdhiveWorkflow(object(), FakeProxy(), HdhiveSessionStore())
    session_id = workflow.sessions.begin("464100862", "Example")
    workflow.set_candidates(session_id, [{
        "media_type": "tv",
        "tmdb_id": "80986",
        "title": "攻壳机动队 SAC_2045",
        "year": "2020",
    }])
    workflow.load_resources(session_id, "tv", "80986")

    document, keyboard = bridge.format_hdhive_resources(workflow, session_id)

    self.assertIsInstance(document, RichDocument)
    self.assertIn("HDHive 资源", document.to_plain())
    self.assertIn("已选择：0 个", document.to_plain())
    self.assertIn("table", [block["type"] for block in document.to_blocks()])
    self.assertIn("hive:toggle", repr(keyboard))
```

Add a regression test for safe task labels. A missing title must never make a share code visible:

```python
def test_task_labels_do_not_fallback_to_share_code(self):
    row = {"cms_task_id": "17", "title": "", "share_code": "secret-share-code"}
    self.assertNotIn("secret-share-code", format_task_label(row))
```

Add tests in `tests/test_telegram_rich.py` for candidate and unlock summaries. The unlock test must assert that a 115 URL containing a password is absent from both plain and rich output:

```python
def test_hdhive_unlock_result_contains_counts_without_links(self):
    document = format_hdhive_unlock_result(
        [
            HdhiveUnlockItem("one", True, "https://115cdn.com/s/x?password=secret", "", "", False),
            HdhiveUnlockItem("two", False, "", "积分不足", "INSUFFICIENT_POINTS", False),
        ],
        {"one": "115", "two": "115"},
        enqueued_count=1,
        enqueue_error="",
    )

    self.assertIn("成功", document.to_plain())
    self.assertIn("失败", document.to_plain())
    self.assertIn("已提交 1 个 115 链接", document.to_plain())
    self.assertNotIn("password=secret", document.to_plain())
    self.assertNotIn("https://115cdn.com", repr(document.to_blocks()))
```

The test imports should use the actual module locations after the formatter is defined; the intended public formatter names are `format_hdhive_candidates` and `format_hdhive_unlock_result`.

- [ ] **Step 2: Run formatter tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_telegram_rich tests.test_hdhive_bridge -q
```

Expected: FAIL because `format_hdhive_resources` still returns `str` and the new formatter functions do not exist.

- [ ] **Step 3: Implement the minimum formatter changes**

In `bridge.py`, change `format_hdhive_resources` to return `(RichDocument, keyboard)` and build this shape:

```python
blocks = [heading("HDHive 资源：" + format_hdhive_candidate_label(selected))]
if not visible_indexes:
    blocks.append(paragraph("当前网盘筛选没有资源。"))
else:
    rows = []
    invalid_details = []
    for index in visible_indexes:
        item = session.resources[index]
        status = "不可用" if item.validate_status.lower() == "invalid" else (
            "已选" if index in session.selected_indexes else "可选"
        )
        if index not in selectable:
            status = "不可选"
        cost = "已解锁" if item.is_unlocked else (
            f"积分 {item.unlock_points}" if item.unlock_points is not None else "积分未知"
        )
        rows.append((
            str(index + 1),
            truncate_text(item.title or "未命名", 40),
            item.pan_type or "未知",
            item.share_size or "大小未知",
            "/".join(item.video_resolution) or "分辨率未知",
            cost,
            status,
        ))
        if item.validate_message and item.validate_status.lower() == "invalid":
            invalid_details.append(paragraph(f"{index + 1}. {truncate_text(item.validate_message, 160)}"))
    if rows:
        blocks.append(table(("#", "资源", "网盘", "大小", "分辨率", "费用", "状态"), rows))
    if invalid_details:
        blocks.append(details("不可用原因", invalid_details))
blocks.append(paragraph(f"已选择：{len(session.selected_indexes)} 个。点击资源行选择，再点击解锁。"))
return RichDocument(tuple(blocks)), hdhive_resource_keyboard(
    session_id,
    session.resources,
    visible_indexes,
    session.selected_indexes,
    workflow.available_pan_types(session_id),
    session.pan_type,
)
```

Keep the existing session lookup, visible/selectable indexes, candidate fallback, keyboard arguments, and error exceptions unchanged. Use the existing `truncate_text` helper and keep the table within `RichDocument`'s row/column limits.

In `app/telegram_ui.py`, add these two small presentation functions:

```python
def format_hdhive_candidates(candidates: list[dict[str, str]]) -> RichDocument:
    if not candidates:
        return document(paragraph("没有找到匹配的 TMDB 媒体。"))
    rows = []
    for index, candidate in enumerate(candidates[:12], 1):
        rows.append((
            str(index),
            truncate_end(str(candidate.get("title") or "未命名"), 56),
            "电影" if candidate.get("media_type") == "movie" else "剧集",
            str(candidate.get("year") or "年份未知"),
            str(candidate.get("tmdb_id") or "-"),
        ))
    return document(
        heading("HDHive 媒体候选"),
        table(("#", "标题", "类型", "年份", "TMDB"), rows),
        paragraph("请选择要查询的媒体。"),
    )


def format_hdhive_unlock_result(
    results: list[Any],
    selected_pan_types: dict[str, str],
    *,
    enqueued_count: int = 0,
    enqueue_error: str = "",
) -> RichDocument:
    rows = []
    successful = 0
    non_115 = 0
    for item in results:
        if item.success:
            successful += 1
            if selected_pan_types.get(item.slug) != "115":
                non_115 += 1
            state = "成功（已拥有）" if item.already_owned else "成功"
            reason = ""
        else:
            state = "失败"
            reason = truncate_text(item.message or item.error_code or "未知原因", 120)
        rows.append((truncate_end(item.slug, 48), state, reason))
    blocks = [heading("HDHive 解锁结果"), table(("资源", "状态", "说明"), rows)]
    if enqueued_count:
        blocks.append(paragraph(f"已提交 {enqueued_count} 个 115 链接到现有入库流程。"))
    if enqueue_error:
        blocks.append(paragraph(f"115 入库提交失败：{truncate_text(enqueue_error, 160)}。"))
    if non_115:
        blocks.append(paragraph(f"成功解锁但非 115 的资源：{non_115} 个，未自动进入 115 入库流程。"))
    if not successful and not rows:
        blocks.append(paragraph("没有返回解锁结果。"))
    return document(*blocks)
```

In `app/workflows/self_share.py`, make the existing user-facing task label safe when CMS has no title:

```python
def format_task_label(row: dict[str, Any]) -> str:
    task_id = row.get("cms_task_id")
    title = row.get("title") or "任务"
    return f"{title} #{task_id}" if task_id else str(title)
```

In `app/telegram_ui.py`, replace `task.share_code` fallbacks in `format_taskstore_history()` and `format_taskstore_status()` with `f"任务 #{task.id}"`. In `bridge.py`, replace the `format_task_snapshot()` fallback with the same non-secret label:

```python
title = task.title or task.metadata.get("received_title") or f"任务 #{task.id}"
```

This keeps the task number and title useful while ensuring missing metadata cannot leak a share code.


- [ ] **Step 4: Run formatter tests and verify they pass**

Run:

```bash
python3 -m unittest tests.test_telegram_rich tests.test_hdhive_bridge -q
```

Expected: PASS for the new formatter tests and the updated legacy label/type assertions. The test fixtures must assert `document.to_plain()` rather than calling string methods on the RichDocument.

- [ ] **Step 5: Commit the formatter changes**

```bash
git add app/telegram_ui.py bridge.py tests/test_telegram_rich.py tests/test_hdhive_bridge.py
git commit -m "feat: structure HDHive rich result views"
```

## Task 2: Route HDHive and task result call sites through Rich

**Files:**
- Modify: `bridge.py`
- Modify: `app/telegram_ui.py` only if a formatter import is needed
- Test: `tests/test_hdhive_bridge.py`
- Test: `tests/test_bridge_task_engine.py`
- Test: `tests/test_bridge_v02_integration.py`

- [ ] **Step 1: Write failing call-site tests**

Extend `FakeTelegram` assertions so rich paths are observable without depending on a real Telegram API. The existing test double already stores `document.to_plain()` in `messages`; add a separate list so tests can distinguish transport choice:

```python
class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.rich_messages = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def send_rich_message(self, chat_id, document, reply_markup=None):
        self.rich_messages.append((chat_id, document, reply_markup))
        self.messages.append((chat_id, document.to_plain(), reply_markup))
```

Add/adjust tests for these flows:

```python
def test_candidate_and_resource_views_use_rich_messages(self):
    telegram = FakeTelegram()
    # Reuse the existing Hdhive workflow/session setup in this test module.
    bridge.handle_hdhive_callback(
        "hive:candidate:session-1:0", "cb-1", "464100862", telegram,
        workflow, None,
    )
    self.assertEqual(len(telegram.rich_messages), 1)
    self.assertIn("HDHive 资源", telegram.rich_messages[0][1].to_plain())
    self.assertIsNotNone(telegram.rich_messages[0][2])
```

Add a task-detail assertion in `tests/test_bridge_task_engine.py`:

```python
self.assertEqual(len(telegram.rich_messages), 1)
self.assertIn("最近事件", telegram.rich_messages[0][1].to_plain())
self.assertIsNotNone(telegram.rich_messages[0][2])
```

Add an inbound multi-link assertion that the final summary is rich and still includes each task title/status in `to_plain()`; do not assert that the transport contains a share URL.

- [ ] **Step 2: Run the focused call-site tests and verify they fail**

Run:

```bash
python3 -m unittest tests.test_hdhive_bridge tests.test_bridge_task_engine tests.test_bridge_v02_integration -q
```

Expected: FAIL at the changed call sites because they still call `send_message` with a string or unpack the old `format_hdhive_resources` return type.

- [ ] **Step 3: Change only the selected long-message call sites**

Import `format_hdhive_candidates` and `format_hdhive_unlock_result` from `app.telegram_ui` in `bridge.py`.

Change the HDHive callback paths as follows:

```python
# candidate, filter, and toggle paths
text, keyboard = format_hdhive_resources(workflow, session_id)
telegram.send_rich_message(chat_id, text, reply_markup=keyboard)
```

For confirmation, keep `format_hdhive_account()` as a string helper but wrap it as a document:

```python
telegram.send_rich_message(
    chat_id,
    document(
        heading("HDHive 解锁确认"),
        paragraph(format_hdhive_account(preview.account)),
        paragraph(f"预计最多消耗积分：{preview.maximum_points}"),
        paragraph("请确认解锁。"),
    ),
    reply_markup=hdhive_confirmation_keyboard(session_id),
)
```

In the pending HDHive search branch, replace the manually joined candidate lines with:

```python
telegram.send_rich_message(
    chat_id,
    format_hdhive_candidates(candidates),
    reply_markup=hdhive_candidate_keyboard(pending.session_id, candidates),
)
```

In `execute_hdhive_unlock`, retain all current workflow and enqueue calls, but collect `success_urls`/`non_115_urls` only for internal enqueue decisions. Replace the `lines` list and final `send_message` with:

```python
enqueued_count = 0
enqueue_error = ""
if success_urls and enqueue_unlocked_links is not None:
    try:
        enqueue_unlocked_links(success_urls, str(chat_id))
        enqueued_count = len(success_urls)
    except Exception as exc:
        LOG.exception("Failed to enqueue unlocked HDHive links")
        enqueue_error = classify_error(exc)

document = format_hdhive_unlock_result(
    results,
    selected_pan_types,
    enqueued_count=enqueued_count,
    enqueue_error=enqueue_error,
)
telegram.answer_callback_query(callback_id, "解锁处理完成", show_alert=False)
telegram.send_rich_message(chat_id, document)
```

Keep the `HdhiveSelectionError` and `HdhiveProxyError` branches as short callback feedback plus a single plain-text error message; these are short/error fallbacks, not result reports. Do not include the collected URLs in logs or messages.

For task detail, create the document at the call site without changing `format_task_intake_reply`'s string contract:

```python
detail_blocks = [paragraph(format_task_intake_reply(task))]
if event_lines:
    detail_blocks.append(details("最近事件", [paragraph(truncate_text(line, 200)) for line in event_lines]))
telegram.send_rich_message(
    chat_id,
    document(heading(f"任务详情 #{task.id}"), *detail_blocks),
    reply_markup=task_action_keyboard([task], max_retries=max_retries, task_store=task_store),
)
```

For the normal multi-source intake result, keep the existing per-source state transitions and `format_task_intake_reply()` calls, but collect display rows `(index, source_type, summary)` and send one document:

```python
telegram.send_rich_message(
    chat_id,
    document(
        heading(f"收到 {len(sources)} 个链接"),
        table(("#", "类型", "结果"), display_rows),
    ),
)
```

The display summary must use existing `format_task_intake_reply()` output with `truncate_text(format_task_intake_reply(task), 180)` before inserting it into a row. Replace each branch's current append, for example `result_lines.append(f"{index}. {format_task_intake_reply(task)}")`, with `display_rows.append((str(index), source.source_type, truncate_text(format_task_intake_reply(task), 180)))`; for literal branch messages, put that same existing message in the third tuple field. Preserve the same branch-specific wording. It must not include raw input links, share codes, receive codes, or unlocked URLs. Leave short single-action confirmations and callback answers on `send_message`/`answer_callback_query`.

- [ ] **Step 4: Run focused tests and verify they pass**

Run:

```bash
python3 -m unittest tests.test_hdhive_bridge tests.test_bridge_task_engine tests.test_bridge_v02_integration -q
```

Expected: PASS. Existing tests may still need only assertion-shape updates from `messages` to `rich_messages`; no state-machine expectation should change.

- [ ] **Step 5: Commit the call-site changes**

```bash
git add bridge.py app/telegram_ui.py tests/test_hdhive_bridge.py tests/test_bridge_task_engine.py tests/test_bridge_v02_integration.py
git commit -m "feat: send operational results as Telegram rich messages"
```

## Task 3: Complete fallback and integration coverage

**Files:**
- Modify: `tests/test_telegram_client.py`
- Modify: `tests/test_telegram_rich.py`
- Modify: `tests/test_hdhive_bridge.py`
- Modify: `tests/test_bridge_task_engine.py`
- Modify: `tests/test_bridge_v02_integration.py`
- Modify: `tests/test_quality_telegram.py` only if the selected quality result path lacks rich transport coverage

- [ ] **Step 1: Add the missing transport assertions**

Use the existing `FakeHttp` in `tests/test_telegram_client.py` to cover the already implemented transport contract:

```python
def test_rich_network_failure_does_not_send_plain_duplicate(self):
    http = FakeHttp([
        RuntimeError("Cannot reach https://api.telegram.org/bot<redacted>/sendRichMessage: EOF")
    ])
    client = TelegramClient("secret", http=http)

    with self.assertRaises(RuntimeError):
        client.send_rich_message(1, RichDocument((heading("健康检查"),)))

    self.assertEqual(len(http.calls), 1)
    self.assertTrue(http.calls[0][0].endswith("/sendRichMessage"))
```

Keep the existing 400/404 fallback tests and add an assertion that only one plain fallback request occurs. Add a rich edit fallback assertion if not already present:

```python
def test_edit_rich_format_failure_edits_plain_text_once(self):
    http = FakeHttp([
        {"ok": False, "error_code": 400, "description": "Bad Request: can't parse rich blocks"},
        {"ok": True},
    ])
    TelegramClient("secret", http=http).edit_rich_message(
        1, 9, RichDocument((heading("任务"),)), reply_markup={"inline_keyboard": []}
    )

    self.assertTrue(http.calls[0][0].endswith("/editMessageText"))
    self.assertNotIn("rich_message", http.calls[1][1]["payload"])
    self.assertEqual(http.calls[1][1]["payload"]["message_id"], 9)
```

- [ ] **Step 2: Run transport and integration tests**

Run:

```bash
python3 -m unittest tests.test_telegram_client tests.test_telegram_rich tests.test_hdhive_bridge tests.test_bridge_task_engine tests.test_bridge_v02_integration tests.test_quality_telegram -q
```

Expected: PASS, including no duplicate plain message on network failure and no sensitive URL in rich output.

- [ ] **Step 3: Fix only assertion-shape regressions**

Where a test needs the rendered text, use `document.to_plain()` or the existing FakeTelegram compatibility `messages` list. Where it needs to prove the transport, assert `rich_messages`. Do not loosen tests by accepting either transport for a path that the plan explicitly migrates.

- [ ] **Step 4: Commit the coverage changes**

```bash
git add tests/test_telegram_client.py tests/test_telegram_rich.py tests/test_hdhive_bridge.py tests/test_bridge_task_engine.py tests/test_bridge_v02_integration.py tests/test_quality_telegram.py
git commit -m "test: cover Telegram rich message fallbacks"
```

## Task 4: Full verification and delivery check

**Files:**
- No source changes expected.

- [ ] **Step 1: Run the complete test suite**

```bash
python3 -m unittest discover -s tests -p 'test*.py' -q
```

Expected: all tests pass with no unexpected traceback. Tests that intentionally exercise failures may still log their simulated exceptions.

- [ ] **Step 2: Run compile and whitespace checks**

```bash
python3 -m compileall -q app bridge.py doctor.py
git diff --check
```

Expected: both commands exit successfully.

- [ ] **Step 3: Audit the changed Telegram paths for secrets**

```bash
rg -n "full_url|password=|receive_code|share_code|access_token|refresh_token|send_message\(" bridge.py app/telegram_ui.py
```

Confirm that `full_url` remains internal to enqueue decisions, that no new rich formatter includes it, and that short existing messages do not gain raw credentials.

- [ ] **Step 4: Review the final diff and status**

```bash
git diff HEAD~3..HEAD --stat
git status --short --branch
```

Expected: only the formatter/call-site/test files listed in this plan changed, and the worktree is clean after commits.

- [ ] **Step 5: Record delivery decision**

Do not publish or update Unraid in this plan unless explicitly requested after verification. If deployment is requested, use the existing fixed-version release workflow and production backup procedure; do not use `latest`.
