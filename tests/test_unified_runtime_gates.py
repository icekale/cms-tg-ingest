import ast
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from app.config import Config
from app.database import SCHEMA_VERSION, Database, SchemaVersionError
from app.task_store import TaskStore


RUNTIME_ROOTS = (
    Path(__file__).resolve().parents[1] / "app",
    Path(__file__).resolve().parents[1] / "bridge.py",
    Path(__file__).resolve().parents[1] / "doctor.py",
)
FORBIDDEN_RUNTIME_TOKENS = (
    "SubmissionStore",
    "best_effort_task_sync",
    "start_status_poll",
    "start_status_repair_loop",
    "repair_stranded_self_share_moves",
    "TASK_ENGINE_ENABLED",
    "TASK_DB_PATH",
    "DB_PATH",
)
FORBIDDEN_RUNTIME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, FORBIDDEN_RUNTIME_TOKENS)) + r")(?![A-Za-z0-9_])"
)


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(path for path in root.rglob("*.py") if path.is_file())


class UnifiedRuntimeGateTests(unittest.TestCase):
    def test_bridge_does_not_export_legacy_executor_types(self):
        self.assertFalse(hasattr(bridge, "SubmissionStore"))
        self.assertFalse(hasattr(bridge, "best_effort_task_sync"))
        self.assertFalse(hasattr(bridge, "start_status_poll"))
        self.assertFalse(hasattr(bridge, "start_status_repair_loop"))

    def test_runtime_sources_do_not_import_legacy_polling(self):
        offenders = []
        for root in RUNTIME_ROOTS:
            for path in _python_files(root):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        offenders.extend(
                            f"{path.name}:{alias.name}"
                            for alias in node.names
                            if alias.name == "app.legacy_polling" or alias.name.startswith("app.legacy_polling.")
                        )
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        if module == "app.legacy_polling" or module.startswith("app.legacy_polling."):
                            offenders.append(f"{path.name}:{module}")
        self.assertEqual(offenders, [])

    def test_runtime_sources_do_not_reference_legacy_executor(self):
        offenders = []
        for root in RUNTIME_ROOTS:
            for path in _python_files(root):
                text = path.read_text(encoding="utf-8")
                for match in FORBIDDEN_RUNTIME_RE.finditer(text):
                    offenders.append(f"{path}:{match.group(0)}")
        self.assertEqual(offenders, [])

    def test_config_uses_database_path_not_legacy_executor_settings(self):
        self.assertIn("database_path", Config.__dataclass_fields__)
        self.assertNotIn("db_path", Config.__dataclass_fields__)
        self.assertNotIn("task_db_path", Config.__dataclass_fields__)
        self.assertNotIn("task_engine_enabled", Config.__dataclass_fields__)

    def test_config_from_env_reads_database_path_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "cms-tg-ingest.db"
            env = {
                "TG_BOT_TOKEN": "123456:test",
                "TG_ALLOWED_CHAT_ID": "464100862",
                "CMS_BASE_URL": "http://cms:9527",
                "CMS_USERNAME": "user",
                "CMS_PASSWORD": "pass",
                "DATABASE_PATH": str(database),
                "TASK_DB_PATH": str(Path(tmp) / "tasks.db"),
                "DB_PATH": str(Path(tmp) / "submissions.db"),
                "TASK_ENGINE_ENABLED": "false",
            }
            with patch.dict(os.environ, env, clear=True):
                cfg = Config.from_env()
            self.assertEqual(cfg.database_path, str(database))
            store = bridge.create_task_store(cfg)
            self.assertEqual(store.db_path, database)

    def test_startup_fails_when_schema_is_incompatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "cms-tg-ingest.db"
            TaskStore(database)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE schema_meta SET version = ?, compatible_from = ?, compatible_to = ? WHERE id = 1",
                    (SCHEMA_VERSION + 9, SCHEMA_VERSION + 9, SCHEMA_VERSION + 9),
                )
                connection.commit()
            config = Config(
                tg_bot_token="token",
                tg_allowed_chat_id="chat",
                cms_base_url="http://cms.test",
                cms_username="user",
                cms_password="pass",
                database_path=str(database),
                web_enabled=False,
                backup_enabled=False,
                media_strm_repair_enabled=False,
            )
            with self.assertRaises(SchemaVersionError):
                bridge.run_forever(config)

    def test_closed_write_gate_does_not_start_runner_or_intake(self):
        captured = {"runner_starts": 0, "updates": 0}

        class FakeCmsClient:
            def __init__(self, *_args, **_kwargs):
                pass

        class FakeTelegramClient:
            def __init__(self, *_args, **_kwargs):
                self.calls = 0

            def get_updates(self, **_kwargs):
                captured["updates"] += 1
                raise KeyboardInterrupt()

        class FakeTaskRunner:
            def __init__(self, *_args, **_kwargs):
                captured["runner_starts"] += 1

            def start(self):
                captured["started"] = True

            def stop(self, **_kwargs):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "cms-tg-ingest.db"
            TaskStore(database)
            Database(database).set_write_gate("closed")
            config = Config(
                tg_bot_token="token",
                tg_allowed_chat_id="chat",
                cms_base_url="http://cms.test",
                cms_username="user",
                cms_password="pass",
                database_path=str(database),
                web_enabled=False,
                backup_enabled=False,
                media_strm_repair_enabled=False,
            )
            stop_event = threading.Event()
            stop_event.set()
            with patch.object(bridge, "CmsClient", FakeCmsClient), patch.object(
                bridge, "TelegramClient", FakeTelegramClient
            ), patch.object(bridge, "TaskRunner", FakeTaskRunner), patch.object(
                bridge, "normalize_emby_parents", lambda *_args, **_kwargs: 0
            ), patch.object(bridge, "write_metrics_snapshot", lambda *_args, **_kwargs: None):
                bridge.run_forever(config, stop_event=stop_event)

        self.assertEqual(captured["runner_starts"], 0)
        self.assertEqual(captured["updates"], 0)

    def test_run_forever_does_not_start_media_strm_repair_loop(self):
        captured = {"repair_starts": 0}

        class FakeCmsClient:
            def __init__(self, *_args, **_kwargs):
                pass

        class FakeTelegramClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def get_updates(self, **_kwargs):
                return []

        class FakeTaskRunner:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def stop(self, **_kwargs):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "cms-tg-ingest.db"
            TaskStore(database)
            cms_db = Path(tmp) / "cms-online.db"
            cms_db.write_bytes(b"")
            stop_event = threading.Event()
            stop_event.set()
            config = Config(
                tg_bot_token="token",
                tg_allowed_chat_id="chat",
                cms_base_url="http://cms.test",
                cms_username="user",
                cms_password="pass",
                database_path=str(database),
                cms_state_db_path=str(cms_db),
                web_enabled=False,
                backup_enabled=False,
                media_strm_repair_enabled=True,
            )
            with patch.object(bridge, "CmsClient", FakeCmsClient), patch.object(
                bridge, "TelegramClient", FakeTelegramClient
            ), patch.object(bridge, "TaskRunner", FakeTaskRunner), patch.object(
                bridge, "normalize_emby_parents", lambda *_args, **_kwargs: 0
            ), patch.object(bridge, "write_metrics_snapshot", lambda *_args, **_kwargs: None), patch.object(
                bridge, "start_media_strm_repair_loop", lambda *_a, **_k: captured.__setitem__("repair_starts", captured["repair_starts"] + 1)
            ):
                bridge.run_forever(config, stop_event=stop_event)
        self.assertEqual(captured["repair_starts"], 0)
