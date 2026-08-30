from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import TaskOperation, TaskSnapshot, TaskStage, TaskStatus
from .quality_rules import quality_attempt_count
from .self_share_settings import normalize_receive_cid, normalize_self_share_review_mode
from .strm_mode import is_strm_mode_locked, normalize_strm_mode


STRM_DEFAULT_MODE_KEY = "strm_default_mode"
OWN_SHARE_RECEIVE_CODE_KEY = "own_share_receive_code_override"
SELF_SHARE_RECEIVE_CID_KEY = "self_share_receive_cid_override"
SELF_SHARE_REVIEW_MODE_KEY = "self_share_review_mode_override"
EMBY_BASE_URL_OVERRIDE_KEY = "emby_base_url_override"
EMBY_API_KEY_OVERRIDE_KEY = "emby_api_key_override"
TMDB_API_KEY_OVERRIDE_KEY = "tmdb_api_key_override"
TMDB_BEARER_TOKEN_OVERRIDE_KEY = "tmdb_bearer_token_override"
TERMINATION_REQUESTED_AT_KEY = "termination_requested_at"
TERMINATION_REQUESTED_BY_KEY = "termination_requested_by"
_TERMINATION_METADATA_DELETE_KEYS = (
    TERMINATION_REQUESTED_AT_KEY,
    TERMINATION_REQUESTED_BY_KEY,
    "_lock_key",
    "_lock_reason",
    "_lock_waiting",
    "_lock_owner_task_id",
)
_DELETABLE_TASK_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED.value,
        TaskStatus.FAILED.value,
        TaskStatus.NEEDS_ACTION.value,
        TaskStatus.CANCELLED.value,
    }
)
REPROCESS_METADATA_DELETE_KEYS = (
    "_defer_stage",
    "_defer_message",
    "_defer_count",
    "_lock_key",
    "_lock_waiting",
    "_lock_owner_task_id",
    "_lock_reason",
    "organized_scan_cursor",
    "organized_folder",
    "organized_targets",
    "multi_target_version",
    "direct_strm_removed",
    "received_title",
    "received_file_ids",
    "received_items",
    "received_items_complete",
    "received_expected_item_count",
    "received_existing_file_ids",
    "received_snapshot_complete",
    "intake_identity",
    "tmdb_hint_id",
    "tmdb_hint_title",
    "tmdb_hint_category",
    "tmdb_hint_source_name",
    "tmdb_hint_normalized",
    "tmdb_hint_normalized_items",
    "self_share_reprocess_reset",
    "own_share_file_id",
    "own_share_file_name",
    "own_share_code",
    "own_share_receive_code",
    "own_share_url",
    "own_share_available",
    "own_share_child_ids",
    "rejected_organized_file_ids",
    "share_alias_name",
    "share_alias_level",
    "share_created_at",
    "share_sync_status",
    "share_sync_wait_task_id",
    "share_validation_status",
    "share_validation_error",
    "share_playback_validated",
    "share_playback_error",
    "share_review_status",
    "share_review_checks",
    "share_review_last_at",
    "share_review_next_at",
    "share_review_error",
    "invalid_share_status",
    "canonical_manifest_json",
    "canonical_strm_paths_restored",
    "source_path",
    "dest_path",
    "category_final",
    "move_status",
    "move_error",
    "move_started_at",
    "move_finished_at",
    "emby_status",
    "emby_match_count",
    "emby_item_id",
    "emby_title",
    "emby_path",
    "emby_parent",
    "emby_refresh_requested",
    "emby_refresh_library",
    "emby_refresh_error",
    "item_id",
    "path",
    "parent",
    "library",
    "cleanup_status",
    "cleanup_file_id",
    "cleanup_error",
    "cleanup_finished_at",
    "cms_delete_settled",
    "quality_cleanup_completed",
    "quality_success_event",
    "quality_repair_queued",
)
# 115 rejects receiving the same share again after a successful receive.
SHARE_RECEIVE_SNAPSHOT_KEYS = frozenset(
    {
        "received_title",
        "received_file_ids",
        "received_items",
        "received_items_complete",
        "received_expected_item_count",
        "received_existing_file_ids",
        "received_snapshot_complete",
        "intake_identity",
    }
)
SHARE_REPROCESS_METADATA_DELETE_KEYS = tuple(
    key for key in REPROCESS_METADATA_DELETE_KEYS if key not in SHARE_RECEIVE_SNAPSHOT_KEYS
)
CLOUD_REPROCESS_METADATA_DELETE_KEYS = REPROCESS_METADATA_DELETE_KEYS + (
    "cloud_info_hash",
    "cloud_task_id",
    "cloud_started_at",
    "cloud_target_cid",
    "cloud_status",
    "cloud_output_file_id",
    "cloud_output_parent_id",
    "cloud_output_name",
    "cloud_output_items",
    "auto_organize_pending",
    "auto_organize_last_error",
    "auto_organize_submitted_at",
)

QUALITY_STATE_DEFAULTS: dict[str, Any] = {
    "quality_manual_status": "open",
    "quality_repair_attempts": 0,
    "quality_last_attempt_at": 0,
    "quality_next_eligible_at": 0,
    "quality_rule_id": "",
    "quality_rule_reason": "",
    "quality_rule_risk_level": "",
    "quality_issue_codes": [],
    "quality_last_run_id": "",
    "quality_last_actor": "",
    "quality_rule_version": "",
    "quality_repair_queued": False,
    "quality_repair_started_at": 0,
    "quality_repair_deadline_at": 0,
    "quality_archived_at": 0,
    "quality_archived_reason": "",
    "quality_snoozed_until": 0,
}

_QUALITY_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


def _quality_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _QUALITY_TRUE_VALUES


def reprocess_stage_for(task: TaskSnapshot) -> TaskStage:
    """Return the first stage appropriate for the task's source type."""
    if str(task.source_type or "").strip().lower() == "cloud_download":
        return TaskStage.CLOUD_DOWNLOADING
    return TaskStage.RECEIVED


def reprocess_delete_keys_for(
    task: TaskSnapshot,
    *,
    preserve_received_snapshot: bool = False,
) -> tuple[str, ...]:
    """Return attempt metadata that must not survive a source reprocess."""
    source_type = str(task.source_type or "").strip().lower()
    if source_type == "cloud_download":
        return CLOUD_REPROCESS_METADATA_DELETE_KEYS
    if preserve_received_snapshot and source_type == "share":
        return SHARE_REPROCESS_METADATA_DELETE_KEYS
    return REPROCESS_METADATA_DELETE_KEYS


def build_reprocess_metadata(
    task: TaskSnapshot,
    metadata_patch: dict[str, Any] | None = None,
    *,
    started_at: float | None = None,
) -> dict[str, Any]:
    target_stage = reprocess_stage_for(task)
    patch = dict(metadata_patch or {})
    if target_stage == TaskStage.CLOUD_DOWNLOADING:
        patch["strm_mode"] = "shared"
    metadata = {
        "retry_from_stage": task.current_stage.value,
        "retry_stage": target_stage.value,
        "force_reprocess": True,
        "reprocess_started_at": time.time() if started_at is None else float(started_at),
        **patch,
    }
    metadata["operation_generation"] = max(0, int(task.metadata.get("operation_generation") or 0)) + 1
    return metadata


def operation_scope(task: TaskSnapshot) -> str:
    generation = max(0, int(task.metadata.get("operation_generation") or 0))
    update_run = max(0, int(task.metadata.get("update_requested_run") or 0))
    return f"g{generation}:u{update_run}"


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
    runner_heartbeat_at: float = 0.0
    runner_state: str = ""
    wait_tasks: tuple[TaskSnapshot, ...] = ()
    latest_problem: TaskSnapshot | None = None
    latest_lock_wait: TaskSnapshot | None = None


@dataclass(frozen=True)
class TaskLockClaimResult:
    task: TaskSnapshot | None = None
    holder: TaskSnapshot | None = None
    stale: bool = False


