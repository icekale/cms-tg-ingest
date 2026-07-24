import json
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
            "TASK_ENGINE_ENABLED": "true",
            "P115_RISK_COOLDOWN_SECONDS": "1200",
            "TMDB_API_KEY": "tmdb-test-key",
            "TMDB_BEARER_TOKEN": "tmdb-test-token",
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
            self.assertEqual(cfg.p115_risk_cooldown_seconds, 1200)
            self.assertEqual(cfg.tmdb_api_key, "tmdb-test-key")
            self.assertEqual(cfg.tmdb_bearer_token, "tmdb-test-token")
            self.assertFalse(cfg.self_share_invalid_cleanup_enabled)
            self.assertEqual(cfg.self_share_invalid_check_interval_seconds, 21600)
            self.assertEqual(cfg.self_share_invalid_check_limit, 3)

    def test_config_reads_task_engine_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()

            self.assertTrue(cfg.task_engine_enabled)

    def test_config_reads_invalid_self_share_cleanup_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update({
                "SELF_SHARE_INVALID_CLEANUP_ENABLED": "true",
                "SELF_SHARE_INVALID_CHECK_INTERVAL_SECONDS": "21600",
                "SELF_SHARE_INVALID_CHECK_LIMIT": "3",
            })
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()

            self.assertTrue(cfg.self_share_invalid_cleanup_enabled)
            self.assertEqual(cfg.self_share_invalid_check_interval_seconds, 21600)
            self.assertEqual(cfg.self_share_invalid_check_limit, 3)

    def test_self_share_retry_default_is_fast_for_task_engine(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()

            self.assertEqual(cfg.self_share_auto_organize_retry_seconds, 15)
            self.assertEqual(cfg.self_share_cloud_poll_seconds, 30)
            self.assertEqual(cfg.self_share_cloud_timeout_seconds, 86400)

    def test_create_task_store_uses_task_db_path(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()
            store = bridge.create_task_store(cfg)
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            self.assertEqual(task.share_code, "abc")
            self.assertTrue(Path(cfg.task_db_path).exists())

    def test_completion_drift_rechecks_direct_file_share_target_not_any_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            dest = Path(tmp) / "library" / "show"
            target = dest / "Season 03" / "Show - S03E03.strm"
            target.parent.mkdir(parents=True)
            target.write_text("https://115.com/d/direct/S03E03.mkv", encoding="utf-8")
            task = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            task = store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "115 转存源已删除，自有分享保留",
                submission_id=42,
                metadata_patch={
                    "dest_path": str(dest),
                    "direct_file_share": True,
                    "direct_file_share_relative_path": "Season 03/Show - S03E03.strm",
                },
            )
            row = {
                "workflow_mode": "self_share_sync",
                "own_share_code": "owncode",
                "own_share_receive_code": "ownpwd",
            }

            stage = bridge.completion_drift_retry_stage(task, row)

            self.assertEqual(stage, TaskStage.EMBY_CONFIRMED)

    def test_maybe_start_web_server_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["TASK_ENGINE_ENABLED"] = "false"
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)
                calls = []

                def fake_start(store, host, port, web_token="", task_engine_enabled=None):
                    calls.append((store, host, port, web_token, task_engine_enabled))
                    return "server"

                server = bridge.maybe_start_web_server(cfg, task_store, starter=fake_start)

                self.assertEqual(server, "server")
                self.assertEqual(calls, [(task_store, "127.0.0.1", 8787, "secret", False)])

    def test_maybe_start_web_server_returns_none_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["WEB_ENABLED"] = "false"
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)

                server = bridge.maybe_start_web_server(cfg, task_store, starter=lambda *args, **kwargs: "server")

                self.assertIsNone(server)

    def test_maybe_start_web_server_rejects_enabled_web_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["WEB_TOKEN"] = ""
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)
                starter = patch.object(bridge, "start_web_server")
                with starter as start:
                    with self.assertRaisesRegex(RuntimeError, "WEB_TOKEN"):
                        bridge.maybe_start_web_server(cfg, task_store)
                start.assert_not_called()

    def test_stop_web_server_closes_server(self):
        class FakeServer:
            def __init__(self):
                self.calls = []

            def shutdown(self):
                self.calls.append("shutdown")

            def server_close(self):
                self.calls.append("close")

        server = FakeServer()

        bridge.stop_web_server(server)

        self.assertEqual(server.calls, ["shutdown", "close"])

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
                seen.append({
                    "task_store": kwargs.get("task_store"),
                    "task_engine_enabled": kwargs.get("task_engine_enabled"),
                })

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
            self.assertIsNotNone(seen[0]["task_store"])
            self.assertTrue(seen[0]["task_engine_enabled"])

    def test_run_forever_starts_task_runner_when_task_engine_and_self_share_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update({
                "WORKFLOW_MODE": "self_share_sync",
                "TASK_ENGINE_ENABLED": "true",
                "TASK_WORKER_INTERVAL_SECONDS": "7",
                "SELF_SHARE_RECEIVE_CID": "pending-cid",
            })
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                seen = []
                p115 = object()

                class OneUpdateTelegram:
                    def __init__(self, token, timeout=60):
                        self.calls = 0

                    def get_updates(self, offset=None, timeout=30):
                        if self.calls:
                            raise KeyboardInterrupt()
                        self.calls += 1
                        return []

                    def send_message(self, *args, **kwargs):
                        return {"ok": True}

                class FakeTaskRunner:
                    def __init__(self, task_store, workflow, *, interval_seconds=5, **kwargs):
                        seen.append({
                            "task_store": task_store,
                            "workflow": workflow,
                            "interval_seconds": interval_seconds,
                            "kwargs": kwargs,
                            "started": False,
                        })

                    def start(self):
                        seen[-1]["started"] = True
                        return "task-thread"

                with patch.object(bridge, "TelegramClient", OneUpdateTelegram), \
                     patch.object(bridge, "CmsClient", lambda config: object()), \
                     patch.object(bridge, "EmbyClient", lambda *args, **kwargs: object()), \
                     patch.object(bridge, "OpenAIClassifier", lambda config: object()), \
                     patch.object(bridge, "TmdbWebResolver", lambda timeout=20: object()), \
                     patch.object(bridge, "P115WebClient", lambda *args, **kwargs: p115), \
                     patch.object(bridge, "maybe_start_web_server", lambda config, task_store: None), \
                     patch.object(bridge, "start_status_repair_loop", lambda *args, **kwargs: None), \
                     patch.object(bridge, "write_metrics_snapshot", lambda *args, **kwargs: None), \
                     patch.object(bridge, "normalize_emby_parents", lambda *args, **kwargs: 0), \
                     patch.object(bridge, "TaskRunner", FakeTaskRunner):
                    with self.assertRaises(KeyboardInterrupt):
                        bridge.run_forever(cfg)

                self.assertEqual(len(seen), 1)
                self.assertIsInstance(seen[0]["task_store"], TaskStore)
                self.assertIsInstance(seen[0]["workflow"], bridge.BridgeSelfShareTaskWorkflow)
                self.assertIs(seen[0]["workflow"].task_store, seen[0]["task_store"])
                self.assertEqual(seen[0]["workflow"].receive_cid, "pending-cid")
                self.assertIsNone(seen[0]["workflow"].cleanup_client)
                self.assertEqual(seen[0]["interval_seconds"], 7)
                self.assertEqual(seen[0]["kwargs"]["risk_cooldown_seconds"], 1200)
                self.assertTrue(seen[0]["started"])

    def test_run_forever_passes_cleanup_client_to_task_workflow_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update({
                "WORKFLOW_MODE": "self_share_sync",
                "TASK_ENGINE_ENABLED": "true",
                "SELF_SHARE_CLEANUP_AFTER_EMBY": "true",
            })
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                seen = []
                p115 = object()

                class OneUpdateTelegram:
                    def __init__(self, token, timeout=60):
                        self.calls = 0

                    def get_updates(self, offset=None, timeout=30):
                        if self.calls:
                            raise KeyboardInterrupt()
                        self.calls += 1
                        return []

                    def send_message(self, *args, **kwargs):
                        return {"ok": True}

                class FakeTaskRunner:
                    def __init__(self, task_store, workflow, *, interval_seconds=5, **kwargs):
                        seen.append({
                            "task_store": task_store,
                            "workflow": workflow,
                            "interval_seconds": interval_seconds,
                            "kwargs": kwargs,
                            "started": False,
                        })

                    def start(self):
                        seen[-1]["started"] = True
                        return "task-thread"

                with patch.object(bridge, "TelegramClient", OneUpdateTelegram), \
                     patch.object(bridge, "CmsClient", lambda config: object()), \
                     patch.object(bridge, "EmbyClient", lambda *args, **kwargs: object()), \
                     patch.object(bridge, "OpenAIClassifier", lambda config: object()), \
                     patch.object(bridge, "TmdbWebResolver", lambda timeout=20: object()), \
                     patch.object(bridge, "P115WebClient", lambda *args, **kwargs: p115), \
                     patch.object(bridge, "maybe_start_web_server", lambda config, task_store: None), \
                     patch.object(bridge, "start_status_repair_loop", lambda *args, **kwargs: None), \
                     patch.object(bridge, "write_metrics_snapshot", lambda *args, **kwargs: None), \
                     patch.object(bridge, "normalize_emby_parents", lambda *args, **kwargs: 0), \
                     patch.object(bridge, "TaskRunner", FakeTaskRunner):
                    with self.assertRaises(KeyboardInterrupt):
                        bridge.run_forever(cfg)

                self.assertEqual(len(seen), 1)
                self.assertIs(seen[0]["workflow"].cleanup_client, p115)

    def test_run_forever_skips_status_repair_when_task_engine_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update({
                "WORKFLOW_MODE": "self_share_sync",
                "TASK_ENGINE_ENABLED": "true",
                "STATUS_REPAIR_ENABLED": "true",
                "SELF_SHARE_RECEIVE_CID": "pending-cid",
            })
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_runner_started = []
                repair_calls = []
                maintenance_calls = []
                p115 = object()

                class OneUpdateTelegram:
                    def __init__(self, token, timeout=60):
                        self.calls = 0

                    def get_updates(self, offset=None, timeout=30):
                        if self.calls:
                            raise KeyboardInterrupt()
                        self.calls += 1
                        return []

                    def send_message(self, *args, **kwargs):
                        return {"ok": True}

                class FakeTaskRunner:
                    def __init__(self, *args, **kwargs):
                        pass

                    def start(self):
                        task_runner_started.append(True)
                        return "task-thread"

                with patch.object(bridge, "TelegramClient", OneUpdateTelegram), \
                     patch.object(bridge, "CmsClient", lambda config: object()), \
                     patch.object(bridge, "EmbyClient", lambda *args, **kwargs: object()), \
                     patch.object(bridge, "OpenAIClassifier", lambda config: object()), \
                     patch.object(bridge, "TmdbWebResolver", lambda timeout=20: object()), \
                     patch.object(bridge, "P115WebClient", lambda *args, **kwargs: p115), \
                     patch.object(bridge, "maybe_start_web_server", lambda config, task_store: None), \
                     patch.object(bridge, "start_status_repair_loop", lambda *args, **kwargs: repair_calls.append((args, kwargs))), \
                     patch.object(bridge, "start_self_share_maintenance_loop", lambda *args, **kwargs: maintenance_calls.append((args, kwargs)), create=True), \
                     patch.object(bridge, "write_metrics_snapshot", lambda *args, **kwargs: None), \
                     patch.object(bridge, "normalize_emby_parents", lambda *args, **kwargs: 0), \
                     patch.object(bridge, "TaskRunner", FakeTaskRunner):
                    with self.assertRaises(KeyboardInterrupt):
                        bridge.run_forever(cfg)

                self.assertEqual(task_runner_started, [True])
                self.assertEqual(repair_calls, [])
                self.assertEqual(maintenance_calls, [])

    def test_run_forever_starts_invalid_self_share_probe_only_when_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update({
                "WORKFLOW_MODE": "self_share_sync",
                "TASK_ENGINE_ENABLED": "true",
                "SELF_SHARE_RECEIVE_CID": "pending-cid",
                "SELF_SHARE_INVALID_CLEANUP_ENABLED": "true",
                "SELF_SHARE_INVALID_CHECK_INTERVAL_SECONDS": "21600",
                "SELF_SHARE_INVALID_CHECK_LIMIT": "3",
            })
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                probe_calls = []
                p115 = object()

                class OneUpdateTelegram:
                    def __init__(self, token, timeout=60):
                        self.calls = 0

                    def get_updates(self, offset=None, timeout=30):
                        if self.calls:
                            raise KeyboardInterrupt()
                        self.calls += 1
                        return []

                    def send_message(self, *args, **kwargs):
                        return {"ok": True}

                class FakeTaskRunner:
                    def __init__(self, *args, **kwargs):
                        pass

                    def start(self):
                        return "task-thread"

                with patch.object(bridge, "TelegramClient", OneUpdateTelegram), \
                     patch.object(bridge, "CmsClient", lambda config: object()), \
                     patch.object(bridge, "EmbyClient", lambda *args, **kwargs: object()), \
                     patch.object(bridge, "OpenAIClassifier", lambda config: object()), \
                     patch.object(bridge, "TmdbWebResolver", lambda timeout=20: object()), \
                     patch.object(bridge, "P115WebClient", lambda *args, **kwargs: p115), \
                     patch.object(bridge, "maybe_start_web_server", lambda config, task_store: None), \
                     patch.object(bridge, "start_status_repair_loop", lambda *args, **kwargs: None), \
                     patch.object(bridge, "start_invalid_self_share_probe_loop", lambda *args, **kwargs: probe_calls.append((args, kwargs)), create=True), \
                     patch.object(bridge, "write_metrics_snapshot", lambda *args, **kwargs: None), \
                     patch.object(bridge, "normalize_emby_parents", lambda *args, **kwargs: 0), \
                     patch.object(bridge, "TaskRunner", FakeTaskRunner):
                    with self.assertRaises(KeyboardInterrupt):
                        bridge.run_forever(cfg)

            self.assertEqual(len(probe_calls), 1)
            args, kwargs = probe_calls[0]
            self.assertIs(args[2], p115)
            self.assertEqual(kwargs["interval_seconds"], 21600)
            self.assertEqual(kwargs["limit"], 3)

    def test_run_forever_starts_status_repair_when_task_engine_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update({
                "WORKFLOW_MODE": "self_share_sync",
                "TASK_ENGINE_ENABLED": "false",
                "STATUS_REPAIR_ENABLED": "true",
                "SELF_SHARE_RECEIVE_CID": "pending-cid",
            })
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                repair_calls = []
                p115 = object()

                class OneUpdateTelegram:
                    def __init__(self, token, timeout=60):
                        self.calls = 0

                    def get_updates(self, offset=None, timeout=30):
                        if self.calls:
                            raise KeyboardInterrupt()
                        self.calls += 1
                        return []

                    def send_message(self, *args, **kwargs):
                        return {"ok": True}

                with patch.object(bridge, "TelegramClient", OneUpdateTelegram), \
                     patch.object(bridge, "CmsClient", lambda config: object()), \
                     patch.object(bridge, "EmbyClient", lambda *args, **kwargs: object()), \
                     patch.object(bridge, "OpenAIClassifier", lambda config: object()), \
                     patch.object(bridge, "TmdbWebResolver", lambda timeout=20: object()), \
                     patch.object(bridge, "P115WebClient", lambda *args, **kwargs: p115), \
                     patch.object(bridge, "maybe_start_web_server", lambda config, task_store: None), \
                     patch.object(bridge, "start_status_repair_loop", lambda *args, **kwargs: repair_calls.append((args, kwargs))), \
                     patch.object(bridge, "write_metrics_snapshot", lambda *args, **kwargs: None), \
                     patch.object(bridge, "normalize_emby_parents", lambda *args, **kwargs: 0):
                    with self.assertRaises(KeyboardInterrupt):
                        bridge.run_forever(cfg)

                self.assertEqual(len(repair_calls), 1)


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return {"ok": True}

    def answer_callback_query(self, callback_id, text=None, show_alert=False):
        self.answers.append((callback_id, text, show_alert))
        return {"ok": True}


