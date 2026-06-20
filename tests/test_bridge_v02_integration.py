import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge


class BridgeV02IntegrationTests(unittest.TestCase):
    def required_env(self, tmp):
        return {
            "TG_BOT_TOKEN": "123456:test",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms:9527",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "pass",
            "DB_PATH": str(Path(tmp) / "submissions.db"),
            "TASK_DB_PATH": str(Path(tmp) / "tasks.db"),
            "WEB_ENABLED": "true",
            "WEB_HOST": "127.0.0.1",
            "WEB_PORT": "8787",
            "WEB_TOKEN": "secret",
            "TASK_MAX_RETRIES": "5",
        }

    def test_config_reads_v02_web_and_task_settings(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()

            self.assertEqual(cfg.task_db_path, str(Path(tmp) / "tasks.db"))
            self.assertTrue(cfg.web_enabled)
            self.assertEqual(cfg.web_host, "127.0.0.1")
            self.assertEqual(cfg.web_port, 8787)
            self.assertEqual(cfg.web_token, "secret")
            self.assertEqual(cfg.task_max_retries, 5)

    def test_create_task_store_uses_task_db_path(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()
            store = bridge.create_task_store(cfg)
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            self.assertEqual(task.share_code, "abc")
            self.assertTrue(Path(cfg.task_db_path).exists())


if __name__ == "__main__":
    unittest.main()