class TaskStore:
    def __init__(self, db_path: str | Path, default_strm_mode: str = "shared"):
        self.db_path = db_path if isinstance(db_path, Path) else Path(db_path)
        self.default_strm_mode = normalize_strm_mode(default_strm_mode)
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
                    source_type TEXT NOT NULL DEFAULT 'share',
                    source_key TEXT NOT NULL DEFAULT '',
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    operation_key TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL NOT NULL DEFAULT 0,
                    finished_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    UNIQUE(task_id, operation_key),
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_operations_task_type_status "
                "ON task_operations(task_id, operation_type, status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quality_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    run_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL DEFAULT 0,
                    scanned_count INTEGER NOT NULL DEFAULT 0,
                    issue_count INTEGER NOT NULL DEFAULT 0,
                    planned_count INTEGER NOT NULL DEFAULT 0,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    manual_count INTEGER NOT NULL DEFAULT 0,
                    cooldown_count INTEGER NOT NULL DEFAULT 0,
                    rule_counts_json TEXT NOT NULL DEFAULT '{}',
                    budget_used_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_runs_started ON quality_runs(started_at)")

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        columns = {
            "chat_id": "TEXT NOT NULL DEFAULT ''",
            "submission_id": "INTEGER",
            "next_run_at": "REAL NOT NULL DEFAULT -1",
            "claimed_by": "TEXT NOT NULL DEFAULT ''",
            "claimed_at": "REAL NOT NULL DEFAULT 0",
            "claim_token": "TEXT NOT NULL DEFAULT ''",
            "claim_heartbeat_at": "REAL NOT NULL DEFAULT 0",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "source_type": "TEXT NOT NULL DEFAULT 'share'",
            "source_key": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        active_legacy_claims = conn.execute(
            "SELECT id, claimed_by, claimed_at FROM tasks WHERE claimed_by != '' AND claim_token = ''"
        ).fetchall()
        for row in active_legacy_claims:
            legacy_token = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"task-claim:{int(row['id'])}:{str(row['claimed_by'])}:{float(row['claimed_at'] or 0)}",
                )
            )
            conn.execute(
                "UPDATE tasks SET claim_token = ?, claim_heartbeat_at = claimed_at WHERE id = ?",
                (legacy_token, int(row["id"])),
            )
        conn.execute("UPDATE tasks SET claim_token = '', claim_heartbeat_at = 0 WHERE claimed_by = ''")
        conn.execute("UPDATE tasks SET source_type = 'share' WHERE source_type IS NULL OR source_type = ''")
        conn.execute(
            "UPDATE tasks SET source_key = 'share:' || share_code || ':' || receive_code WHERE source_key IS NULL OR source_key = ''"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source_key ON tasks(source_type, source_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next_run ON tasks(status, next_run_at, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(claimed_by, claimed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_claim_heartbeat ON tasks(claimed_by, claim_heartbeat_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_stage_status_next ON tasks(current_stage, status, next_run_at, id)")

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> TaskSnapshot:
        return TaskSnapshot.from_row(dict(row))

    @staticmethod
    def _operation(row: sqlite3.Row) -> TaskOperation:
        return TaskOperation.from_row(dict(row))

    @staticmethod
    def _operation_json(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    def prepare_operation(
        self,
        task_id: int,
        operation_key: str,
        operation_type: str,
        request: dict[str, Any],
    ) -> TaskOperation:
        request_json = self._operation_json(request)
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM task_operations WHERE task_id = ? AND operation_key = ?",
                (int(task_id), str(operation_key)),
            ).fetchone()
            if existing is not None:
                if existing["operation_type"] != str(operation_type) or existing["request_json"] != request_json:
                    raise ValueError("operation request identity is immutable")
                return self._operation(existing)
            conn.execute(
                """
                INSERT INTO task_operations (
                    task_id, operation_key, operation_type, status, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'prepared', ?, ?, ?)
                """,
                (int(task_id), str(operation_key), str(operation_type), request_json, now, now),
            )
            row = conn.execute(
                "SELECT * FROM task_operations WHERE task_id = ? AND operation_key = ?",
                (int(task_id), str(operation_key)),
            ).fetchone()
        return self._operation(row)

    def find_operation(self, task_id: int, operation_key: str) -> TaskOperation | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_operations WHERE task_id = ? AND operation_key = ?",
                (int(task_id), str(operation_key)),
            ).fetchone()
        return self._operation(row) if row is not None else None

    def list_operations(self, task_id: int) -> list[TaskOperation]:
        """All operation rows for one task, oldest first."""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_operations WHERE task_id = ? ORDER BY id",
                (int(task_id),),
            ).fetchall()
        return [self._operation(row) for row in rows]

    def start_operation(self, task_id: int, operation_key: str) -> TaskOperation | None:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE task_operations
                SET status = 'started', attempt_count = attempt_count + 1, started_at = ?, updated_at = ?
                WHERE task_id = ? AND operation_key = ? AND status = 'prepared'
                """,
                (now, now, int(task_id), str(operation_key)),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM task_operations WHERE task_id = ? AND operation_key = ?",
                (int(task_id), str(operation_key)),
            ).fetchone()
        return self._operation(row)

    def reprepare_operation(self, task_id: int, operation_key: str) -> TaskOperation | None:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE task_operations
                SET status = 'prepared',
                    started_at = 0,
                    finished_at = 0,
                    result_json = '{}',
                    last_error = '',
                    updated_at = ?
                WHERE task_id = ? AND operation_key = ? AND status IN ('started', 'uncertain')
                """,
                (now, int(task_id), str(operation_key)),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM task_operations WHERE task_id = ? AND operation_key = ?",
                (int(task_id), str(operation_key)),
            ).fetchone()
        return self._operation(row)

    def complete_operation(
        self,
        task_id: int,
        operation_key: str,
        result: dict[str, Any],
    ) -> TaskOperation | None:
        result_json = self._operation_json(result)
        return self._finish_operation(task_id, operation_key, "succeeded", result_json=result_json)

    def mark_operation_uncertain(
        self,
        task_id: int,
        operation_key: str,
        last_error: str,
    ) -> TaskOperation | None:
        return self._finish_operation(task_id, operation_key, "uncertain", last_error=last_error)

    def mark_operation_failed(
        self,
        task_id: int,
        operation_key: str,
        last_error: str,
    ) -> TaskOperation | None:
        return self._finish_operation(task_id, operation_key, "failed", last_error=last_error)

    def _finish_operation(
        self,
        task_id: int,
        operation_key: str,
        status: str,
        *,
        result_json: str | None = None,
        last_error: str | None = None,
    ) -> TaskOperation | None:
        now = time.time()
        assignments = ["status = ?", "finished_at = ?", "updated_at = ?"]
        values: list[Any] = [status, now, now]
        if result_json is not None:
            assignments.append("result_json = ?")
            values.append(result_json)
        if last_error is not None:
            assignments.append("last_error = ?")
            values.append(str(last_error))
        values.extend([int(task_id), str(operation_key)])
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"UPDATE task_operations SET {', '.join(assignments)} "
                "WHERE task_id = ? AND operation_key = ? AND status = 'started'",
                values,
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM task_operations WHERE task_id = ? AND operation_key = ?",
                (int(task_id), str(operation_key)),
            ).fetchone()
        return self._operation(row)

    def set_runtime_state(self, key: str, value: str, updated_at: float | None = None) -> None:
        timestamp = time.time() if updated_at is None else float(updated_at)
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (str(key), str(value), timestamp),
            )

    def refresh_runtime_state_timestamp(self, key: str, updated_at: float | None = None) -> bool:
        timestamp = time.time() if updated_at is None else float(updated_at)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE runtime_state SET updated_at = ? WHERE key = ?",
                (timestamp, str(key)),
            )
        return cursor.rowcount > 0

    def get_runtime_state(self, key: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT value, updated_at FROM runtime_state WHERE key = ?", (str(key),)).fetchone()
        if row is None:
            return None
        return {"value": str(row["value"]), "updated_at": float(row["updated_at"])}

    def delete_runtime_state(self, key: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM runtime_state WHERE key = ?", (str(key),))

    def record_quality_run(
        self,
        run_id: str,
        run_date: str,
        status: str,
        started_at: float,
        finished_at: float | None = None,
        *,
        scanned_count: int = 0,
        issue_count: int = 0,
        planned_count: int = 0,
        queued_count: int = 0,
        failed_count: int = 0,
        skipped_count: int = 0,
        manual_count: int = 0,
        cooldown_count: int = 0,
        rule_counts: dict[str, int] | None = None,
        budget_used: dict[str, object] | None = None,
    ) -> None:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO quality_runs (
                    run_id, run_date, status, started_at, finished_at,
                    scanned_count, issue_count, planned_count, queued_count,
                    failed_count, skipped_count, manual_count, cooldown_count,
                    rule_counts_json, budget_used_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    run_date = excluded.run_date,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    scanned_count = excluded.scanned_count,
                    issue_count = excluded.issue_count,
                    planned_count = excluded.planned_count,
                    queued_count = excluded.queued_count,
                    failed_count = excluded.failed_count,
                    skipped_count = excluded.skipped_count,
                    manual_count = excluded.manual_count,
                    cooldown_count = excluded.cooldown_count,
                    rule_counts_json = excluded.rule_counts_json,
                    budget_used_json = excluded.budget_used_json,
                    created_at = excluded.created_at
                """,
                (
                    str(run_id),
                    str(run_date),
                    str(status),
                    float(started_at),
                    float(finished_at or 0),
                    int(scanned_count),
                    int(issue_count),
                    int(planned_count),
                    int(queued_count),
                    int(failed_count),
                    int(skipped_count),
                    int(manual_count),
                    int(cooldown_count),
                    json.dumps(rule_counts or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(budget_used or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

    def list_quality_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        normalized_limit = max(1, min(int(limit), 365))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM quality_runs
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def quality_run_trend(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = time.time() - max(1, int(days)) * 86400
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT run_date,
                       COUNT(*) AS runs,
                       SUM(scanned_count) AS scanned_count,
                       SUM(issue_count) AS issue_count,
                       SUM(planned_count) AS planned_count,
                       SUM(queued_count) AS queued_count,
                       SUM(failed_count) AS failed_count,
                       SUM(manual_count) AS manual_count,
                       SUM(cooldown_count) AS cooldown_count
                FROM quality_runs
                WHERE started_at >= ?
                GROUP BY run_date
                ORDER BY run_date ASC
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_default_strm_mode(self) -> str:
        state = self.get_runtime_state(STRM_DEFAULT_MODE_KEY)
        return normalize_strm_mode(state["value"] if state else self.default_strm_mode)

    def set_default_strm_mode(self, mode: str) -> str:
        normalized = normalize_strm_mode(mode)
        self.set_runtime_state(STRM_DEFAULT_MODE_KEY, normalized)
        return normalized

    def get_own_share_receive_code_override(self) -> str | None:
        state = self.get_runtime_state(OWN_SHARE_RECEIVE_CODE_KEY)
        if state is None:
            return None
        value = str(state["value"] or "").strip()
        return value or None

    def set_own_share_receive_code_override(self, receive_code: str) -> str:
        value = str(receive_code or "").strip()
        if not value or not value.isascii() or not value.isalnum():
            raise ValueError("分享访问码只能包含英文字母和数字")
        self.set_runtime_state(OWN_SHARE_RECEIVE_CODE_KEY, value)
        return value

    def clear_own_share_receive_code_override(self) -> None:
        self.delete_runtime_state(OWN_SHARE_RECEIVE_CODE_KEY)

    def get_self_share_receive_cid_override(self) -> str | None:
        state = self.get_runtime_state(SELF_SHARE_RECEIVE_CID_KEY)
        if state is None:
            return None
        value = normalize_receive_cid(state["value"])
        return value or None

    def set_self_share_receive_cid_override(self, receive_cid: str) -> str:
        value = normalize_receive_cid(receive_cid)
        if not value:
            raise ValueError("待整理目录 ID 必须是大于 0 的数字")
        self.set_runtime_state(SELF_SHARE_RECEIVE_CID_KEY, value)
        return value

    def clear_self_share_receive_cid_override(self) -> None:
        self.delete_runtime_state(SELF_SHARE_RECEIVE_CID_KEY)

    def get_self_share_review_mode_override(self) -> str | None:
        state = self.get_runtime_state(SELF_SHARE_REVIEW_MODE_KEY)
        if state is None:
            return None
        try:
            return normalize_self_share_review_mode(state["value"])
        except ValueError:
            return None

    def set_self_share_review_mode_override(self, mode: str) -> str:
        normalized = normalize_self_share_review_mode(mode)
        self.set_runtime_state(SELF_SHARE_REVIEW_MODE_KEY, normalized)
        return normalized

    def clear_self_share_review_mode_override(self) -> None:
        self.delete_runtime_state(SELF_SHARE_REVIEW_MODE_KEY)

    def get_emby_base_url_override(self) -> str | None:
        state = self.get_runtime_state(EMBY_BASE_URL_OVERRIDE_KEY)
        if state is None:
            return None
        value = str(state["value"] or "").strip()
        return value or None

    def set_emby_base_url_override(self, base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        if not value:
            raise ValueError("Emby 地址不能为空")
        if not value.startswith(("http://", "https://")):
            raise ValueError("Emby 地址必须以 http:// 或 https:// 开头")
        self.set_runtime_state(EMBY_BASE_URL_OVERRIDE_KEY, value)
        return value

    def clear_emby_base_url_override(self) -> None:
        self.delete_runtime_state(EMBY_BASE_URL_OVERRIDE_KEY)

    def get_emby_api_key_override(self) -> str | None:
        state = self.get_runtime_state(EMBY_API_KEY_OVERRIDE_KEY)
        if state is None:
            return None
        value = str(state["value"] or "").strip()
        return value or None

    def set_emby_api_key_override(self, api_key: str) -> str:
        value = str(api_key or "").strip()
        if not value:
            raise ValueError("Emby API Key 不能为空")
        if len(value) < 8:
            raise ValueError("Emby API Key 长度不足（至少 8 位）")
        self.set_runtime_state(EMBY_API_KEY_OVERRIDE_KEY, value)
        return value

    def clear_emby_api_key_override(self) -> None:
        self.delete_runtime_state(EMBY_API_KEY_OVERRIDE_KEY)

    def get_tmdb_api_key_override(self) -> str | None:
        state = self.get_runtime_state(TMDB_API_KEY_OVERRIDE_KEY)
        if state is None:
            return None
        value = str(state["value"] or "").strip()
        return value or None

    def set_tmdb_api_key_override(self, api_key: str) -> str:
        value = str(api_key or "").strip()
        if not value:
            raise ValueError("TMDB API Key 不能为空")
        self.set_runtime_state(TMDB_API_KEY_OVERRIDE_KEY, value)
        return value

    def clear_tmdb_api_key_override(self) -> None:
        self.delete_runtime_state(TMDB_API_KEY_OVERRIDE_KEY)

    def get_tmdb_bearer_token_override(self) -> str | None:
        state = self.get_runtime_state(TMDB_BEARER_TOKEN_OVERRIDE_KEY)
        if state is None:
            return None
        value = str(state["value"] or "").strip()
        return value or None

    def set_tmdb_bearer_token_override(self, token: str) -> str:
        value = str(token or "").strip()
        self.set_runtime_state(TMDB_BEARER_TOKEN_OVERRIDE_KEY, value)
        return value

    def clear_tmdb_bearer_token_override(self) -> None:
        self.delete_runtime_state(TMDB_BEARER_TOKEN_OVERRIDE_KEY)

    def get_cms_version_overrides(self) -> dict[str, Any]:
        state = self.get_runtime_state("cms_version_overrides")
        if not state:
            return {}
        try:
            payload = json.loads(str(state["value"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def set_cms_version_overrides(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_cms_version_overrides()
        merged = dict(current)
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[str(key)] = value
        self.set_runtime_state(
            "cms_version_overrides",
            json.dumps(merged, ensure_ascii=False, sort_keys=True),
        )
        return merged

    def clear_cms_version_overrides(self) -> None:
        self.delete_runtime_state("cms_version_overrides")

    def wake_self_share_review_tasks(self, now: float | None = None) -> int:
        current_time = time.time() if now is None else float(now)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET next_run_at = ?, updated_at = ?
                WHERE current_stage = ?
                  AND status IN (?, ?)
                  AND next_run_at > ?
                  AND TRIM(claimed_by) = ''
                  AND json_valid(metadata_json)
                  AND COALESCE(json_extract(metadata_json, '$.share_review_status'), '') IN ('pending', 'unknown')
                """,
                (
                    current_time,
                    current_time,
                    TaskStage.CLEANED.value,
                    TaskStatus.PENDING.value,
                    TaskStatus.RUNNING.value,
                    current_time,
                ),
            )
        return int(cursor.rowcount or 0)

    def set_task_strm_mode(self, task_id: int, mode: str) -> TaskSnapshot:
        normalized = normalize_strm_mode(mode)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                raise KeyError(f"task not found: {task_id}")
            if row["source_type"] == "cloud_download" and normalized != "shared":
                raise ValueError("云任务只支持 shared STRM 模式")
            if is_strm_mode_locked(row["current_stage"]):
                raise RuntimeError("STRM 模式已锁定，不能修改")
            merged_metadata = self._merge_metadata(row["metadata_json"], {"strm_mode": normalized})
            conn.execute(
                "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (merged_metadata, time.time(), int(task_id)),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        return self._snapshot(updated)

    def claim_quality_run(self, run_date: str, now: float) -> bool:
        state_key = f"quality_auto_run:{run_date}"
        timestamp = float(now)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT 1 FROM runtime_state WHERE key = ?", (state_key,)).fetchone()
            if existing is not None:
                return False
            conn.execute(
                "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
                (state_key, str(run_date), timestamp),
            )
            self._prune_quality_run_keys(conn, timestamp)
            return True

    @staticmethod
    def _prune_quality_run_keys(conn: sqlite3.Connection, now: float) -> None:
        """Drop daily quality run claims older than a week.

        Each daily run adds one permanent key; without pruning the runtime_state
        table grows one row per day forever.
        """
        conn.execute(
            "DELETE FROM runtime_state WHERE key LIKE 'quality_auto_run:%' AND updated_at < ?",
            (now - 7 * 24 * 3600,),
        )

    def claim_quality_run_execution(
        self,
        run_id: str,
        now: float,
        *,
        run_date: str | None = None,
        stale_after_seconds: int = 21600,
    ) -> bool:
        """Atomically acquire the quality runtime lease and optional local-date claim."""
        timestamp = float(now)
        stale_before = timestamp - max(1, int(stale_after_seconds))
        current_run_key = "quality_auto_current_run_id"
        current_date_key = "quality_auto_current_run_date"
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            status_row = conn.execute(
                "SELECT value, updated_at FROM runtime_state WHERE key = ?",
                ("quality_auto_status",),
            ).fetchone()
            status = str(status_row["value"] or "").strip().lower() if status_row else ""
            running_is_stale = bool(
                status_row
                and status == "running"
                and float(status_row["updated_at"]) <= stale_before
            )
            current_run_row = conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                (current_run_key,),
            ).fetchone()
            current_run_id = str(current_run_row["value"] or "").strip() if current_run_row else ""
            if status == "running" and current_run_id == str(run_id):
                return False
            if status == "running" and not running_is_stale:
                return False

            current_date_row = conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                (current_date_key,),
            ).fetchone()
            current_date = str(current_date_row["value"] or "").strip() if current_date_row else ""
            claimed_date = current_date
            if not claimed_date and current_run_id.startswith("quality-"):
                run_id_parts = current_run_id.removeprefix("quality-").split("-")
                if len(run_id_parts) >= 3:
                    claimed_date = "-".join(run_id_parts[:3])
            if run_date:
                date_key = f"quality_auto_run:{run_date}"
                date_row = conn.execute(
                    "SELECT updated_at FROM runtime_state WHERE key = ?",
                    (date_key,),
                ).fetchone()
                if date_row is not None and not (running_is_stale and claimed_date == str(run_date)):
                    return False
                conn.execute(
                    """
                    INSERT INTO runtime_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (date_key, str(run_date), timestamp),
                )
                self._prune_quality_run_keys(conn, timestamp)

            target_date = str(run_date) if run_date is not None else ""
            runtime_values = [
                ("quality_auto_status", "running"),
                (current_run_key, str(run_id)),
                (current_date_key, target_date),
            ]
            for key, value in runtime_values:
                conn.execute(
                    """
                    INSERT INTO runtime_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, timestamp),
                )
            return True

    def update_quality_run_state_if_owner(
        self,
        run_id: str,
        status: str,
        summary_json: str,
        updated_at: float,
    ) -> bool:
        """Persist a quality summary only while run_id owns the current runtime lease."""
        timestamp = float(updated_at)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                ("quality_auto_current_run_id",),
            ).fetchone()
            if owner is None or str(owner["value"] or "") != str(run_id):
                return False
            values = [
                ("quality_auto_status", str(status)),
                ("quality_auto_last_summary", str(summary_json)),
            ]
            if str(status) != "running":
                values.extend(
                    [
                        ("quality_auto_current_run_id", ""),
                        ("quality_auto_current_run_date", ""),
                    ]
                )
            for key, value in values:
                conn.execute(
                    """
                    INSERT INTO runtime_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, timestamp),
                )
            return True

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

    def upsert_task(
        self,
        share_code: str,
        receive_code: str,
        url: str,
        chat_id: str = "",
        strm_mode: str | None = None,
    ) -> TaskSnapshot:
        now = time.time()
        explicit_mode = strm_mode is not None
        effective_mode = normalize_strm_mode(strm_mode) if explicit_mode else self.get_default_strm_mode()
        initial_metadata = json.dumps({"strm_mode": effective_mode}, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    share_code, receive_code, source_type, source_key, url, chat_id,
                    current_stage, status, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, 'share', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_code, receive_code) DO UPDATE SET
                    url = CASE WHEN tasks.claimed_by = '' THEN excluded.url ELSE tasks.url END,
                    chat_id = CASE
                        WHEN tasks.claimed_by = '' THEN COALESCE(NULLIF(excluded.chat_id, ''), tasks.chat_id)
                        ELSE tasks.chat_id
                    END,
                    updated_at = CASE WHEN tasks.claimed_by = '' THEN excluded.updated_at ELSE tasks.updated_at END
                """,
                (
                    share_code,
                    receive_code,
                    f"share:{share_code}:{receive_code}",
                    url,
                    chat_id,
                    TaskStage.RECEIVED.value,
                    TaskStatus.PENDING.value,
                    initial_metadata,
                    now,
                    now,
                ),
            )
            if explicit_mode:
                current = conn.execute(
                    "SELECT metadata_json FROM tasks WHERE share_code = ? AND receive_code = ?",
                    (share_code, receive_code),
                ).fetchone()
                try:
                    metadata = json.loads(current["metadata_json"] or "{}") if current else {}
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict) or "strm_mode" not in metadata:
                    merged_metadata = self._merge_metadata(
                        current["metadata_json"] if current else "{}",
                        {"strm_mode": effective_mode},
                    )
                    conn.execute(
                        """
                        UPDATE tasks SET metadata_json = ?
                        WHERE share_code = ? AND receive_code = ? AND claimed_by = ''
                        """,
                        (merged_metadata, share_code, receive_code),
                    )
            row = conn.execute(
                "SELECT * FROM tasks WHERE share_code = ? AND receive_code = ?",
                (share_code, receive_code),
            ).fetchone()
        return self._snapshot(row)

    def get_or_create_share_task(
        self,
        share_code: str,
        receive_code: str,
        url: str,
        chat_id: str = "",
    ) -> TaskSnapshot:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            default_mode_row = conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                (STRM_DEFAULT_MODE_KEY,),
            ).fetchone()
            effective_mode = normalize_strm_mode(
                default_mode_row["value"] if default_mode_row else self.default_strm_mode
            )
            now = time.time()
            conn.execute(
                """
                INSERT INTO tasks (
                    share_code, receive_code, source_type, source_key, url, chat_id,
                    current_stage, status, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, 'share', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_code, receive_code) DO NOTHING
                """,
                (
                    share_code,
                    receive_code,
                    f"share:{share_code}:{receive_code}",
                    url,
                    chat_id,
                    TaskStage.RECEIVED.value,
                    TaskStatus.PENDING.value,
                    json.dumps({"strm_mode": effective_mode}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE share_code = ? AND receive_code = ?",
                (share_code, receive_code),
            ).fetchone()
        return self._snapshot(row)

    def upsert_cloud_task(
        self,
        source_key: str,
        url: str,
        chat_id: str = "",
        title: str = "",
    ) -> TaskSnapshot:
        now = time.time()
        source_key = str(source_key).strip()
        if not source_key:
            raise ValueError("cloud source key is empty")
        internal_share_code = f"cloud:{source_key}"
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    share_code, receive_code, source_type, source_key, url, title, chat_id,
                    current_stage, status, metadata_json, created_at, updated_at
                )
                VALUES (?, '', 'cloud_download', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_key) DO UPDATE SET
                    url = CASE WHEN tasks.claimed_by = '' THEN excluded.url ELSE tasks.url END,
                    title = CASE
                        WHEN tasks.claimed_by = '' THEN COALESCE(NULLIF(excluded.title, ''), tasks.title)
                        ELSE tasks.title
                    END,
                    chat_id = CASE
                        WHEN tasks.claimed_by = '' THEN COALESCE(NULLIF(excluded.chat_id, ''), tasks.chat_id)
                        ELSE tasks.chat_id
                    END,
                    updated_at = CASE WHEN tasks.claimed_by = '' THEN excluded.updated_at ELSE tasks.updated_at END
                """,
                (
                    internal_share_code,
                    source_key,
                    url,
                    title,
                    chat_id,
                    TaskStage.CLOUD_DOWNLOADING.value,
                    TaskStatus.PENDING.value,
                    json.dumps({"strm_mode": "shared"}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE source_type = ? AND source_key = ?",
                ("cloud_download", source_key),
            ).fetchone()
            if row is not None and not str(row["claimed_by"] or "").strip():
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                if "strm_mode" not in metadata:
                    merged_metadata = self._merge_metadata(row["metadata_json"], {"strm_mode": "shared"})
                    conn.execute(
                        "UPDATE tasks SET metadata_json = ? WHERE id = ?",
                        (merged_metadata, int(row["id"])),
                    )
                    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(row["id"]),)).fetchone()
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

    def list_tasks_by_own_share_file_id(
        self,
        file_id: str,
        *,
        exclude_task_id: int | None = None,
    ) -> list[TaskSnapshot]:
        normalized = str(file_id or "").strip()
        if not normalized:
            return []
        params: list[Any] = [normalized]
        exclude_clause = ""
        if exclude_task_id is not None:
            exclude_clause = " AND id <> ?"
            params.append(int(exclude_task_id))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE json_valid(metadata_json)
                  AND CAST(json_extract(metadata_json, '$.own_share_file_id') AS TEXT) = ?
                  {exclude_clause}
                ORDER BY updated_at DESC, id DESC
                """,
                params,
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def find_task_by_source(self, source_type: str, source_key: str) -> TaskSnapshot | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE source_type = ? AND source_key = ?",
                (str(source_type), str(source_key)),
            ).fetchone()
        return self._snapshot(row) if row else None

    def list_live_share_codes(self) -> set[str]:
        """Own share codes still referenced by an alive task.

        A code is "live" when some task has it as own_share_code and the share
        is not confirmed dead (share_validation_status not invalid, no
        invalid_share_status/invalid_share_cleaned/source_deleted marker, task
        not quality-archived). Used by stale-STRM cleanup to avoid deleting
        files that another task may still serve to Emby.
        """
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT json_extract(metadata_json, '$.own_share_code') AS code
                FROM tasks
                WHERE json_valid(metadata_json)
                  AND COALESCE(json_extract(metadata_json, '$.own_share_code'), '') <> ''
                  AND COALESCE(json_extract(metadata_json, '$.share_validation_status'), '')
                      NOT IN ('invalid', 'invalid_share_cleaned')
                  AND COALESCE(json_extract(metadata_json, '$.invalid_share_status'), '')
                      NOT IN ('invalid', 'invalid_share_cleaned')
                  AND COALESCE(json_extract(metadata_json, '$.source_deleted'), '')
                      NOT IN ('1', 'true', 'yes', 'on', 'enabled')
                  AND COALESCE(json_extract(metadata_json, '$.quality_archived_at'), 0) <= 0
                """
            ).fetchall()
        return {str(row["code"]).strip() for row in rows if str(row["code"] or "").strip()}

    def flag_claim_lost(
        self,
        task_id: int,
        expected_claimed_by: str,
        expected_claim_token: str,
        *,
        now: float | None = None,
    ) -> TaskSnapshot | None:
        """Mark a RUNNING task as needs-human-action when its claim is dying.

        Called by the heartbeat when claim renewal keeps failing: the stage may
        have already performed external side effects, so the task must NOT be
        auto-reclaimed and re-run (that would replay 115/CMS operations). The
        transition only applies when the claim still belongs to this worker;
        otherwise None is returned and the other owner is left untouched.
        """
        current_time = time.time() if now is None else float(now)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return None
            if (
                row["status"] != TaskStatus.RUNNING.value
                or str(row["claimed_by"] or "") != str(expected_claimed_by)
                or str(row["claim_token"] or "") != str(expected_claim_token)
            ):
                return None
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["claim_renewal_failed_at"] = current_time
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, current_stage = ?, claimed_by = '', claim_token = '',
                    claimed_at = 0, claim_heartbeat_at = 0, next_run_at = -1,
                    error_type = 'claim_renewal_failed',
                    error_summary = '任务续租失败，阶段可能已执行但结果未提交；请确认远端状态后手动重试',
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    TaskStatus.NEEDS_ACTION.value,
                    TaskStage.NEEDS_ACTION.value,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    current_time,
                    int(task_id),
                ),
            )
            conn.execute(
                """
                INSERT INTO task_events (task_id, stage, status, message, error_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(task_id),
                    TaskStage.NEEDS_ACTION.value,
                    TaskStatus.NEEDS_ACTION.value,
                    "任务续租失败：阶段可能已执行但结果未提交，已停止自动重跑，请确认后手动处理",
                    "claim_renewal_failed",
                    current_time,
                ),
            )
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        return self._snapshot(updated)

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
            runner_heartbeat_row = conn.execute(
                "SELECT value, updated_at FROM runtime_state WHERE key = ?",
                ("task_runner",),
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
            runner_heartbeat_at=float(runner_heartbeat_row["updated_at"] or 0) if runner_heartbeat_row else 0.0,
            runner_state=str(runner_heartbeat_row["value"] or "") if runner_heartbeat_row else "",
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
            claim_heartbeat_at = task.claim_heartbeat_at or task.claimed_at
            if claim_heartbeat_at <= stale_before or task.metadata.get("_lock_waiting"):
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

    def patch_claimed_metadata(
        self,
        task_id: int,
        expected_claimed_by: str,
        expected_claimed_at: float,
        expected_claim_token: str,
        expected_updated_at: float,
        patch: dict[str, Any],
    ) -> TaskSnapshot | None:
        """Merge metadata without changing task version while the worker claim is current."""
        worker_id = str(expected_claimed_by or "").strip()
        if not worker_id:
            return None
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if current is None or not self._claim_matches(
                current,
                TaskStage(str(current["current_stage"])),
                worker_id,
                float(expected_claimed_at),
                str(expected_claim_token),
                float(expected_updated_at),
            ):
                return None
            merged_metadata = self._merge_metadata(current["metadata_json"], patch)
            conn.execute(
                "UPDATE tasks SET metadata_json = ? WHERE id = ?",
                (merged_metadata, int(task_id)),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        return self._snapshot(row) if row else None

    def claim_task_lock(
        self,
        task_id: int,
        lock_metadata: dict[str, Any],
        conflicts_with_holder: Callable[[TaskSnapshot], bool],
        *,
        expected_stage: TaskStage,
        expected_claimed_by: str,
        expected_claimed_at: float,
        expected_claim_token: str,
        expected_updated_at: float,
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
                  AND COALESCE(NULLIF(claim_heartbeat_at, 0), claimed_at) > ?
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
            if not self._claim_matches(
                current,
                expected_stage,
                expected_claimed_by,
                expected_claimed_at,
                expected_claim_token,
                expected_updated_at,
            ):
                return TaskLockClaimResult(stale=True)
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
                SET status = ?, metadata_json = ?, next_run_at = ?, claimed_by = '', claimed_at = 0,
                    claim_token = '', claim_heartbeat_at = 0, updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.RUNNING.value, merged_metadata, float(next_run_at), current_time, task_id),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return TaskLockClaimResult(task=self._snapshot(row) if row else None, holder=holder)

    def list_events(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def quality_state(self, task_id: int, *, now: float | None = None) -> dict[str, Any]:
        """Return normalized quality metadata without writing legacy tasks."""
        task = self.find_task(int(task_id))
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        metadata = task.metadata
        state = dict(QUALITY_STATE_DEFAULTS)
        state["quality_issue_codes"] = []
        for key in QUALITY_STATE_DEFAULTS:
            if key in metadata and metadata[key] is not None:
                state[key] = metadata[key]

        state["quality_repair_attempts"] = quality_attempt_count(task)
        if "quality_last_attempt_at" not in metadata and metadata.get("quality_repair_started_at") is not None:
            try:
                state["quality_last_attempt_at"] = float(metadata["quality_repair_started_at"] or 0)
            except (TypeError, ValueError):
                pass
        issue_codes = state.get("quality_issue_codes")
        if isinstance(issue_codes, str):
            try:
                parsed = json.loads(issue_codes)
            except (TypeError, ValueError):
                parsed = [part.strip() for part in issue_codes.split(",") if part.strip()]
            issue_codes = parsed
        state["quality_issue_codes"] = list(issue_codes) if isinstance(issue_codes, (list, tuple)) else []
        state["quality_repair_queued"] = _quality_bool(metadata.get("quality_repair_queued"))
        for key in ("quality_repair_started_at", "quality_repair_deadline_at", "quality_snoozed_until"):
            try:
                state[key] = float(metadata.get(key) or 0)
            except (TypeError, ValueError):
                state[key] = 0
        raw_next_eligible = metadata.get("quality_next_eligible_at")
        invalid_cooldown = False
        if raw_next_eligible is None or (
            isinstance(raw_next_eligible, str) and not raw_next_eligible.strip()
        ):
            state["quality_next_eligible_at"] = 0
        else:
            try:
                if isinstance(raw_next_eligible, bool):
                    raise ValueError
                state["quality_next_eligible_at"] = float(raw_next_eligible)
                if not math.isfinite(state["quality_next_eligible_at"]):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                state["quality_next_eligible_at"] = 0
                invalid_cooldown = True
        if invalid_cooldown:
            state["quality_manual_status"] = "manual_required"
            state["quality_rule_reason"] = "invalid_cooldown"
        current_time = time.time() if now is None else float(now)
        if (
            str(state.get("quality_manual_status") or "").strip().lower() == "snoozed"
            and state["quality_snoozed_until"] <= current_time
        ):
            state["quality_manual_status"] = "open"
        return state

    @staticmethod
    def _quality_patch(patch: dict[str, Any] | None) -> dict[str, Any]:
        values = {str(key): value for key, value in (patch or {}).items()}
        invalid = [key for key in values if not key.startswith("quality_")]
        if invalid:
            raise ValueError(f"quality state keys must use quality_ prefix: {', '.join(sorted(invalid))}")
        return values

    def update_quality_state(
        self,
        task_id: int,
        expected_updated_at: float,
        patch: dict[str, Any],
        message: str,
        actor: str,
        *,
        metadata_delete_keys: tuple[str, ...] = (),
        rule_id: str | None = None,
        action: str | None = None,
    ) -> TaskSnapshot | None:
        """CAS-update quality metadata and append one explainable task event."""
        values = self._quality_patch(patch)
        delete_keys = tuple(str(key) for key in metadata_delete_keys)
        invalid_delete_keys = [key for key in delete_keys if not key.startswith("quality_")]
        if invalid_delete_keys:
            raise ValueError(
                "quality state delete keys must use quality_ prefix: "
                + ", ".join(sorted(invalid_delete_keys))
            )
        values["quality_last_actor"] = str(actor or "")
        if rule_id is not None:
            values["quality_rule_id"] = str(rule_id or "")
        if action is not None:
            values["quality_repair_action"] = str(action or "")
        current = self.find_task(int(task_id))
        if current is None:
            raise KeyError(f"task not found: {task_id}")
        context = []
        if rule_id is not None:
            context.append(f"rule={str(rule_id or '')}")
        if action is not None:
            context.append(f"action={str(action or '')}")
        context.append(f"actor={str(actor or '')}")
        event_message = f"{message}（{'; '.join(context)}）"
        return self.record_event(
            int(task_id),
            current.current_stage,
            current.status,
            event_message,
            metadata_patch=values,
            metadata_delete_keys=delete_keys,
            expected_updated_at=float(expected_updated_at),
        )

    def mark_quality_snoozed(
        self,
        task_id: int,
        until: float,
        actor: str,
        *,
        rule_id: str | None = None,
        expected_updated_at: float | None = None,
    ) -> TaskSnapshot | None:
        task = self.find_task(int(task_id))
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        timestamp = float(until)
        return self.update_quality_state(
            task.id,
            task.updated_at if expected_updated_at is None else float(expected_updated_at),
            {
                "quality_manual_status": "snoozed",
                "quality_next_eligible_at": timestamp,
                "quality_snoozed_until": timestamp,
            },
            "质量问题已暂缓",
            actor,
            rule_id=rule_id,
            action="snooze" if rule_id is not None else None,
        )

    def mark_quality_ignored(
        self,
        task_id: int,
        actor: str,
        *,
        rule_id: str | None = None,
        expected_updated_at: float | None = None,
    ) -> TaskSnapshot | None:
        task = self.find_task(int(task_id))
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return self.update_quality_state(
            task.id,
            task.updated_at if expected_updated_at is None else float(expected_updated_at),
            {"quality_manual_status": "ignored"},
            "质量问题已忽略",
            actor,
            rule_id=rule_id,
            action="ignore" if rule_id is not None else None,
        )

    def resume_quality(
        self,
        task_id: int,
        actor: str,
        *,
        rule_id: str | None = None,
        expected_updated_at: float | None = None,
    ) -> TaskSnapshot | None:
        task = self.find_task(int(task_id))
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return self.update_quality_state(
            task.id,
            task.updated_at if expected_updated_at is None else float(expected_updated_at),
            {
                "quality_manual_status": "open",
                "quality_repair_attempts": 0,
                "quality_next_eligible_at": 0,
                "quality_snoozed_until": 0,
                "quality_rule_id": str(rule_id or "") if rule_id is not None else "",
                "quality_rule_reason": "",
                "quality_rule_risk_level": "",
                "quality_issue_codes": [],
                "quality_rule_version": "",
                "quality_repair_queued": False,
                "quality_repair_started_at": 0,
                "quality_repair_deadline_at": 0,
            },
            "质量问题已恢复自动评估",
            actor,
            metadata_delete_keys=(
                (
                    "quality_repair_action",
                    "quality_repair_reason",
                    "quality_run_id",
                    "quality_last_run_id",
                    "quality_last_attempt_at",
                    "quality_archived_at",
                    "quality_archived_reason",
                )
                if rule_id is None
                else (
                "quality_repair_reason",
                "quality_run_id",
                "quality_last_run_id",
                "quality_last_attempt_at",
                "quality_archived_at",
                "quality_archived_reason",
                )
            ),
            rule_id=rule_id,
            action="resume" if rule_id is not None else None,
        )

    def clear_finished_tasks(self) -> int:
        terminal_statuses = (
            TaskStatus.SUCCEEDED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        )
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN (?, ?, ?) AND claimed_by = ''",
                terminal_statuses,
            ).fetchall()
            task_ids = [int(row["id"]) for row in rows]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM task_events WHERE task_id IN ({placeholders})", task_ids)
            conn.execute(f"DELETE FROM task_operations WHERE task_id IN ({placeholders})", task_ids)
            cursor = conn.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders}) AND claimed_by = ''",
                task_ids,
            )
        return int(cursor.rowcount or 0)

    def delete_finished_task(self, task_id: int, *, expected_updated_at: float) -> bool:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return False
            if str(row["claimed_by"] or ""):
                return False
            if row["status"] not in _DELETABLE_TASK_STATUSES:
                return False
            if float(row["updated_at"] or 0) != float(expected_updated_at):
                return False
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (int(task_id),))
            conn.execute("DELETE FROM task_operations WHERE task_id = ?", (int(task_id),))
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
        return int(cursor.rowcount or 0) == 1

    def request_task_termination(
        self,
        task_id: int,
        actor: str,
        now: float | None = None,
    ) -> TaskSnapshot | None:
        current_time = time.time() if now is None else float(now)
        actor = str(actor)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return None
            if row["status"] == TaskStatus.CANCELLED.value:
                return self._snapshot(row)
            if row["status"] in _DELETABLE_TASK_STATUSES:
                return None
            if str(row["claimed_by"] or ""):
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                if TERMINATION_REQUESTED_AT_KEY not in metadata:
                    metadata = self._merge_metadata(
                        row["metadata_json"],
                        {
                            TERMINATION_REQUESTED_AT_KEY: current_time,
                            TERMINATION_REQUESTED_BY_KEY: actor,
                        },
                    )
                    conn.execute(
                        """
                        INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
                        VALUES (?, ?, ?, ?, '', '', ?)
                        """,
                        (
                            int(task_id),
                            row["current_stage"],
                            row["status"],
                            f"{actor} 已请求终止，等待当前阶段结束",
                            current_time,
                        ),
                    )
                    conn.execute(
                        "UPDATE tasks SET metadata_json = ? WHERE id = ?",
                        (metadata, int(task_id)),
                    )
                result = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
                return self._snapshot(result)

            metadata = self._merge_metadata(
                row["metadata_json"],
                None,
                delete_keys=_TERMINATION_METADATA_DELETE_KEYS,
            )
            conn.execute(
                """
                INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
                VALUES (?, ?, ?, ?, '', '', ?)
                """,
                (
                    int(task_id),
                    row["current_stage"],
                    TaskStatus.CANCELLED.value,
                    f"{actor} 已终止任务",
                    current_time,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, metadata_json = ?, next_run_at = -1,
                    claimed_by = '', claimed_at = 0, claim_token = '', claim_heartbeat_at = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.CANCELLED.value, metadata, current_time, int(task_id)),
            )
            result = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        return self._snapshot(result)

    def settle_requested_termination(
        self,
        task_id: int,
        expected_claimed_by: str,
        expected_claim_token: str,
        *,
        error_type: str = "",
        error_summary: str = "",
        error_detail: str = "",
        now: float | None = None,
    ) -> TaskSnapshot | None:
        current_time = time.time() if now is None else float(now)
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return None
            if (
                row["status"] != TaskStatus.RUNNING.value
                or str(row["claimed_by"] or "") != str(expected_claimed_by)
                or str(row["claim_token"] or "") != str(expected_claim_token)
                or not self._termination_requested(row)
            ):
                return None
            result = self._settle_requested_termination_in_transaction(
                conn,
                row,
                error_type=error_type,
                error_summary=error_summary,
                error_detail=error_detail,
                now=current_time,
            )
        return self._snapshot(result)

    @staticmethod
    def _termination_requested(row: sqlite3.Row) -> bool:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            requested_at = float(
                metadata.get(TERMINATION_REQUESTED_AT_KEY, 0) if isinstance(metadata, dict) else 0
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return requested_at > 0

    def _settle_requested_termination_in_transaction(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        error_type: str = "",
        error_summary: str = "",
        error_detail: str = "",
        now: float,
    ) -> sqlite3.Row:
        metadata = self._merge_metadata(
            row["metadata_json"],
            None,
            delete_keys=_TERMINATION_METADATA_DELETE_KEYS,
        )
        final_error_type = str(error_type) if error_type else str(row["error_type"] or "")
        final_error_summary = str(error_summary) if error_summary else str(row["error_summary"] or "")
        conn.execute(
            """
            INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                row["current_stage"],
                TaskStatus.CANCELLED.value,
                "已终止任务",
                final_error_type,
                str(error_detail),
                now,
            ),
        )
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, error_type = ?, error_summary = ?, metadata_json = ?, next_run_at = -1,
                claimed_by = '', claimed_at = 0, claim_token = '', claim_heartbeat_at = 0,
                updated_at = ?
            WHERE id = ?
            """,
            (
                TaskStatus.CANCELLED.value,
                final_error_type,
                final_error_summary,
                metadata,
                now,
                int(row["id"]),
            ),
        )
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (int(row["id"]),)).fetchone()

    def clear_worker_claims(self, worker_id: str, now: float | None = None) -> int:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            return 0
        current_time = time.time() if now is None else float(now)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET claimed_by = '', claimed_at = 0, claim_token = '', claim_heartbeat_at = 0,
                    next_run_at = ?, updated_at = ?
                WHERE claimed_by = ?
                  AND status = ?
                """,
                (current_time, current_time, worker_id, TaskStatus.RUNNING.value),
            )
        return int(cursor.rowcount or 0)

    @staticmethod
    def _claim_matches(
        row: sqlite3.Row,
        expected_stage: TaskStage,
        expected_claimed_by: str,
        expected_claimed_at: float,
        expected_claim_token: str,
        expected_updated_at: float,
    ) -> bool:
        return (
            row["current_stage"] == expected_stage.value
            and row["status"] == TaskStatus.RUNNING.value
            and str(row["claimed_by"] or "") == str(expected_claimed_by)
            and float(row["claimed_at"] or 0) == float(expected_claimed_at)
            and str(row["claim_token"] or "") == str(expected_claim_token)
            and float(row["updated_at"] or 0) == float(expected_updated_at)
        )

    def complete_claimed_stage(
        self,
        task_id: int,
        *,
        expected_stage: TaskStage,
        expected_claimed_by: str,
        expected_claimed_at: float,
        expected_claim_token: str,
        expected_updated_at: float,
        success_message: str,
        success_metadata: dict[str, Any],
        next_stage: TaskStage | None,
        next_run_at: float,
        metadata_delete_keys: tuple[str, ...] = (),
    ) -> TaskSnapshot | None:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None or not self._claim_matches(
                row,
                expected_stage,
                expected_claimed_by,
                expected_claimed_at,
                expected_claim_token,
                expected_updated_at,
            ):
                return None
            now = time.time()
            if self._termination_requested(row):
                result = self._settle_requested_termination_in_transaction(conn, row, now=now)
                return self._snapshot(result)
            metadata = self._merge_metadata(row["metadata_json"], success_metadata, metadata_delete_keys)
            conn.execute(
                "INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at) "
                "VALUES (?, ?, ?, ?, '', '', ?)",
                (task_id, expected_stage.value, TaskStatus.SUCCEEDED.value, success_message, now),
            )
            target_stage = next_stage or expected_stage
            target_status = TaskStatus.PENDING if next_stage else TaskStatus.SUCCEEDED
            if next_stage:
                conn.execute(
                    "INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at) "
                    "VALUES (?, ?, ?, '等待执行', '', '', ?)",
                    (task_id, next_stage.value, TaskStatus.PENDING.value, now),
                )
            conn.execute(
                "UPDATE tasks SET current_stage = ?, status = ?, error_type = '', error_summary = '', "
                "metadata_json = ?, next_run_at = ?, claimed_by = '', claimed_at = 0, claim_token = '', "
                "claim_heartbeat_at = 0, updated_at = ? WHERE id = ?",
                (target_stage.value, target_status.value, metadata, next_run_at, now, task_id),
            )
            result = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(result) if result else None

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
        deduplicate: bool = False,
        submission_id: int | None = None,
        metadata_patch: dict[str, Any] | None = None,
        metadata_delete_keys: tuple[str, ...] | None = None,
        next_run_at: float | None = None,
        clear_claim: bool = False,
        expected_stage: TaskStage | None = None,
        expected_status: TaskStatus | None = None,
        expected_claimed_by: str | None = None,
        expected_claimed_at: float | None = None,
        expected_claim_token: str | None = None,
        expected_updated_at: float | None = None,
    ) -> TaskSnapshot | None:
        now = time.time()
        with self._lock, self._connection() as conn:
            # Acquire the write lock before reading metadata so concurrent patches do not lose updates.
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if current is None:
                if any(value is not None for value in (expected_stage, expected_status, expected_claimed_by, expected_claimed_at, expected_claim_token, expected_updated_at)):
                    return None
                raise KeyError(f"task not found: {task_id}")
            claim_expectations = (
                expected_stage,
                expected_claimed_by,
                expected_claimed_at,
                expected_claim_token,
                expected_updated_at,
            )
            has_claim_expectation = any(
                value is not None for value in (expected_claimed_by, expected_claimed_at, expected_claim_token)
            )
            if has_claim_expectation:
                if expected_status != TaskStatus.RUNNING or not all(value is not None for value in claim_expectations):
                    return None
                if not self._claim_matches(
                    current,
                    expected_stage,
                    expected_claimed_by,
                    expected_claimed_at,
                    expected_claim_token,
                    expected_updated_at,
                ):
                    return None
                if self._termination_requested(current):
                    result = self._settle_requested_termination_in_transaction(
                        conn,
                        current,
                        error_type=error_type,
                        error_summary=error_summary,
                        error_detail=error_detail,
                        now=now,
                    )
                    return self._snapshot(result)
            else:
                if expected_stage is not None and current["current_stage"] != expected_stage.value:
                    return None
                if expected_status is not None and current["status"] != expected_status.value:
                    return None
                if expected_claimed_by is not None and str(current["claimed_by"] or "") != str(expected_claimed_by):
                    return None
                if expected_claimed_at is not None and float(current["claimed_at"] or 0) != float(expected_claimed_at):
                    return None
                if expected_claim_token is not None and str(current["claim_token"] or "") != str(expected_claim_token):
                    return None
                if expected_updated_at is not None and float(current["updated_at"] or 0) != float(expected_updated_at):
                    return None
            merged_metadata = self._merge_metadata(
                current["metadata_json"],
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
            duplicate_event = bool(
                deduplicate
                and last_event
                and last_event["stage"] == stage.value
                and last_event["status"] == status.value
                and last_event["message"] == message
                and last_event["error_type"] == error_type
                and last_event["error_detail"] == error_detail
                and str(current["error_summary"] or "") == str(error_summary or "")
            )
            if duplicate_event:
                return self._snapshot(current)
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
            has_any_expectation = any(
                value is not None
                for value in (
                    expected_stage,
                    expected_status,
                    expected_claimed_by,
                    expected_claimed_at,
                    expected_claim_token,
                    expected_updated_at,
                )
            )
            # A task that a worker currently claims and runs must not have its
            # stage/status/updated_at silently rewritten by an external status
            # sync (legacy CMS polling, submission events, web backfill). Such
            # a write invalidates the worker's claim CAS, the completed stage
            # result is discarded, and the stage is re-run after the stale
            # window — replaying external side effects (115 share creation,
            # CMS submission). Record the event, but leave claimed task fields
            # untouched. Callers with explicit expectations or clear_claim are
            # intentionally not protected (they are authoritative transitions).
            claim_protected = bool(
                not clear_claim
                and not has_any_expectation
                and str(current["claimed_by"] or "") != ""
                and str(current["status"] or "") == TaskStatus.RUNNING.value
            )
            if claim_protected:
                return self._snapshot(current)
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
                updates.append("claim_token = ''")
                updates.append("claim_heartbeat_at = 0")
            values.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(row) if row else None

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
        claim_by: str | None = None,
        expected_updated_at: float | None = None,
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
            if expected_updated_at is not None and float(current["updated_at"] or 0) != float(expected_updated_at):
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
                updates.append("claim_token = ''")
                updates.append("claim_heartbeat_at = 0")
            if claim_by is not None:
                updates.append("claimed_by = ?")
                values.extend([str(claim_by), now, str(uuid.uuid4()), now])
                updates.append("claimed_at = ?")
                updates.append("claim_token = ?")
                updates.append("claim_heartbeat_at = ?")
            values.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._snapshot(row) if row else None

    def claim_quality_cleanup(
        self,
        task_id: int,
        run_id: str,
        now: float | None = None,
        *,
        expected_updated_at: float | None = None,
        stale_after_seconds: int = 21600,
    ) -> TaskSnapshot | None:
        """Atomically reserve one cleanup attempt for a quality run."""
        current_time = time.time() if now is None else float(now)
        stale_before = current_time - max(1, int(stale_after_seconds))
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if row is None:
                return None
            claimed_by = str(row["claimed_by"] or "").strip()
            claim_heartbeat_at = float(row["claim_heartbeat_at"] or row["claimed_at"] or 0)
            if claimed_by and claim_heartbeat_at > stale_before:
                return None
            if expected_updated_at is not None and float(row["updated_at"] or 0) != float(expected_updated_at):
                return None
            metadata = self._merge_metadata(row["metadata_json"], {"quality_cleanup_run_id": str(run_id)})
            claimed_cursor = conn.execute(
                """
                UPDATE tasks
                SET metadata_json = ?, claimed_by = ?, claimed_at = ?, claim_token = ?,
                    claim_heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND (
                    claimed_by = '' OR COALESCE(NULLIF(claim_heartbeat_at, 0), claimed_at) <= ?
                )
                """,
                (
                    metadata,
                    f"quality-cleanup:{run_id}",
                    current_time,
                    str(uuid.uuid4()),
                    current_time,
                    current_time,
                    int(task_id),
                    stale_before,
                ),
            )
            # Use the statement's own rowcount instead of the connection-wide
            # total_changes, which also counts rows touched by triggers or by
            # the earlier SELECTs on some SQLite build/PRAGMA combinations.
            if claimed_cursor.rowcount < 1:
                return None
            claimed = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        return self._snapshot(claimed) if claimed else None

    def has_quality_success_event(self, task_id: int) -> bool:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT stage, status FROM task_events
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(task_id),),
            ).fetchone()
            if row is None:
                return False
            return (
                row["status"] == TaskStatus.SUCCEEDED.value
                and row["stage"] in {TaskStage.EMBY_CONFIRMED.value, TaskStage.CLEANED.value}
            )

    def finalize_quality_cleanup(
        self,
        task_id: int,
        run_id: str,
        *,
        expected_claimed_at: float,
        expected_claim_token: str,
        expected_updated_at: float,
    ) -> bool:
        """Persist an idempotent cleanup marker and release its lease."""
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT metadata_json, claimed_by, claimed_at, claim_token, updated_at FROM tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if row is None or str(row["claimed_by"] or "") != f"quality-cleanup:{run_id}":
                return False
            if float(row["claimed_at"] or 0) != float(expected_claimed_at):
                return False
            if str(row["claim_token"] or "") != str(expected_claim_token):
                return False
            if float(row["updated_at"] or 0) != float(expected_updated_at):
                return False
            metadata = self._merge_metadata(row["metadata_json"], {"quality_cleanup_completed": True})
            cursor = conn.execute(
                """
                UPDATE tasks
                SET metadata_json = ?, claimed_by = '', claimed_at = 0, claim_token = '',
                    claim_heartbeat_at = 0, updated_at = ?
                WHERE id = ? AND claimed_by = ? AND claim_token = ?
                """,
                (metadata, time.time(), int(task_id), f"quality-cleanup:{run_id}", str(expected_claim_token)),
            )
        return int(cursor.rowcount or 0) == 1

    def record_quality_cleanup_event(
        self,
        task_id: int,
        run_id: str,
        status: TaskStatus,
        message: str,
        *,
        metadata_patch: dict[str, Any] | None = None,
        error_type: str = "",
        error_summary: str = "",
        expected_claimed_at: float,
        expected_claim_token: str,
        expected_updated_at: float,
    ) -> bool:
        """Record cleanup completion only while the same cleanup run owns the lease."""
        now = time.time()
        owner = f"quality-cleanup:{run_id}"
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT current_stage, metadata_json, claimed_at, claim_token, updated_at "
                "FROM tasks WHERE id = ? AND claimed_by = ? AND claim_token = ?",
                (int(task_id), owner, str(expected_claim_token)),
            ).fetchone()
            if row is None:
                return False
            if float(row["claimed_at"] or 0) != float(expected_claimed_at):
                return False
            if float(row["updated_at"] or 0) != float(expected_updated_at):
                return False
            metadata = self._merge_metadata(row["metadata_json"], metadata_patch)
            conn.execute(
                """
                INSERT INTO task_events (task_id, stage, status, message, error_type, error_detail, created_at)
                VALUES (?, ?, ?, ?, ?, '', ?)
                """,
                (int(task_id), row["current_stage"], status.value, message, error_type, now),
            )
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, error_type = ?, error_summary = ?, metadata_json = ?,
                    claimed_by = '', claimed_at = 0, claim_token = '', claim_heartbeat_at = 0, updated_at = ?
                WHERE id = ? AND claimed_by = ? AND claim_token = ?
                """,
                (status.value, error_type, error_summary, metadata, now, int(task_id), owner, str(expected_claim_token)),
            )
        return int(cursor.rowcount or 0) == 1

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
        metadata_patch: dict[str, Any] | None = None,
    ) -> TaskSnapshot:
        task = self.find_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        target_stage = reprocess_stage_for(task)
        preserve_received_snapshot = any(
            operation.operation_type == "receive_share" and operation.status == "succeeded"
            for operation in self.list_operations(task.id)
        )
        return self.record_event(
            task_id,
            target_stage,
            TaskStatus.PENDING,
            message,
            metadata_patch=build_reprocess_metadata(task, metadata_patch),
            metadata_delete_keys=reprocess_delete_keys_for(
                task,
                preserve_received_snapshot=preserve_received_snapshot,
            ),
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
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                  AND current_stage NOT IN (?, ?)
                  AND next_run_at >= 0
                  AND next_run_at <= ?
                  AND (claimed_by = '' OR COALESCE(NULLIF(claim_heartbeat_at, 0), claimed_at) <= ?)
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
                claim_token = str(uuid.uuid4())
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, claimed_by = ?, claimed_at = ?, claim_token = ?,
                        claim_heartbeat_at = ?, updated_at = ?
                    WHERE id = ?
                      AND status IN (?, ?)
                      AND current_stage NOT IN (?, ?)
                      AND next_run_at >= 0
                      AND next_run_at <= ?
                      AND (claimed_by = '' OR COALESCE(NULLIF(claim_heartbeat_at, 0), claimed_at) <= ?)
                    """,
                    (
                        TaskStatus.RUNNING.value,
                        worker_id,
                        current_time,
                        claim_token,
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

    def renew_claim(
        self,
        task_id: int,
        expected_claimed_by: str,
        expected_claim_token: str,
        *,
        now: float | None = None,
    ) -> bool:
        current_time = time.time() if now is None else float(now)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET claim_heartbeat_at = ?
                WHERE id = ? AND claimed_by = ? AND claim_token = ?
                """,
                (
                    current_time,
                    int(task_id),
                    str(expected_claimed_by),
                    str(expected_claim_token),
                ),
            )
        return int(cursor.rowcount or 0) == 1