class FakeCmsSubmit:
    def __init__(self):
        self.submitted = []
        self.auto_runs = 0

    def add_share_down(self, link):
        self.submitted.append(link)
        return {"id": "cms-1", "name": "示例电影"}

    def run_auto_organize(self):
        self.auto_runs += 1
        return {"code": 200}


class FakeP115Receive:
    def __init__(self):
        self.received = []

    def receive_share_to_cid(self, share_code, receive_code, target_cid):
        self.received.append((share_code, receive_code, target_cid))
        return {"title": "示例电影", "file_ids": ["fid-source"]}


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

    def test_self_share_update_receives_115_share_without_cms_plain_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
            )

            row = submission_store.recent(limit=1)[0]
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [("abc", "1234", "pending-cid")])
            self.assertEqual(row["status"], "received")
            self.assertEqual(row["workflow_mode"], "self_share_sync")
            self.assertEqual(row["workflow_phase"], "received_to_pending")
            self.assertEqual(row["title"], "示例电影")
            self.assertIn("已接收", telegram.messages[-1][1])

    def test_task_engine_self_share_intake_enqueues_without_receiving_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            poll_calls = []
            with patch.object(bridge, "start_status_poll", lambda *args, **kwargs: poll_calls.append((args, kwargs))):
                bridge.handle_update(
                    self.update("https://115cdn.com/s/abc?password=1234"),
                    cms,
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=True,
                    task_store=task_store,
                    self_share_workflow=object(),
                    cleanup_client=p115,
                    self_share_receive_cid="pending-cid",
                    task_engine_enabled=True,
                )

            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertEqual(poll_calls, [])
            self.assertIsNone(submission_store.find_by_key(bridge.ShareKey("abc", "1234")))
            tasks = task_store.list_recent_tasks(limit=10)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].current_stage, TaskStage.RECEIVED)
            self.assertEqual(tasks[0].status, TaskStatus.PENDING)
            self.assertIn("任务", telegram.messages[-1][1])

    def test_task_engine_self_share_without_taskstore_does_not_fallback_to_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()
            poll_calls = []

            with patch.object(bridge, "start_status_poll", lambda *args, **kwargs: poll_calls.append((args, kwargs))):
                bridge.handle_update(
                    self.update("https://115cdn.com/s/abc?password=1234"),
                    cms,
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=True,
                    task_store=None,
                    self_share_workflow=object(),
                    cleanup_client=p115,
                    self_share_receive_cid="pending-cid",
                    task_engine_enabled=True,
                )

            row = submission_store.find_by_key(bridge.ShareKey("abc", "1234"))
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertEqual(poll_calls, [])
            self.assertEqual(row["status"], "failed")
            self.assertIn("TaskStore", row["last_error"])
            self.assertIn("失败", telegram.messages[-1][1])

    def test_task_engine_disabled_self_share_still_allows_legacy_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()
            poll_calls = []

            with patch.object(bridge, "start_status_poll", lambda *args, **kwargs: poll_calls.append((args, kwargs))):
                bridge.handle_update(
                    self.update("https://115cdn.com/s/abc?password=1234"),
                    cms,
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=True,
                    task_store=None,
                    self_share_workflow=object(),
                    cleanup_client=p115,
                    self_share_receive_cid="pending-cid",
                    task_engine_enabled=False,
                )

            row = submission_store.find_by_key(bridge.ShareKey("abc", "1234"))
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [("abc", "1234", "pending-cid")])
            self.assertEqual(len(poll_calls), 1)
            self.assertEqual(row["status"], "received")
            self.assertIn("已接收", telegram.messages[-1][1])

    def test_task_engine_duplicate_running_link_reports_current_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "CMS 整理中")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
                task_engine_enabled=True,
            )

            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertIn("CMS 整理", telegram.messages[-1][1])

    def test_task_engine_duplicate_completed_link_requeues_when_dest_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "任务完成",
                metadata_patch={"dest_path": str(Path(tmp) / "missing" / "movie-folder")},
            )
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
                task_engine_enabled=True,
            )

            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=9999999999.0)
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(claimed.id, task.id)
            self.assertNotIn("任务已完成", telegram.messages[-1][1])
            self.assertIn("重新检查", telegram.messages[-1][1])


    def test_status_command_prefers_taskstore_when_authoritative_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            submission_store.upsert_submission(
                bridge.ShareKey("old", ""),
                "https://115cdn.com/s/old",
                "submitted",
                title="旧兼容记录",
            )
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "等待自有分享 STRM 源目录生成",
                title="新任务电影",
                error_summary="未找到 STRM",
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/status"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            message = telegram.messages[-1][1]
            self.assertIn("TaskStore 最近任务", message)
            self.assertIn("#1 新任务电影", message)
            self.assertIn("STRM 生成", message)
            self.assertIn("failed", message)
            self.assertIn("未找到 STRM", message)
            self.assertNotIn("旧兼容记录", message)

    def test_status_command_shows_taskstore_wait_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM",
                title="等待电影",
                metadata_patch={"_defer_message": "等待自有分享 STRM", "_defer_count": 2},
                next_run_at=9999999999.0,
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/status"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            message = telegram.messages[-1][1]
            self.assertIn("等待自有分享 STRM", message)
            self.assertIn("第 2 次", message)

    def test_status_command_shows_slowness_and_p115_call_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.EMBY_CONFIRMED,
                TaskStatus.RUNNING,
                "等待 Emby 扫描入库",
                title="等待电影",
                metadata_patch={
                    "_defer_message": "等待 Emby 扫描入库",
                    "_defer_count": 2,
                    "stage_elapsed_seconds": 4.0,
                    "stage_wait_seconds": 20.0,
                    "p115_stage_request_count": 0,
                    "p115_total_request_count": 5,
                },
                next_run_at=9999999999.0,
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/status"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            message = telegram.messages[-1][1]
            self.assertIn("为什么慢：等 Emby 入库", message)
            self.assertIn("执行 4 秒", message)
            self.assertIn("排队/等待 20 秒", message)
            self.assertIn("115调用 本阶段0次/累计5次", message)

    def test_status_command_truncates_long_taskstore_wait_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            long_reason = "等待自有分享 STRM" + "B" * 260
            task_store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                long_reason,
                title="等待电影",
                metadata_patch={"_defer_message": long_reason, "_defer_count": 2},
                next_run_at=9999999999.0,
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/status"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            message = telegram.messages[-1][1]
            wait_lines = [line for line in message.splitlines() if "等待：" in line]
            self.assertEqual(len(wait_lines), 1)
            self.assertIn("等待自有分享 STRM", wait_lines[0])
            self.assertIn("第 2 次", wait_lines[0])
            self.assertIn("下次检查", wait_lines[0])
            self.assertIn("...", wait_lines[0])
            self.assertNotIn("B" * 160, wait_lines[0])
            self.assertLessEqual(len(wait_lines[0]), 230)

    def test_status_command_truncates_long_taskstore_titles_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            title_prefix = "长标题"
            error_prefix = "错误摘要"

            for idx in range(8):
                task = task_store.upsert_task(
                    f"code{idx}",
                    "1234",
                    f"https://115cdn.com/s/code{idx}?password=1234",
                    chat_id="464100862",
                )
                task_store.record_event(
                    task.id,
                    TaskStage.STRM_READY,
                    TaskStatus.FAILED,
                    "STRM missing",
                    title=f"{title_prefix}{idx}-" + "A" * 300,
                    error_summary=f"{error_prefix}{idx}-" + "B" * 300,
                )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/status"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            message = telegram.messages[-1][1]
            task_lines = [line for line in message.splitlines() if line.startswith(tuple(f"{idx}." for idx in range(1, 9)))]
            self.assertEqual(len(task_lines), 8)
            self.assertIn(title_prefix, message)
            self.assertIn(error_prefix, message)
            self.assertIn("...", message)
            self.assertNotIn("A" * 160, message)
            self.assertNotIn("B" * 160, message)
            self.assertLess(len(message), 2500)
            for line in task_lines:
                self.assertLessEqual(len(line), 260)

    def test_history_command_uses_taskstore_then_submission_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            submission_store.upsert_submission(
                bridge.ShareKey("old", ""),
                "https://115cdn.com/s/old",
                "submitted",
                title="旧兼容记录",
            )
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "", "https://115cdn.com/s/abc", chat_id="464100862")
            task_store.record_event(task.id, TaskStage.MOVED, TaskStatus.SUCCEEDED, "已移动", title="新任务电影")
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/history"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )
            taskstore_message = telegram.messages[-1][1]

            empty_task_store = TaskStore(Path(tmp) / "empty-tasks.db")
            bridge.handle_update(
                self.update("/history"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=empty_task_store,
                task_engine_enabled=True,
            )
            fallback_message = telegram.messages[-1][1]

            self.assertIn("TaskStore 最近历史", taskstore_message)
            self.assertIn("新任务电影", taskstore_message)
            self.assertNotIn("旧兼容记录", taskstore_message)
            self.assertIn("最近历史", fallback_message)
            self.assertIn("旧兼容记录", fallback_message)


    def test_status_command_includes_task_action_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM missing",
                title="按钮电影",
                error_summary="未找到 STRM",
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/status"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            reply_markup = telegram.messages[-1][2]
            buttons = [button for row in reply_markup["inline_keyboard"] for button in row]
            self.assertIn({"text": "详情 #1", "callback_data": "task_detail:1"}, buttons)
            self.assertIn({"text": "重试 #1", "callback_data": "task_retry:1"}, buttons)
            self.assertIn({"text": "查 Emby #1", "callback_data": "task_emby:1"}, buttons)
            self.assertIn({"text": "恢复 STRM #1", "callback_data": "task_restore:1"}, buttons)
            self.assertIn({"text": "从头重跑 #1", "callback_data": "task_reprocess:1"}, buttons)

    def test_start_series_update_task_requeues_completed_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "completed",
                title="追更剧集",
            )
            row = submission_store.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            task = task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "清理完成",
                category="外国电视",
                submission_id=int(row["id"]),
            )

            updated, result = bridge.start_series_update_task(task, submission_store, task_store, source="文本追更")

            self.assertEqual(result, "started")
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.metadata["update_requested_run"], 1)
            self.assertEqual(updated.metadata["update_received_run"], 0)

    def test_explicit_series_update_command_requeues_completed_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "completed",
                title="追更剧集",
            )
            row = submission_store.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "清理完成",
                category="外国电视",
                submission_id=int(row["id"]),
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("追更 https://115cdn.com/s/abc?password=1234"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            updated = task_store.find_task(task.id)
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.metadata["update_requested_run"], 1)
            self.assertIn("已开始追更", telegram.messages[-1][1])

    def test_explicit_series_update_command_falls_back_to_new_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("追更 https://115cdn.com/s/new?password=1234"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            task = task_store.find_task_by_share_key("new", "1234")
            self.assertIsNotNone(task)
            self.assertEqual(task.current_stage, TaskStage.RECEIVED)
            self.assertEqual(task.status, TaskStatus.PENDING)

    def test_explicit_series_update_command_keeps_completed_movie_in_normal_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("movie", "1234", "https://115cdn.com/s/movie?password=1234")
            task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "清理完成",
                category="欧美电影",
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("追更 https://115cdn.com/s/movie?password=1234"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            unchanged = task_store.find_task(task.id)
            self.assertEqual(unchanged.current_stage, TaskStage.CLEANED)
            self.assertEqual(unchanged.status, TaskStatus.SUCCEEDED)
            self.assertIn("任务已完成", telegram.messages[-1][1])

    def test_completed_tv_task_exposes_update_button_and_resets_for_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "completed",
                title="追更剧集",
            )
            recognition = {"title": "追更剧集", "tmdb_id": "1416", "category": "外国电视", "type": "tv"}
            row = submission_store.update_recognition(int(row["id"]), recognition, "selected")
            row = submission_store.update_category(int(row["id"]), "外国电视", "selected")
            row = submission_store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="share_sync_submitted",
                own_share_file_id="old-folder",
                own_share_file_name="J-追更剧集-2026-[tmdb=1416]",
                own_share_code="old-share",
                own_share_receive_code="1212",
                own_share_url="https://115cdn.com/s/old-share?password=1212",
                share_sync_status="submitted",
            )
            row = submission_store.update_move(
                int(row["id"]),
                "moved",
                source_path="/strm/share/J-追更剧集",
                dest_path="/strm/TV/J-追更剧集",
                category_final="外国电视",
            )
            row = submission_store.update_emby(int(row["id"]), "confirmed", item_id="emby-id", title="追更剧集", path="/strm/TV/J-追更剧集", parent="剧集")
            row = submission_store.update_cleanup(int(row["id"]), "deleted", file_id="old-folder")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "清理完成",
                title="追更剧集",
                tmdb_id="1416",
                category="外国电视",
                metadata_patch={
                    "submission_id": int(row["id"]),
                    "own_share_code": "old-share",
                    "dest_path": "/strm/TV/J-追更剧集",
                },
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/status"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            buttons = [button for button_row in telegram.messages[-1][2]["inline_keyboard"] for button in button_row]
            self.assertIn({"text": f"追更 #{task.id}", "callback_data": f"task_update:{task.id}"}, buttons)

            bridge.handle_update(
                {
                    "callback_query": {
                        "id": "task-update-1",
                        "from": {"id": 464100862},
                        "message": {"chat": {"id": 464100862}},
                        "data": f"task_update:{task.id}",
                    }
                },
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            updated_task = task_store.find_task(task.id)
            updated_row = submission_store.find_by_id(int(row["id"]))
            self.assertEqual(updated_task.status, TaskStatus.PENDING)
            self.assertEqual(updated_task.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated_task.metadata["update_requested_run"], 1)
            self.assertEqual(updated_task.metadata["update_received_run"], 0)
            self.assertNotIn("own_share_code", updated_task.metadata)
            self.assertEqual(updated_row["workflow_phase"], "update_requested")
            self.assertEqual(updated_row["category_choice"], "外国电视")
            self.assertEqual(updated_row["recognition_json"], json.dumps(recognition, ensure_ascii=False, sort_keys=True))
            self.assertIsNone(updated_row["own_share_code"])
            self.assertIsNone(updated_row["move_status"])
            self.assertIsNone(updated_row["emby_status"])
            self.assertIsNone(updated_row["cleanup_status"])
            self.assertEqual(telegram.answers[-1][1], "已开始追更")
            self.assertIn("已开始追更", telegram.messages[-1][1])

    def test_task_retry_callback_requeues_failed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", error_summary="未找到 STRM")
            telegram = FakeTelegram()

            bridge.handle_update(
                {
                    "callback_query": {
                        "id": "task-retry-1",
                        "from": {"id": 464100862},
                        "message": {"chat": {"id": 464100862}},
                        "data": f"task_retry:{task.id}",
                    }
                },
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=0)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.STRM_READY)
            self.assertEqual(updated.retry_count, 1)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(telegram.answers[-1][1], "已重新入队")
            self.assertIn("已重新入队", telegram.messages[-1][1])

    def test_task_reprocess_callback_requeues_from_received_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "cleanup complete",
                title="重跑电影",
                metadata_patch={"own_share_code": "ownabc"},
            )
            task_store.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=1.0)
            task_store.claim_next_runnable("stale-worker", now=1.0)
            telegram = FakeTelegram()

            bridge.handle_update(
                {
                    "callback_query": {
                        "id": "task-reprocess-1",
                        "from": {"id": 464100862},
                        "message": {"chat": {"id": 464100862}},
                        "data": f"task_reprocess:{task.id}",
                    }
                },
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=0)
            events = task_store.list_events(task.id)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.next_run_at, 0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.retry_count, 1)
            self.assertEqual(updated.metadata["retry_from_stage"], TaskStage.CLEANED.value)
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.RECEIVED.value)
            self.assertTrue(updated.metadata["force_reprocess"])
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.RECEIVED)
            self.assertIn("TG 按钮触发从头重跑", [event["message"] for event in events])
            self.assertEqual(telegram.answers[-1][1], "已从头重跑")
            self.assertIn("已从头重跑", telegram.messages[-1][1])

    def test_task_emby_and_restore_callbacks_enqueue_target_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            emby_task = task_store.upsert_task("emby", "", "https://115cdn.com/s/emby", chat_id="464100862")
            task_store.record_event(emby_task.id, TaskStage.MOVED, TaskStatus.SUCCEEDED, "moved")
            restore_task = task_store.upsert_task("restore", "", "https://115cdn.com/s/restore", chat_id="464100862")
            task_store.record_event(restore_task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done", metadata_patch={"dest_path": str(Path(tmp) / "missing")})
            telegram = FakeTelegram()

            for callback_id, data in (("emby", f"task_emby:{emby_task.id}"), ("restore", f"task_restore:{restore_task.id}")):
                bridge.handle_update(
                    {
                        "callback_query": {
                            "id": callback_id,
                            "from": {"id": 464100862},
                            "message": {"chat": {"id": 464100862}},
                            "data": data,
                        }
                    },
                    FakeCmsSubmit(),
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=False,
                    task_store=task_store,
                    task_engine_enabled=True,
                )

            updated_emby = task_store.find_task(emby_task.id)
            updated_restore = task_store.find_task(restore_task.id)
            self.assertEqual(updated_emby.status, TaskStatus.PENDING)
            self.assertEqual(updated_emby.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(updated_restore.status, TaskStatus.PENDING)
            self.assertEqual(updated_restore.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertIn("已加入 Emby 检查队列", telegram.messages[-2][1])
            self.assertIn("已加入 STRM 恢复队列", telegram.messages[-1][1])

    def test_health_command_includes_taskstore_queue_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submission_store = bridge.SubmissionStore(root / "submissions.db")
            task_store = TaskStore(root / "tasks.db")
            pending = task_store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            task_store.enqueue_task(pending.id, TaskStage.RECEIVED, next_run_at=0)
            running = task_store.upsert_task("running", "", "https://115cdn.com/s/running")
            task_store.enqueue_task(running.id, TaskStage.ORGANIZING, next_run_at=0)
            task_store.claim_next_runnable("worker", now=0)
            failed = task_store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            task_store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", title="失败电影", error_summary="未找到 STRM")
            move_config = bridge.MoveConfig(source_roots=[root], library_roots={"测试": root}, stable_seconds=0)
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("/health"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
                move_config=move_config,
            )

            message = telegram.messages[-1][1]
            self.assertIn("TaskEngine: ENABLED", message)
            self.assertIn("TaskStore最近任务: 3", message)
            self.assertIn("待执行: 1", message)
            self.assertIn("运行中: 1", message)
            self.assertIn("失败/需处理: 1", message)
            self.assertIn("最近问题: #3 失败电影", message)

    def test_category_callback_requeues_authoritative_recognizing_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row = submission_store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
                title="Suggest.Show.S01.2025",
            )
            task = task_store.upsert_task("abc", "1234", row["url"], chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.RECOGNIZING,
                TaskStatus.NEEDS_ACTION,
                "等待人工确认分类",
                submission_id=int(row["id"]),
            )
            telegram = FakeTelegram()
            update = {
                "callback_query": {
                    "id": "callback-1",
                    "from": {"id": 464100862},
                    "message": {"chat": {"id": 464100862}},
                    "data": f"cat:{row['id']}:cn_movie",
                }
            }

            bridge.handle_update(update, object(), telegram, "464100862", submission_store, task_store=task_store)

            stored_row = submission_store.find_by_id(int(row["id"]))
            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=9999999999.0)
            self.assertEqual(stored_row["category_choice"], "华语电影")
            self.assertEqual(stored_row["category_status"], "selected")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(telegram.answers[-1][1], "已记录分类：华语电影")

    def test_category_callback_remembers_organized_parent_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row = submission_store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
                title="太行谣 (2026) {tmdb-323682}",
            )
            recognition = {
                "title": "T-太行谣-2026-[tmdb=323682]",
                "organized_parent_id": "parent-tvcn",
                "parent_id": "parent-tvcn",
                "category_status": "needs_action",
            }
            row = submission_store.update_recognition(int(row["id"]), recognition, "needs_action") or row
            task = task_store.upsert_task("abc", "1234", row["url"], chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.RECOGNIZING,
                TaskStatus.NEEDS_ACTION,
                "等待人工确认分类",
                submission_id=int(row["id"]),
                metadata_patch={"submission_id": int(row["id"])},
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                {
                    "callback_query": {
                        "id": "callback-remember",
                        "from": {"id": 464100862},
                        "message": {"chat": {"id": 464100862}},
                        "data": f"cat:{row['id']}:cn_tv",
                    }
                },
                object(),
                telegram,
                "464100862",
                submission_store,
                task_store=task_store,
            )

            remembered = submission_store.category_for_parent_id("parent-tvcn")
            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=9999999999.0)

            self.assertEqual(remembered, "国产电视")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(telegram.answers[-1][1], "已记录分类：国产电视")

    def test_category_callback_skip_marks_authoritative_task_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row = submission_store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
                title="Suggest.Show.S01.2025",
            )
            task = task_store.upsert_task("abc", "1234", row["url"], chat_id="464100862")
            task_store.record_event(
                task.id,
                TaskStage.RECOGNIZING,
                TaskStatus.NEEDS_ACTION,
                "等待人工确认分类",
                submission_id=int(row["id"]),
                metadata_patch={"submission_id": int(row["id"])},
            )
            telegram = FakeTelegram()
            update = {
                "callback_query": {
                    "id": "callback-skip",
                    "from": {"id": 464100862},
                    "message": {"chat": {"id": 464100862}},
                    "data": f"cat:{row['id']}:skip",
                }
            }

            bridge.handle_update(update, object(), telegram, "464100862", submission_store, task_store=task_store)

            stored_row = submission_store.find_by_id(int(row["id"]))
            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=9999999999.0)
            events = task_store.list_events(task.id)
            self.assertIsNone(stored_row["category_choice"])
            self.assertEqual(stored_row["category_status"], "skipped")
            self.assertEqual(updated.status, TaskStatus.FAILED)
            self.assertEqual(updated.current_stage, TaskStage.FAILED)
            self.assertEqual(updated.error_type, "category_skipped")
            self.assertEqual(updated.error_summary, "已跳过分类，任务停止")
            self.assertEqual(updated.metadata["submission_id"], int(row["id"]))
            self.assertIsNone(claimed)
            self.assertIn("已跳过分类，任务停止", [event["message"] for event in events])
            self.assertEqual(telegram.answers[-1][1], "已记录分类：跳过")

    def test_task_engine_requeues_sentinel_needs_action_to_claimable_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task = task_store.record_event(
                task.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                "等待人工处理",
                metadata_patch={"retry_stage": TaskStage.RECOGNIZING.value},
            )
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
                task_engine_enabled=True,
            )

            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=9999999999.0)
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.RECOGNIZING)

    def test_task_engine_requeue_clears_stale_defer_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task = task_store.record_event(
                task.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                "CMS 整理等待超时",
                error_type="organizing_timeout",
                error_summary="CMS 整理等待超时",
                metadata_patch={
                    "_defer_stage": TaskStage.ORGANIZING.value,
                    "_defer_message": "等待 CMS 整理完成",
                    "_defer_count": 31,
                    "retry_stage": TaskStage.ORGANIZING.value,
                },
            )
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
                task_engine_enabled=True,
            )

            updated = task_store.find_task(task.id)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.ORGANIZING)
            self.assertNotIn("_defer_count", updated.metadata)
            self.assertNotIn("_defer_stage", updated.metadata)
            self.assertNotIn("_defer_message", updated.metadata)

    def test_task_engine_requeues_sentinel_failed_to_received_fallback_when_no_retry_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            task = task_store.record_event(task.id, TaskStage.FAILED, TaskStatus.FAILED, "兼容同步失败")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
                task_engine_enabled=True,
            )

            updated = task_store.find_task(task.id)
            claimed = task_store.claim_next_runnable("worker", now=9999999999.0)
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.RECEIVED)

    def test_duplicate_self_share_received_link_does_not_receive_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()
            update = self.update("https://115cdn.com/s/abc?password=1234")

            for _ in range(2):
                bridge.handle_update(
                    update,
                    cms,
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=False,
                    self_share_workflow=object(),
                    cleanup_client=p115,
                    self_share_receive_cid="pending-cid",
                )

            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [("abc", "1234", "pending-cid")])
            self.assertIn("已存在", telegram.messages[-1][1])

    def test_duplicate_self_share_numeric_completed_status_does_not_receive_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            key = bridge.ShareKey("abc", "1234")
            row = submission_store.upsert_submission(key, "https://115cdn.com/s/abc?password=1234", "1", title="已完成影片")
            submission_store.update_self_share(row["id"], workflow_mode="self_share_sync", workflow_phase="share_sync_submitted")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
            )

            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [])
            self.assertIn("已存在", telegram.messages[-1][1])

    def test_self_share_reprocesses_legacy_plain_submitted_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            key = bridge.ShareKey("abc", "1234")
            submission_store.upsert_submission(key, "https://115cdn.com/s/abc?password=1234", "submitted")
            cms = FakeCmsSubmit()
            telegram = FakeTelegram()
            p115 = FakeP115Receive()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc?password=1234"),
                cms,
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                self_share_workflow=object(),
                cleanup_client=p115,
                self_share_receive_cid="pending-cid",
            )

            row = submission_store.find_by_key(key)
            self.assertEqual(cms.submitted, [])
            self.assertEqual(p115.received, [("abc", "1234", "pending-cid")])
            self.assertEqual(row["status"], "received")
            self.assertEqual(row["workflow_mode"], "self_share_sync")
            self.assertIn("已接收", telegram.messages[-1][1])


if __name__ == "__main__":
    unittest.main()
