# Telegram Rich Message Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send selected Telegram reports as Bot API 10.1 rich messages (`blocks`), with a plain-text fallback when rich formatting is rejected.

**Architecture:** A small `RichDocument` in `app/telegram_rich.py` (heading / paragraph / table / details) serializes to `InputRichMessage.blocks`. Report formatters return `RichDocument`. `TelegramClient.send_rich_message` posts `/sendRichMessage`; HTTP/API 400–404 or unknown-method errors fall back to `send_message(document.to_plain())`. Short confirmations and the quiet `/quality` one-liner stay on `send_message`.

**Tech Stack:** Python 3, existing `HttpJson` / `HttpRequestError`, unittest, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-telegram-rich-messages-design.md`

**Files:**
- Create: `app/telegram_rich.py`, `tests/test_telegram_rich.py`
- Modify: `bridge.py` (`TelegramClient`, `format_health`, `_quality_attention_message`, `format_hdhive_subscription_view`, command/callback send paths)
- Modify: `app/telegram_ui.py` (selected `format_*`)
- Modify: `app/quality.py` (`format_task_quality_report`)
- Modify: `tests/test_telegram_client.py`, `tests/test_quality_telegram.py`, `tests/test_quality_checks.py`, `tests/test_hdhive_bridge.py`, and every test `FakeTelegram` used on rich paths
- Modify: `CHANGELOG.md`

Do not touch Web UI, HDHive resource picker, completed-subscription notify, posters, or `format_quality_scan_summary`.

---

### Task 1: RichDocument model

**Files:**
- Create: `tests/test_telegram_rich.py`
- Create: `app/telegram_rich.py`

- [ ] **Step 1: Write the failing tests**

```python
import unittest

from app.telegram_rich import RichDocument, bold, details, heading, paragraph, table


class TelegramRichTests(unittest.TestCase):
    def test_empty_document_is_false(self):
        self.assertFalse(RichDocument())
        self.assertTrue(RichDocument((heading("最近任务"),)))

    def test_heading_and_table_to_blocks(self):
        doc = RichDocument(
            (
                heading("最近任务"),
                table(("任务", "状态"), (("HK1", bold("OK")),)),
            )
        )
        blocks = doc.to_blocks()
        self.assertEqual(blocks[0]["type"], "heading")
        self.assertEqual(blocks[0]["size"], 3)
        self.assertEqual(blocks[0]["text"], "最近任务")
        self.assertEqual(blocks[1]["type"], "table")
        self.assertTrue(blocks[1]["is_bordered"])
        self.assertTrue(blocks[1]["is_striped"])
        header = blocks[1]["cells"][0][0]
        self.assertTrue(header["is_header"])
        self.assertEqual(header["align"], "left")
        self.assertEqual(header["valign"], "top")
        self.assertEqual(blocks[1]["cells"][1][1]["text"]["type"], "bold")
        self.assertEqual(blocks[1]["cells"][1][1]["text"]["text"], "OK")

    def test_to_plain_joins_tables_and_details(self):
        doc = RichDocument(
            (
                heading("最近任务"),
                table(("任务", "状态"), (("A", "ok"),)),
                details("等待", (paragraph("还在搬"),)),
            )
        )
        text = doc.to_plain()
        self.assertIn("最近任务", text)
        self.assertIn("任务 | 状态", text)
        self.assertIn("A | ok", text)
        self.assertIn("等待", text)
        self.assertIn("  还在搬", text)
        self.assertNotIn("**", text)

    def test_table_overflow_moves_extra_rows_to_details(self):
        rows = [(f"r{i}", "ok") for i in range(21)]
        doc = RichDocument((table(("任务", "状态"), rows),))
        blocks = doc.to_blocks()
        self.assertEqual(blocks[0]["type"], "table")
        self.assertEqual(len(blocks[0]["cells"]), 21)
        self.assertEqual(blocks[1]["type"], "details")
        self.assertEqual(blocks[1]["summary"], "还有 1 条")
        self.assertFalse(blocks[1].get("is_open"))
        self.assertIn("还有 1 条", doc.to_plain())
        self.assertIn("r20 | ok", doc.to_plain())

    def test_with_leading_paragraph(self):
        doc = RichDocument((heading("订阅"),)).with_leading_paragraph("已设置集数过滤：S01")
        self.assertEqual(doc.to_blocks()[0]["type"], "paragraph")
        self.assertIn("已设置集数过滤：S01", doc.to_plain())
        self.assertEqual(RichDocument((heading("订阅"),)).with_leading_paragraph("").to_blocks()[0]["type"], "heading")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_telegram_rich -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.telegram_rich'`

