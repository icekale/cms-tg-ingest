from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import time

from .database import Database
from .sqlite_utils import sqlite_foreign_key_check, sqlite_quick_check


SYNTHETIC_EXECUTABLE = 0

KNOWN_SUBMISSION_COLUMNS = frozenset(
    {
        "id",
        "share_code",
        "receive_code",
        "url",
        "cms_task_id",
        "title",
        "status",
        "last_error",
        "created_at",
        "updated_at",
        "category_choice",
        "category_status",
        "recognition_json",
        "emby_status",
        "emby_item_id",
        "emby_title",
        "emby_path",
        "emby_parent",
        "source_path",
        "dest_path",
        "move_status",
        "move_error",
        "move_started_at",
        "move_finished_at",
        "category_final",
        "workflow_mode",
        "workflow_phase",
        "own_share_file_id",
        "own_share_file_name",
        "own_share_code",
        "own_share_receive_code",
        "own_share_url",
        "share_sync_status",
        "cleanup_status",
        "cleanup_file_id",
        "cleanup_error",
        "cleanup_finished_at",
        "share_probe_at",
        "share_invalid_at",
        "share_invalid_reason",
        "canonical_manifest_json",
        "share_alias_name",
        "share_alias_level",
        "share_validation_status",
        "share_validation_error",
    }
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationReport:
    source_hashes: dict[str, str]
    source_counts: dict[str, int]
    destination_counts: dict[str, int]
    matched_submissions: int
    synthetic_tasks: int
    unmapped_rows: int
    logical_checksums: dict[str, str]
    foreign_key_errors: tuple[str, ...]
    migration_id: str = ""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.expanduser().resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if table not in _table_names(connection):
        return []
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def _identity(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("share_code") or "").strip().lower(), str(row.get("receive_code") or "").strip())


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _task_links(task: dict[str, Any]) -> tuple[int | None, int | None]:
    typed = _int_or_none(task.get("submission_id"))
    try:
        metadata = json.loads(task.get("metadata_json") or "{}")
    except json.JSONDecodeError as exc:
        raise MigrationError(f"task {task.get('id')} metadata_json is invalid") from exc
    json_id = None
    if isinstance(metadata, dict):
        json_id = _int_or_none(metadata.get("submission_id"))
    return typed, json_id


def _copy_rows(destination: sqlite3.Connection, table: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    destination.executemany(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
        [tuple(row.get(column) for column in columns) for row in rows],
    )


def _set_sequence(destination: sqlite3.Connection, table: str) -> None:
    row = destination.execute(f"SELECT MAX(id) FROM {table}").fetchone()
    maximum = int(row[0] or 0)
    if maximum <= 0:
        return
    destination.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, maximum))


