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

    def test_config_reads_task_engine_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            cfg = bridge.Config.from_env()

            self.assertTrue(cfg.task_engine_enabled)

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
                     patch.object(bridge, "write_metrics_snapshot", lambda *args, **kwargs: None), \
                     patch.object(bridge, "normalize_emby_parents", lambda *args, **kwargs: 0), \
                     patch.object(bridge, "TaskRunner", FakeTaskRunner):
                    with self.assertRaises(KeyboardInterrupt):
                        bridge.run_forever(cfg)

                self.assertEqual(task_runner_started, [True])
                self.assertEqual(repair_calls, [])

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
            tasks = task_store.list_recent_tasks(limit=10)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].current_stage, TaskStage.RECEIVED)
            self.assertEqual(tasks[0].status, TaskStatus.PENDING)
            self.assertIn("任务", telegram.messages[-1][1])

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