- [ ] **Step 3: Write `app/telegram_rich.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence, Union

MAX_TABLE_ROWS = 20
MAX_TABLE_COLS = 20


@dataclass(frozen=True)
class Bold:
    text: str


@dataclass(frozen=True)
class Code:
    text: str


RichInline = Union[str, Bold, Code]


@dataclass(frozen=True)
class Heading:
    text: str
    size: int = 3


@dataclass(frozen=True)
class Paragraph:
    text: RichInline


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[RichInline, ...], ...]


@dataclass(frozen=True)
class Details:
    summary: str
    blocks: tuple["Block", ...]
    is_open: bool = False


Block = Union[Heading, Paragraph, Table, Details]


def bold(text: object) -> Bold:
    return Bold(str(text))


def code(text: object) -> Code:
    return Code(str(text))


def heading(text: object, size: int = 3) -> Heading:
    return Heading(str(text), size=int(size))


def paragraph(text: RichInline) -> Paragraph:
    return Paragraph(text if isinstance(text, (Bold, Code)) else str(text))


def table(headers: Sequence[str], rows: Sequence[Sequence[RichInline]]) -> Table:
    return Table(tuple(str(item) for item in headers), tuple(tuple(row) for row in rows))


def details(summary: object, blocks: Sequence[Block], is_open: bool = False) -> Details:
    return Details(str(summary), tuple(blocks), is_open=bool(is_open))


def _plain_inline(value: RichInline) -> str:
    if isinstance(value, (Bold, Code)):
        return value.text
    return str(value)


def _api_inline(value: RichInline) -> Any:
    if isinstance(value, Bold):
        return {"type": "bold", "text": value.text}
    if isinstance(value, Code):
        return {"type": "code", "text": value.text}
    return str(value)


def _plain_blocks(blocks: Sequence[Block], indent: str = "") -> list[str]:
    lines: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            lines.append(f"{indent}{block.text}")
        elif isinstance(block, Paragraph):
            lines.append(f"{indent}{_plain_inline(block.text)}")
        elif isinstance(block, Table):
            lines.append(f"{indent}{' | '.join(block.headers)}")
            for row in block.rows:
                lines.append(f"{indent}{' | '.join(_plain_inline(cell) for cell in row)}")
        elif isinstance(block, Details):
            lines.append(f"{indent}{block.summary}")
            lines.extend(_plain_blocks(block.blocks, indent=f"{indent}  "))
    return lines


def _cell(text: Any, *, is_header: bool = False) -> dict[str, Any]:
    return {
        "text": text,
        "is_header": True if is_header else None,
        "align": "left",
        "valign": "top",
    }


def _table_block(block: Table) -> dict[str, Any]:
    headers = block.headers[:MAX_TABLE_COLS]
    rows = [row[:MAX_TABLE_COLS] for row in block.rows]
    cells = [[_cell(header, is_header=True) for header in headers]]
    for row in rows:
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        cells.append([_cell(_api_inline(cell)) for cell in padded[: len(headers)]])
    return {"type": "table", "cells": cells, "is_bordered": True, "is_striped": True}


def _api_blocks(blocks: Sequence[Block]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, Heading):
            payload.append({"type": "heading", "text": block.text, "size": block.size})
        elif isinstance(block, Paragraph):
            payload.append({"type": "paragraph", "text": _api_inline(block.text)})
        elif isinstance(block, Table):
            visible = Table(block.headers, block.rows[:MAX_TABLE_ROWS])
            payload.append(_table_block(visible))
            overflow = block.rows[MAX_TABLE_ROWS:]
            if overflow:
                extra = Table(block.headers, overflow)
                payload.append(
                    {
                        "type": "details",
                        "summary": f"还有 {len(overflow)} 条",
                        "blocks": [_table_block(extra)],
                    }
                )
        elif isinstance(block, Details):
            item = {
                "type": "details",
                "summary": block.summary,
                "blocks": _api_blocks(block.blocks),
            }
            if block.is_open:
                item["is_open"] = True
            payload.append(item)
    return payload


@dataclass(frozen=True)
class RichDocument:
    blocks: tuple[Block, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.blocks)

    def with_leading_paragraph(self, text: object) -> "RichDocument":
        value = str(text or "").strip()
        if not value:
            return self
        return RichDocument((paragraph(value),) + self.blocks)

    def to_plain(self) -> str:
        expanded: list[Block] = []
        for block in self.blocks:
            if isinstance(block, Table) and len(block.rows) > MAX_TABLE_ROWS:
                expanded.append(Table(block.headers, block.rows[:MAX_TABLE_ROWS]))
                expanded.append(
                    details(f"还有 {len(block.rows) - MAX_TABLE_ROWS} 条", (Table(block.headers, block.rows[MAX_TABLE_ROWS:]),))
                )
            else:
                expanded.append(block)
        return "\n".join(_plain_blocks(expanded))

    def to_blocks(self) -> list[dict[str, Any]]:
        return _api_blocks(self.blocks)


def document(*blocks: Block) -> RichDocument:
    return RichDocument(blocks)
```

Omit `is_header: None` from the JSON if Telegram rejects nulls. Prefer dropping the key when `is_header` is false:

In `_cell`, only add `"is_header": True` for header cells. Do not send `null`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.test_telegram_rich -v`

Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/telegram_rich.py tests/test_telegram_rich.py
git commit -m "feat: add RichDocument blocks for Telegram reports"
```

---

### Task 2: TelegramClient.send_rich_message

**Files:**
- Modify: `tests/test_telegram_client.py`
- Modify: `bridge.py` (`TelegramClient` after `send_message`)
- Use: `app/clients/http.py` (`HttpRequestError`)

- [ ] **Step 1: Add failing client tests at the end of `TelegramClientTests`**

```python
from app.clients.http import HttpRequestError
from app.telegram_rich import RichDocument, heading, table


class TelegramRichClientTests(unittest.TestCase):
    def test_send_rich_message_posts_blocks(self):
        http = SequenceHttp([{"ok": True}])
        doc = RichDocument((heading("健康检查"), table(("组件", "状态"), (("CMS", "OK"),))))
        keyboard = {"inline_keyboard": [[{"text": "x", "callback_data": "x"}]]}

        TelegramClient("secret", http=http).send_rich_message(1, doc, reply_markup=keyboard)

        url, kwargs = http.calls[0]
        self.assertTrue(url.endswith("/sendRichMessage"))
        payload = kwargs["payload"]
        self.assertEqual(payload["chat_id"], 1)
        self.assertTrue(payload["rich_message"]["skip_entity_detection"])
        self.assertEqual(payload["rich_message"]["blocks"][0]["type"], "heading")
        self.assertEqual(payload["reply_markup"], keyboard)
        self.assertEqual(len(http.calls), 1)

    def test_empty_document_does_not_send(self):
        http = SequenceHttp([])
        TelegramClient("secret", http=http).send_rich_message(1, RichDocument())
        self.assertEqual(http.calls, [])

    def test_http_400_falls_back_to_send_message(self):
        http = SequenceHttp(
            [
                HttpRequestError("HTTP 400 from https://api.telegram.org/bot<redacted>/sendRichMessage: bad", status_code=400),
                {"ok": True},
            ]
        )
        doc = RichDocument((heading("任务统计"),))
        keyboard = {"inline_keyboard": []}

        TelegramClient("secret", http=http).send_rich_message(9, doc, reply_markup=keyboard)

        self.assertTrue(http.calls[0][0].endswith("/sendRichMessage"))
        self.assertTrue(http.calls[1][0].endswith("/sendMessage"))
        self.assertEqual(http.calls[1][1]["payload"]["text"], doc.to_plain())
        self.assertEqual(http.calls[1][1]["payload"]["reply_markup"], keyboard)

    def test_ok_false_400_falls_back(self):
        http = SequenceHttp(
            [
                {"ok": False, "error_code": 400, "description": "Bad Request: can't parse rich blocks"},
                {"ok": True},
            ]
        )
        doc = RichDocument((heading("最近任务"),))
        TelegramClient("secret", http=http).send_rich_message(1, doc)
        self.assertTrue(http.calls[1][0].endswith("/sendMessage"))
        self.assertEqual(http.calls[1][1]["payload"]["text"], "最近任务")

    def test_unknown_method_falls_back(self):
        http = SequenceHttp(
            [
                HttpRequestError("HTTP 404 from https://api.telegram.org/bot<redacted>/sendRichMessage: unknown method", status_code=404),
                {"ok": True},
            ]
        )
        TelegramClient("secret", http=http).send_rich_message(1, RichDocument((heading("最近历史"),)))
        self.assertTrue(http.calls[1][0].endswith("/sendMessage"))

    def test_network_error_does_not_fall_back(self):
        http = SequenceHttp(
            [RuntimeError("Cannot reach https://api.telegram.org/bot<redacted>/sendRichMessage: Remote end closed")]
        )
        with self.assertRaises(RuntimeError):
            TelegramClient("secret", http=http).send_rich_message(1, RichDocument((heading("健康检查"),)))
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0][0].endswith("/sendRichMessage"))
```

