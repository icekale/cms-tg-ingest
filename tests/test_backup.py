import sqlite3
import tempfile
import unittest
from pathlib import Path

import bridge
from app.backup import BackupScheduler, backup_sqlite_databases
from app.config import Config
from app.task_store import TaskStore


class BackupTests(unittest.TestCase):
    def _make_database(self, path: Path, value: str = "ok") -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
            connection.execute("INSERT INTO values_table (value) VALUES (?)", (value,))

    def test_backup_creates_readable_online_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tasks.db"
            destination = root / "backups"
            self._make_database(source, "snapshot")

            result = backup_sqlite_databases(
                [source],
                destination,
                now=1_735_689_600.0,
                retention_days=14,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(result.files), 1)
            backup_path = Path(result.files[0])
            self.assertTrue(backup_path.exists())
            with sqlite3.connect(backup_path) as connection:
                value = connection.execute("SELECT value FROM values_table").fetchone()[0]
            self.assertEqual(value, "snapshot")

    def test_backup_reports_missing_source_without_discarding_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tasks.db"
            missing = root / "submissions.db"
            destination = root / "backups"
            self._make_database(source)

            result = backup_sqlite_databases(
                [source, missing],
                destination,
                now=1_735_689_600.0,
                retention_days=14,
            )

            self.assertEqual(result.status, "partial")
            self.assertEqual(result.skipped, [str(missing)])
            self.assertEqual(len(result.files), 1)

    def test_backup_retention_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tasks.db"
            destination = root / "backups"
            destination.mkdir()
            self._make_database(source)
            old_backup = destination / "tasks-20260101T000000Z.db"
            old_backup.write_bytes(b"old")
            unrelated = destination / "keep-me.db"
            unrelated.write_bytes(b"keep")

            backup_sqlite_databases(
                [source],
                destination,
                now=1_769_904_000.0,
                retention_days=14,
            )

            self.assertFalse(old_backup.exists())
            self.assertTrue(unrelated.exists())

    def test_scheduler_persists_last_backup_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tasks.db"
            self._make_database(source)
            store = TaskStore(root / "state.db")
            scheduler = BackupScheduler(
                store,
                [source],
                root / "backups",
                run_time="03:30",
                timezone_name="Asia/Shanghai",
                retention_days=14,
            )

            result = scheduler.run_once(now=1_735_689_600.0)

            self.assertEqual(result.status, "succeeded")
            state = store.get_runtime_state("backup_last_result")
            self.assertIsNotNone(state)
            self.assertIn('"status": "succeeded"', state["value"])

    def test_scheduler_retries_after_failed_backup_on_the_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tasks.db"
            self._make_database(source)
            destination = root / "backups"
            destination.write_text("not a directory", encoding="utf-8")
            store = TaskStore(root / "state.db")
            scheduler = BackupScheduler(
                store,
                [source],
                destination,
                run_time="03:30",
                timezone_name="Asia/Shanghai",
            )

            failed = scheduler.run_if_due(now=1_735_689_600.0)
            self.assertIsNotNone(failed)
            self.assertEqual(failed.status, "failed")

            scheduler.destination = root / "backups-fixed"
            retried = scheduler.run_if_due(now=1_735_689_600.0)
            self.assertIsNotNone(retried)
            self.assertEqual(retried.status, "succeeded")

    def test_bridge_builds_scheduler_for_both_runtime_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config(
                tg_bot_token="token",
                tg_allowed_chat_id="chat",
                cms_base_url="http://cms",
                cms_username="user",
                cms_password="pass",
                db_path=str(root / "submissions.db"),
                task_db_path=str(root / "tasks.db"),
                backup_dir=str(root / "backups"),
                backup_time="04:10",
                backup_timezone="UTC",
                backup_retention_days=9,
                backup_enabled=True,
            )
            store = TaskStore(config.task_db_path)

            scheduler = bridge.create_backup_scheduler(config, store)

            self.assertEqual(
                scheduler.sources,
                (Path(config.db_path), Path(config.task_db_path)),
            )
            self.assertEqual(scheduler.destination, Path(config.backup_dir))
            self.assertEqual(scheduler.run_time.hour, 4)
            self.assertEqual(scheduler.run_time.minute, 10)
            self.assertEqual(scheduler.retention_days, 9)
            self.assertTrue(scheduler.enabled)


if __name__ == "__main__":
    unittest.main()
