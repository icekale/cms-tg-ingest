import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore


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

    def test_maybe_start_web_server_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()
            task_store = bridge.create_task_store(cfg)
            calls = []

            def fake_start(store, host, port, web_token=""):
                calls.append((store, host, port, web_token))
                return "server"

            server = bridge.maybe_start_web_server(cfg, task_store, starter=fake_start)

            self.assertEqual(server, "server")
            self.assertEqual(calls, [(task_store, "127.0.0.1", 8787, "secret")])

    def test_maybe_start_web_server_returns_none_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["WEB_ENABLED"] = "false"
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)

                server = bridge.maybe_start_web_server(cfg, task_store, starter=lambda *args, **kwargs: "server")

                self.assertIsNone(server)

    def test_run_forever_passes_task_store_to_handle_update(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()
            seen = []

            class OneUpdateTelegram:
                def __init__(self, token, timeout=60):
                    self.calls = 0

                def get_updates(self, offset=None, timeout=30):
                    if self.calls:
                        raise KeyboardInterrupt()
                    self.calls += 1
                    return [{"update_id": 1, "message": {"chat": {"id": 464100862}, "from": {"id": 464100862}, "text": "/help"}}]

                def send_message(self, *args, **kwargs):
                    return {"ok": True}

            def fake_handle_update(*args, **kwargs):
                seen.append(kwargs.get("task_store"))

            with patch.object(bridge, "TelegramClient", OneUpdateTelegram), \
                 patch.object(bridge, "CmsClient", lambda config: object()), \
                 patch.object(bridge, "EmbyClient", lambda *args, **kwargs: None), \
                 patch.object(bridge, "OpenAIClassifier", lambda config: None), \
                 patch.object(bridge, "TmdbWebResolver", lambda timeout=20: None), \
                 patch.object(bridge, "maybe_start_web_server", lambda config, task_store: None), \
                 patch.object(bridge, "start_status_repair_loop", lambda *args, **kwargs: None), \
                 patch.object(bridge, "write_metrics_snapshot", lambda *args, **kwargs: None), \
                 patch.object(bridge, "normalize_emby_parents", lambda *args, **kwargs: 0), \
                 patch.object(bridge, "handle_update", fake_handle_update):
                with self.assertRaises(KeyboardInterrupt):
                    bridge.run_forever(cfg)

            self.assertEqual(len(seen), 1)
            self.assertIsNotNone(seen[0])


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return {"ok": True}


class FakeCmsSubmit:
    def __init__(self):
        self.submitted = []

    def add_share_down(self, link):
        self.submitted.append(link)
        return {"id": "cms-1", "name": "示例电影"}


class BridgeTaskStoreHandleUpdateTests(unittest.TestCase):
    def update(self, text):
        return {
            "message": {
                "chat": {"id": 464100862},
                "from": {"id": 464100862},
                "text": text,
            }
        }

    def test_handle_update_records_received_and_cms_submitted_task_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
            )

            tasks = task_store.list_recent_tasks(limit=10)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].share_code, "abc")
            self.assertEqual(tasks[0].receive_code, "1234")
            self.assertEqual(tasks[0].current_stage, TaskStage.CMS_SUBMITTED)
            self.assertEqual(tasks[0].status, TaskStatus.RUNNING)
            events = task_store.list_events(tasks[0].id)
            self.assertEqual([event["stage"] for event in events], ["received", "cms_submitted"])
            self.assertEqual(cms.submitted, ["https://115cdn.com/s/abc?password=1234"])

    def test_duplicate_link_does_not_resubmit_but_keeps_taskstore_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            update = self.update("https://115cdn.com/s/abc")

            bridge.handle_update(update, cms, telegram, "464100862", submission_store, poll_status=False, task_store=task_store)
            bridge.handle_update(update, cms, telegram, "464100862", submission_store, poll_status=False, task_store=task_store)

            tasks = task_store.list_recent_tasks(limit=10)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(len(cms.submitted), 1)
            self.assertIn("cms_submitted", [event["stage"] for event in task_store.list_events(tasks[0].id)])

    def test_handle_update_without_task_store_preserves_existing_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
            )

            self.assertEqual(len(cms.submitted), 1)
            self.assertEqual(submission_store.recent(limit=1)[0]["share_code"], "abc")

    def test_handle_update_with_polling_accepts_task_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=True,
                status_poll_seconds=0,
                task_store=task_store,
            )

            self.assertEqual(len(cms.submitted), 1)
            self.assertNotIn("失败", telegram.messages[-1][1])
            self.assertEqual(submission_store.recent(limit=1)[0]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