`SequenceHttp` already lives in this file. Import `HttpRequestError` at the top.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.TelegramRichClientTests -v`

Use: `uv run python -m unittest tests.test_telegram_client.TelegramRichClientTests -v`

Expected: FAIL with `AttributeError: 'TelegramClient' object has no attribute 'send_rich_message'`

- [ ] **Step 3: Implement helpers and `send_rich_message` on `TelegramClient` in `bridge.py`**

Add near the other telegram imports at the top of `bridge.py`:

```python
from app.clients.http import FormHttp, HttpJson, HttpRequestError, load_cookie_value
from app.telegram_rich import RichDocument, bold, details, document, heading, paragraph, table
```

Keep the existing `from app.clients.http import FormHttp, HttpJson, load_cookie_value` — replace it with the line that also imports `HttpRequestError`. Do not import unused factories if a later task adds them; Task 2 only needs `RichDocument`. For this task import only:

```python
from app.telegram_rich import RichDocument
```

Add these methods on `TelegramClient` immediately after `send_message`:

```python
    @staticmethod
    def _is_rich_format_failure(exc: Exception | None = None, resp: dict | None = None) -> bool:
        if exc is not None and TelegramClient._is_transient_telegram_error(exc):
            return False
        status = int(getattr(exc, "status_code", 0) or 0) if exc is not None else int((resp or {}).get("error_code") or 0)
        text = str(exc if exc is not None else (resp or {}).get("description") or "").lower()
        if status in {400, 404}:
            return True
        if "unknown method" in text or "method not found" in text:
            return True
        return "bad request" in text and ("rich" in text or "block" in text)

    def send_rich_message(self, chat_id: int | str, document: RichDocument, reply_markup: dict | None = None) -> None:
        if not document:
            return
        payload = {
            "chat_id": chat_id,
            "rich_message": {
                "blocks": document.to_blocks(),
                "skip_entity_detection": True,
            },
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = self.http.request(self.base_url + "/sendRichMessage", method="POST", payload=payload)
        except Exception as exc:
            if self._is_rich_format_failure(exc=exc):
                LOG.warning("Telegram sendRichMessage rejected, falling back to sendMessage: %s", exc)
                self.send_message(chat_id, document.to_plain(), reply_markup=reply_markup)
                return
            raise
        if resp.get("ok"):
            return
        if self._is_rich_format_failure(resp=resp):
            LOG.warning(
                "Telegram sendRichMessage rejected, falling back to sendMessage: %s",
                resp.get("description") or resp.get("error_code"),
            )
            self.send_message(chat_id, document.to_plain(), reply_markup=reply_markup)
            return
        raise RuntimeError(resp.get("description") or "Telegram sendRichMessage failed")
```

- [ ] **Step 4: Run the new client tests**

Run: `uv run python -m unittest tests.test_telegram_client.TelegramRichClientTests tests.test_telegram_client.TelegramClientTests -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bridge.py tests/test_telegram_client.py
git commit -m "feat: send Telegram rich reports with plain-text fallback"
```

---

### Task 3: TelegramClient.edit_rich_message

**Files:**
- Modify: `tests/test_telegram_client.py`
- Modify: `bridge.py` (`TelegramClient`)

First-period call sites still use `edit_message_text` for `/quality`. Implement `edit_rich_message` now so the client is complete and tested.

- [ ] **Step 1: Add failing tests to `TelegramRichClientTests`**

```python
    def test_edit_rich_message_posts_rich_message(self):
        http = SequenceHttp([{"ok": True}])
        doc = RichDocument((heading("健康检查"),))
        TelegramClient("secret", http=http).edit_rich_message(1, 17, doc)
        url, kwargs = http.calls[0]
        self.assertTrue(url.endswith("/editMessageText"))
        self.assertEqual(kwargs["payload"]["message_id"], 17)
        self.assertTrue(kwargs["payload"]["rich_message"]["skip_entity_detection"])

    def test_edit_rich_not_modified_is_success(self):
        http = SequenceHttp(
            [{"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}]
        )
        TelegramClient("secret", http=http).edit_rich_message(1, 17, RichDocument((heading("最近任务"),)))
        self.assertEqual(len(http.calls), 1)

    def test_edit_rich_400_falls_back_to_edit_text_not_new_message(self):
        http = SequenceHttp(
            [
                HttpRequestError("HTTP 400 from https://api.telegram.org/bot<redacted>/editMessageText: can't parse blocks", status_code=400),
                {"ok": True},
            ]
        )
        doc = RichDocument((heading("任务统计"),))
        TelegramClient("secret", http=http).edit_rich_message(1, 17, doc, reply_markup={"inline_keyboard": []})
        self.assertTrue(http.calls[1][0].endswith("/editMessageText"))
        self.assertEqual(http.calls[1][1]["payload"]["text"], "任务统计")
        self.assertNotIn("rich_message", http.calls[1][1]["payload"])
        self.assertFalse(any(url.endswith("/sendMessage") for url, _kwargs in http.calls))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_telegram_client.TelegramRichClientTests.test_edit_rich_message_posts_rich_message -v`

Expected: FAIL with `AttributeError: ... edit_rich_message`

- [ ] **Step 3: Implement `edit_rich_message` after `send_rich_message`**

```python
    def edit_rich_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        document: RichDocument,
        reply_markup: dict | None = None,
    ) -> None:
        if not document:
            return
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {
                "blocks": document.to_blocks(),
                "skip_entity_detection": True,
            },
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = self.http.request(self.base_url + "/editMessageText", method="POST", payload=payload)
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            if self._is_rich_format_failure(exc=exc):
                LOG.warning("Telegram edit rich message rejected, falling back to text: %s", exc)
                self.edit_message_text(chat_id, message_id, document.to_plain(), reply_markup=reply_markup)
                return
            raise
        description = str(resp.get("description") or "")
        if resp.get("ok") or "message is not modified" in description.lower():
            return
        if self._is_rich_format_failure(resp=resp):
            LOG.warning("Telegram edit rich message rejected, falling back to text: %s", description)
            self.edit_message_text(chat_id, message_id, document.to_plain(), reply_markup=reply_markup)
            return
        raise RuntimeError(description or "Telegram editMessageText failed")
```

- [ ] **Step 4: Run client tests**

Run: `uv run python -m unittest tests.test_telegram_client -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bridge.py tests/test_telegram_client.py
git commit -m "feat: edit Telegram rich messages with text fallback"
```

---

### Task 4: telegram_ui report formatters

**Files:**
- Modify: `app/telegram_ui.py`
- Modify: `tests/test_quality_telegram.py` (`format_quality_manual_report` assertion)
- Modify: `tests/test_quality_checks.py` (`format_quality_report` assertion)
- Create extra tests in `tests/test_telegram_rich.py`

`format_quality_scan_summary`, keyboards, `format_counts`, `format_failure_summary`, `format_library_summary`, truncators stay `str`.

- [ ] **Step 1: Add formatter tests to `tests/test_telegram_rich.py`**

```python
from app.telegram_ui import (
    format_history,
    format_metrics,
    format_quality_manual_report,
    format_quality_report,
    format_quality_scan_summary,
    format_status,
    format_taskstore_history,
    format_taskstore_status,
)


class TelegramUiRichTests(unittest.TestCase):
    def test_format_status_empty_is_paragraph(self):
        doc = format_status([])
        self.assertIn("暂无记录", doc.to_plain())
        self.assertEqual(doc.to_blocks()[0]["type"], "paragraph")

    def test_format_status_table(self):
        doc = format_status([{"title": "海贼王", "status": "done", "last_error": ""}])
        types = [block["type"] for block in doc.to_blocks()]
        self.assertIn("heading", types)
        self.assertIn("table", types)
        self.assertIn("最近任务", doc.to_plain())
        self.assertIn("海贼王", doc.to_plain())

    def test_format_metrics_is_key_value_table(self):
        doc = format_metrics({"generated_at": "t", "total": 2, "status_counts": {"done": 2}})
        self.assertEqual(doc.to_blocks()[0]["text"], "任务统计")
        self.assertEqual(doc.to_blocks()[1]["type"], "table")
        self.assertIn("总数", doc.to_plain())

    def test_quality_scan_summary_stays_str(self):
        self.assertIsInstance(format_quality_scan_summary([]), str)

    def test_quality_report_table(self):
        rows = [
            {
                "id": 72,
                "title": "航海王 (1999) {tmdb=37854}",
                "emby_status": "confirmed",
                "emby_title": "我是余欢水",
                "emby_path": "/mnt/user/Unraid/strm/转存/TVCN/W-我是余欢水-2020-[tmdb=101588]",
                "recognition_json": "{}",
            }
        ]
        doc = format_quality_report(rows)
        self.assertIn("疑似错配", doc.to_plain())
        self.assertIn("table", [block["type"] for block in doc.to_blocks()])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_telegram_rich.TelegramUiRichTests -v`

Expected: FAIL because formatters still return `str` (`'str' object has no attribute 'to_plain'`)

- [ ] **Step 3: Change the listed formatters in `app/telegram_ui.py`**

Add import:

```python
from app.telegram_rich import RichDocument, details, document, heading, paragraph, table
```

Replace these functions (keep helpers that still return `str`):

```python
def format_history(rows: list[dict[str, Any]]) -> RichDocument:
    if not rows:
        return document(paragraph("暂无历史记录。"))
    table_rows = []
    for idx, row in enumerate(rows, 1):
        table_rows.append(
            (
                str(idx),
                format_task_label(row),
                str(row.get("category_final") or row.get("category_choice") or row.get("category_status") or "-"),
                str(row.get("move_status") or "-"),
                str(row.get("emby_status") or "-"),
            )
        )
    blocks: list = [heading("最近历史"), table(("#", "任务", "分类", "移动", "Emby"), table_rows)]
    failure_summary = format_failure_summary(rows)
    if failure_summary:
        blocks.append(paragraph(failure_summary))
    library_summary = format_library_summary(rows)
    if library_summary:
        blocks.append(paragraph(library_summary))
    return RichDocument(tuple(blocks))


def format_taskstore_history(tasks: list[Any]) -> RichDocument:
    if not tasks:
        return RichDocument()
    table_rows = []
    for idx, task in enumerate(tasks, 1):
        title = task.title or task.metadata.get("received_title") or task.share_code
        category = task.category or task.metadata.get("category") or task.metadata.get("category_final") or "-"
        dest = task.metadata.get("dest_path") or "-"
        emby_parent = task.metadata.get("emby_parent") or task.metadata.get("emby_refresh_library") or "-"
        table_rows.append(
            (
                f"#{task.id}",
                str(title),
                stage_display_name(task.current_stage),
                task.status.value,
                str(category),
                str(emby_parent),
                str(dest),
            )
        )
    return document(heading("TaskStore 最近历史"), table(("#", "任务", "阶段", "状态", "分类", "媒体库", "路径"), table_rows))


def format_quality_report(rows: list[dict[str, Any]]) -> RichDocument:
    table_rows = []
    for row in rows:
        issue = quality_issue_for_row(row)
        if not issue:
            continue
        table_rows.append((str(len(table_rows) + 1), format_task_label(row), str(row.get("emby_title") or "-"), issue))
    if not table_rows:
        return document(paragraph("最近任务未发现明显错配。"))
    return document(heading("质量巡检：发现疑似错配"), table(("#", "任务", "Emby", "问题"), table_rows))


def format_quality_manual_report(rows: list[dict[str, Any]]) -> RichDocument:
    rows = quality_manual_rows(rows)
    if not rows:
        return document(paragraph("质量巡检：当前没有需要人工处理的问题。"))
    table_rows = []
    for row in rows:
        title = truncate_text(str(row.get("title") or f"任务 #{row.get('task_id')}"), 70)
        reason = truncate_text(str(row.get("rule_reason") or row.get("message") or "需要人工确认"), 120)
        table_rows.append(
            (
                f"#{row.get('task_id')}",
                title,
                str(row.get("rule_id") or "-"),
                str(row.get("risk_level") or "-"),
                str(row.get("manual_status") or "open"),
                reason,
                str(row.get("attempts", 0)),
            )
        )
    return document(
        heading(f"质量巡检：{len(rows)} 项需要关注"),
        table(("#", "任务", "规则", "风险", "状态", "原因", "尝试"), table_rows),
    )


def format_metrics(payload: dict[str, Any]) -> RichDocument:
    rows = (
        ("生成时间", payload.get("generated_at") or "-"),
        ("总数", payload.get("total", 0)),
        ("任务", format_counts(payload.get("status_counts") or {})),
        ("Emby", format_counts(payload.get("emby_status_counts") or {})),
        ("移动", format_counts(payload.get("move_status_counts") or {})),
        ("失败", payload.get("failure_summary") or "-"),
        ("媒体库", payload.get("library_summary") or "-"),
        ("Telegram瞬时错误", payload.get("telegram_last_error_at") or payload.get("telegram_last_transient_error_at") or "-"),
    )
    return document(heading("任务统计"), table(("项", "值"), rows))


def format_status(rows: list[dict[str, Any]]) -> RichDocument:
    if not rows:
        return document(paragraph("暂无记录。直接发送 115 分享链接即可创建任务。"))
    table_rows = []
    for row in rows:
        table_rows.append((format_task_label(row), str(row.get("status") or "unknown"), str(row.get("last_error") or "")))
    blocks: list = [heading("最近任务"), table(("任务", "状态", "错误"), table_rows)]
    failure_summary = format_failure_summary(rows)
    if failure_summary:
        blocks.append(paragraph(failure_summary))
    return RichDocument(tuple(blocks))


def format_taskstore_status(tasks: list[Any]) -> RichDocument:
    if not tasks:
        return RichDocument()
    table_rows = []
    extra = []
    for task in tasks:
        title = truncate_text(str(task.title or task.metadata.get("received_title") or task.share_code), 80)
        table_rows.append(
            (
                f"#{task.id}",
                title,
                stage_display_name(task.current_stage),
                task.status.value,
                truncate_text(task.error_summary, 100) if task.error_summary else "",
            )
        )
        detail_lines = []
        if task.status in {TaskStatus.RUNNING, TaskStatus.PENDING}:
            detail_lines.append(paragraph(f"等待：{truncate_text(describe_task_wait(task, now=time.time()), 200)}"))
        for line in format_task_observability(task, now=time.time()):
            detail_lines.append(paragraph(truncate_text(line, 200)))
        if detail_lines:
            extra.append(details(f"#{task.id} {title}", detail_lines))
    return RichDocument((heading("TaskStore 最近任务"), table(("#", "任务", "阶段", "状态", "错误"), table_rows), *extra))
```

Use the existing metrics key `telegram_last_transient_error_at` (keep current field; do not invent `telegram_last_error_at` unless the payload already has it). The implementation row should be:

```python
("Telegram瞬时错误", payload.get("telegram_last_transient_error_at") or "-"),
```

- [ ] **Step 4: Update existing string assertions**

In `tests/test_quality_telegram.py` replace:

```python
self.assertIn("质量任务", format_quality_manual_report(rows))
```

with:

```python
self.assertIn("质量任务", format_quality_manual_report(rows).to_plain())
```

In `tests/test_quality_checks.py` replace:

```python
report = bridge.format_quality_report(rows)
self.assertIn("疑似错配", report)
```

with:

```python
report = bridge.format_quality_report(rows).to_plain()
self.assertIn("疑似错配", report)
```

Keep the other `assertIn` lines on `report` after that change.

- [ ] **Step 5: Run tests**

Run:

```bash
uv run python -m unittest tests.test_telegram_rich tests.test_quality_telegram tests.test_quality_checks tests.test_refactor_imports -v
```

Expected: PASS. `test_refactor_imports` still checks `bridge.format_status is telegram_ui.format_status`.

- [ ] **Step 6: Commit**

```bash
git add app/telegram_ui.py tests/test_telegram_rich.py tests/test_quality_telegram.py tests/test_quality_checks.py
git commit -m "feat: render Telegram status history and quality reports as RichDocument"
```

---

### Task 5: quality.py and bridge health / attention formatters

**Files:**
- Modify: `app/quality.py`
- Modify: `bridge.py` (`format_health`, `_quality_attention_message`)
- Modify: `tests/test_telegram_rich.py`

- [ ] **Step 1: Add failing tests**

```python
from app.quality import QualityIssue, format_task_quality_report
from bridge import _quality_attention_message, format_health
from app.quality_automation import QualityRepairPlan, QualityRunSummary


class BridgeRichFormatterTests(unittest.TestCase):
    def test_task_quality_report_table(self):
        doc = format_task_quality_report(
            [QualityIssue(code="unexpected_strm", message="多余 STRM", detail="", task_id=4, title="剧")]
        )
        self.assertIn("TaskStore 轻量巡检", doc.to_plain())
        self.assertIn("table", [block["type"] for block in doc.to_blocks()])

    def test_task_quality_report_empty(self):
        self.assertIn("未发现本地 STRM 问题", format_task_quality_report([]).to_plain())

    def test_quality_attention_includes_run_id(self):
        summary = QualityRunSummary(
            run_id="run-1",
            status="ok",
            scanned_count=3,
            issue_count=1,
            failed_count=1,
            plans=(
                QualityRepairPlan(
                    task_id=8,
                    action="reprocess",
                    reason="失败",
                    title="剧",
                    execution_status="failed",
                ),
            ),
        )
        doc = _quality_attention_message(summary)
        self.assertIn("run-1", doc.to_plain())
        self.assertIn("质量巡检需要关注", doc.to_plain())
        self.assertIn("table", [block["type"] for block in doc.to_blocks()])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_telegram_rich.BridgeRichFormatterTests -v`

Expected: FAIL on `.to_plain()`

- [ ] **Step 3: Implement `format_task_quality_report` in `app/quality.py`**

```python
from .telegram_rich import RichDocument, document, heading, paragraph, table


def format_task_quality_report(issues: list[QualityIssue]) -> RichDocument:
    if not issues:
        return document(paragraph("TaskStore 轻量巡检：未发现本地 STRM 问题。"))
    rows = []
    for issue in issues:
        title = issue.title or f"任务 #{issue.task_id}"
        task_label = f"#{issue.task_id} {title}" if issue.task_id else title
        detail = f"：{redact_quality_detail(issue.detail)}" if issue.detail else ""
        rows.append((task_label, f"{issue.message}{detail}"))
    return document(heading("TaskStore 轻量巡检"), table(("# / 任务", "问题"), rows))
```

Keep importing `redact_quality_detail` from wherever it already comes (same file or existing import). If the current function uses it unqualified, keep that.

Replace `_quality_attention_message` in `bridge.py`:

```python
def _quality_attention_message(summary: QualityRunSummary) -> RichDocument:
    plans = [plan for plan in summary.plans if plan.execution_status in {"failed", "skipped"}]
    rows = []
    for plan in plans[:10]:
        title = plan.title or f"任务 #{plan.task_id}"
        rows.append((f"#{plan.task_id}", title, plan.reason))
    blocks = [
        heading("质量巡检需要关注"),
        paragraph(
            f"{summary.run_id}：扫描 {summary.scanned_count} 个任务，发现 {summary.issue_count} 个问题，"
            f"失败 {summary.failed_count} 个，跳过 {len(plans)} 个。"
        ),
    ]
    if rows:
        blocks.append(table(("#", "任务", "原因"), rows))
    return RichDocument(tuple(blocks))
```

Replace `format_health` so it returns `RichDocument`:

```python
def format_health(...) -> RichDocument:
    # keep the same argument names and existence checks as today
    source_ok = all(safe_resolve(root).exists() for root in move_config.source_roots)
    lib_ok = all(safe_resolve(root).exists() for root in move_config.library_roots.values())
    rows = [("CMS", bold("OK") if cms_ok else bold("FAIL"), "")]
    if telegram_ok is not None:
        extra = telegram_last_error_at or ""
        rows.append(("Telegram", bold("OK") if telegram_ok else bold("FAIL"), extra))
    if openai_enabled is not None:
        if openai_enabled:
            rows.append(("OpenAI分类兜底", bold("OK") if openai_ok else bold("FAIL"), ""))
        else:
            rows.append(("OpenAI分类兜底", "DISABLED", ""))
    if hdhive_enabled is not None:
        if hdhive_enabled:
            rows.append(("HDHive", bold("OK") if hdhive_ok else bold("FAIL"), ""))
        else:
            rows.append(("HDHive", "DISABLED", ""))
    rows.extend(
        [
            ("Emby", bold("OK") if emby_ok else bold("FAIL"), ""),
            ("STRM源", bold("OK") if source_ok else bold("FAIL"), str(len(move_config.source_roots))),
            ("媒体库映射", bold("OK") if lib_ok else bold("FAIL"), str(len(move_config.library_roots))),
            ("冲突策略", move_config.conflict_policy, ""),
        ]
    )
    blocks = [heading("健康检查"), table(("组件", "状态", "说明"), rows)]
    if task_health:
        health_lines = [paragraph(line) for line in str(task_health).splitlines() if line]
        blocks.append(details("TaskStore", health_lines))
    return RichDocument(tuple(blocks))
```

Copy the current `format_health` signature exactly (do not rename parameters). Import `bold`, `details`, `heading`, `paragraph`, `table`, `RichDocument` in `bridge.py`.

- [ ] **Step 4: Run tests**

Run:

```bash
uv run python -m unittest tests.test_telegram_rich tests.test_task_health -v
```

If `tests/test_task_health.py` only tests `format_taskstore_health` (still `str`), it must still pass.

- [ ] **Step 5: Commit**

```bash
git add app/quality.py bridge.py tests/test_telegram_rich.py
git commit -m "feat: format health and quality attention as RichDocument"
```

---

### Task 6: HDHive subscription list

**Files:**
- Modify: `app/telegram_ui.py` (`format_hdhive_subscriptions`)
- Modify: `bridge.py` (`format_hdhive_subscription_view` return type only)
- Modify: `tests/test_hdhive_bridge.py`

- [ ] **Step 1: Change assertions in `tests/test_hdhive_bridge.py`**

```python
text = format_hdhive_subscriptions([subscription]).to_plain()
```

and

```python
text = format_hdhive_subscriptions([subscription], items_by_subscription_id={1: items}).to_plain()
```

Add:

```python
self.assertIn("details", [block["type"] for block in format_hdhive_subscriptions([subscription]).to_blocks()])
```

in `test_completed_subscription_renders_status_filter_and_summary` because that subscription has a filter and summary.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_completed_subscription_renders_status_filter_and_summary -v`

Expected: FAIL (`str` has no `to_plain`)

- [ ] **Step 3: Rewrite `format_hdhive_subscriptions`**

```python
def format_hdhive_subscriptions(
    subscriptions: list[Any],
    scheduler_snapshot: dict[str, Any] | None = None,
    pending_items: list[Any] | None = None,
    items_by_subscription_id: dict[int, list[Any]] | None = None,
) -> RichDocument:
    if not subscriptions:
        return document(paragraph("暂无 HDHive 剧集订阅。"))
    blocks: list = [heading("HDHive 剧集订阅")]
    if scheduler_snapshot:
        blocks.append(
            paragraph(
                f"自动检查：{'开启' if scheduler_snapshot.get('enabled') else '关闭'}，"
                f"每天 {scheduler_snapshot.get('time') or '01:30'}，下次：{scheduler_snapshot.get('next_run_at') or '-'}"
            )
        )
    table_rows = []
    extras = []
    status_map = {"active": "运行中", "paused": "已暂停", "error": "异常", "completed": "已完结"}
    for subscription in subscriptions:
        status = status_map.get(subscription.status, subscription.status)
        source = subscription.source_url or f"TMDB:{subscription.tmdb_id}"
        title = subscription.title or subscription.tmdb_id
        table_rows.append((f"#{subscription.id}", str(title), str(status), str(source)))
        detail_blocks = []
        episode_filter = str(getattr(subscription, "episode_filter", "") or "").strip()
        if episode_filter:
            detail_blocks.append(paragraph(f"集数过滤：{episode_filter}"))
        try:
            summary = json.loads(str(getattr(subscription, "last_summary_json", "{}") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        items = (items_by_subscription_id or {}).get(int(getattr(subscription, "id", 0) or 0), ())
        diagnosis = diagnose_subscription_check(summary if isinstance(summary, dict) else {}, items)
        if isinstance(summary, dict) and summary:
            counters = []
            for key, label in (
                ("discovered", "发现"),
                ("enqueued", "入队"),
                ("emby_exists", "Emby已有"),
                ("filtered", "过滤"),
                ("pending_confirmation", "待确认"),
                ("failed", "失败"),
                ("unparsed", "无法识别"),
                ("blocked", "阻塞"),
            ):
                if key in summary:
                    counters.append(f"{label} {summary[key]}")
            if counters:
                detail_blocks.append(paragraph("最近检查：" + "，".join(counters)))
            if diagnosis.conclusion:
                detail_blocks.append(paragraph(diagnosis.conclusion))
            if diagnosis.reasons:
                detail_blocks.append(paragraph("原因：" + "；".join(diagnosis.reasons)))
        if subscription.last_error:
            detail_blocks.append(paragraph(f"最近错误：{truncate_text(subscription.last_error, 120)}"))
        if detail_blocks:
            extras.append(details(f"#{subscription.id} {title}", detail_blocks))
    blocks.append(table(("#", "剧名", "状态", "来源"), table_rows))
    blocks.extend(extras)
    if pending_items:
        blocks.append(paragraph(f"待确认高费用资源：{len(pending_items)} 个，请点击按钮确认。"))
    return RichDocument(tuple(blocks))
```

Change `format_hdhive_subscription_view` annotation to `tuple[RichDocument, dict[str, Any] | None]`. Body stays `return (format_hdhive_subscriptions(...), keyboard)`.

- [ ] **Step 4: Run HDHive formatter tests**

Run: `uv run python -m unittest tests.test_hdhive_bridge -v`

Expected: FAIL on any remaining `send_message(..., f"{message}\n\n{text}")` if those tests hit callbacks (string concat with `RichDocument`). If they fail, do not “fix” by stringifying — Task 7 wires `send_rich_message`. If only formatter tests run and pass, continue. If callback tests fail this task, skip to Task 7 in the same sitting after committing formatters only if callback tests were not collected; otherwise implement Task 7 immediately after this commit.

If `tests.test_hdhive_bridge` fails because `f"{message}\n\n{text}"` TypeErrors, leave that failure for Task 7 (do not stringify). Commit formatter + updated formatter assertions only if the rest of the file still passes. If the file cannot pass until Task 7, keep the formatter commit and accept callback failures until Task 7.

Safer split: run only the two formatter tests:

```bash
uv run python -m unittest tests.test_hdhive_bridge.HdhiveBridgeTests.test_completed_subscription_renders_status_filter_and_summary tests.test_hdhive_bridge.HdhiveBridgeTests.test_subscription_list_uses_diagnosis_and_omits_misleading_emby_warning -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/telegram_ui.py bridge.py tests/test_hdhive_bridge.py
git commit -m "feat: render HDHive subscription lists as RichDocument"
```

---

### Task 7: Wire send_rich_message and FakeTelegram

**Files:**
- Modify: `bridge.py` call sites listed below
- Modify FakeTelegram in:
  - `tests/test_quality_telegram.py`
  - `tests/test_hdhive_bridge.py`
  - `tests/test_bridge_task_engine.py`
  - `tests/test_bridge_v02_integration.py` (class `FakeTelegram` and any nested class used with `/status` `/health` `/metrics` `/history` `/quality` without automation)
  - `tests/test_taskstore_workflow_events.py`
  - `tests/test_invalid_share_cleanup.py`
  - `tests/test_cloud_workflow.py`
  - `tests/test_self_share_workflow.py` (both nested classes)
  - `tests/test_openai_fallback.py` (both nested classes)

- [ ] **Step 1: Add this method to every listed `FakeTelegram`**

```python
    def send_rich_message(self, chat_id, document, reply_markup=None):
        self.messages.append((chat_id, document.to_plain(), reply_markup))
```

Do not implement a default on `TelegramClient` subclasses used as fakes that swallow missing methods. Nested fakes that only send short confirmations still get the method so a later rich call cannot hide as `send_message`.

If a fake records `send_message` via `*args, **kwargs`, add `send_rich_message` that appends `(chat_id, document.to_plain(), reply_markup)` to the same list.

- [ ] **Step 2: Change these `bridge.py` sends to `send_rich_message`**

Prefix + view (one message):

```python
text, keyboard = format_hdhive_subscription_view(...)
telegram.send_rich_message(chat_id, text.with_leading_paragraph(message), reply_markup=keyboard)
```

Apply that pattern at:

- `handle_hdhive_subscription_callback` completion: `message` is `format_subscription_check_message(...)` (still `str`)
- `handle_hdhive_filter_input`: leading text `已清除集数过滤。` / `已设置集数过滤：{value}`
- subscribe callback: leading `已订阅：{subscription.title}`
- `/订阅` command and raw HDHive URL handler: leading `"\n".join(lines)`
- `/hdhive_subscriptions`: `telegram.send_rich_message(chat_id, text, reply_markup=keyboard)` with no prefix

Commands:

```python
if command == "/status":
    if task_engine_enabled and task_store is not None:
        tasks = task_store.list_recent_tasks(limit=8)
        taskstore_status = format_taskstore_status(tasks)
        if taskstore_status:
            telegram.send_rich_message(chat_id, taskstore_status, reply_markup=task_action_keyboard(tasks, max_retries=max_retries))
            return
    telegram.send_rich_message(chat_id, format_status(store.recent(limit=8)))
    return
if command == "/metrics":
    ...
    telegram.send_rich_message(chat_id, format_metrics(payload))
    return
if command == "/history":
    if task_engine_enabled and task_store is not None:
        taskstore_history = format_taskstore_history(task_store.list_recent_tasks(limit=10))
        if taskstore_history:
            telegram.send_rich_message(chat_id, taskstore_history)
            return
    telegram.send_rich_message(chat_id, format_history(store.recent(limit=10)))
    return
```

`/quality` without automation:

```python
telegram.send_rich_message(chat_id, format_task_quality_report(issues))
...
telegram.send_rich_message(chat_id, format_quality_report(rows), reply_markup=quality_keyboard(rows))
```

`/health`:

```python
telegram.send_rich_message(chat_id, format_health(...))  # same arguments as today
```

`notify_quality_run`:

```python
telegram.send_rich_message(
    chat_id,
    _quality_attention_message(summary),
    reply_markup=quality_manual_keyboard(rows),
)
```

Do **not** change `send_quality_manual_queue` (still `format_quality_scan_summary` + `edit_message_text` / `send_message`).

Do **not** change `format_hdhive_subscription_completed`, `send_photo`, HDHive resource `send_message`, or one-line errors.

- [ ] **Step 3: Add a regression test that quiet `/quality` still uses `send_message`**

In `tests/test_quality_telegram.py`, `test_quality_command_shows_rule_queue_and_callbacks` already asserts the one-line text. Keep it. Add:

```python
    def send_rich_message(self, chat_id, document, reply_markup=None):
        self.rich_messages = getattr(self, "rich_messages", [])
        self.rich_messages.append((chat_id, document, reply_markup))
        self.messages.append((chat_id, document.to_plain(), reply_markup))
```

Then in that test:

```python
self.assertFalse(getattr(telegram, "rich_messages", []))
self.assertIn("质量巡检：发现", telegram.messages[-1][1])
```

- [ ] **Step 4: Run the focused suite**

```bash
uv run python -m unittest tests.test_telegram_rich tests.test_telegram_client tests.test_quality_telegram tests.test_quality_checks tests.test_hdhive_bridge tests.test_refactor_imports -v
```

Expected: PASS

Then:

```bash
uv run python -m unittest tests.test_bridge_task_engine tests.test_bridge_v02_integration tests.test_taskstore_workflow_events -q
```

Expected: PASS. If a fake missed `send_rich_message`, you get `AttributeError` — add the method, do not catch it in production code.

- [ ] **Step 5: Commit**

```bash
git add bridge.py tests
git commit -m "feat: send structured Telegram reports through sendRichMessage"
```

---

### Task 8: Changelog and full regression

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-08-22-telegram-rich-messages-design.md` status line

- [ ] **Step 1: Add a `0.4.13` section at the top of `CHANGELOG.md`**

```markdown
## 0.4.13 - 2026-08-22

- **Telegram 结构化报表改为原生富文本**：`/status`、`/history`、`/metrics`、`/health`、HDHive 订阅列表和质量告警用表格和折叠详情发送；短确认和 `/quality` 一行摘要不变。`sendRichMessage` 不可用时回退纯文本。
```

Change spec status to `已实施`.

- [ ] **Step 2: Run the full unit suite**

```bash
uv run python -m unittest discover -s tests -q
```

Expected: PASS (or the same pre-existing failures as `main` before this work — there should be no new failures).

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/superpowers/specs/2026-08-22-telegram-rich-messages-design.md
git commit -m "docs: note Telegram rich-message reports in changelog"
```

---

## Self-review vs spec

| Spec requirement | Task |
|---|---|
| `RichDocument` + heading/paragraph/table/details + Bold/Code | 1 |
| `to_blocks` / `to_plain` / empty is false / 20-row overflow | 1 |
| `send_rich_message` + skip_entity_detection + empty no-op | 2 |
| Fallback 400/404/unknown method; no fallback on network | 2 |
| `edit_rich_message` + not-modified + no extra sendMessage | 3 |
| Listed `format_*` return `RichDocument` | 4–6 |
| `format_quality_scan_summary` stays `str` | 4 |
| `format_hdhive_subscription_view` returns document | 6 |
| Prefix + view via `with_leading_paragraph` | 1, 7 |
| Command/callback/notify wiring | 7 |
| Quiet `/quality` unchanged | 7 |
| FakeTelegram must define `send_rich_message` | 7 |
| Existing keyword assertions via `to_plain()` | 4, 6 |
| No draft/html/markdown/parse_mode/Web/picker/posters | not in plan |
