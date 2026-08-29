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


@dataclass(frozen=True)
class Italic:
    text: str


RichInline = Union[str, Bold, Code, Italic]


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
    caption: str = ""


@dataclass(frozen=True)
class Details:
    summary: str
    blocks: tuple["Block", ...]
    is_open: bool = False


@dataclass(frozen=True)
class Divider:
    pass


Block = Union[Heading, Paragraph, Table, Details, Divider]


def bold(text: object) -> Bold:
    return Bold(str(text))


def code(text: object) -> Code:
    return Code(str(text))


def italic(text: object) -> Italic:
    return Italic(str(text))


def divider() -> Divider:
    return Divider()


def heading(text: object, size: int = 3) -> Heading:
    return Heading(str(text), size=int(size))


def paragraph(text: RichInline) -> Paragraph:
    return Paragraph(text if isinstance(text, (Bold, Code, Italic)) else str(text))


def table(headers: Sequence[str], rows: Sequence[Sequence[RichInline]], caption: object = "") -> Table:
    return Table(tuple(str(item) for item in headers), tuple(tuple(row) for row in rows), caption=str(caption or ""))


def details(summary: object, blocks: Sequence[Block], is_open: bool = False) -> Details:
    return Details(str(summary), tuple(blocks), is_open=bool(is_open))


def _plain_inline(value: RichInline) -> str:
    if isinstance(value, (Bold, Code, Italic)):
        return value.text
    return str(value)


def _api_inline(value: RichInline) -> Any:
    if isinstance(value, Bold):
        return {"type": "bold", "text": value.text}
    if isinstance(value, Code):
        return {"type": "code", "text": value.text}
    if isinstance(value, Italic):
        return {"type": "italic", "text": value.text}
    return str(value)


def _plain_blocks(blocks: Sequence[Block], indent: str = "") -> list[str]:
    lines: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            lines.append(f"{indent}{block.text}")
        elif isinstance(block, Paragraph):
            lines.append(f"{indent}{_plain_inline(block.text)}")
        elif isinstance(block, Table):
            if block.caption:
                lines.append(f"{indent}{block.caption}")
            lines.append(f"{indent}{' | '.join(block.headers)}")
            for row in block.rows:
                lines.append(f"{indent}{' | '.join(_plain_inline(cell) for cell in row)}")
        elif isinstance(block, Details):
            lines.append(f"{indent}{block.summary}")
            lines.extend(_plain_blocks(block.blocks, indent=f"{indent}  "))
        elif isinstance(block, Divider):
            if lines and lines[-1] != "":
                lines.append("")
    return lines


def _cell(text: Any, *, is_header: bool = False) -> dict[str, Any]:
    payload = {
        "text": text,
        "align": "left",
        "valign": "top",
    }
    if is_header:
        payload["is_header"] = True
    return payload


def _table_block(block: Table) -> dict[str, Any]:
    headers = block.headers[:MAX_TABLE_COLS]
    rows = [row[:MAX_TABLE_COLS] for row in block.rows]
    cells = [[_cell(header, is_header=True) for header in headers]]
    for row in rows:
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        cells.append([_cell(_api_inline(cell)) for cell in padded[: len(headers)]])
    payload = {"type": "table", "cells": cells, "is_bordered": True, "is_striped": True}
    if block.caption:
        payload["caption"] = block.caption
    return payload


def _api_blocks(blocks: Sequence[Block]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, Heading):
            payload.append({"type": "heading", "text": block.text, "size": block.size})
        elif isinstance(block, Paragraph):
            payload.append({"type": "paragraph", "text": _api_inline(block.text)})
        elif isinstance(block, Table):
            visible = Table(block.headers, block.rows[:MAX_TABLE_ROWS], caption=block.caption)
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
        elif isinstance(block, Divider):
            payload.append({"type": "divider"})
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
                expanded.append(Table(block.headers, block.rows[:MAX_TABLE_ROWS], caption=block.caption))
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
