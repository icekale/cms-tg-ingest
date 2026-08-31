from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .sqlite_utils import sqlite_connection, sqlite_foreign_key_check, sqlite_quick_check


SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    pass


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    compatible_from INTEGER NOT NULL,
    compatible_to INTEGER NOT NULL,
    migration_id TEXT NOT NULL DEFAULT '',
    migrated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    share_code TEXT NOT NULL DEFAULT '',
    receive_code TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    tmdb_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    submission_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'runtime',
    is_executable INTEGER NOT NULL DEFAULT 1,
    current_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    error_type TEXT NOT NULL DEFAULT '',
    error_summary TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_run_at REAL NOT NULL DEFAULT -1,
    claimed_by TEXT NOT NULL DEFAULT '',
    claimed_at REAL NOT NULL DEFAULT 0,
    claim_token TEXT NOT NULL DEFAULT '',
    claim_heartbeat_at REAL NOT NULL DEFAULT 0,
    archived_at REAL,
    archived_by TEXT NOT NULL DEFAULT '',
    archive_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(source_type, source_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_share_identity
    ON tasks(share_code, receive_code) WHERE source_type = 'share';
CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_next_run ON tasks(status, next_run_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(claimed_by, claimed_at);
CREATE INDEX IF NOT EXISTS idx_tasks_claim_heartbeat ON tasks(claimed_by, claim_heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_tasks_stage_status_next ON tasks(current_stage, status, next_run_at, id);

CREATE TABLE IF NOT EXISTS task_media (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    cms_task_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    tmdb_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    recognition_status TEXT NOT NULL DEFAULT '',
    recognition_json TEXT NOT NULL DEFAULT '{}',
    poster_path TEXT NOT NULL DEFAULT '',
    backdrop_path TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    genres_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS task_shares (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL DEFAULT '',
    folder_id TEXT NOT NULL DEFAULT '',
    own_share_code TEXT NOT NULL DEFAULT '',
    own_share_receive_code TEXT NOT NULL DEFAULT '',
    canonical_name TEXT NOT NULL DEFAULT '',
    alias_name TEXT NOT NULL DEFAULT '',
    canonical_manifest_json TEXT NOT NULL DEFAULT '{}',
    validation_status TEXT NOT NULL DEFAULT '',
    validation_error TEXT NOT NULL DEFAULT '',
    share_sync_status TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_moves (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL DEFAULT '',
    dest_path TEXT NOT NULL DEFAULT '',
    move_status TEXT NOT NULL DEFAULT '',
    move_error TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0,
    validated_dest_path TEXT NOT NULL DEFAULT '',
    validated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_emby (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT '',
    item_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    library TEXT NOT NULL DEFAULT '',
    refresh_requested_at REAL NOT NULL DEFAULT 0,
    confirmed_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_cleanups (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    attempted_at REAL NOT NULL DEFAULT 0,
    finished_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_probes (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    last_probe_at REAL NOT NULL DEFAULT 0,
    invalid_at REAL NOT NULL DEFAULT 0,
    invalid_reason TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT '',
    next_check_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    folder_id TEXT NOT NULL DEFAULT '',
    recognition_json TEXT NOT NULL DEFAULT '{}',
    share_json TEXT NOT NULL DEFAULT '{}',
    strm_json TEXT NOT NULL DEFAULT '{}',
    move_json TEXT NOT NULL DEFAULT '{}',
    emby_json TEXT NOT NULL DEFAULT '{}',
    cleanup_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(task_id, target_key)
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
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
    UNIQUE(task_id, operation_key)
);
CREATE TABLE IF NOT EXISTS task_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    task_id INTEGER REFERENCES tasks(id),
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    claim_token TEXT NOT NULL DEFAULT '',
    claimed_by TEXT NOT NULL DEFAULT '',
    claimed_at REAL NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runner_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    renew_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
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
);
CREATE TABLE IF NOT EXISTS parent_category_memory (
    parent_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
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
    episode_filter TEXT NOT NULL DEFAULT '',
    last_summary_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(chat_id, source_type, source_value)
);
CREATE TABLE IF NOT EXISTS hdhive_subscription_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES hdhive_subscriptions(id),
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
    UNIQUE(subscription_id, episode_key, resource_slug)
);
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
CREATE TABLE IF NOT EXISTS tmdb_details (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_submission_map (
    legacy_submission_id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
    imported_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_submission_archive (
    legacy_submission_id INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    imported_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_id TEXT NOT NULL UNIQUE,
    source_hashes_json TEXT NOT NULL DEFAULT '{}',
    source_counts_json TEXT NOT NULL DEFAULT '{}',
    destination_counts_json TEXT NOT NULL DEFAULT '{}',
    validation_json TEXT NOT NULL DEFAULT '{}',
    write_gate TEXT NOT NULL DEFAULT 'closed',
    runner_opened_at REAL NOT NULL DEFAULT 0,
    intake_opened_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_purge_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    identity_json TEXT NOT NULL DEFAULT '{}',
    purged_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 30_000):
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        timeout = max(1.0, self.busy_timeout_ms / 1000)
        if read_only:
            target = f"{self.path.expanduser().resolve().as_uri()}?mode=ro"
            uri = True
        else:
            target = self.path
            uri = False
        connection = sqlite3.connect(target, uri=uri, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        created = False
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "schema_meta" not in tables and "tasks" not in tables:
                connection.executescript(_SCHEMA_SQL)
                created = True
            if created or "schema_meta" in tables:
                existing = connection.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO schema_meta (id, version, compatible_from, compatible_to)
                        VALUES (1, ?, ?, ?)
                        """,
                        (SCHEMA_VERSION, SCHEMA_VERSION, SCHEMA_VERSION),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if created or self._has_schema_meta():
            self.verify()

    def _has_schema_meta(self) -> bool:
        connection = self.connect(read_only=self.path.is_file())
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            return "schema_meta" in tables
        finally:
            connection.close()

    def verify(self) -> None:
        sqlite_quick_check(self.path)
        sqlite_foreign_key_check(self.path)
        with sqlite_connection(
            f"{self.path.expanduser().resolve().as_uri()}?mode=ro",
            uri=True,
            read_only=True,
            row_factory=sqlite3.Row,
        ) as connection:
            row = connection.execute("SELECT version, compatible_from, compatible_to FROM schema_meta WHERE id = 1").fetchone()
        if row is None:
            raise SchemaVersionError("schema_meta is missing")
        version = int(row["version"])
        compatible_from = int(row["compatible_from"])
        compatible_to = int(row["compatible_to"])
        if version < compatible_from or version > compatible_to or SCHEMA_VERSION < compatible_from or SCHEMA_VERSION > compatible_to:
            raise SchemaVersionError(
                f"unsupported schema version {version}; binary supports {SCHEMA_VERSION}"
            )

    def write_gate(self) -> str:
        connection = self.connect(read_only=self.path.is_file())
        try:
            row = connection.execute(
                "SELECT write_gate FROM migration_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return "open"
        gate = str(row["write_gate"] or "")
        if gate not in {"closed", "runner_open", "open"}:
            raise RuntimeError(f"incompatible write gate: {gate}")
        return gate

    def set_write_gate(self, gate: str) -> None:
        order = {"closed": 0, "runner_open": 1, "open": 2}
        if gate not in order:
            raise ValueError(f"incompatible write gate: {gate}")
        now = time.time()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT id, write_gate FROM migration_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                if gate != "closed":
                    raise ValueError(f"write gate must start closed, got {gate}")
                connection.execute(
                    "INSERT INTO migration_runs (migration_id, write_gate, created_at) VALUES (?, ?, ?)",
                    ("runtime", gate, now),
                )
                return
            current = str(row["write_gate"] or "closed")
            if gate == current:
                return
            if order[gate] != order.get(current, 0) + 1:
                raise ValueError(f"cannot move write gate from {current} to {gate}")
            connection.execute(
                "UPDATE migration_runs SET write_gate = ? WHERE id = ?",
                (gate, int(row["id"])),
            )
