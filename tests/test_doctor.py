import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import doctor


class DoctorConfigTests(unittest.TestCase):
    def test_reports_missing_required_environment_without_secret_values(self):
        env = {
            "TG_BOT_TOKEN": "123456:secret-token",
            "CMS_PASSWORD": "secret-password",
        }

        report = doctor.run_checks(env=env, filesystem=doctor.MemoryFilesystem(existing_paths=set()))

        self.assertFalse(report.ok)
        self.assertTrue(any(item.name == "required_env" and not item.ok for item in report.items))
        text = report.to_text()
        self.assertIn("TG_ALLOWED_CHAT_ID", text)
        self.assertIn("CMS_BASE_URL", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("secret-password", text)

    def test_valid_self_share_config_checks_expected_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            strm = root / "strm"
            share = strm / "share"
            movie = strm / "movie"
            cookie = root / "115-cookies.txt"
            for path in (data, share, movie):
                path.mkdir(parents=True)
            cookie.write_text("UID=1;CID=2", encoding="utf-8")
            env = {
                "TG_BOT_TOKEN": "123456:secret-token",
                "TG_ALLOWED_CHAT_ID": "464100862",
                "CMS_BASE_URL": "http://cms:9527",
                "CMS_USERNAME": "user",
                "CMS_PASSWORD": "secret-password",
                "DB_PATH": str(data / "submissions.db"),
                "TASK_DB_PATH": str(data / "tasks.db"),
                "WORKFLOW_MODE": "self_share_sync",
                "P115_COOKIE_PATH": str(cookie),
                "SELF_SHARE_RECEIVE_CID": "pending-cid",
                "SELF_SHARE_STRM_ROOT": str(share),
                "STRM_SOURCE_ROOTS": str(strm),
                "STRM_LIBRARY_MAP": "欧美电影=" + str(movie),
                "EMBY_BASE_URL": "http://emby:8096",
                "EMBY_API_KEY": "emby-secret",
            }

            report = doctor.run_checks(env=env)

            self.assertTrue(report.ok, report.to_text())
            text = report.to_text()
            self.assertIn("OK required_env", text)
            self.assertIn("OK filesystem", text)
            self.assertNotIn("secret-token", text)
            self.assertNotIn("secret-password", text)
            self.assertNotIn("emby-secret", text)

    def test_main_returns_nonzero_when_required_config_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(1, doctor.main([]))

    def test_web_enabled_requires_valid_port_and_task_db_directory(self):
        env = {
            "TG_BOT_TOKEN": "123456:secret-token",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms:9527",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "secret-password",
            "WEB_ENABLED": "true",
            "WEB_PORT": "not-a-port",
            "TASK_DB_PATH": "/missing/tasks.db",
        }

        report = doctor.run_checks(env=env, filesystem=doctor.MemoryFilesystem(existing_paths={"/data"}))

        self.assertFalse(report.ok)
        text = report.to_text()
        self.assertIn("WEB_PORT", text)
        self.assertIn("TASK_DB directory does not exist", text)


if __name__ == "__main__":
    unittest.main()
