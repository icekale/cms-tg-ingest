import gc
import sqlite3
import tempfile
import unittest
import warnings
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import bridge
from app.backup import BackupScheduler, backup_sqlite_databases
from app.config import Config
from app.cms_cloud_index import CmsCloudDataIndex
from app.hdhive_cards import TmdbDetailCache
from app.task_store import TaskStore


class _TrackingConnection:
    def __init__(self, path: str | Path, *, fail_backup: bool = False):
        self.path = Path(path)
        self.closed = False
        self.fail_backup = fail_backup

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def backup(self, target):
        if self.fail_backup:
            raise sqlite3.DatabaseError("forced backup failure")
        target.path.write_bytes(b"backup")

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class BackupTests(unittest.TestCase):
    def _make_database(self, path: Path, value: str = "ok") -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
            connection.execute("INSERT INTO values_table (value) VALUES (?)", (value,))
            connection.commit()

    def test_backup_closes_source_and_target_on_success_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tasks.db"
            destination = root / "backups"
            self._make_database(source)
            connections = []
            fail_backup = False

            def connect(path, *args, **kwargs):
                tracked = _TrackingConnection(path, fail_backup=fail_backup)
                connections.append(tracked)
                return tracked

            with patch("app.backup.sqlite3.connect", side_effect=connect), patch(
                "app.backup.sqlite_quick_check", create=True
            ):
                succeeded = backup_sqlite_databases([source], destination, now=1_735_689_600.0)
                fail_backup = True
                failed = backup_sqlite_databases([source], destination, now=1_735_689_601.0)

            self.assertEqual(succeeded.status, "succeeded")
            self.assertEqual(failed.status, "failed")
            self.assertEqual(len(connections), 4)
            closed = [connection.closed for connection in connections]
            self.assertTrue(all(closed))

    def test_repeated_short_lived_sqlite_calls_do_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cms_db = root / "cms-online.db"
            with closing(sqlite3.connect(cms_db)) as connection:
                connection.execute(
                    "CREATE TABLE cloud_data (fid TEXT PRIMARY KEY, pid TEXT, name TEXT, is_dir INTEGER)"
                )
                connection.commit()
            source = root / "tasks.db"
            self._make_database(source)
            index = CmsCloudDataIndex(cms_db)
            cache = TmdbDetailCache(root / "cache.db")

            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                for _ in range(25):
                    index.has_file_id("missing")
                    cache.get("tv", "1416", lambda: {"id": 1416})
                    backup_sqlite_databases([source], root / "backups", now=1_735_689_600.0)
                gc.collect()

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
            with closing(sqlite3.connect(backup_path)) as connection:
                value = connection.execute("SELECT value FROM values_table").fetchone()[0]
            self.assertEqual(value, "snapshot")

    def test_backup_rejects_duplicate_derived_names_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = root / "one" / "state.db"
            two = root / "two" / "state.db"
            destination = root / "backups"
            one.parent.mkdir()
            two.parent.mkdir()
            self._make_database(one, "one")
            self._make_database(two, "two")

            result = backup_sqlite_databases([one, two], destination, now=1_735_689_600.0)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.files, [])
            self.assertIn(str(one), result.error)
            self.assertIn(str(two), result.error)
            self.assertEqual(list(destination.glob("state-*.db")), [])

    def test_backup_uses_stable_mapping_names_for_same_stem_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = root / "one" / "state.db"
            two = root / "two" / "state.db"
            destination = root / "backups"
            one.parent.mkdir()
            two.parent.mkdir()
            self._make_database(one, "one")
            self._make_database(two, "two")

            result = backup_sqlite_databases(
                {"submissions": one, "tasks": two},
                destination,
                now=1_735_689_600.0,
            )

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(result.files), 2)
            backup_paths = [Path(path) for path in result.files]
            self.assertTrue(any(path.name.startswith("submissions-") for path in backup_paths))
            self.assertTrue(any(path.name.startswith("tasks-") for path in backup_paths))
            values = {}
            for path in backup_paths:
                with closing(sqlite3.connect(path)) as connection:
                    values[path.name.split("-", 1)[0]] = connection.execute(
                        "SELECT value FROM values_table"
                    ).fetchone()[0]
            self.assertEqual(values, {"submissions": "one", "tasks": "two"})

    def test_backup_does_not_publish_unverified_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "tasks.db"
            destination = root / "backups"
            self._make_database(source)

            with patch(
                "app.backup.sqlite_quick_check",
                side_effect=sqlite3.DatabaseError("corrupt snapshot"),
                create=True,
            ):
                result = backup_sqlite_databases([source], destination, now=1_735_689_600.0)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.files, [])
            self.assertIn("corrupt snapshot", result.error)
            self.assertEqual(list(destination.glob("tasks-*.db")), [])
            self.assertEqual(list(destination.glob("*.tmp")), [])

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
            self.assertEqual(
                scheduler.named_sources,
                {
                    "submissions": Path(config.db_path),
                    "tasks": Path(config.task_db_path),
                },
            )
            self.assertEqual(scheduler.destination, Path(config.backup_dir))
            self.assertEqual(scheduler.run_time.hour, 4)
            self.assertEqual(scheduler.run_time.minute, 10)
            self.assertEqual(scheduler.retention_days, 9)
            self.assertTrue(scheduler.enabled)


if __name__ == "__main__":
    unittest.main()