def _write_facts(destination: sqlite3.Connection, task_id: int, task: dict[str, Any], submission: dict[str, Any] | None) -> None:
    submission = submission or {}
    destination.execute(
        """
        INSERT OR REPLACE INTO task_media (
            task_id, cms_task_id, title, media_type, tmdb_id, category, recognition_status, recognition_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            str(submission.get("cms_task_id") or ""),
            str(submission.get("title") or task.get("title") or ""),
            "",
            str(task.get("tmdb_id") or ""),
            str(submission.get("category_final") or task.get("category") or ""),
            str(submission.get("category_status") or ""),
            str(submission.get("recognition_json") or "{}"),
        ),
    )
    if submission.get("own_share_code") or submission.get("own_share_file_id"):
        destination.execute(
            """
            INSERT OR REPLACE INTO task_shares (
                task_id, file_id, own_share_code, own_share_receive_code, canonical_name, alias_name,
                canonical_manifest_json, validation_status, validation_error, share_sync_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                str(submission.get("own_share_file_id") or ""),
                str(submission.get("own_share_code") or ""),
                str(submission.get("own_share_receive_code") or ""),
                str(submission.get("own_share_file_name") or ""),
                str(submission.get("share_alias_name") or ""),
                str(submission.get("canonical_manifest_json") or "{}"),
                str(submission.get("share_validation_status") or ""),
                str(submission.get("share_validation_error") or ""),
                str(submission.get("share_sync_status") or ""),
            ),
        )
    if submission.get("move_status") or submission.get("dest_path") or submission.get("source_path"):
        destination.execute(
            """
            INSERT OR REPLACE INTO task_moves (
                task_id, source_path, dest_path, move_status, move_error, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                str(submission.get("source_path") or ""),
                str(submission.get("dest_path") or ""),
                str(submission.get("move_status") or ""),
                str(submission.get("move_error") or ""),
                float(submission.get("move_started_at") or 0),
                float(submission.get("move_finished_at") or 0),
            ),
        )
    if submission.get("emby_status") or submission.get("emby_item_id"):
        destination.execute(
            """
            INSERT OR REPLACE INTO task_emby (task_id, status, item_id, title, path, library)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                str(submission.get("emby_status") or ""),
                str(submission.get("emby_item_id") or ""),
                str(submission.get("emby_title") or ""),
                str(submission.get("emby_path") or ""),
                str(submission.get("emby_parent") or ""),
            ),
        )
    if submission.get("cleanup_status") or submission.get("cleanup_file_id"):
        destination.execute(
            """
            INSERT OR REPLACE INTO task_cleanups (task_id, target_id, status, error, finished_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                str(submission.get("cleanup_file_id") or ""),
                str(submission.get("cleanup_status") or ""),
                str(submission.get("cleanup_error") or ""),
                float(submission.get("cleanup_finished_at") or 0),
            ),
        )
    if submission.get("share_probe_at") or submission.get("share_invalid_at"):
        destination.execute(
            """
            INSERT OR REPLACE INTO task_probes (task_id, last_probe_at, invalid_at, invalid_reason)
            VALUES (?, ?, ?, ?)
            """,
            (
                task_id,
                float(submission.get("share_probe_at") or 0),
                float(submission.get("share_invalid_at") or 0),
                str(submission.get("share_invalid_reason") or ""),
            ),
        )


def _logical_checksums(connection: sqlite3.Connection) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for table in ("tasks", "task_events", "task_operations", "legacy_submission_map", "legacy_submission_archive"):
        encoded = []
        for row in connection.execute(f"SELECT * FROM {table}"):
            encoded.append(_canonical(dict(row)))
        encoded.sort()
        checksums[table] = _sha256_text("\n".join(encoded))
    for row in connection.execute("SELECT legacy_submission_id, checksum FROM legacy_submission_archive"):
        checksums[f"legacy_submission_archive_row:{int(row[0])}"] = str(row[1])
    return checksums


def migrate_legacy_databases(
    tasks_path: str | Path,
    submissions_path: str | Path,
    output_path: str | Path,
) -> MigrationReport:
    tasks_db = Path(tasks_path)
    submissions_db = Path(submissions_path)
    output_db = Path(output_path)
    if output_db.exists():
        raise MigrationError(f"output already exists: {output_db}")
    sqlite_quick_check(tasks_db)
    sqlite_quick_check(submissions_db)
    source_hashes = {"tasks": _file_hash(tasks_db), "submissions": _file_hash(submissions_db)}
    source = _open_ro(tasks_db)
    submissions_source = _open_ro(submissions_db)
    created = False
    try:
        submission_columns = {row[1] for row in submissions_source.execute("PRAGMA table_info(submissions)")}
        unknown = sorted(submission_columns - KNOWN_SUBMISSION_COLUMNS)
        if unknown:
            raise MigrationError(f"unmapped submission columns: {', '.join(unknown)}")
        tasks = _rows(source, "tasks")
        events = _rows(source, "task_events")
        operations = _rows(source, "task_operations")
        runtime_state = _rows(source, "runtime_state")
        quality_runs = _rows(source, "quality_runs")
        hdhive_subscriptions = _rows(source, "hdhive_subscriptions")
        hdhive_items = _rows(source, "hdhive_subscription_items")
        hdhive_runs = _rows(source, "hdhive_subscription_runs")
        hdhive_settings = _rows(source, "hdhive_subscription_settings")
        submissions = _rows(submissions_source, "submissions")
        parent_memory = _rows(submissions_source, "parent_category_memory")
        source_counts = {
            "tasks": len(tasks),
            "task_events": len(events),
            "task_operations": len(operations),
            "submissions": len(submissions),
            "quality_runs": len(quality_runs),
            "runtime_state": len(runtime_state),
            "parent_category_memory": len(parent_memory),
            "hdhive_subscriptions": len(hdhive_subscriptions),
        }
        seen_source: dict[tuple[str, str], int] = {}
        for task in tasks:
            source_type = str(task.get("source_type") or "").strip()
            source_key = str(task.get("source_key") or "").strip()
            if not source_type or not source_key:
                raise MigrationError(f"task {task.get('id')} has malformed source identity")
            key = (source_type, source_key)
            if key in seen_source:
                raise MigrationError(f"duplicate source identity {key}")
            seen_source[key] = int(task["id"])
        requests_by_key: dict[tuple[int, str], str] = {}
        for operation in operations:
            key = (int(operation["task_id"]), str(operation["operation_key"]))
            request = _canonical(json.loads(operation.get("request_json") or "{}"))
            previous = requests_by_key.get(key)
            if previous is not None and previous != request:
                raise MigrationError(f"duplicate operation key {key} with different request identity")
            requests_by_key[key] = request
        task_ids = {int(task["id"]) for task in tasks}
        for event in events:
            if int(event["task_id"]) not in task_ids:
                raise MigrationError(f"orphan event {event.get('id')} task_id={event['task_id']}")
        for operation in operations:
            if int(operation["task_id"]) not in task_ids:
                raise MigrationError(f"orphan operation {operation.get('id')} task_id={operation['task_id']}")
        submissions_by_id = {int(row["id"]): row for row in submissions}
        submissions_by_identity = {_identity(row): row for row in submissions}
        claims: dict[int, set[int]] = defaultdict(set)
        for task in tasks:
            task_id = int(task["id"])
            typed, json_id = _task_links(task)
            if typed is not None and json_id is not None and typed != json_id:
                raise MigrationError(f"task {task_id} has conflicting typed/JSON submission links")
            for linked in (typed, json_id):
                if linked is not None:
                    claims[linked].add(task_id)
            if str(task.get("source_type") or "") == "share":
                matched = submissions_by_identity.get(_identity(task))
                if matched is not None:
                    claims[int(matched["id"])].add(task_id)
        for submission_id, owners in claims.items():
            if submission_id not in submissions_by_id:
                raise MigrationError(f"task(s) {sorted(owners)} link missing submission {submission_id}")
            if len(owners) > 1:
                raise MigrationError(f"submission {submission_id} claimed by tasks {sorted(owners)}")
            submission = submissions_by_id[submission_id]
            owner_id = next(iter(owners))
            owner = next(task for task in tasks if int(task["id"]) == owner_id)
            if str(owner.get("source_type") or "") == "share" and _identity(owner) != _identity(submission):
                raise MigrationError(f"task {owner_id} identity does not match submission {submission_id}")
        matched = {submission_id: next(iter(owners)) for submission_id, owners in claims.items()}
        unmatched = [row for row in submissions if int(row["id"]) not in matched]
        unmatched.sort(key=lambda row: int(row["id"]))
        max_task_id = max(task_ids) if task_ids else 0
        synthetic_ids = {int(row["id"]): max_task_id + index for index, row in enumerate(unmatched, start=1)}

        database = Database(output_db)
        database.initialize()
        created = True
        with database.transaction(immediate=True) as destination:
            for task in tasks:
                destination.execute(
                    """
                    INSERT INTO tasks (
                        id, source_type, source_key, share_code, receive_code, url, title, chat_id, origin,
                        is_executable, current_stage, status, error_type, error_summary, retry_count, next_run_at,
                        claimed_by, claimed_at, claim_token, claim_heartbeat_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'runtime', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(task["id"]),
                        str(task.get("source_type") or "share"),
                        str(task.get("source_key") or ""),
                        str(task.get("share_code") or ""),
                        str(task.get("receive_code") or ""),
                        str(task.get("url") or ""),
                        str(task.get("title") or ""),
                        str(task.get("chat_id") or ""),
                        str(task.get("current_stage") or "received"),
                        str(task.get("status") or "pending"),
                        str(task.get("error_type") or ""),
                        str(task.get("error_summary") or ""),
                        int(task.get("retry_count") or 0),
                        float(task.get("next_run_at") if task.get("next_run_at") is not None else -1),
                        str(task.get("claimed_by") or ""),
                        float(task.get("claimed_at") or 0),
                        str(task.get("claim_token") or ""),
                        float(task.get("claim_heartbeat_at") or 0),
                        float(task.get("created_at") or 0),
                        float(task.get("updated_at") or 0),
                    ),
                )
                linked_id = next((sub_id for sub_id, owner in matched.items() if owner == int(task["id"])), None)
                _write_facts(destination, int(task["id"]), task, submissions_by_id.get(linked_id) if linked_id else None)
            now = 1.0
            for submission in unmatched:
                task_id = synthetic_ids[int(submission["id"])]
                identity = _identity(submission)
                source_key = f"share:{identity[0]}:{identity[1]}"
                destination.execute(
                    """
                    INSERT INTO tasks (
                        id, source_type, source_key, share_code, receive_code, url, title, origin, is_executable,
                        current_stage, status, next_run_at, created_at, updated_at
                    ) VALUES (?, 'share', ?, ?, ?, ?, ?, 'legacy_import', ?, 'cleaned', 'succeeded', -1, ?, ?)
                    """,
                    (
                        task_id,
                        source_key,
                        identity[0],
                        identity[1],
                        str(submission.get("url") or ""),
                        str(submission.get("title") or ""),
                        int(SYNTHETIC_EXECUTABLE),
                        float(submission.get("created_at") or now),
                        float(submission.get("updated_at") or now),
                    ),
                )
                _write_facts(destination, task_id, {"title": submission.get("title"), "tmdb_id": "", "category": ""}, submission)
                payload = _canonical(submission)
                checksum = _sha256_text(payload)
                destination.execute(
                    "INSERT INTO legacy_submission_map (legacy_submission_id, task_id, imported_at) VALUES (?, ?, ?)",
                    (int(submission["id"]), task_id, now),
                )
                destination.execute(
                    """
                    INSERT INTO legacy_submission_archive (legacy_submission_id, payload_json, checksum, imported_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (int(submission["id"]), payload, checksum, now),
                )
            for submission_id, task_id in matched.items():
                payload = _canonical(submissions_by_id[submission_id])
                checksum = _sha256_text(payload)
                destination.execute(
                    "INSERT INTO legacy_submission_map (legacy_submission_id, task_id, imported_at) VALUES (?, ?, ?)",
                    (submission_id, task_id, now),
                )
                destination.execute(
                    """
                    INSERT INTO legacy_submission_archive (legacy_submission_id, payload_json, checksum, imported_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (submission_id, payload, checksum, now),
                )
            _copy_rows(
                destination,
                "task_events",
                events,
                ["id", "task_id", "stage", "status", "message", "error_type", "error_detail", "created_at"],
            )
            _copy_rows(
                destination,
                "task_operations",
                operations,
                [
                    "id",
                    "task_id",
                    "operation_key",
                    "operation_type",
                    "status",
                    "request_json",
                    "result_json",
                    "attempt_count",
                    "last_error",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "updated_at",
                ],
            )
            _copy_rows(destination, "runtime_state", runtime_state, ["key", "value", "updated_at"])
            _copy_rows(
                destination,
                "quality_runs",
                quality_runs,
                [
                    "id",
                    "run_id",
                    "run_date",
                    "status",
                    "started_at",
                    "finished_at",
                    "scanned_count",
                    "issue_count",
                    "planned_count",
                    "queued_count",
                    "failed_count",
                    "skipped_count",
                    "manual_count",
                    "cooldown_count",
                    "rule_counts_json",
                    "budget_used_json",
                    "created_at",
                ],
            )
            _copy_rows(
                destination,
                "parent_category_memory",
                parent_memory,
                ["parent_id", "category", "source", "created_at", "updated_at"],
            )
            _copy_rows(
                destination,
                "hdhive_subscriptions",
                hdhive_subscriptions,
                [
                    "id",
                    "chat_id",
                    "source_type",
                    "source_value",
                    "source_url",
                    "title",
                    "tmdb_id",
                    "media_type",
                    "pan_type",
                    "status",
                    "last_checked_at",
                    "last_error",
                    "created_at",
                    "updated_at",
                    "episode_filter",
                    "last_summary_json",
                ],
            )
            _copy_rows(
                destination,
                "hdhive_subscription_items",
                hdhive_items,
                [
                    "id",
                    "subscription_id",
                    "episode_key",
                    "resource_slug",
                    "title",
                    "validate_status",
                    "resolution_score",
                    "unlock_points",
                    "status",
                    "task_id",
                    "last_error",
                    "created_at",
                    "updated_at",
                ],
            )
            _copy_rows(
                destination,
                "hdhive_subscription_runs",
                hdhive_runs,
                ["id", "run_id", "run_date", "status", "summary_json", "started_at", "finished_at"],
            )
            _copy_rows(destination, "hdhive_subscription_settings", hdhive_settings, ["key", "value", "updated_at"])
            runnable = destination.execute(
                """
                SELECT id FROM tasks
                WHERE origin = 'legacy_import' AND (is_executable != 0 OR next_run_at >= 0 OR claimed_by != '')
                """
            ).fetchall()
            if runnable:
                raise MigrationError(f"synthetic historical tasks would be runnable: {[row[0] for row in runnable]}")
            for table in (
                "tasks",
                "task_events",
                "task_operations",
                "quality_runs",
                "hdhive_subscriptions",
                "hdhive_subscription_items",
                "hdhive_subscription_runs",
            ):
                _set_sequence(destination, table)
            destination_counts = {
                "tasks": destination.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "legacy_submission_map": destination.execute("SELECT COUNT(*) FROM legacy_submission_map").fetchone()[0],
                "task_events": destination.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
                "task_operations": destination.execute("SELECT COUNT(*) FROM task_operations").fetchone()[0],
            }
            checksums = _logical_checksums(destination)
            migration_id = _sha256_text(_canonical(source_hashes))
            now_ts = time.time()
            destination.execute(
                "UPDATE schema_meta SET migration_id = ?, migrated_at = ? WHERE id = 1",
                (migration_id, now_ts),
            )
            destination.execute(
                """
                INSERT INTO migration_runs (
                    migration_id, source_hashes_json, source_counts_json, destination_counts_json,
                    validation_json, write_gate, created_at
                ) VALUES (?, ?, ?, ?, ?, 'closed', ?)
                """,
                (
                    migration_id,
                    _canonical(source_hashes),
                    _canonical(source_counts),
                    _canonical(destination_counts),
                    _canonical({"foreign_key_errors": []}),
                    now_ts,
                ),
            )
        sqlite_foreign_key_check(output_db)
        return MigrationReport(
            source_hashes=source_hashes,
            source_counts=source_counts,
            destination_counts={key: int(value) for key, value in destination_counts.items()},
            matched_submissions=len(matched),
            synthetic_tasks=len(unmatched),
            unmapped_rows=0,
            logical_checksums=checksums,
            foreign_key_errors=(),
            migration_id=migration_id,
        )
    except Exception:
        if created:
            output_db.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(output_db) + suffix).unlink(missing_ok=True)
        raise
    finally:
        source.close()
        submissions_source.close()


