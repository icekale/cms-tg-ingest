from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HdhiveSubscription:
    id: int
    chat_id: str
    source_type: str
    source_value: str
    source_url: str
    title: str
    tmdb_id: str
    media_type: str
    pan_type: str
    status: str
    last_checked_at: float
    last_error: str
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HdhiveSubscription":
        return cls(
            id=int(row["id"]),
            chat_id=str(row["chat_id"] or ""),
            source_type=str(row["source_type"] or ""),
            source_value=str(row["source_value"] or ""),
            source_url=str(row["source_url"] or ""),
            title=str(row["title"] or ""),
            tmdb_id=str(row["tmdb_id"] or ""),
            media_type=str(row["media_type"] or "tv"),
            pan_type=str(row["pan_type"] or "115"),
            status=str(row["status"] or "active"),
            last_checked_at=float(row["last_checked_at"] or 0),
            last_error=str(row["last_error"] or ""),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )


@dataclass(frozen=True)
class HdhiveSubscriptionItem:
    id: int
    subscription_id: int
    episode_key: str
    resource_slug: str
    title: str
    validate_status: str
    resolution_score: int
    unlock_points: int | None
    status: str
    task_id: int | None
    last_error: str
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HdhiveSubscriptionItem":
        return cls(
            id=int(row["id"]),
            subscription_id=int(row["subscription_id"]),
            episode_key=str(row["episode_key"] or ""),
            resource_slug=str(row["resource_slug"] or ""),
            title=str(row["title"] or ""),
            validate_status=str(row["validate_status"] or ""),
            resolution_score=int(row["resolution_score"] or 0),
            unlock_points=int(row["unlock_points"]) if row["unlock_points"] is not None else None,
            status=str(row["status"] or "discovered"),
            task_id=int(row["task_id"]) if row["task_id"] is not None else None,
            last_error=str(row["last_error"] or ""),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )


@dataclass(frozen=True)
class HdhiveSubscriptionRun:
    id: int
    run_id: str
    run_date: str
    status: str
    summary_json: str
    started_at: float
    finished_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HdhiveSubscriptionRun":
        return cls(
            id=int(row["id"]),
            run_id=str(row["run_id"] or ""),
            run_date=str(row["run_date"] or ""),
            status=str(row["status"] or ""),
            summary_json=str(row["summary_json"] or "{}"),
            started_at=float(row["started_at"] or 0),
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
        )


class HdhiveSubscriptionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = db_path if isinstance(db_path, Path) else Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS hdhive_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_value TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    tmdb_id TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT 'tv',
                    pan_type TEXT NOT NULL DEFAULT '115',
                    status TEXT NOT NULL DEFAULT 'active',
                    last_checked_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(chat_id, source_type, source_value)
                );
                CREATE INDEX IF NOT EXISTS idx_hdhive_subscriptions_status
                    ON hdhive_subscriptions(status, updated_at);
                CREATE TABLE IF NOT EXISTS hdhive_subscription_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    episode_key TEXT NOT NULL,
                    resource_slug TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    validate_status TEXT NOT NULL DEFAULT '',
                    resolution_score INTEGER NOT NULL DEFAULT 0,
                    unlock_points INTEGER,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    task_id INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(subscription_id, episode_key, resource_slug),
                    FOREIGN KEY(subscription_id) REFERENCES hdhive_subscriptions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_hdhive_subscription_items_lookup
                    ON hdhive_subscription_items(subscription_id, episode_key, status);
                CREATE TABLE IF NOT EXISTS hdhive_subscription_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    run_date TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    started_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS hdhive_subscription_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _row_or_none(row: sqlite3.Row | None, factory):
        return factory(row) if row is not None else None

    def create_subscription(
        self,
        chat_id: str,
        source_type: str,
        source_value: str,
        title: str,
        tmdb_id: str,
        source_url: str = "",
    ) -> HdhiveSubscription:
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO hdhive_subscriptions
                    (chat_id, source_type, source_value, source_url, title, tmdb_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, source_type, source_value) DO UPDATE SET
                    source_url = CASE WHEN excluded.source_url <> '' THEN excluded.source_url ELSE source_url END,
                    title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE title END,
                    tmdb_id = CASE WHEN excluded.tmdb_id <> '' THEN excluded.tmdb_id ELSE tmdb_id END,
                    updated_at = excluded.updated_at
                """,
                (
                    str(chat_id),
                    str(source_type),
                    str(source_value),
                    str(source_url or ""),
                    str(title or ""),
                    str(tmdb_id),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM hdhive_subscriptions
                WHERE chat_id = ? AND source_type = ? AND source_value = ?
                """,
                (str(chat_id), str(source_type), str(source_value)),
            ).fetchone()
        return HdhiveSubscription.from_row(row)

    def list_subscriptions(self, chat_id: str | None = None, include_deleted: bool = False) -> list[HdhiveSubscription]:
        clauses: list[str] = []
        values: list[Any] = []
        if chat_id is not None:
            clauses.append("chat_id = ?")
            values.append(str(chat_id))
        if not include_deleted:
            clauses.append("status <> 'deleted'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM hdhive_subscriptions {where} ORDER BY updated_at DESC, id DESC",
                values,
            ).fetchall()
        return [HdhiveSubscription.from_row(row) for row in rows]

    def get_subscription(self, subscription_id: int) -> HdhiveSubscription | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM hdhive_subscriptions WHERE id = ?",
                (int(subscription_id),),
            ).fetchone()
        return self._row_or_none(row, HdhiveSubscription.from_row)

    def set_status(self, subscription_id: int, status: str) -> HdhiveSubscription:
        status = str(status).strip().lower()
        if status not in {"active", "paused", "error", "deleted"}:
            raise ValueError("invalid HDHive subscription status")
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE hdhive_subscriptions SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), int(subscription_id)),
            )
            row = connection.execute(
                "SELECT * FROM hdhive_subscriptions WHERE id = ?",
                (int(subscription_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"HDHive subscription {subscription_id} does not exist")
        return HdhiveSubscription.from_row(row)

    def record_check(self, subscription_id: int, error: str = "", checked_at: float | None = None) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE hdhive_subscriptions
                SET last_checked_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (float(checked_at if checked_at is not None else time.time()), str(error or ""), time.time(), int(subscription_id)),
            )

    def upsert_item(
        self,
        subscription_id: int,
        episode_key: str,
        resource_slug: str,
        validate_status: str,
        resolution_score: int,
        unlock_points: int | None,
        title: str = "",
    ) -> HdhiveSubscriptionItem:
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO hdhive_subscription_items
                    (subscription_id, episode_key, resource_slug, title, validate_status,
                     resolution_score, unlock_points, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id, episode_key, resource_slug) DO UPDATE SET
                    title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE title END,
                    validate_status = excluded.validate_status,
                    resolution_score = excluded.resolution_score,
                    unlock_points = COALESCE(excluded.unlock_points, unlock_points),
                    updated_at = excluded.updated_at
                """,
                (
                    int(subscription_id),
                    str(episode_key),
                    str(resource_slug),
                    str(title or ""),
                    str(validate_status or ""),
                    int(resolution_score),
                    unlock_points,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM hdhive_subscription_items
                WHERE subscription_id = ? AND episode_key = ? AND resource_slug = ?
                """,
                (int(subscription_id), str(episode_key), str(resource_slug)),
            ).fetchone()
        return HdhiveSubscriptionItem.from_row(row)

    def list_items(self, subscription_id: int) -> list[HdhiveSubscriptionItem]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM hdhive_subscription_items
                WHERE subscription_id = ?
                ORDER BY episode_key, resolution_score DESC, id
                """,
                (int(subscription_id),),
            ).fetchall()
        return [HdhiveSubscriptionItem.from_row(row) for row in rows]

    def get_item(self, item_id: int) -> HdhiveSubscriptionItem | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM hdhive_subscription_items WHERE id = ?",
                (int(item_id),),
            ).fetchone()
        return self._row_or_none(row, HdhiveSubscriptionItem.from_row)

    def mark_item_pending(self, item_id: int, error: str = "") -> HdhiveSubscriptionItem:
        return self._update_item(item_id, status="pending_confirmation", last_error=error)

    def mark_item_enqueued(self, item_id: int, task_id: int | None = None) -> HdhiveSubscriptionItem:
        return self._update_item(item_id, status="enqueued", task_id=task_id, last_error="")

    def mark_item_failed(self, item_id: int, error: str) -> HdhiveSubscriptionItem:
        return self._update_item(item_id, status="failed", last_error=error)

    def mark_item_unlocking(self, item_id: int) -> HdhiveSubscriptionItem:
        return self._update_item(item_id, status="unlocking", last_error="")

    def _update_item(self, item_id: int, *, status: str, task_id: int | None = None, last_error: str = "") -> HdhiveSubscriptionItem:
        with self._lock, self._connection() as connection:
            if task_id is None:
                connection.execute(
                    "UPDATE hdhive_subscription_items SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (status, str(last_error or ""), time.time(), int(item_id)),
                )
            else:
                connection.execute(
                    """
                    UPDATE hdhive_subscription_items
                    SET status = ?, task_id = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, int(task_id), str(last_error or ""), time.time(), int(item_id)),
                )
            row = connection.execute(
                "SELECT * FROM hdhive_subscription_items WHERE id = ?",
                (int(item_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"HDHive subscription item {item_id} does not exist")
        return HdhiveSubscriptionItem.from_row(row)

    def claim_daily_run(self, run_date: str, run_id: str, now: float) -> bool:
        with self._lock, self._connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO hdhive_subscription_runs
                        (run_id, run_date, status, started_at)
                    VALUES (?, ?, 'running', ?)
                    """,
                    (str(run_id), str(run_date), float(now)),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any], finished_at: float | None = None) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE hdhive_subscription_runs
                SET status = ?, summary_json = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    str(status),
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    float(finished_at if finished_at is not None else time.time()),
                    str(run_id),
                ),
            )

    def latest_run(self) -> HdhiveSubscriptionRun | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM hdhive_subscription_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row_or_none(row, HdhiveSubscriptionRun.from_row)

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO hdhive_subscription_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (str(key), str(value), time.time()),
            )

    def get_setting(self, key: str) -> str | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM hdhive_subscription_settings WHERE key = ?",
                (str(key),),
            ).fetchone()
        return str(row["value"]) if row is not None else None
