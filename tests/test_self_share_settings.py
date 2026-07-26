import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.task_store import TaskStore


class OwnShareReceiveCodeResolutionTests(unittest.TestCase):
    def _cms_db(self, root: str, receive_code: str) -> Path:
        path = Path(root) / "cms.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE cms_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO cms_config (key, value) VALUES (?, ?)",
                ("share_115_sync", json.dumps({"SHARE_115_PASSWORD": receive_code})),
            )
        return path

    def test_web_override_precedes_cms_and_environment(self):
        from app.self_share_settings import resolve_own_share_receive_code

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_own_share_receive_code_override("web9")
            config = SimpleNamespace(
                cms_state_db_path=self._cms_db(tmp, "cms8"),
                own_share_receive_code="env7",
            )

            resolved = resolve_own_share_receive_code(store, config)

        self.assertEqual((resolved.value, resolved.source), ("web9", "web"))

    def test_cms_precedes_environment(self):
        from app.self_share_settings import resolve_own_share_receive_code

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            config = SimpleNamespace(
                cms_state_db_path=self._cms_db(tmp, "cms8"),
                own_share_receive_code="env7",
            )

            resolved = resolve_own_share_receive_code(store, config)

        self.assertEqual((resolved.value, resolved.source), ("cms8", "cms"))

    def test_environment_precedes_default_when_cms_is_unavailable(self):
        from app.self_share_settings import resolve_own_share_receive_code

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            config = SimpleNamespace(
                cms_state_db_path=Path(tmp) / "missing.db",
                own_share_receive_code="env7",
            )

            resolved = resolve_own_share_receive_code(store, config)

        self.assertEqual((resolved.value, resolved.source), ("env7", "env"))

    def test_default_is_used_when_no_other_source_is_configured(self):
        from app.self_share_settings import resolve_own_share_receive_code

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            config = SimpleNamespace(
                cms_state_db_path=Path(tmp) / "missing.db",
                own_share_receive_code="",
            )

            resolved = resolve_own_share_receive_code(store, config)

        self.assertEqual((resolved.value, resolved.source), ("1212", "default"))
