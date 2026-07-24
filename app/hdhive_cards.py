from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable


class TmdbDetailCache:
    """Small TTL cache that keeps Telegram notifications from calling TMDB repeatedly."""

    def __init__(self, db_path: str | Path, ttl_seconds: int = 86400):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(60, int(ttl_seconds))
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tmdb_details (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )

    def get(self, media_type: str, tmdb_id: str, fetcher: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        key = f"{media_type}:{tmdb_id}"
        now = time.time()
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute("SELECT payload, updated_at FROM tmdb_details WHERE cache_key = ?", (key,)).fetchone()
        if row is not None and now - float(row[1]) < self.ttl_seconds:
            try:
                value = json.loads(row[0])
            except (TypeError, ValueError):
                value = {}
            if isinstance(value, dict):
                return value
        try:
            value = fetcher()
        except Exception:
            value = {}
        if not isinstance(value, dict):
            value = {}
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT INTO tmdb_details(cache_key, payload, updated_at) VALUES (?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now),
            )
        return value


def _time_label(value: float | None) -> str:
    if not value:
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _tmdb_poster_url(details: dict[str, Any]) -> str:
    path = str(details.get("poster_path") or "").strip()
    return f"https://image.tmdb.org/t/p/w500{path}" if path.startswith("/") else ""


def build_hdhive_unlock_card(
    subscription: Any,
    item: Any,
    *,
    task_id: int | None = None,
    tmdb_details: dict[str, Any] | None = None,
) -> tuple[str, str]:
    details = tmdb_details or {}
    title = str(details.get("title") or getattr(subscription, "title", "") or "HDHive 资源")
    points = getattr(item, "unlock_points_spent", None)
    source = {"actual": "实际", "estimated": "估算"}.get(str(getattr(item, "unlock_points_source", "") or ""), "")
    points_label = f"{points} 分" if points is not None else "未知"
    if source:
        points_label += f"（{source}）"
    task_label = f"#{task_id}" if task_id is not None else (f"#{item.task_id}" if getattr(item, "task_id", None) else "待分配")
    caption = "\n".join(
        (
            f"🎬 {title}",
            f"📺 {getattr(item, 'episode_key', '') or '资源'}",
            "✅ HDHive 已解锁，已进入 CMS 入库队列",
            f"💳 积分：{points_label}",
            f"🕒 解锁时间：{_time_label(getattr(item, 'unlocked_at', None))}",
            f"🆔 任务：{task_label}",
            f"📦 {getattr(item, 'title', '') or getattr(item, 'resource_slug', '')}",
        )
    )
    return caption, _tmdb_poster_url(details)