def report_dict(report: MigrationReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["foreign_key_errors"] = list(report.foreign_key_errors)
    return payload


def validate_unified_database(path: str | Path) -> dict[str, Any]:
    database = Database(path)
    database.verify()
    sqlite_foreign_key_check(database.path)
    connection = database.connect(read_only=True)
    try:
        meta = connection.execute("SELECT version, migration_id FROM schema_meta WHERE id = 1").fetchone()
        run = connection.execute(
            "SELECT migration_id, write_gate FROM migration_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        executable = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE origin = 'legacy_import' AND is_executable != 0"
        ).fetchone()[0]
    finally:
        connection.close()
    if meta is None:
        raise MigrationError("schema_meta is missing")
    if int(executable or 0):
        raise MigrationError("legacy_import tasks are executable")
    migration_id = str((run["migration_id"] if run else meta["migration_id"]) or "")
    write_gate = str(run["write_gate"] if run else "closed")
    return {
        "schema_version": int(meta["version"]),
        "migration_id": migration_id,
        "write_gate": write_gate,
        "legacy_import_executable": int(executable or 0),
    }


def _require_migration_run(connection: sqlite3.Connection, migration_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM migration_runs WHERE migration_id = ? ORDER BY id DESC LIMIT 1",
        (str(migration_id),),
    ).fetchone()
    if row is None:
        raise MigrationError("migration id does not match")
    return row


def open_runner_gate(path: str | Path, migration_id: str) -> None:
    database = Database(path)
    with database.transaction(immediate=True) as connection:
        row = _require_migration_run(connection, migration_id)
        if str(row["write_gate"]) != "closed":
            raise MigrationError(f"write gate is {row['write_gate']}, expected closed")
        connection.execute(
            "UPDATE migration_runs SET write_gate = 'runner_open', runner_opened_at = ? WHERE id = ?",
            (time.time(), int(row["id"])),
        )


def open_intake_gate(path: str | Path, migration_id: str) -> None:
    database = Database(path)
    with database.transaction(immediate=True) as connection:
        row = _require_migration_run(connection, migration_id)
        if str(row["write_gate"]) != "runner_open":
            raise MigrationError(f"write gate is {row['write_gate']}, expected runner_open")
        connection.execute(
            "UPDATE migration_runs SET write_gate = 'open', intake_opened_at = ? WHERE id = ?",
            (time.time(), int(row["id"])),
        )
