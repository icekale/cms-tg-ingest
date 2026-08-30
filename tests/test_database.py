import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import SCHEMA_VERSION, Database, SchemaVersionError
from app.sqlite_utils import sqlite_foreign_key_check, sqlite_quick_check


EXPECTED_TABLES = {
    "schema_meta",
    "tasks",
    "task_media",
    "task_shares",
    "task_moves",
    "task_emby",
    "task_cleanups",
    "task_probes",
    "task_targets",
    "task_events",
    "task_operations",
    "task_commands",
    "runner_leases",
    "runtime_state",
    "quality_runs",
    "parent_category_memory",
    "hdhive_subscriptions",
    "hdhive_subscription_items",
    "hdhive_subscription_runs",
    "hdhive_subscription_settings",
    "tmdb_details",
    "legacy_submission_map",
    "legacy_submission_archive",
    "migration_runs",
    "task_purge_audit",
}


class DatabaseSchemaTests(unittest.TestCase):
    def _db(self, tmp):
        database = Database(Path(tmp) / "cms-tg-ingest.db")
        database.initialize()
        return database

    def test_initialize_creates_canonical_tables_with_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self._db(tmp)
            with database.connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            sqlite_quick_check(database.path)
            sqlite_foreign_key_check(database.path)
            self.assertTrue(EXPECTED_TABLES <= tables)
            self.assertEqual(foreign_keys, 1)

    def test_source_identity_is_unique_and_share_identity_is_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self._db(tmp)
            with database.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, source_type, source_key, share_code, receive_code, url,
                        current_stage, status, created_at, updated_at
                    ) VALUES (1, 'share', 'share:abc:1234', 'abc', '1234', 'https://115cdn.com/s/abc',
                              'received', 'pending', 1, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, source_type, source_key, share_code, receive_code, url,
                        current_stage, status, created_at, updated_at
                    ) VALUES (2, 'ed2k', 'ed2k:one', '', '', 'ed2k://one',
                              'received', 'pending', 1, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, source_type, source_key, share_code, receive_code, url,
                        current_stage, status, created_at, updated_at
                    ) VALUES (3, 'ed2k', 'ed2k:two', '', '', 'ed2k://two',
                              'received', 'pending', 1, 1)
                    """
                )
            with database.transaction(immediate=True) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            id, source_type, source_key, share_code, receive_code, url,
                            current_stage, status, created_at, updated_at
                        ) VALUES (4, 'share', 'share:abc:1234', 'abc', '1234', 'https://115cdn.com/s/abc',
                                  'received', 'pending', 1, 1)
                        """
                    )
            with database.transaction(immediate=True) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            id, source_type, source_key, share_code, receive_code, url,
                            current_stage, status, created_at, updated_at
                        ) VALUES (5, 'share', 'share:abc:other', 'abc', '1234', 'https://115cdn.com/s/abc',
                                  'received', 'pending', 1, 1)
                        """
                    )

    def test_child_fact_without_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self._db(tmp)
            with database.transaction(immediate=True) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO task_media (task_id, title) VALUES (99, 'orphan')"
                    )

    def test_unsupported_schema_version_fails_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = self._db(tmp)
            with database.transaction(immediate=True) as conn:
                conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION + 1,))
            with self.assertRaises(SchemaVersionError):
                database.verify()
            with database.connect(read_only=True) as conn:
                version = conn.execute("SELECT version FROM schema_meta").fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION + 1)


if __name__ == "__main__":
    unittest.main()
