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
class TaskHealthAggregate:
    recent_count: int
    pending_count: int
    running_count: int
    needs_action_count: int
    failed_count: int
    unscheduled_count: int
    problem_count: int
    lock_wait_count: int
    p115_cooldown_until: float
    wait_tasks: tuple[TaskSnapshot, ...] = ()
    latest_problem: TaskSnapshot | None = None
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_stage_status_next ON tasks(current_stage, status, next_run_at, id)")

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

    def find_task_by_share_key(self, share_code: str, receive_code: str) -> TaskSnapshot | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE share_code = ? AND receive_code = ?",
                (str(share_code), str(receive_code)),
            ).fetchone()
        return self._snapshot(row) if row else None

    def list_recent_tasks(self, limit: int = 20) -> list[TaskSnapshot]:
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._snapshot(row) for row in rows]

    def list_open_tasks(self) -> list[TaskSnapshot]:
        open_statuses = (
            TaskStatus.PENDING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.FAILED.value,
            TaskStatus.NEEDS_ACTION.value,
        )
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks INDEXED BY idx_tasks_next_run
                WHERE status IN (?, ?, ?, ?)
                ORDER BY updated_at DESC, id DESC
                """,
                open_statuses,
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def find_pending_stage(self, stage: TaskStage, *, exclude_task_id: int | None = None) -> TaskSnapshot | None:
        params: list[Any] = [stage.value, TaskStatus.PENDING.value, TaskStatus.RUNNING.value]
        exclude_clause = ""
        if exclude_task_id is not None:
            exclude_clause = " AND id <> ?"
            params.append(int(exclude_task_id))
        with self._lock, self._connection() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM tasks INDEXED BY idx_tasks_stage_status_next
                WHERE current_stage = ?
                  AND status IN (?, ?)
                  AND next_run_at >= 0
                  {exclude_clause}
                ORDER BY updated_at ASC, id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return self._snapshot(row) if row else None

    def aggregate_open_task_health(self, limit: int = 5) -> TaskHealthAggregate:
        recent_limit = max(0, int(limit))
        detail_limit = min(5, recent_limit)
        open_statuses = (
            TaskStatus.PENDING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.FAILED.value,
            TaskStatus.NEEDS_ACTION.value,
        )
        lock_wait_value = """
            CASE
                WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$._lock_waiting')
                ELSE NULL
            END
        """
        lock_wait_condition = f"COALESCE({lock_wait_value}, '') NOT IN ('', 0)"
        p115_cooldown_value = """
            CASE
                WHEN json_valid(metadata_json)
                THEN CAST(COALESCE(json_extract(metadata_json, '$.p115_risk_cooldown_until'), 0) AS REAL)
                ELSE 0
            END
        """
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN")
            recent_count = conn.execute(
                """
                SELECT COUNT(*) AS recent_count
                FROM (
                    SELECT id FROM tasks
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                )
                """,
                (recent_limit,),
            ).fetchone()
            aggregate = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0) AS pending_count,
                    COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0) AS running_count,
                    COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0) AS needs_action_count,
                    COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0) AS failed_count,
                    COALESCE(SUM(CASE
                        WHEN status IN (?, ?) AND next_run_at < 0 AND TRIM(claimed_by) = ''
                        THEN 1 ELSE 0 END), 0) AS unscheduled_count,
                    COALESCE(SUM(CASE
                        WHEN status IN (?, ?)
                          OR (status IN (?, ?) AND next_run_at < 0 AND TRIM(claimed_by) = '')
                        THEN 1 ELSE 0 END), 0) AS problem_count,
                    COALESCE(SUM(CASE
                        WHEN status = ? AND {lock_wait_condition}
                        THEN 1 ELSE 0 END), 0) AS lock_wait_count,
                    COALESCE(MAX({p115_cooldown_value}), 0) AS p115_cooldown_until
                FROM tasks INDEXED BY idx_tasks_next_run
                WHERE status IN (?, ?, ?, ?)
                """,
                (
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.NEEDS_ACTION.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.NEEDS_ACTION.value,
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.RUNNING.value,
                    *open_statuses,
                ),
            ).fetchone()
            wait_rows = conn.execute(
                """
                SELECT * FROM tasks INDEXED BY idx_tasks_next_run
                WHERE status IN (?, ?)
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (TaskStatus.PENDING.value, TaskStatus.RUNNING.value, detail_limit),
            ).fetchall()
            latest_problem_row = conn.execute(
                """
                SELECT * FROM tasks INDEXED BY idx_tasks_next_run
                WHERE status IN (?, ?)
                   OR (status IN (?, ?) AND next_run_at < 0 AND TRIM(claimed_by) = '')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (
                    TaskStatus.FAILED.value,
                    TaskStatus.NEEDS_ACTION.value,
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                ),
            ).fetchone()
            latest_lock_wait_row = conn.execute(
                f"""
                SELECT * FROM tasks INDEXED BY idx_tasks_next_run
                WHERE status = ? AND {lock_wait_condition}
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (TaskStatus.RUNNING.value,),
            ).fetchone()

        snapshots: dict[int, TaskSnapshot] = {}

        def snapshot(row: sqlite3.Row | None) -> TaskSnapshot | None:
            if row is None:
                return None
            task_id = int(row["id"])
            existing = snapshots.get(task_id)
            if existing is not None:
                return existing
            task = self._snapshot(row)
            snapshots[task_id] = task
            return task

        wait_tasks = tuple(snapshot(row) for row in wait_rows)
        return TaskHealthAggregate(
            recent_count=int(recent_count["recent_count"]),
            pending_count=int(aggregate["pending_count"]),
            running_count=int(aggregate["running_count"]),
            needs_action_count=int(aggregate["needs_action_count"]),
            failed_count=int(aggregate["failed_count"]),
            unscheduled_count=int(aggregate["unscheduled_count"]),
            problem_count=int(aggregate["problem_count"]),
            lock_wait_count=int(aggregate["lock_wait_count"]),
            p115_cooldown_until=float(aggregate["p115_cooldown_until"] or 0),
            wait_tasks=tuple(task for task in wait_tasks if task is not None),
            latest_problem=snapshot(latest_problem_row),
            latest_lock_wait=snapshot(latest_lock_wait_row),
        )

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
                  AND next_run_at >= 0
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

    def compare_and_set_transition(
        self,
        task_id: int,
        expected_stage: TaskStage,
        allowed_statuses: set[TaskStatus] | tuple[TaskStatus, ...],
        *,
        require_unclaimed: bool,
        target_stage: TaskStage,
        target_status: TaskStatus,
        target_event_message: str,
        initial_event_message: str | None = None,
        initial_event_stage: TaskStage | None = None,
        increment_retry: bool = False,
        metadata_patch: dict[str, Any] | None = None,
        metadata_delete_keys: tuple[str, ...] | None = None,
        next_run_at: float | None = None,
        clear_errors: bool = False,
        clear_claim: bool = False,
    ) -> TaskSnapshot | None:
        allowed_status_values = {status.value for status in allowed_statuses}
        if not allowed_status_values:
            return None
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current is None:
                return None
            if current["current_stage"] != expected_stage.value:
                return None
            if current["status"] not in allowed_status_values:
                return None
            if require_unclaimed and str(current["claimed_by"] or "").strip():
                return None

            merged_metadata = self._merge_metadata(
                current["metadata_json"],
                metadata_patch,
                metadata_delete_keys,
            )
            if initial_event_message is not None:
                conn.execute(
                    """
                    INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
                    VALUES (?, ?, ?, ?, '', '', ?)
                    """,
                    (
                        task_id,
                        (initial_event_stage or expected_stage).value,
                        TaskStatus.PENDING.value,
                        initial_event_message,
                        now,
                    ),
                )
            conn.execute(
                """
                INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
                VALUES (?, ?, ?, ?, '', '', ?)
                """,
                (task_id, target_stage.value, target_status.value, target_event_message, now),
            )
            updates = [
                "current_stage = ?",
                "status = ?",
                "metadata_json = ?",
                "updated_at = ?",
            ]
            values: list[Any] = [target_stage.value, target_status.value, merged_metadata, now]
            if increment_retry:
                updates.append("retry_count = retry_count + 1")
            if next_run_at is not None:
                updates.append("next_run_at = ?")
                values.append(float(next_run_at))
            if clear_errors:
                updates.append("error_type = ''")
                updates.append("error_summary = ''")
            if clear_claim:
                updates.append("claimed_by = ''")
                updates.append("claimed_at = 0")
            values.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(row) if row else None

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
