import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from io import StringIO
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

    def test_task_engine_requires_self_share_workflow_without_leaking_secrets(self):
        env = {
            "TG_BOT_TOKEN": "123456:secret-token",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms:9527",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "secret-password",
            "DB_PATH": "/data/submissions.db",
            "TASK_DB_PATH": "/data/tasks.db",
            "TASK_ENGINE_ENABLED": "true",
            "WORKFLOW_MODE": "direct",
        }

        report = doctor.run_checks(env=env, filesystem=doctor.MemoryFilesystem(existing_paths={"/data"}))

        self.assertFalse(report.ok)
        text = report.to_text()
        self.assertIn("Task engine currently requires WORKFLOW_MODE=self_share_sync", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("secret-password", text)

    def test_task_engine_enabled_alias_requires_self_share_workflow_without_leaking_secrets(self):
        env = {
            "TG_BOT_TOKEN": "123456:secret-token",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms:9527",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "secret-password",
            "DB_PATH": "/data/submissions.db",
            "TASK_DB_PATH": "/data/tasks.db",
            "TASK_ENGINE_ENABLED": "enabled",
            "WORKFLOW_MODE": "direct",
        }

        report = doctor.run_checks(env=env, filesystem=doctor.MemoryFilesystem(existing_paths={"/data"}))

        self.assertFalse(report.ok)
        text = report.to_text()
        self.assertIn("Task engine currently requires WORKFLOW_MODE=self_share_sync", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("secret-password", text)

    def test_task_engine_requires_positive_worker_interval(self):
        env = {
            "TG_BOT_TOKEN": "123456:secret-token",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms:9527",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "secret-password",
            "DB_PATH": "/data/submissions.db",
            "TASK_DB_PATH": "/data/tasks.db",
            "TASK_ENGINE_ENABLED": "true",
            "TASK_WORKER_INTERVAL_SECONDS": "0",
            "WORKFLOW_MODE": "self_share_sync",
            "P115_COOKIE_PATH": "/config/115-cookies.txt",
            "SELF_SHARE_RECEIVE_CID": "pending-cid",
            "SELF_SHARE_STRM_ROOT": "/mnt/share",
            "STRM_SOURCE_ROOTS": "/mnt/share",
        }
        filesystem = doctor.MemoryFilesystem(existing_paths={"/data", "/config/115-cookies.txt", "/mnt/share"})

        report = doctor.run_checks(env=env, filesystem=filesystem)

        self.assertFalse(report.ok)
        self.assertIn("TASK_WORKER_INTERVAL_SECONDS must be a positive number", report.to_text())

    def test_audit_db_reports_tmdb_mismatch_direct_and_unexpected_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "submissions.db"
            mismatch_dest = root / "library" / "D-得闲谨制-2025-[tmdb=1356454]"
            direct_dest = root / "library" / "direct"
            unexpected_dest = root / "library" / "unexpected"
            for path in (mismatch_dest, direct_dest, unexpected_dest):
                path.mkdir(parents=True)
            (direct_dest / "direct.strm").write_text("http://cms/d/direct.mkv", encoding="utf-8")
            (unexpected_dest / "wrong.strm").write_text("http://cms/s/othershare_1212_file.mkv", encoding="utf-8")
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE submissions (
                        id INTEGER PRIMARY KEY,
                        title TEXT,
                        recognition_json TEXT,
                        dest_path TEXT,
                        source_path TEXT,
                        emby_path TEXT,
                        own_share_code TEXT,
                        own_share_receive_code TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO submissions
                    (id, title, recognition_json, dest_path, source_path, emby_path, own_share_code, own_share_receive_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        "双喜",
                        '{"tmdb_id":"1570664"}',
                        str(mismatch_dest),
                        "",
                        str(mismatch_dest / "x.strm"),
                        "ownshare",
                        "1212",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO submissions
                    (id, title, recognition_json, dest_path, source_path, emby_path, own_share_code, own_share_receive_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (2, "直链", "{}", str(direct_dest), "", "", "ownshare", "1212"),
                )
                conn.execute(
                    """
                    INSERT INTO submissions
                    (id, title, recognition_json, dest_path, source_path, emby_path, own_share_code, own_share_receive_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (3, "错分享", "{}", str(unexpected_dest), "", "", "ownshare", "1212"),
                )
                conn.commit()

            issues = doctor.audit_submission_db(db_path)
            text = "\n".join(issue.to_text() for issue in issues)

            self.assertIn("tmdb_mismatch", text)
            self.assertIn("任务 TMDB 1570664", text)
            self.assertIn("路径 TMDB 1356454", text)
            self.assertIn("direct_strm", text)
            self.assertIn("unexpected_strm", text)

    def test_audit_db_closes_sqlite_connection(self):
        class FakeCursor:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

            def fetchall(self):
                return []

        class FakeConnection:
            def __init__(self):
                self.closed = False
                self.row_factory = None

            def execute(self, sql):
                if "sqlite_master" in sql:
                    return FakeCursor({"name": "submissions"})
                return FakeCursor()

            def close(self):
                self.closed = True

        fake = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp, patch("doctor.sqlite3.connect", return_value=fake):
            db_path = Path(tmp) / "submissions.db"
            db_path.write_text("", encoding="utf-8")

            doctor.audit_submission_db(db_path)

        self.assertTrue(fake.closed)

    def test_audit_summary_groups_counts_and_limits_samples(self):
        issues = [
            doctor.AuditIssue(1, "direct_strm", "one"),
            doctor.AuditIssue(1, "direct_strm", "two"),
            doctor.AuditIssue(2, "tmdb_mismatch", "three"),
        ]

        text = doctor.format_audit_summary(issues, sample_limit=2)

        self.assertIn("issues=3", text)
        self.assertIn("affected_rows=2", text)
        self.assertIn("direct_strm: 2", text)
        self.assertIn("tmdb_mismatch: 1", text)
        self.assertIn("direct_strm row=1: one", text)
        self.assertIn("direct_strm row=1: two", text)
        self.assertNotIn("tmdb_mismatch row=2: three", text)

    def test_main_audit_summary_prints_condensed_report(self):
        report = doctor.DoctorReport([
            doctor.CheckItem("required_env", True, "ok"),
            doctor.CheckItem("optional_env", True, "ok"),
            doctor.CheckItem("filesystem", True, "ok"),
        ])
        issues = [
            doctor.AuditIssue(1, "direct_strm", "one"),
            doctor.AuditIssue(2, "direct_strm", "two"),
            doctor.AuditIssue(3, "tmdb_mismatch", "three"),
        ]
        output = StringIO()

        with (
            patch("doctor.run_checks", return_value=report),
            patch("doctor.audit_submission_db", return_value=issues),
            redirect_stdout(output),
        ):
            exit_code = doctor.main(["--audit-db", "/tmp/submissions.db", "--audit-summary"])

        text = output.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("issues=3", text)
        self.assertIn("direct_strm: 2", text)
        self.assertIn("tmdb_mismatch: 1", text)
        self.assertNotIn("FAIL direct_strm row=2: two", text)

    def test_audit_summary_can_group_samples_by_row(self):
        issues = [
            doctor.AuditIssue(6, "direct_strm", "first episode"),
            doctor.AuditIssue(6, "direct_strm", "second episode"),
            doctor.AuditIssue(22, "tmdb_mismatch", "wrong folder"),
            doctor.AuditIssue(22, "direct_strm", "direct file"),
            doctor.AuditIssue(24, "direct_strm", "another movie"),
        ]

        text = doctor.format_audit_summary(issues, sample_limit=2, group_by_row=True)

        self.assertIn("issues=5", text)
        self.assertIn("affected_rows=3", text)
        self.assertIn("row_samples(first 2)", text)
        self.assertIn("row=6 issues=2 direct_strm=2 sample=first episode", text)
        self.assertIn("row=22 issues=2 direct_strm=1, tmdb_mismatch=1 sample=wrong folder", text)
        self.assertNotIn("row=24", text)


if __name__ == "__main__":
    unittest.main()
