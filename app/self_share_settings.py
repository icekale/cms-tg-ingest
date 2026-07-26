from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOG = logging.getLogger("cms-tg-ingest")


@dataclass(frozen=True)
class OwnShareReceiveCode:
    value: str
    source: str

    @property
    def masked(self) -> str:
        return "****"


def _valid_receive_code(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized and normalized.isascii() and normalized.isalnum():
        return normalized
    return ""


def _cms_receive_code(db_path: str | Path) -> str:
    path = Path(db_path).expanduser()
    if not path.is_file():
        return ""
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT value FROM cms_config WHERE key = ? LIMIT 1",
                ("share_115_sync",),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return ""
        payload = json.loads(str(row[0] or "{}"))
        if not isinstance(payload, dict):
            return ""
        return _valid_receive_code(payload.get("SHARE_115_PASSWORD"))
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        LOG.debug("Failed to read CMS self-share receive code", exc_info=True)
        return ""


def resolve_own_share_receive_code(store: Any, config: Any) -> OwnShareReceiveCode:
    getter = getattr(store, "get_own_share_receive_code_override", None)
    web_value = _valid_receive_code(getter() if callable(getter) else "")
    if web_value:
        return OwnShareReceiveCode(web_value, "web")
    cms_value = _cms_receive_code(getattr(config, "cms_state_db_path", "/cms/cms-online.db"))
    if cms_value:
        return OwnShareReceiveCode(cms_value, "cms")
    env_value = _valid_receive_code(getattr(config, "own_share_receive_code", ""))
    if env_value:
        return OwnShareReceiveCode(env_value, "env")
    return OwnShareReceiveCode("1212", "default")
