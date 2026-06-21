from __future__ import annotations

import json
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
            self._ensure_columns(conn)
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

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        columns = {
            "chat_id": "TEXT NOT NULL DEFAULT ''",
            "submission_id": "INTEGER",
            "next_run_at": "REAL NOT NULL DEFAULT 0",
            "claimed_by": "TEXT NOT NULL DEFAULT ''",
            "claimed_at": "REAL NOT NULL DEFAULT 0",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next_run ON tasks(status, next_run_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(claimed_by, claimed_at)")

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> TaskSnapshot:
        return TaskSnapshot.from_row(dict(row))

    @staticmethod
    def _merge_metadata(existing_json: str | None, patch: dict[str, Any] | None) -> str:
        try:
            current = json.loads(existing_json or "{}")
        except Exception:
            current = {}
        if not isinstance(current, dict):
            current = {}
        if patch:
            current.update({str(key): value for key, value in patch.items() if value is not None})
        return json.dumps(current, ensure_ascii=False, sort_keys=True)

    def upsert_task(self, share_code: str, receive_code: str, url: str, chat_id: str = "") -> TaskSnapshot:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks (share_code, receive_code, url, chat_id, current_stage, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_code, receive_code) DO UPDATE SET
                    url = excluded.url,
                    chat_id = COALESCE(NULLIF(excluded.chat_id, ''), tasks.chat_id),
                    updated_at = excluded.updated_at
                """,
                (share_code, receive_code, url, chat_id, TaskStage.RECEIVED.value, TaskStatus.PENDING.value, now, now),
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
        submission_id: int | None = None,
        metadata_patch: dict[str, Any] | None = None,
        next_run_at: float | None = None,
        clear_claim: bool = False,
    ) -> TaskSnapshot:
        now = time.time()
        with self._lock, self._connection() as conn:
            current = conn.execute("SELECT metadata_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            merged_metadata = self._merge_metadata(current["metadata_json"] if current else "{}", metadata_patch)
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
                "metadata_json = ?",
                "updated_at = ?",
            ]
            values: list[Any] = [stage.value, status.value, error_type, error_summary, merged_metadata, now]
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
            if submission_id is not None:
                updates.append("submission_id = ?")
                values.append(int(submission_id))
            if next_run_at is not None:
                updates.append("next_run_at = ?")
                values.append(float(next_run_at))
            if clear_claim:
                updates.append("claimed_by = ''")
                updates.append("claimed_at = 0")
            values.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(row)

    def enqueue_task(
        self,
        task_id: int,
        stage: TaskStage | None = None,
        message: str = "等待执行",
        next_run_at: float | None = None,
    ) -> TaskSnapshot:
        task = self.find_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        target_stage = stage or task.current_stage
        return self.record_event(
            task_id,
            target_stage,
            TaskStatus.PENDING,
            message,
            next_run_at=time.time() if next_run_at is None else float(next_run_at),
            clear_claim=True,
        )

    def claim_next_runnable(
        self,
        worker_id: str,
        now: float | None = None,
        stale_after_seconds: int = 21600,
    ) -> TaskSnapshot | None:
        current_time = time.time() if now is None else float(now)
        stale_before = current_time - max(1, int(stale_after_seconds))
        runnable_statuses = (TaskStatus.PENDING.value, TaskStatus.RUNNING.value)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                  AND current_stage NOT IN (?, ?, ?)
                  AND next_run_at <= ?
                  AND (claimed_by = '' OR claimed_at <= ?)
                ORDER BY updated_at ASC, id ASC
                LIMIT 10
                """,
                (
                    runnable_statuses[0],
                    runnable_statuses[1],
                    TaskStage.CLEANED.value,
                    TaskStage.NEEDS_ACTION.value,
                    TaskStage.FAILED.value,
                    current_time,
                    stale_before,
                ),
            ).fetchall()
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, claimed_by = ?, claimed_at = ?, updated_at = ?
                    WHERE id = ?
                      AND status IN (?, ?)
                      AND current_stage NOT IN (?, ?, ?)
                      AND next_run_at <= ?
                      AND (claimed_by = '' OR claimed_at <= ?)
                    """,
                    (
                        TaskStatus.RUNNING.value,
                        worker_id,
                        current_time,
                        current_time,
                        int(row["id"]),
                        runnable_statuses[0],
                        runnable_statuses[1],
                        TaskStage.CLEANED.value,
                        TaskStage.NEEDS_ACTION.value,
                        TaskStage.FAILED.value,
                        current_time,
                        stale_before,
                    ),
                )
                if cursor.rowcount == 0:
                    continue
                claimed = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(row["id"]),)).fetchone()
                return self._snapshot(claimed) if claimed else None
            else:
                return None
