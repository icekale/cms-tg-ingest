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


@dataclass(frozen=True)
class SelfShareReceiveCid:
    value: str
    source: str


@dataclass(frozen=True)
class SelfShareReviewPolicy:
    mode: str
    seconds: int
    source: str
    checkpoints: tuple[int, ...]


def normalize_self_share_review_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"ten_minutes", "off"}:
        raise ValueError("分享审核观察只能设置为 10 分钟或关闭")
    return normalized


def resolve_self_share_review_policy(store: Any, config: Any) -> SelfShareReviewPolicy:
    getter = getattr(store, "get_self_share_review_mode_override", None)
    override = str(getter() or "").strip().lower() if callable(getter) else ""
    if override == "ten_minutes":
        return SelfShareReviewPolicy("ten_minutes", 600, "web", (600,))
    if override == "off":
        return SelfShareReviewPolicy("off", 0, "web", ())

    configured = tuple(
        int(value)
        for value in getattr(config, "review_checkpoints_seconds", ())
        if int(value) > 0
    )
    grace_seconds = max(1, int(getattr(config, "review_grace_seconds", 86400)))
    checkpoints = configured or (grace_seconds,)
    return SelfShareReviewPolicy("env", checkpoints[-1], "env", checkpoints)


def _valid_receive_code(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized and normalized.isascii() and normalized.isalnum():
        return normalized
    return ""


def normalize_receive_cid(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized and normalized.isascii() and normalized.isdigit() and int(normalized) > 0:
        return normalized
    return ""


def resolve_self_share_receive_cid(store: Any, config: Any) -> SelfShareReceiveCid:
    getter = getattr(store, "get_self_share_receive_cid_override", None)
    web_value = normalize_receive_cid(getter() if callable(getter) else "")
    if web_value:
        return SelfShareReceiveCid(web_value, "web")
    env_value = normalize_receive_cid(
        getattr(config, "receive_cid", getattr(config, "self_share_receive_cid", ""))
    )
    if env_value:
        return SelfShareReceiveCid(env_value, "env")
    return SelfShareReceiveCid("", "unset")


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
