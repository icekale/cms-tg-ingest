from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import TaskSnapshot, TaskStage, TaskStatus


class TaskStore:
    def __init__(self, db_path: str | Path):
        self.db_path = db_path if isinstance(db_path, Path) else Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    share_code TEXT NOT NULL,
                    receive_code TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    tmdb_id TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    current_stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_type TEXT NOT NULL DEFAULT '',
                    error_summary TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(share_code, receive_code)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    error_type TEXT NOT NULL DEFAULT '',
                    error_detail TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, id)")

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> TaskSnapshot:
        return TaskSnapshot.from_row(dict(row))

    def upsert_task(self, share_code: str, receive_code: str, url: str) -> TaskSnapshot:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks (share_code, receive_code, url, current_stage, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_code, receive_code) DO UPDATE SET
                    url = excluded.url,
                    updated_at = excluded.updated_at
                """,
                (share_code, receive_code, url, TaskStage.RECEIVED.value, TaskStatus.PENDING.value, now, now),
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE share_code = ? AND receive_code = ?",
                (share_code, receive_code),
            ).fetchone()
        return self._snapshot(row)

    def find_task(self, task_id: int) -> TaskSnapshot | None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(row) if row else None

    def list_recent_tasks(self, limit: int = 20) -> list[TaskSnapshot]:
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._snapshot(row) for row in rows]

    def list_events(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def record_event(
        self,
        task_id: int,
        stage: TaskStage,
        status: TaskStatus,
        message: str,
        *,
        title: str | None = None,
        tmdb_id: str | None = None,
        category: str | None = None,
        error_type: str = "",
        error_summary: str = "",
        error_detail: str = "",
        increment_retry: bool = False,
    ) -> TaskSnapshot:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, stage.value, status.value, message, error_type, error_detail, now),
            )
            updates = [
                "current_stage = ?",
                "status = ?",
                "error_type = ?",
                "error_summary = ?",
                "updated_at = ?",
            ]
            values: list[Any] = [stage.value, status.value, error_type, error_summary, now]
            if title is not None:
                updates.append("title = ?")
                values.append(title)
            if tmdb_id is not None:
                updates.append("tmdb_id = ?")
                values.append(tmdb_id)
            if category is not None:
                updates.append("category = ?")
                values.append(category)
            if increment_retry:
                updates.append("retry_count = retry_count + 1")
            values.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(row)
