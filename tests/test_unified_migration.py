from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.unified_migration import MigrationError, migrate_legacy_databases
from tests.fixtures.legacy_databases import build_legacy_databases


def load_task_ids(path: Path) -> set[int]:
    with sqlite3.connect(path) as conn:
        return {int(row[0]) for row in conn.execute("SELECT id FROM tasks")}


def load_task(path: Path, task_id: int) -> dict:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row)


def load_legacy_map(path: Path, legacy_submission_id: int) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT task_id FROM legacy_submission_map WHERE legacy_submission_id = ?",
            (legacy_submission_id,),
        ).fetchone()
    return int(row[0])


class UnifiedMigrationTests(unittest.TestCase):
    def test_imports_matched_and_synthetic_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_legacy_databases(tmp)
            output = Path(tmp) / "cms-tg-ingest.db"
            report = migrate_legacy_databases(fixture.tasks_db, fixture.submissions_db, output)
            self.assertEqual(report.matched_submissions, 2)
            self.assertEqual(report.synthetic_tasks, 1)
            self.assertEqual(report.unmapped_rows, 0)
            self.assertEqual(report.foreign_key_errors, ())
            self.assertEqual(load_task_ids(output), {10, 20, 30, 31})
            self.assertFalse(load_task(output, 31)["is_executable"])
            self.assertEqual(load_legacy_map(output, legacy_submission_id=9), 31)
            with sqlite3.connect(output) as conn:
                conn.row_factory = sqlite3.Row
                archive = conn.execute(
                    "SELECT payload_json, checksum FROM legacy_submission_archive WHERE legacy_submission_id = 9"
                ).fetchone()
                event_ids = [int(row[0]) for row in conn.execute("SELECT id FROM task_events")]
                operation = conn.execute("SELECT id, operation_key, request_json FROM task_operations").fetchone()
            payload = json.loads(archive["payload_json"])
            self.assertEqual(payload["share_code"], "xyz")
            self.assertEqual(archive["checksum"], report.logical_checksums["legacy_submission_archive_row:9"])
            self.assertEqual(event_ids, [100])
            self.assertEqual(int(operation["id"]), 7)
            self.assertEqual(json.loads(operation["request_json"])["share_code"], "abc")

    def _migrate_expecting_abort(self, mutate) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_legacy_databases(tmp)
            mutate(fixture)
            output = Path(tmp) / "cms-tg-ingest.db"
            with self.assertRaises(MigrationError):
                migrate_legacy_databases(fixture.tasks_db, fixture.submissions_db, output)
            self.assertFalse(output.exists())

    def test_aborts_duplicate_source_identity(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.tasks_db) as conn:
                conn.execute("DROP INDEX IF EXISTS idx_tasks_source_key")
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, share_code, receive_code, source_type, source_key, url,
                        current_stage, status, created_at, updated_at
                    ) VALUES (40, 'zzz', '9999', 'share', 'share:abc:1111', 'https://115cdn.com/s/zzz',
                              'received', 'pending', 1, 1)
                    """
                )

        self._migrate_expecting_abort(mutate)

    def test_aborts_conflicting_typed_and_json_links(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.tasks_db) as conn:
                conn.execute("UPDATE tasks SET submission_id = 2, metadata_json = '{\"submission_id\": 1}' WHERE id = 10")

        self._migrate_expecting_abort(mutate)

    def test_aborts_two_tasks_claiming_one_submission(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.tasks_db) as conn:
                conn.execute("UPDATE tasks SET submission_id = 1 WHERE id IN (10, 20)")

        self._migrate_expecting_abort(mutate)

    def test_aborts_linked_identity_mismatch(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.submissions_db) as conn:
                conn.execute("UPDATE submissions SET share_code = 'nope' WHERE id = 1")

        self._migrate_expecting_abort(mutate)

    def test_aborts_orphan_event(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.tasks_db) as conn:
                conn.execute(
                    "INSERT INTO task_events (id, task_id, stage, status, message, created_at) VALUES (101, 999, 'received', 'pending', 'x', 1)"
                )

        self._migrate_expecting_abort(mutate)

    def test_aborts_malformed_source_identity(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.tasks_db) as conn:
                conn.execute("UPDATE tasks SET source_key = '' WHERE id = 30")

        self._migrate_expecting_abort(mutate)

    def test_aborts_duplicate_operation_key_with_different_request(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.tasks_db) as conn:
                conn.execute("ALTER TABLE task_operations RENAME TO task_operations_old")
                conn.execute(
                    """
                    CREATE TABLE task_operations (
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
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute("INSERT INTO task_operations SELECT * FROM task_operations_old")
                conn.execute(
                    """
                    INSERT INTO task_operations (
                        id, task_id, operation_key, operation_type, status, request_json, created_at, updated_at
                    ) VALUES (8, 10, 'receive:abc', 'receive_share', 'succeeded', '{"share_code":"other"}', 1, 1)
                    """
                )

        self._migrate_expecting_abort(mutate)

    def test_aborts_unmapped_legacy_submission_column(self):
        def mutate(fixture):
            with sqlite3.connect(fixture.submissions_db) as conn:
                conn.execute("ALTER TABLE submissions ADD COLUMN mystery_flag TEXT")

        self._migrate_expecting_abort(mutate)

    def test_aborts_if_synthetic_task_would_be_runnable(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_legacy_databases(tmp)
            output = Path(tmp) / "cms-tg-ingest.db"
            with patch("app.unified_migration.SYNTHETIC_EXECUTABLE", 1):
                with self.assertRaises(MigrationError):
                    migrate_legacy_databases(fixture.tasks_db, fixture.submissions_db, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
