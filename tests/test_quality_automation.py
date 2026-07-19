import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config


class QualityAutomationConfigTests(unittest.TestCase):
    def required_env(self, tmp):
        return {
            "TG_BOT_TOKEN": "123456:test",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms:9527",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "pass",
            "TASK_DB_PATH": str(Path(tmp) / "tasks.db"),
        }

    def test_quality_automation_defaults_are_disabled_and_conservative(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            config = Config.from_env()

        self.assertFalse(config.quality_auto_enabled)
        self.assertEqual(config.quality_auto_time, "02:50")
        self.assertEqual(config.quality_auto_timezone, "Asia/Shanghai")
        self.assertEqual(config.quality_auto_max_tasks, 50)
        self.assertEqual(config.quality_auto_115_check_limit, 3)

    def test_quality_automation_settings_parse_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update(
                {
                    "QUALITY_AUTO_ENABLED": "true",
                    "QUALITY_AUTO_TIME": "23:05",
                    "QUALITY_AUTO_TIMEZONE": "UTC",
                    "QUALITY_AUTO_MAX_TASKS": "12",
                    "QUALITY_AUTO_115_CHECK_LIMIT": "7",
                }
            )
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

        self.assertTrue(config.quality_auto_enabled)
        self.assertEqual(config.quality_auto_time, "23:05")
        self.assertEqual(config.quality_auto_timezone, "UTC")
        self.assertEqual(config.quality_auto_max_tasks, 12)
        self.assertEqual(config.quality_auto_115_check_limit, 7)

    def test_quality_automation_rejects_invalid_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("2:50", "24:00", "02:60", "02:50:00"):
                env = self.required_env(tmp)
                env["QUALITY_AUTO_TIME"] = value
                with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(ValueError):
                        Config.from_env()

    def test_quality_automation_rejects_invalid_timezone(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {**self.required_env(tmp), "QUALITY_AUTO_TIMEZONE": "Not/AZone"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_quality_automation_rejects_non_positive_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("QUALITY_AUTO_MAX_TASKS", "QUALITY_AUTO_115_CHECK_LIMIT"):
                for value in ("0", "-1"):
                    env = self.required_env(tmp)
                    env[name] = value
                    with self.subTest(name=name, value=value), patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(ValueError):
                            Config.from_env()
