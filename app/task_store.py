from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import TaskSnapshot, TaskStage, TaskStatus


@dataclass(frozen=True)
class TaskQueueSummary:
    recent_count: int
    pending_count: int
    running_count: int
    needs_action_count: int
    failed_count: int
    lock_wait_count: int
    latest_lock_wait: TaskSnapshot | None = None


@dataclass(frozen=True)
class TaskLockClaimResult:
    task: TaskSnapshot | None = None
    holder: TaskSnapshot | None = None


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
            "next_run_at": "REAL NOT NULL DEFAULT -1",
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
    def _merge_metadata(
        existing_json: str | None,
        patch: dict[str, Any] | None,
        delete_keys: tuple[str, ...] | None = None,
    ) -> str:
        try:
            current = json.loads(existing_json or "{}")
        except Exception:
            current = {}
        if not isinstance(current, dict):
            current = {}
        for key in delete_keys or ():
            current.pop(str(key), None)
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

    def queue_summary(self, limit: int = 100) -> TaskQueueSummary:
        tasks = self.list_recent_tasks(limit=limit)
        lock_waits = [
            task
            for task in tasks
            if task.status == TaskStatus.RUNNING and bool(task.metadata.get("_lock_waiting"))
        ]
        return TaskQueueSummary(
            recent_count=len(tasks),
            pending_count=sum(1 for task in tasks if task.status == TaskStatus.PENDING),
            running_count=sum(1 for task in tasks if task.status == TaskStatus.RUNNING),
            needs_action_count=sum(1 for task in tasks if task.status == TaskStatus.NEEDS_ACTION),
            failed_count=sum(1 for task in tasks if task.status == TaskStatus.FAILED),
            lock_wait_count=len(lock_waits),
            latest_lock_wait=lock_waits[0] if lock_waits else None,
        )

    def has_active_task_work(self) -> bool:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM tasks
                WHERE status IN (?, ?)
                  AND current_stage NOT IN (?, ?)
                LIMIT 1
                """,
                (
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                    TaskStage.NEEDS_ACTION.value,
                    TaskStage.FAILED.value,
                ),
            ).fetchone()
        return row is not None

    def find_active_lock_holder(
        self,
        lock_key: str,
        *,
        exclude_task_id: int,
        now: float | None = None,
        stale_after_seconds: int = 21600,
        limit: int = 100,
    ) -> TaskSnapshot | None:
        if not lock_key:
            return None
        current_time = time.time() if now is None else float(now)
        stale_before = current_time - max(1, int(stale_after_seconds))
        for task in self.list_recent_tasks(limit=limit):
            if task.id == exclude_task_id:
                continue
            if task.status != TaskStatus.RUNNING or not task.claimed_by:
                continue
            if task.claimed_at <= stale_before or task.metadata.get("_lock_waiting"):
                continue
            if str(task.metadata.get("_lock_key") or "") == lock_key:
                return task
        return None

    def patch_metadata(self, task_id: int, metadata_patch: dict[str, Any]) -> TaskSnapshot:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT metadata_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(f"task not found: {task_id}")
            merged_metadata = self._merge_metadata(current["metadata_json"], metadata_patch)
            conn.execute(
                "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (merged_metadata, now, task_id),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(row)

    def claim_task_lock(
        self,
        task_id: int,
        lock_metadata: dict[str, Any],
        conflicts_with_holder: Callable[[TaskSnapshot], bool],
        *,
        wait_message: str,
        next_run_at: float,
        now: float | None = None,
        stale_after_seconds: int = 21600,
        limit: int = 100,
    ) -> TaskLockClaimResult:
        current_time = time.time() if now is None else float(now)
        stale_before = current_time - max(1, int(stale_after_seconds))
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            holder: TaskSnapshot | None = None
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE id != ?
                  AND status = ?
                  AND claimed_by != ''
                  AND claimed_at > ?
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (task_id, TaskStatus.RUNNING.value, stale_before, int(limit)),
            ).fetchall()
            for row in rows:
                candidate = self._snapshot(row)
                if candidate.metadata.get("_lock_waiting"):
                    continue
                if conflicts_with_holder(candidate):
                    holder = candidate
                    break

            current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(f"task not found: {task_id}")
            metadata_patch = dict(lock_metadata)
            if holder is not None:
                metadata_patch.update({"_lock_waiting": True, "_lock_owner_task_id": holder.id})
            merged_metadata = self._merge_metadata(current["metadata_json"], metadata_patch)
            if holder is None:
                conn.execute(
                    "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE id = ?",
                    (merged_metadata, current_time, task_id),
                )
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                return TaskLockClaimResult(task=self._snapshot(row) if row else None)

            last_event = conn.execute(
                """
                SELECT stage, status, message, error_type, error_detail
                FROM task_events
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            duplicate_running_event = bool(
                last_event
                and last_event["stage"] == current["current_stage"]
                and last_event["status"] == TaskStatus.RUNNING.value
                and last_event["message"] == wait_message
                and last_event["error_type"] == ""
                and last_event["error_detail"] == ""
            )
            if not duplicate_running_event:
                conn.execute(
                    """
                    INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, current["current_stage"], TaskStatus.RUNNING.value, wait_message, "", "", current_time),
                )
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, metadata_json = ?, next_run_at = ?, claimed_by = '', claimed_at = 0, updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.RUNNING.value, merged_metadata, float(next_run_at), current_time, task_id),
            )
        return TaskLockClaimResult(holder=holder)

    def list_events(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def clear_finished_tasks(self) -> int:
        terminal_statuses = (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN (?, ?)",
                terminal_statuses,
            ).fetchall()
            task_ids = [int(row["id"]) for row in rows]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", task_ids)
            cursor = conn.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", task_ids)
        return int(cursor.rowcount or 0)

    def clear_worker_claims(self, worker_id: str, now: float | None = None) -> int:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            return 0
        current_time = time.time() if now is None else float(now)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET claimed_by = '', claimed_at = 0, next_run_at = ?, updated_at = ?
                WHERE claimed_by = ?
                  AND status = ?
                """,
                (current_time, current_time, worker_id, TaskStatus.RUNNING.value),
            )
        return int(cursor.rowcount or 0)

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
        metadata_delete_keys: tuple[str, ...] | None = None,
        next_run_at: float | None = None,
        clear_claim: bool = False,
    ) -> TaskSnapshot:
        now = time.time()
        with self._lock, self._connection() as conn:
            # Acquire the write lock before reading metadata so concurrent patches do not lose updates.
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT metadata_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            merged_metadata = self._merge_metadata(
                current["metadata_json"] if current else "{}",
                metadata_patch,
                metadata_delete_keys,
            )
            last_event = conn.execute(
                """
                SELECT stage, status, message, error_type, error_detail
                FROM task_events
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            duplicate_running_event = bool(
                status == TaskStatus.RUNNING
                and last_event
                and last_event["stage"] == stage.value
                and last_event["status"] == status.value
                and last_event["message"] == message
                and last_event["error_type"] == error_type
                and last_event["error_detail"] == error_detail
            )
            if not duplicate_running_event:
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
            metadata_delete_keys=("_defer_stage", "_defer_message", "_defer_count"),
            next_run_at=time.time() if next_run_at is None else float(next_run_at),
            clear_claim=True,
        )

    def reprocess_task(
        self,
        task_id: int,
        message: str = "从头重跑已入队",
        next_run_at: float = 0,
    ) -> TaskSnapshot:
        task = self.find_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return self.record_event(
            task_id,
            TaskStage.RECEIVED,
            TaskStatus.PENDING,
            message,
            increment_retry=True,
            metadata_patch={
                "retry_from_stage": task.current_stage.value,
                "retry_stage": TaskStage.RECEIVED.value,
                "force_reprocess": True,
            },
            metadata_delete_keys=("_defer_stage", "_defer_message", "_defer_count"),
            next_run_at=next_run_at,
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
                  AND current_stage NOT IN (?, ?)
                  AND next_run_at >= 0
                  AND next_run_at <= ?
                  AND (claimed_by = '' OR claimed_at <= ?)
                ORDER BY updated_at ASC, id ASC
                LIMIT 10
                """,
                (
                    runnable_statuses[0],
                    runnable_statuses[1],
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
                      AND current_stage NOT IN (?, ?)
                      AND next_run_at >= 0
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
