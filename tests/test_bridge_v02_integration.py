import json
import os
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace
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

    def test_config_normalizes_invalid_task_max_retries(self):
        for raw_value in ("0", "-1", "not-a-number"):
            with self.subTest(raw_value=raw_value), tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ,
                {**self.required_env(tmp), "TASK_MAX_RETRIES": raw_value},
                clear=True,
            ):
                self.assertEqual(bridge.Config.from_env().task_max_retries, 3)

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

    def test_maybe_start_web_server_allows_loopback_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["WEB_TOKEN"] = ""
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)
                calls = []

                def fake_start(store, host, port, web_token="", task_engine_enabled=None):
                    calls.append((store, host, port, web_token, task_engine_enabled))
                    return "server"

                server = bridge.maybe_start_web_server(cfg, task_store, starter=fake_start)

                self.assertEqual(server, "server")
                self.assertEqual(calls, [(task_store, "127.0.0.1", 8787, "", True)])

    def test_maybe_start_web_server_refuses_public_bind_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["WEB_TOKEN"] = ""
            env["WEB_HOST"] = "0.0.0.0"
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)

                with self.assertRaises(RuntimeError):
                    bridge.maybe_start_web_server(cfg, task_store, starter=lambda *args, **kwargs: "server")

    def test_maybe_start_web_server_accepts_username_password_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["WEB_TOKEN"] = ""
            env["WEB_USERNAME"] = "admin"
            env["WEB_PASSWORD"] = "secret"
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)
                calls = []

                def fake_start(store, host, port, **kwargs):
                    calls.append(kwargs)
                    return "server"

                server = bridge.maybe_start_web_server(cfg, task_store, starter=fake_start)

                self.assertEqual(server, "server")
                self.assertEqual(calls[0]["web_username"], "admin")
                self.assertEqual(calls[0]["web_password"], "secret")
                self.assertEqual(calls[0]["web_token"], "")

    def test_maybe_start_web_server_rejects_token_and_username_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["WEB_USERNAME"] = "admin"
            env["WEB_PASSWORD"] = "secret"
            with patch.dict(os.environ, env, clear=True):
                cfg = bridge.Config.from_env()
                task_store = bridge.create_task_store(cfg)

                with self.assertRaises(RuntimeError):
                    bridge.maybe_start_web_server(cfg, task_store, starter=lambda *args, **kwargs: "server")

    def test_maybe_start_web_server_passes_log_hub_only_to_supporting_starter(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            with patch.dict(os.environ, env, clear=True):
                config = bridge.Config.from_env()
                store = bridge.create_task_store(config)
                hub = object()
                calls = []

                def modern_starter(task_store, host, port, **kwargs):
                    calls.append((task_store, host, port, kwargs))
                    return "modern"

                result = bridge.maybe_start_web_server(config, store, starter=modern_starter, log_hub=hub)

            self.assertEqual(result, "modern")
            self.assertIs(calls[0][3]["log_hub"], hub)

    def test_maybe_start_web_server_does_not_break_legacy_starter_without_log_hub(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            with patch.dict(os.environ, env, clear=True):
                config = bridge.Config.from_env()
                store = bridge.create_task_store(config)

                def legacy_starter(task_store, host, port, web_token="", task_engine_enabled=None):
                    return (task_store, host, port, web_token, task_engine_enabled)

                result = bridge.maybe_start_web_server(config, store, starter=legacy_starter, log_hub=object())

            self.assertEqual(result[1:], ("127.0.0.1", 8787, "secret", True))

    def test_call_maybe_start_web_server_passes_only_log_hub_to_supporting_callee(self):
        config = object()
        task_store = object()
        hub = object()

        def log_hub_only(actual_config, actual_task_store, *, log_hub):
            self.assertIs(actual_config, config)
            self.assertIs(actual_task_store, task_store)
            self.assertIs(log_hub, hub)
            return "log-hub-only"

        with patch.object(bridge, "maybe_start_web_server", log_hub_only):
            result = bridge.call_maybe_start_web_server(
                config,
                task_store,
                submission_store=object(),
                quality_automation=object(),
                hdhive_service=object(),
                hdhive_scheduler=object(),
                frontend_dist_path="/tmp/frontend",
                background_jobs=object(),
                log_hub=hub,
            )

        self.assertEqual(result, "log-hub-only")

    def test_call_maybe_start_web_server_skips_positional_only_log_hub(self):
        config = object()
        task_store = object()

        def positional_only_callee(actual_config, actual_task_store, log_hub=None, /):
            self.assertIs(actual_config, config)
            self.assertIs(actual_task_store, task_store)
            return log_hub

        with patch.object(bridge, "maybe_start_web_server", positional_only_callee):
            result = bridge.call_maybe_start_web_server(config, task_store, log_hub=object())

        self.assertIsNone(result)

    def test_call_maybe_start_web_server_skips_positional_only_log_hub_even_with_var_keywords(self):
        config = object()
        task_store = object()

        def positional_only_callee(actual_config, actual_task_store, log_hub=None, /, **kwargs):
            self.assertIs(actual_config, config)
            self.assertIs(actual_task_store, task_store)
            return log_hub, kwargs.get("log_hub")

        with patch.object(bridge, "maybe_start_web_server", positional_only_callee):
            result = bridge.call_maybe_start_web_server(config, task_store, log_hub=object())

        self.assertEqual(result, (None, None))

    def test_call_maybe_start_web_server_keeps_two_argument_legacy_callee(self):
        config = object()
        task_store = object()
        calls = []

        def legacy_callee(actual_config, actual_task_store):
            calls.append((actual_config, actual_task_store))
            return "legacy"

        with patch.object(bridge, "maybe_start_web_server", legacy_callee):
            result = bridge.call_maybe_start_web_server(
                config,
                task_store,
                submission_store=object(),
                quality_automation=object(),
                hdhive_service=object(),
                hdhive_scheduler=object(),
                frontend_dist_path="/tmp/frontend",
                background_jobs=object(),
                log_hub=object(),
            )

        self.assertEqual(result, "legacy")
        self.assertEqual(calls, [(config, task_store)])

    def test_main_configures_logging_once_and_injects_hub_into_runtime(self):
        runtime = SimpleNamespace(hub=object())
        config = SimpleNamespace()
        with patch.object(bridge, "configure_logging", return_value=runtime) as configure, patch.object(
            bridge.Config, "from_env", return_value=config
        ), patch.object(bridge, "run_forever") as run, patch.object(bridge.signal, "signal"):
            exit_code = bridge.main()

        self.assertEqual(exit_code, 0)
        configure.assert_called_once_with(
            os.environ.get("LOG_LEVEL", "INFO"),
            rate_limit=False,
        )
        self.assertIs(run.call_args.kwargs["log_hub"], runtime.hub)

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
                self.assertEqual(len(maintenance_calls), 1)
                self.assertEqual(
                    maintenance_calls[0][1]["interval_seconds"],
                    cfg.status_repair_interval_seconds,
                )
                self.assertEqual(maintenance_calls[0][1]["limit"], cfg.status_repair_limit)

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
        self.rich_messages = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return {"ok": True}

    def send_rich_message(self, chat_id, document, reply_markup=None):
        self.rich_messages.append((chat_id, document, reply_markup))
        self.messages.append((chat_id, document.to_plain(), reply_markup))

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


class FailingCmsSubmit(FakeCmsSubmit):
    def add_share_down(self, link):
        raise RuntimeError("CMS unavailable")


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

    def make_completed_target(
        self,
        submission_store,
        task_store,
        *,
        share_code="old",
        category="国产电视",
        media_type="tv",
        tmdb_id="273114",
        stage=TaskStage.CLEANED,
        status=TaskStatus.SUCCEEDED,
    ):
        recognition = {
            "ok": True,
            "title": f"X-悬案-2026-[tmdb={tmdb_id}]",
            "tmdb_id": tmdb_id,
            "type": media_type,
            "category": category,
        }
        row = submission_store.upsert_submission(
            bridge.ShareKey(share_code, "1212"),
            f"https://115cdn.com/s/{share_code}?password=1212",
            "completed",
            title=recognition["title"],
        )
        row = submission_store.update_recognition(int(row["id"]), recognition, "selected")
        row = submission_store.update_category(int(row["id"]), category, "selected")
        row = submission_store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="cleanup_completed",
            own_share_file_id="old-tv-folder",
            own_share_file_name=recognition["title"],
            own_share_code="old-share",
            own_share_receive_code="1212",
            own_share_url="https://115cdn.com/s/old-share?password=1212",
            share_sync_status="submitted",
        )
        task = task_store.upsert_task(
            share_code,
            "1212",
            f"https://115cdn.com/s/{share_code}?password=1212",
            chat_id="464100862",
        )
        task = task_store.record_event(
            task.id,
            stage,
            status,
            "目标任务状态",
            title=recognition["title"],
            tmdb_id=tmdb_id,
            category=category,
            submission_id=int(row["id"]),
            metadata_patch={"submission_id": int(row["id"]), "own_share_code": "old-share"},
        )
        return row, task, recognition

    def make_existing_source(
        self,
        submission_store,
        task_store,
        *,
        parent_task_id=None,
        task_own_share_code="",
        submission_own_share_code="",
        claimed=False,
    ):
        wrong_folder_id = "3481694900213253783"
        row = submission_store.upsert_submission(
            bridge.ShareKey("new", "1212"),
            "https://115cdn.com/s/new?password=1212",
            "running",
            title="错误电影 (2025)",
        )
        row = submission_store.update_recognition(
            int(row["id"]),
            {"title": "错误电影 (2025)", "tmdb_id": "999", "type": "movie", "category": "欧美电影"},
            "selected",
        )
        row = submission_store.update_category(int(row["id"]), "欧美电影", "selected")
        row = submission_store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="share_creating",
            own_share_file_id=wrong_folder_id,
            own_share_file_name="错误电影 (2025)",
            own_share_code=submission_own_share_code or None,
            share_sync_status="creating",
        )
        task = task_store.upsert_task(
            "new",
            "1212",
            "https://115cdn.com/s/new?password=1212",
            chat_id="464100862",
        )
        metadata = {
            "submission_id": int(row["id"]),
            "own_share_file_id": wrong_folder_id,
            "share_create_status": "creating",
            "recognition": {"tmdb_id": "999", "type": "movie", "category": "欧美电影"},
        }
        if parent_task_id is not None:
            metadata["series_update_parent_task_id"] = int(parent_task_id)
        if task_own_share_code:
            metadata["own_share_code"] = task_own_share_code
        task = task_store.record_event(
            task.id,
            TaskStage.OWN_SHARE_CREATED,
            TaskStatus.RUNNING,
            "生产残留任务",
            title="错误电影 (2025)",
            tmdb_id="999",
            category="欧美电影",
            submission_id=int(row["id"]),
            metadata_patch=metadata,
        )
        if claimed:
            task = task_store.compare_and_set_transition(
                task.id,
                TaskStage.OWN_SHARE_CREATED,
                {TaskStatus.RUNNING},
                require_unclaimed=True,
                target_stage=TaskStage.OWN_SHARE_CREATED,
                target_status=TaskStatus.RUNNING,
                target_event_message="生产任务已认领",
                claim_by="worker-338",
                expected_updated_at=task.updated_at,
            )
        return row, task

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

    def test_cms_submit_exception_records_failure_without_retry_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("https://115cdn.com/s/abc"),
                FailingCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
            )

            task = task_store.find_task_by_share_key("abc", "")
            events = task_store.list_events(task.id)
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error_type, "cms_submit_failed")
            self.assertEqual(task.retry_count, 0)
            self.assertEqual([event["stage"] for event in events], ["received", "cms_submitted"])

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

            self.assertEqual(len(telegram.rich_messages), 1)
            message = telegram.rich_messages[-1][1].to_plain()
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

            self.assertEqual(len(telegram.rich_messages), 1)
            message = telegram.rich_messages[-1][1].to_plain()
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

            self.assertEqual(len(telegram.rich_messages), 1)
            message = telegram.rich_messages[-1][1].to_plain()
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

            self.assertEqual(len(telegram.rich_messages), 1)
            message = telegram.rich_messages[-1][1].to_plain()
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

            self.assertEqual(len(telegram.rich_messages), 1)
            message = telegram.rich_messages[-1][1].to_plain()
            task_lines = [line for line in message.splitlines() if " | " in line and title_prefix in line]
            self.assertEqual(len(task_lines), 8)
            self.assertIn(title_prefix, message)
            self.assertIn(error_prefix, message)
            self.assertIn("...", message)
            self.assertNotIn("A" * 160, message)
            self.assertNotIn("B" * 160, message)
            self.assertLess(len(message), 3000)
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
            self.assertEqual(len(telegram.rich_messages), 1)
            taskstore_message = telegram.rich_messages[-1][1].to_plain()

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
            self.assertEqual(len(telegram.rich_messages), 2)
            fallback_message = telegram.rich_messages[-1][1].to_plain()

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

            self.assertEqual(len(telegram.rich_messages), 1)
            self.assertIn("TaskStore 最近任务", telegram.rich_messages[-1][1].to_plain())
            reply_markup = telegram.rich_messages[-1][2]
            buttons = [button for row in reply_markup["inline_keyboard"] for button in row]
            self.assertIn({"text": "详情 #1", "callback_data": "task_detail:1"}, buttons)
            self.assertIn({"text": "重试 #1", "callback_data": "task_retry:1"}, buttons)
            self.assertIn({"text": "从头重跑 #1", "callback_data": "task_reprocess:1"}, buttons)
            self.assertNotIn({"text": "查 Emby #1", "callback_data": "task_emby:1"}, buttons)
            self.assertNotIn({"text": "恢复 STRM #1", "callback_data": "task_restore:1"}, buttons)

    def test_task_detail_callback_sends_rich_document_with_recent_events_and_keyboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("detail", "", "https://115cdn.com/s/detail", chat_id="464100862")
            secret_url = "https://evil.test/event?password=event-password"
            task_store.record_event(
                task.id,
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                f"等待执行，正常诊断 {secret_url} token=event-token",
                title="详情电影",
                error_summary=f"正常任务错误 {secret_url} token=task-token",
            )
            telegram = FakeTelegram()

            handled = bridge.handle_task_action_callback(
                "task_detail",
                task.id,
                "callback-detail",
                "464100862",
                telegram,
                task_store,
                max_retries=3,
            )

            self.assertTrue(handled)
            self.assertEqual(len(telegram.rich_messages), 1)
            document = telegram.rich_messages[-1][1]
            self.assertIn("任务详情 #1", document.to_plain())
            self.assertIn("正常诊断", document.to_plain())
            self.assertNotIn(secret_url, document.to_plain())
            self.assertNotIn("evil.test", document.to_plain())
            self.assertNotIn("event-password", document.to_plain())
            self.assertNotIn("event-token", document.to_plain())
            self.assertNotIn("task-token", document.to_plain())
            self.assertEqual(
                telegram.rich_messages[-1][2],
                bridge.task_action_keyboard([task], max_retries=3, task_store=task_store),
            )

    def test_multi_source_intake_summary_is_rich_and_lists_task_titles_and_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            telegram = FakeTelegram()
            text = (
                "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=第一部"
                " ed2k://|file|第二部.mkv|10|0123456789ABCDEF0123456789ABCDEF|/"
            )

            bridge.handle_update(
                self.update(text),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
                self_share_workflow=object(),
            )

            self.assertEqual(len(telegram.rich_messages), 1)
            document = telegram.rich_messages[-1][1]
            plain = document.to_plain()
            self.assertIn("收到 2 个链接", plain)
            self.assertIn("第一部", plain)
            self.assertIn("第二部", plain)
            self.assertIn("pending", plain)
            self.assertNotIn("magnet:?", plain)
            self.assertNotIn("ed2k://", plain)

    def test_task_intake_success_metadata_is_redacted_and_bounded(self):
        secret_url = "https://evil.test/library?share_code=parent-share&password=parent-password"
        task = SimpleNamespace(
            id=11,
            title="正常任务",
            metadata={
                "emby_parent": f"媒体库 {secret_url} token=parent-token",
                "dest_path": f"/library/{secret_url} token=path-token",
            },
            current_stage=TaskStage.CLEANED,
            status=TaskStatus.SUCCEEDED,
            error_summary="",
        )

        reply = bridge.format_task_intake_reply(task)

        self.assertIn("任务已完成：#11 正常任务", reply)
        for secret in (secret_url, "evil.test", "parent-share", "parent-password", "parent-token", "path-token"):
            self.assertNotIn(secret, reply)
        self.assertLessEqual(len(reply), 600)

    def test_multi_source_intake_summary_redacts_persisted_task_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            telegram = FakeTelegram()
            secret_url = "https://evil.test/intake?share_code=intake-share&password=intake-password"
            with patch.object(bridge, "format_task_intake_reply", return_value=f"收到任务 {secret_url} token=intake-token"):
                bridge.handle_update(
                    self.update("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"),
                    FakeCmsSubmit(),
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=False,
                    task_store=task_store,
                    task_engine_enabled=True,
                    self_share_workflow=object(),
                )

            plain = telegram.rich_messages[-1][1].to_plain()
            self.assertIn("收到 1 个链接", plain)
            self.assertIn("收到任务", plain)
            for secret in (secret_url, "evil.test", "intake-share", "intake-password", "intake-token"):
                self.assertNotIn(secret, plain)

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

    def test_start_series_update_task_reports_reset_exception_accurately(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row, task, _recognition = self.make_completed_target(submission_store, task_store)

            with patch.object(
                submission_store,
                "reset_self_share_for_update",
                side_effect=RuntimeError("submission reset crashed"),
            ):
                updated, result = bridge.start_series_update_task(
                    task,
                    submission_store,
                    task_store,
                    source="文本追更",
                )

            self.assertEqual(result, "failed")
            self.assertIsNotNone(updated)
            self.assertEqual(updated.current_stage, TaskStage.FAILED)
            self.assertEqual(updated.status, TaskStatus.FAILED)
            self.assertEqual(updated.error_type, "submission_reset_failed")
            self.assertEqual(updated.error_summary, "追更准备失败：无法重置原任务提交记录")
            events = task_store.list_events(task.id)
            self.assertIn("submission reset crashed", events[-1]["error_detail"])
            self.assertEqual(submission_store.find_by_id(int(row["id"]))["workflow_phase"], "cleanup_completed")

    def test_start_series_update_task_reports_missing_submission_accurately(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row, task, _recognition = self.make_completed_target(submission_store, task_store)

            with patch.object(
                submission_store,
                "reset_self_share_for_update",
                return_value=None,
            ):
                updated, result = bridge.start_series_update_task(
                    task,
                    submission_store,
                    task_store,
                    source="文本追更",
                )

            self.assertEqual(result, "failed")
            self.assertIsNotNone(updated)
            self.assertEqual(updated.current_stage, TaskStage.FAILED)
            self.assertEqual(updated.status, TaskStatus.FAILED)
            self.assertEqual(updated.error_type, "submission_missing")
            self.assertEqual(updated.error_summary, "追更准备失败：原任务提交记录不存在或已被清理")

    def test_start_series_update_task_rejects_claimed_completed_series_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row, task, _recognition = self.make_completed_target(submission_store, task_store)
            claimed = task_store.compare_and_set_transition(
                task.id,
                TaskStage.CLEANED,
                {TaskStatus.SUCCEEDED},
                require_unclaimed=True,
                target_stage=TaskStage.CLEANED,
                target_status=TaskStatus.SUCCEEDED,
                target_event_message="保留生产任务",
                claim_by="worker-live",
                expected_updated_at=task.updated_at,
            )
            original_row = submission_store.find_by_id(int(row["id"]))

            updated, result = bridge.start_series_update_task(
                claimed,
                submission_store,
                task_store,
                source="文本追更",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "not_eligible")
            self.assertEqual(task_store.find_task(task.id), claimed)
            self.assertEqual(submission_store.find_by_id(int(row["id"])), original_row)

    def test_start_series_update_task_recovers_freeze_checkpoint_before_submission_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_db_path = Path(tmp) / "tasks.db"
            task_store = TaskStore(task_db_path)
            row, task, recognition = self.make_completed_target(submission_store, task_store)
            original_row = submission_store.find_by_id(int(row["id"]))

            with patch.object(
                submission_store,
                "reset_self_share_for_update",
                side_effect=KeyboardInterrupt("process exit after task freeze"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    bridge.start_series_update_task(
                        task,
                        submission_store,
                        task_store,
                        source="冻结后中断",
                    )

            frozen = task_store.find_task(task.id)
            self.assertEqual(frozen.current_stage, TaskStage.RECEIVED)
            self.assertEqual(frozen.status, TaskStatus.PENDING)
            self.assertEqual(frozen.next_run_at, -1)
            self.assertEqual(frozen.claimed_by, "")
            self.assertEqual(frozen.claim_token, "")
            self.assertEqual(frozen.metadata["update_requested_run"], 1)
            self.assertEqual(frozen.metadata["update_received_run"], 0)
            self.assertEqual(submission_store.find_by_id(int(row["id"])), original_row)
            frozen = task_store.record_event(
                frozen.id,
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "模拟修复前持久化检查点",
                metadata_delete_keys=("series_update_checkpoint",),
                next_run_at=-1,
                clear_claim=True,
                expected_stage=TaskStage.RECEIVED,
                expected_status=TaskStatus.PENDING,
                expected_updated_at=frozen.updated_at,
            )
            self.assertNotIn("series_update_checkpoint", frozen.metadata)

            restarted_store = TaskStore(task_db_path)
            recovered, result = bridge.start_series_update_task(
                restarted_store.find_task(task.id),
                submission_store,
                restarted_store,
                source="重启恢复",
            )

            recovered_row = submission_store.find_by_id(int(row["id"]))
            self.assertEqual(result, "started")
            self.assertEqual(recovered.current_stage, TaskStage.RECEIVED)
            self.assertEqual(recovered.status, TaskStatus.PENDING)
            self.assertEqual(recovered.next_run_at, 0)
            self.assertEqual(recovered.metadata["update_requested_run"], 1)
            self.assertEqual(recovered.metadata["update_received_run"], 0)
            self.assertEqual(recovered_row["workflow_phase"], "update_requested")
            self.assertEqual(json.loads(recovered_row["recognition_json"]), recognition)
            self.assertIsNone(recovered_row["own_share_code"])
            claimed = restarted_store.claim_next_runnable("restart-worker", now=9999999999.0)
            self.assertEqual(claimed.id, task.id)

    def test_start_series_update_task_recovers_checkpoint_after_submission_reset_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_db_path = Path(tmp) / "tasks.db"
            task_store = TaskStore(task_db_path)
            row, task, recognition = self.make_completed_target(submission_store, task_store)

            with patch.object(
                task_store,
                "record_event",
                side_effect=KeyboardInterrupt("process exit before task activation"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    bridge.start_series_update_task(
                        task,
                        submission_store,
                        task_store,
                        source="提交后中断",
                    )

            frozen = task_store.find_task(task.id)
            reset_row = submission_store.find_by_id(int(row["id"]))
            self.assertEqual(frozen.current_stage, TaskStage.RECEIVED)
            self.assertEqual(frozen.status, TaskStatus.PENDING)
            self.assertEqual(frozen.next_run_at, -1)
            self.assertEqual(reset_row["workflow_phase"], "update_requested")
            self.assertEqual(json.loads(reset_row["recognition_json"]), recognition)
            self.assertIsNone(reset_row["own_share_code"])

            restarted_store = TaskStore(task_db_path)
            recovered, result = bridge.start_series_update_task(
                restarted_store.find_task(task.id),
                submission_store,
                restarted_store,
                source="提交后重启恢复",
            )

            recovered_row = submission_store.find_by_id(int(row["id"]))
            self.assertEqual(result, "started")
            self.assertEqual(recovered.current_stage, TaskStage.RECEIVED)
            self.assertEqual(recovered.status, TaskStatus.PENDING)
            self.assertEqual(recovered.next_run_at, 0)
            self.assertEqual(recovered.metadata["update_requested_run"], 1)
            self.assertEqual(recovered.metadata["update_received_run"], 0)
            self.assertEqual(recovered_row["id"], reset_row["id"])
            self.assertEqual(recovered_row["workflow_phase"], "update_requested")
            self.assertEqual(json.loads(recovered_row["recognition_json"]), recognition)
            self.assertIsNone(recovered_row["own_share_code"])

    def test_start_series_update_task_parks_unchanged_checkpoint_after_activation_cas_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row, task, _recognition = self.make_completed_target(submission_store, task_store)
            real_record_event = task_store.record_event

            def lose_activation(task_id, stage, status, message, **kwargs):
                if stage == TaskStage.RECEIVED and kwargs.get("next_run_at") == 0:
                    return None
                return real_record_event(task_id, stage, status, message, **kwargs)

            with patch.object(task_store, "record_event", side_effect=lose_activation):
                parked, result = bridge.start_series_update_task(
                    task,
                    submission_store,
                    task_store,
                    source="激活丢失",
                )

            reset_row = submission_store.find_by_id(int(row["id"]))
            self.assertEqual(result, "failed")
            self.assertEqual(parked.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(parked.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(parked.next_run_at, -1)
            self.assertEqual(task_store.find_task(task.id), parked)
            self.assertEqual(reset_row["workflow_phase"], "update_requested")
            self.assertIsNone(reset_row["own_share_code"])

    def test_start_series_update_task_preserves_competing_activation_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row, task, _recognition = self.make_completed_target(submission_store, task_store)
            real_record_event = task_store.record_event
            competing_snapshot = None

            def lose_activation_after_competing_update(task_id, stage, status, message, **kwargs):
                nonlocal competing_snapshot
                if stage == TaskStage.RECEIVED and kwargs.get("next_run_at") == 0:
                    competing_snapshot = real_record_event(
                        task_id,
                        TaskStage.NEEDS_ACTION,
                        TaskStatus.NEEDS_ACTION,
                        "其他写入者已接管同链接追更检查点",
                        metadata_patch={"competing_same_link_writer": True},
                        next_run_at=-1,
                        expected_stage=TaskStage.RECEIVED,
                        expected_status=TaskStatus.PENDING,
                        expected_updated_at=kwargs["expected_updated_at"],
                    )
                    return None
                return real_record_event(task_id, stage, status, message, **kwargs)

            with patch.object(task_store, "record_event", side_effect=lose_activation_after_competing_update):
                updated, result = bridge.start_series_update_task(
                    task,
                    submission_store,
                    task_store,
                    source="激活竞争",
                )

            reset_row = submission_store.find_by_id(int(row["id"]))
            self.assertEqual(result, "failed")
            self.assertIsNotNone(competing_snapshot)
            self.assertEqual(updated, competing_snapshot)
            self.assertEqual(task_store.find_task(task.id), competing_snapshot)
            self.assertTrue(updated.metadata["competing_same_link_writer"])
            self.assertEqual(reset_row["workflow_phase"], "update_requested")
            self.assertIsNone(reset_row["own_share_code"])

    def test_start_series_update_task_rejects_explicit_parent_checkpoint_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row, task, _recognition = self.make_completed_target(submission_store, task_store)
            lookalike = task_store.record_event(
                task.id,
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "显式新链接准备检查点",
                metadata_patch={
                    "submission_id": int(row["id"]),
                    "series_update_parent_task_id": task.id + 100,
                    "series_update_parent_submission_id": int(row["id"]),
                    "update_requested_run": 1,
                    "update_received_run": 0,
                    "update_started_at": 1000.0,
                    "previous_own_share_code": "old-share",
                    "force_reprocess": True,
                    "reprocess_started_at": 1000.0,
                },
                next_run_at=-1,
                expected_stage=TaskStage.CLEANED,
                expected_status=TaskStatus.SUCCEEDED,
                expected_updated_at=task.updated_at,
            )
            original_row = submission_store.find_by_id(int(row["id"]))

            updated, result = bridge.start_series_update_task(
                lookalike,
                submission_store,
                task_store,
                source="错误恢复保护",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "not_eligible")
            self.assertEqual(task_store.find_task(task.id), lookalike)
            self.assertEqual(submission_store.find_by_id(int(row["id"])), original_row)

    def test_start_series_update_task_rejects_claimed_freeze_checkpoint_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            row, task, _recognition = self.make_completed_target(submission_store, task_store)

            with patch.object(
                submission_store,
                "reset_self_share_for_update",
                side_effect=KeyboardInterrupt("process exit after task freeze"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    bridge.start_series_update_task(
                        task,
                        submission_store,
                        task_store,
                        source="冻结后中断",
                    )
            frozen = task_store.find_task(task.id)
            claimed = task_store.compare_and_set_transition(
                frozen.id,
                TaskStage.RECEIVED,
                {TaskStatus.PENDING},
                require_unclaimed=True,
                target_stage=TaskStage.RECEIVED,
                target_status=TaskStatus.PENDING,
                target_event_message="其他工作者认领检查点",
                next_run_at=-1,
                claim_by="worker-live",
                expected_updated_at=frozen.updated_at,
            )
            original_row = submission_store.find_by_id(int(row["id"]))

            updated, result = bridge.start_series_update_task(
                claimed,
                submission_store,
                task_store,
                source="错误恢复保护",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "not_eligible")
            self.assertEqual(task_store.find_task(task.id), claimed)
            self.assertEqual(submission_store.find_by_id(int(row["id"])), original_row)

    def test_parse_explicit_series_update_command(self):
        cases = [
            (
                "追更 #328 https://115cdn.com/s/new?password=1212",
                (True, 328, "https://115cdn.com/s/new?password=1212"),
            ),
            (
                "追更：https://115cdn.com/s/old?password=1212",
                (True, None, "https://115cdn.com/s/old?password=1212"),
            ),
            (
                "https://115cdn.com/s/plain?password=1212",
                (False, None, "https://115cdn.com/s/plain?password=1212"),
            ),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(bridge.parse_explicit_series_update_command(text), expected)

    def test_explicit_new_link_series_update_repairs_existing_unclaimed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            target_row, target, target_recognition = self.make_completed_target(submission_store, task_store)
            source_row, source_task = self.make_existing_source(submission_store, task_store)

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            self.assertEqual(result, "started")
            self.assertEqual(updated.id, source_task.id)
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.tmdb_id, "273114")
            self.assertEqual(updated.category, "国产电视")
            self.assertEqual(updated.metadata["series_update_parent_task_id"], target.id)
            self.assertEqual(updated.metadata["update_requested_run"], 1)
            self.assertEqual(updated.metadata["update_received_run"], 0)
            self.assertNotIn("own_share_file_id", updated.metadata)
            self.assertNotIn("share_create_status", updated.metadata)
            self.assertEqual(updated.next_run_at, 0)

            prepared = submission_store.find_by_id(int(source_row["id"]))
            self.assertEqual(json.loads(prepared["recognition_json"]), target_recognition)
            self.assertEqual(prepared["category_choice"], "国产电视")
            self.assertEqual(prepared["workflow_mode"], "self_share_sync")
            self.assertEqual(prepared["workflow_phase"], "update_requested")
            self.assertIsNone(prepared["own_share_file_id"])
            self.assertIsNone(prepared["own_share_file_name"])
            self.assertIsNone(prepared["own_share_code"])
            self.assertNotIn("3481694900213253783", json.dumps(prepared, ensure_ascii=False))
            self.assertEqual(submission_store.find_by_id(int(target_row["id"]))["own_share_code"], "old-share")

    def test_explicit_new_link_series_update_clears_stale_same_link_checkpoint_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            _source_row, source_task = self.make_existing_source(submission_store, task_store)
            source_task = task_store.patch_metadata(
                source_task.id,
                {"series_update_checkpoint": "same_link"},
            )

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="显式路径保护",
            )

            self.assertEqual(result, "started")
            self.assertEqual(updated.id, source_task.id)
            self.assertNotIn("series_update_checkpoint", updated.metadata)

    def test_explicit_new_link_series_update_writes_task_first_identity_to_both_child_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            original_target_row = submission_store.find_by_id(int(target_row["id"]))
            target = task_store.record_event(
                target.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "任务身份优先",
                title="T-权威剧集-2027-[tmdb=884422]",
                tmdb_id="884422",
                category="外国电视",
            )
            expected_recognition = {
                "ok": True,
                "title": "T-权威剧集-2027-[tmdb=884422]",
                "tmdb_id": "884422",
                "type": "tv",
                "category": "外国电视",
            }

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="身份修复",
            )

            child_row = submission_store.find_by_id(int(updated.submission_id))
            self.assertEqual(result, "started")
            self.assertEqual(updated.title, "T-权威剧集-2027-[tmdb=884422]")
            self.assertEqual(updated.tmdb_id, "884422")
            self.assertEqual(updated.category, "外国电视")
            self.assertEqual(updated.metadata["recognition"], expected_recognition)
            self.assertEqual(child_row["title"], "T-权威剧集-2027-[tmdb=884422]")
            self.assertEqual(child_row["category_choice"], "外国电视")
            self.assertEqual(json.loads(child_row["recognition_json"]), expected_recognition)
            self.assertEqual(submission_store.find_by_id(int(target_row["id"])), original_target_row)

    def test_explicit_new_link_series_update_rejects_claimed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            source_row, claimed = self.make_existing_source(submission_store, task_store, claimed=True)
            original_row = submission_store.find_by_id(int(source_row["id"]))

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "source_busy")
            self.assertTrue(claimed.claimed_by)
            self.assertTrue(claimed.claim_token)
            self.assertEqual(task_store.find_task(claimed.id), claimed)
            self.assertEqual(submission_store.find_by_id(int(source_row["id"])), original_row)

    def test_explicit_new_link_series_update_rejects_claimed_target_without_creating_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            claimed = task_store.compare_and_set_transition(
                target.id,
                TaskStage.CLEANED,
                {TaskStatus.SUCCEEDED},
                require_unclaimed=True,
                target_stage=TaskStage.CLEANED,
                target_status=TaskStatus.SUCCEEDED,
                target_event_message="目标任务已认领",
                claim_by="worker-live",
                expected_updated_at=target.updated_at,
            )

            updated, result = bridge.start_series_update_from_link(
                claimed,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "not_eligible")
            self.assertEqual(task_store.find_task(target.id), claimed)
            self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))

    def test_explicit_new_link_series_update_rejects_invalid_target_identity_without_creating_source(self):
        invalid_targets = [
            {"media_type": "movie"},
            {"tmdb_id": ""},
            {"workflow_mode": "direct"},
        ]
        for invalid in invalid_targets:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
                task_store = TaskStore(Path(tmp) / "tasks.db")
                target_row, target, _recognition = self.make_completed_target(
                    submission_store,
                    task_store,
                    media_type=invalid.get("media_type", "tv"),
                    tmdb_id=invalid.get("tmdb_id", "273114"),
                )
                if "workflow_mode" in invalid:
                    submission_store.update_self_share(
                        int(target_row["id"]),
                        workflow_mode=invalid["workflow_mode"],
                    )

                updated, result = bridge.start_series_update_from_link(
                    target,
                    bridge.ShareKey("new", "1212"),
                    "https://115cdn.com/s/new?password=1212",
                    "464100862",
                    submission_store,
                    task_store,
                    source="生产修复",
                )

                self.assertIsNone(updated)
                self.assertEqual(result, "not_eligible")
                self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))

    def test_explicit_new_link_series_update_rejects_different_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            source_row, source_task = self.make_existing_source(
                submission_store,
                task_store,
                parent_task_id=target.id + 100,
            )
            original_row = submission_store.find_by_id(int(source_row["id"]))

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "source_conflict")
            self.assertEqual(task_store.find_task(source_task.id), source_task)
            self.assertEqual(submission_store.find_by_id(int(source_row["id"])), original_row)

    def test_explicit_new_link_series_update_rejects_completed_remote_share_in_task_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            source_row, source_task = self.make_existing_source(
                submission_store,
                task_store,
                task_own_share_code="remote-share",
            )
            original_row = submission_store.find_by_id(int(source_row["id"]))

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "source_conflict")
            self.assertEqual(task_store.find_task(source_task.id), source_task)
            self.assertEqual(submission_store.find_by_id(int(source_row["id"])), original_row)
            self.assertEqual(source_task.metadata["own_share_code"], "remote-share")
            self.assertIsNone(original_row["own_share_code"])

    def test_explicit_new_link_series_update_rejects_completed_remote_share_in_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            source_row, source_task = self.make_existing_source(
                submission_store,
                task_store,
                submission_own_share_code="remote-share",
            )
            original_row = submission_store.find_by_id(int(source_row["id"]))

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            self.assertIsNone(updated)
            self.assertEqual(result, "source_conflict")
            self.assertEqual(task_store.find_task(source_task.id), source_task)
            self.assertEqual(submission_store.find_by_id(int(source_row["id"])), original_row)
            self.assertNotIn("own_share_code", source_task.metadata)
            self.assertEqual(original_row["own_share_code"], "remote-share")

    def test_explicit_new_link_series_update_detects_submission_share_completed_after_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            source_row, _source_task = self.make_existing_source(submission_store, task_store)
            real_freeze = task_store.compare_and_set_transition

            def freeze_then_complete_share(*args, **kwargs):
                frozen = real_freeze(*args, **kwargs)
                submission_store.update_self_share(
                    int(source_row["id"]),
                    workflow_phase="share_sync_submitted",
                    own_share_file_id="remote-folder",
                    own_share_file_name="remote-series",
                    own_share_code="race-share",
                    own_share_receive_code="3434",
                    own_share_url="https://115cdn.com/s/race-share?password=3434",
                    share_sync_status="submitted",
                    canonical_manifest_json='{"remote": true}',
                    share_alias_name="remote-alias",
                    share_alias_level=1,
                    share_validation_status="passed",
                    share_validation_error="",
                )
                return frozen

            with patch.object(task_store, "compare_and_set_transition", side_effect=freeze_then_complete_share):
                updated, result = bridge.start_series_update_from_link(
                    target,
                    bridge.ShareKey("new", "1212"),
                    "https://115cdn.com/s/new?password=1212",
                    "464100862",
                    submission_store,
                    task_store,
                    source="并发修复",
                )

            current_row = submission_store.find_by_id(int(source_row["id"]))
            self.assertEqual(result, "source_conflict")
            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(updated.next_run_at, -1)
            self.assertEqual(updated.error_type, "series_update_source_conflict")
            self.assertEqual(current_row["own_share_file_id"], "remote-folder")
            self.assertEqual(current_row["own_share_file_name"], "remote-series")
            self.assertEqual(current_row["own_share_code"], "race-share")
            self.assertEqual(current_row["own_share_receive_code"], "3434")
            self.assertEqual(current_row["own_share_url"], "https://115cdn.com/s/race-share?password=3434")
            self.assertEqual(current_row["share_sync_status"], "submitted")
            self.assertEqual(current_row["canonical_manifest_json"], '{"remote": true}')
            self.assertEqual(current_row["share_alias_name"], "remote-alias")
            self.assertEqual(current_row["share_alias_level"], 1)
            self.assertEqual(current_row["share_validation_status"], "passed")
            self.assertEqual(current_row["share_validation_error"], "")

    def test_explicit_new_link_series_update_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            key = bridge.ShareKey("new", "1212")
            first, first_result = bridge.start_series_update_from_link(
                target,
                key,
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            repeated, result = bridge.start_series_update_from_link(
                target,
                key,
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="生产修复",
            )

            self.assertEqual(first_result, "started")
            self.assertEqual(result, "already_started")
            self.assertEqual(repeated, first)
            self.assertEqual(repeated.metadata["update_requested_run"], 1)

    def test_explicit_new_link_series_update_keeps_active_same_parent_started_with_stale_share_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            key = bridge.ShareKey("new", "1212")
            first, first_result = bridge.start_series_update_from_link(
                target,
                key,
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="首次请求",
            )
            submission_store.update_self_share(
                int(first.submission_id),
                own_share_code="stale-share",
                share_sync_status="submitted",
            )

            repeated, repeated_result = bridge.start_series_update_from_link(
                target,
                key,
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="重复请求",
            )

            self.assertEqual(first_result, "started")
            self.assertEqual(repeated_result, "already_started")
            self.assertEqual(repeated, first)

    def test_explicit_new_link_series_update_atomic_source_creation_preserves_race_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_db_path = Path(tmp) / "tasks.db"
            winner_waiting_to_create = threading.Event()
            allow_winner_creation = threading.Event()
            loser_entered = threading.Event()

            class PausingTaskStore(TaskStore):
                def get_or_create_share_task(self, share_code, receive_code, url, chat_id=""):
                    winner_waiting_to_create.set()
                    if not allow_winner_creation.wait(5):
                        raise AssertionError("winner creation was not released")
                    return super().get_or_create_share_task(share_code, receive_code, url, chat_id=chat_id)

            class CoordinatedTaskStore(TaskStore):
                @property
                def db_path(self):
                    loser_entered.set()
                    return self._db_path

                @db_path.setter
                def db_path(self, value):
                    self._db_path = value

            winner_store = PausingTaskStore(task_db_path)
            loser_store = CoordinatedTaskStore(task_db_path)
            loser_entered.clear()
            _target_row, target, _recognition = self.make_completed_target(submission_store, winner_store)
            key = bridge.ShareKey("new", "1212")
            with ThreadPoolExecutor(max_workers=2) as executor:
                winning = executor.submit(
                    bridge.start_series_update_from_link,
                    target,
                    key,
                    "https://115cdn.com/s/new?password=1212",
                    "winner-chat",
                    submission_store,
                    winner_store,
                    source="并发胜方",
                )
                self.assertTrue(winner_waiting_to_create.wait(5), "winner did not reach source creation")
                losing = executor.submit(
                    bridge.start_series_update_from_link,
                    target,
                    key,
                    "https://115cdn.com/s/loser?password=9999",
                    "loser-chat",
                    submission_store,
                    loser_store,
                    source="并发败方",
                )
                try:
                    self.assertTrue(loser_entered.wait(5), "loser did not enter the operation")
                finally:
                    allow_winner_creation.set()
                winner, winner_result = winning.result(timeout=5)
                repeated, loser_result = losing.result(timeout=5)

            current = winner_store.find_task(winner.id)
            self.assertEqual(winner_result, "started")
            self.assertEqual(loser_result, "already_started")
            self.assertEqual(repeated, winner)
            self.assertEqual(current, winner)
            self.assertEqual(current.url, "https://115cdn.com/s/new?password=1212")
            self.assertEqual(current.chat_id, "winner-chat")
            self.assertEqual(current.updated_at, winner.updated_at)
            self.assertEqual(current.metadata, winner.metadata)
            self.assertEqual(current.metadata["series_update_parent_task_id"], target.id)
            self.assertEqual(current.current_stage, TaskStage.RECEIVED)
            self.assertEqual(current.status, TaskStatus.PENDING)
            self.assertEqual(current.next_run_at, 0)

    def test_explicit_new_link_series_update_duplicate_does_not_park_active_preparation_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(task_db_path)
            _target_row, target, _recognition = self.make_completed_target(submission_store, first_store)
            preparation_started = threading.Event()
            release_preparation = threading.Event()
            duplicate_entered = threading.Event()
            frozen_checkpoint = None
            real_prepare = submission_store.prepare_series_update_child

            class CoordinatedTaskStore(TaskStore):
                @property
                def db_path(self):
                    duplicate_entered.set()
                    return self._db_path

                @db_path.setter
                def db_path(self, value):
                    self._db_path = value

            duplicate_store = CoordinatedTaskStore(task_db_path)
            duplicate_entered.clear()

            def prepare_while_blocked(*args, **kwargs):
                nonlocal frozen_checkpoint
                frozen_checkpoint = first_store.find_task_by_share_key("new", "1212")
                preparation_started.set()
                if not release_preparation.wait(5):
                    raise AssertionError("preparation was not released")
                return real_prepare(*args, **kwargs)

            def run_duplicate():
                return bridge.start_series_update_from_link(
                    target,
                    bridge.ShareKey("new", "1212"),
                    "https://115cdn.com/s/duplicate?password=9999",
                    "duplicate-chat",
                    submission_store,
                    duplicate_store,
                    source="并发重复请求",
                )

            with patch.object(
                submission_store,
                "prepare_series_update_child",
                side_effect=prepare_while_blocked,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    bridge.start_series_update_from_link,
                    target,
                    bridge.ShareKey("new", "1212"),
                    "https://115cdn.com/s/new?password=1212",
                    "first-chat",
                    submission_store,
                    first_store,
                    source="首次请求",
                )
                self.assertTrue(preparation_started.wait(5), "first call did not enter preparation")
                duplicate = executor.submit(run_duplicate)
                self.assertTrue(duplicate_entered.wait(5), "duplicate call did not enter the operation")
                try:
                    try:
                        duplicate.result(timeout=1)
                    except FutureTimeoutError:
                        pass
                    current = first_store.find_task(frozen_checkpoint.id)
                    self.assertEqual(current.current_stage, TaskStage.RECEIVED)
                    self.assertEqual(current.status, TaskStatus.PENDING)
                    self.assertEqual(current.next_run_at, -1)
                    self.assertEqual(current.updated_at, frozen_checkpoint.updated_at)
                finally:
                    release_preparation.set()
                started, first_result = first.result(timeout=5)
                repeated, duplicate_result = duplicate.result(timeout=5)

            self.assertEqual(first_result, "started")
            self.assertEqual(duplicate_result, "already_started")
            self.assertEqual(repeated, started)
            self.assertEqual(first_store.find_task(started.id), started)

    def test_explicit_new_link_series_update_parks_preparation_checkpoint_after_preparation_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            key = bridge.ShareKey("new", "1212")

            with patch.object(
                submission_store,
                "prepare_series_update_child",
                side_effect=KeyboardInterrupt("process exit before preparation"),
            ), patch.object(bridge.LOG, "exception"):
                try:
                    parked, result = bridge.start_series_update_from_link(
                        target,
                        key,
                        "https://115cdn.com/s/new?password=1212",
                        "464100862",
                        submission_store,
                        task_store,
                        source="中断恢复",
                    )
                except KeyboardInterrupt:
                    self.fail("preparation interruption escaped without parking the checkpoint")

            self.assertEqual(result, "failed")
            self.assertEqual(parked.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(parked.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(parked.next_run_at, -1)
            self.assertIsNone(submission_store.find_by_key(key))

            restarted, retry_result = bridge.start_series_update_from_link(
                target,
                key,
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="显式重试",
            )
            self.assertEqual(retry_result, "started")
            self.assertEqual(restarted.metadata["update_requested_run"], 2)
            self.assertEqual(restarted.next_run_at, 0)

    def test_explicit_new_link_series_update_parks_checkpoint_after_submission_commit_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            key = bridge.ShareKey("new", "1212")
            real_prepare = submission_store.prepare_series_update_child

            def prepare_then_interrupt(*args, **kwargs):
                real_prepare(*args, **kwargs)
                raise KeyboardInterrupt("process exit after submission commit")

            with patch.object(
                submission_store,
                "prepare_series_update_child",
                side_effect=prepare_then_interrupt,
            ), patch.object(bridge.LOG, "exception"):
                try:
                    parked, result = bridge.start_series_update_from_link(
                        target,
                        key,
                        "https://115cdn.com/s/new?password=1212",
                        "464100862",
                        submission_store,
                        task_store,
                        source="提交后中断",
                    )
                except KeyboardInterrupt:
                    self.fail("post-commit interruption escaped without parking the checkpoint")

            prepared = submission_store.find_by_key(key)
            self.assertEqual(result, "failed")
            self.assertEqual(parked.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(parked.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(parked.next_run_at, -1)
            self.assertEqual(prepared["workflow_phase"], "update_requested")

    def test_explicit_new_link_series_update_parks_exact_checkpoint_after_activation_cas_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            real_record_event = task_store.record_event
            activation_lost = False

            def lose_activation(task_id, stage, status, message, **kwargs):
                nonlocal activation_lost
                if stage == TaskStage.RECEIVED and kwargs.get("next_run_at") == 0:
                    activation_lost = True
                    return None
                return real_record_event(task_id, stage, status, message, **kwargs)

            with patch.object(task_store, "record_event", side_effect=lose_activation):
                parked, result = bridge.start_series_update_from_link(
                    target,
                    bridge.ShareKey("new", "1212"),
                    "https://115cdn.com/s/new?password=1212",
                    "464100862",
                    submission_store,
                    task_store,
                    source="激活丢失",
                )

            self.assertTrue(activation_lost)
            self.assertEqual(result, "failed")
            self.assertEqual(parked.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(parked.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(parked.next_run_at, -1)
            self.assertEqual(task_store.find_task(parked.id), parked)

    def test_explicit_new_link_series_update_does_not_park_a_changed_activation_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            real_record_event = task_store.record_event
            competing_snapshot = None

            def lose_activation_after_competing_update(task_id, stage, status, message, **kwargs):
                nonlocal competing_snapshot
                if stage == TaskStage.RECEIVED and kwargs.get("next_run_at") == 0:
                    competing_snapshot = real_record_event(
                        task_id,
                        TaskStage.NEEDS_ACTION,
                        TaskStatus.NEEDS_ACTION,
                        "其他写入者已接管追更检查点",
                        metadata_patch={"competing_writer": True},
                        next_run_at=-1,
                        expected_stage=TaskStage.RECEIVED,
                        expected_status=TaskStatus.PENDING,
                        expected_updated_at=kwargs["expected_updated_at"],
                    )
                    return None
                return real_record_event(task_id, stage, status, message, **kwargs)

            with patch.object(task_store, "record_event", side_effect=lose_activation_after_competing_update):
                updated, result = bridge.start_series_update_from_link(
                    target,
                    bridge.ShareKey("new", "1212"),
                    "https://115cdn.com/s/new?password=1212",
                    "464100862",
                    submission_store,
                    task_store,
                    source="激活竞争",
                )

            self.assertEqual(result, "failed")
            self.assertIsNotNone(competing_snapshot)
            self.assertEqual(updated, competing_snapshot)
            self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertTrue(updated.metadata["competing_writer"])
            self.assertEqual(task_store.find_task(updated.id), competing_snapshot)

    def test_explicit_new_link_series_update_leaves_non_checkpoint_same_parent_task_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            child = task_store.upsert_task("new", "1212", "https://115cdn.com/s/new?password=1212")
            paused = task_store.record_event(
                child.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "其他流程暂停",
                metadata_patch={"series_update_parent_task_id": target.id},
                next_run_at=-1,
                expected_stage=TaskStage.RECEIVED,
                expected_status=TaskStatus.PENDING,
                expected_updated_at=child.updated_at,
            )

            updated, result = bridge.start_series_update_from_link(
                target,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "464100862",
                submission_store,
                task_store,
                source="错误恢复保护",
            )

            self.assertEqual(result, "failed")
            self.assertEqual(updated, paused)
            self.assertEqual(task_store.find_task(child.id), paused)

    def test_explicit_new_link_series_update_preparation_failure_stays_frozen(self):
        failures = [None, RuntimeError("prepare failed")]
        for failure in failures:
            with self.subTest(failure=repr(failure)), tempfile.TemporaryDirectory() as tmp:
                submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
                task_store = TaskStore(Path(tmp) / "tasks.db")
                _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
                if isinstance(failure, Exception):
                    preparation = patch.object(
                        submission_store,
                        "prepare_series_update_child",
                        side_effect=failure,
                    )
                else:
                    preparation = patch.object(
                        submission_store,
                        "prepare_series_update_child",
                        return_value=None,
                    )
                with preparation, patch.object(bridge.LOG, "exception"):
                    updated, result = bridge.start_series_update_from_link(
                        target,
                        bridge.ShareKey("new", "1212"),
                        "https://115cdn.com/s/new?password=1212",
                        "464100862",
                        submission_store,
                        task_store,
                        source="生产修复",
                    )

                self.assertEqual(result, "failed")
                self.assertEqual(updated.current_stage, TaskStage.NEEDS_ACTION)
                self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
                self.assertEqual(updated.next_run_at, -1)
                self.assertEqual(updated.metadata["series_update_parent_task_id"], target.id)

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

    def test_untargeted_colon_series_update_command_requeues_completed_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _row, task, _recognition = self.make_completed_target(submission_store, task_store)
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("追更：https://115cdn.com/s/old?password=1212"),
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
            self.assertIn("已开始追更", telegram.messages[-1][1])

    def test_explicit_series_update_command_requires_target_for_new_link(self):
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

            self.assertIsNone(task_store.find_task_by_share_key("new", "1234"))
            self.assertIn("需指定历史任务号", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_starts_new_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update(f"追更 #{target.id} https://115cdn.com/s/new?password=1212"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            child = task_store.find_task_by_share_key("new", "1212")
            self.assertIsNotNone(child)
            self.assertEqual(child.metadata["series_update_parent_task_id"], target.id)
            self.assertIn("已开始追更", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_rejects_movie_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(
                submission_store,
                task_store,
                category="欧美电影",
                media_type="movie",
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update(f"追更 #{target.id} https://115cdn.com/s/new?password=1212"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))
            self.assertIn("不符合追更条件", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_rejects_unfinished_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(
                submission_store,
                task_store,
                stage=TaskStage.EMBY_CONFIRMED,
                status=TaskStatus.RUNNING,
            )
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update(f"追更 #{target.id} https://115cdn.com/s/new?password=1212"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))
            self.assertIn("不符合追更条件", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_rejects_missing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update("追更 #999 https://115cdn.com/s/new?password=1212"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))
            self.assertIn("历史任务不存在", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_rejects_multiple_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update(
                    f"追更 #{target.id} https://115cdn.com/s/new?password=1212 "
                    "https://115cdn.com/s/second?password=3434"
                ),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))
            self.assertIsNone(task_store.find_task_by_share_key("second", "3434"))
            self.assertIn("仅支持一个 115 分享链接", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_rejects_extra_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update(f"追更 #{target.id} note https://115cdn.com/s/new?password=1212"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))
            self.assertIsNone(submission_store.find_by_key(bridge.ShareKey("new", "1212")))
            self.assertIn("仅支持一个 115 分享链接", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_rejects_duplicate_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            telegram = FakeTelegram()
            link = "https://115cdn.com/s/new?password=1212"

            bridge.handle_update(
                self.update(f"追更 #{target.id} {link} {link}"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            self.assertIsNone(task_store.find_task_by_share_key("new", "1212"))
            self.assertIsNone(submission_store.find_by_key(bridge.ShareKey("new", "1212")))
            self.assertIn("仅支持一个 115 分享链接", telegram.messages[-1][1])

    def test_targeted_explicit_series_update_command_rejects_magnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            _target_row, target, _recognition = self.make_completed_target(submission_store, task_store)
            telegram = FakeTelegram()

            bridge.handle_update(
                self.update(f"追更 #{target.id} magnet:?xt=urn:btih:0123456789abcdef"),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
                self_share_workflow=object(),
                task_engine_enabled=True,
            )

            self.assertEqual([task.id for task in task_store.list_recent_tasks(limit=10)], [target.id])
            self.assertIn("仅支持一个 115 分享链接", telegram.messages[-1][1])

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

    def test_prepare_series_update_child_copies_only_stable_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            target_recognition = {
                "ok": True,
                "title": "X-悬案-2026-[tmdb=273114]",
                "share_name": "悬案 (2026)",
                "tmdb_id": "273114",
                "type": "tv",
                "category": "国产电视",
                "category_status": "self_share_resolved",
                "organized_parent_id": "tv-parent",
                "parent_id": "tv-parent",
            }
            target = submission_store.upsert_submission(
                bridge.ShareKey("old", "1212"),
                "https://115cdn.com/s/old?password=1212",
                "completed",
                title=target_recognition["title"],
            )
            target = submission_store.update_recognition(int(target["id"]), target_recognition, "self_share_resolved")
            target = submission_store.update_category(int(target["id"]), "国产电视", "self_share_resolved")
            target = submission_store.update_self_share(
                int(target["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="cleanup_completed",
                own_share_code="old-share",
                own_share_receive_code="1212",
                own_share_file_id="tv-parent",
            )
            child = submission_store.upsert_submission(
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
                "failed",
                title="错误电影 (2025)",
            )
            child = submission_store.update_recognition(
                int(child["id"]),
                {"title": "错误电影 (2025)", "type": "movie", "category": "欧美电影"},
                "selected",
            )
            child = submission_store.update_category(int(child["id"]), "欧美电影", "selected")
            child = submission_store.update_self_share(
                int(child["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="share_sync_submitted",
                own_share_file_id="wrong-movie-folder",
                own_share_code="stale-share",
            )
            child = submission_store.update_move(int(child["id"]), "moved", category_final="欧美电影")
            child = submission_store.update_emby(int(child["id"]), "confirmed", item_id="stale-emby")
            submission_store.update_cleanup(int(child["id"]), "deleted", file_id="wrong-movie-folder")

            prepared = submission_store.prepare_series_update_child(
                int(target["id"]),
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
            )

            self.assertEqual(json.loads(prepared["recognition_json"]), target_recognition)
            self.assertEqual(prepared["category_choice"], "国产电视")
            self.assertEqual(prepared["category_status"], "selected")
            self.assertEqual(prepared["workflow_mode"], "self_share_sync")
            self.assertEqual(prepared["workflow_phase"], "update_requested")
            self.assertEqual(prepared["status"], "received")
            self.assertIsNone(prepared["own_share_file_id"])
            self.assertIsNone(prepared["own_share_code"])
            self.assertIsNone(prepared["move_status"])
            self.assertIsNone(prepared["emby_status"])
            self.assertIsNone(prepared["cleanup_status"])
            self.assertEqual(submission_store.find_by_id(int(target["id"]))["own_share_code"], "old-share")

    def test_prepare_series_update_child_missing_target_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")

            prepared = submission_store.prepare_series_update_child(
                99999,
                bridge.ShareKey("new", "1212"),
                "https://115cdn.com/s/new?password=1212",
            )

            self.assertIsNone(prepared)
            self.assertIsNone(submission_store.find_by_key(bridge.ShareKey("new", "1212")))

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
            self.assertEqual(updated.retry_count, 0)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(telegram.answers[-1][1], "已重新入队")
            self.assertIn("已重新入队", telegram.messages[-1][1])

    def test_task_retry_callback_honors_configured_retry_limit(self):
        def make_task(task_store, key):
            task = task_store.upsert_task(key, "", f"https://115cdn.com/s/{key}")
            for index in range(3):
                task = task_store.record_event(
                    task.id,
                    TaskStage.STRM_READY,
                    TaskStatus.FAILED,
                    f"failed {index}",
                    increment_retry=True,
                )
            return task

        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            limited = make_task(task_store, "limited")
            telegram = FakeTelegram()
            update = {
                "callback_query": {
                    "id": "task-retry-limited",
                    "from": {"id": 464100862},
                    "message": {"chat": {"id": 464100862}},
                    "data": f"task_retry:{limited.id}",
                }
            }

            bridge.handle_update(update, FakeCmsSubmit(), telegram, "464100862", submission_store, task_store=task_store, task_engine_enabled=True, max_retries=3)
            self.assertIn("超过限制", telegram.answers[-1][1])
            self.assertNotIn("task_retry", str(bridge.task_action_keyboard([limited], max_retries=3)))

            allowed = make_task(task_store, "allowed")
            update["callback_query"]["id"] = "task-retry-allowed"
            update["callback_query"]["data"] = f"task_retry:{allowed.id}"
            bridge.handle_update(update, FakeCmsSubmit(), telegram, "464100862", submission_store, task_store=task_store, task_engine_enabled=True, max_retries=5)
            self.assertEqual(task_store.find_task(allowed.id).status, TaskStatus.PENDING)
            self.assertIn("已重新入队", telegram.answers[-1][1])

    def test_task_reprocess_callback_requeues_from_received_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")
            row = submission_store.upsert_submission(
                bridge.ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "received",
                title="重跑电影",
            )
            submission_store.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="share_sync_submitted",
                own_share_file_id="old-folder",
                own_share_file_name="旧目录-[tmdb=952936]",
                own_share_code="old-share",
                own_share_receive_code="1212",
            )
            task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "cleanup complete",
                title="重跑电影",
                submission_id=int(row["id"]),
                metadata_patch={"own_share_code": "ownabc"},
            )
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
            self.assertEqual(updated.retry_count, 0)
            self.assertEqual(updated.metadata["retry_from_stage"], TaskStage.CLEANED.value)
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.RECEIVED.value)
            self.assertTrue(updated.metadata["force_reprocess"])
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.RECEIVED)
            self.assertIn("TG 按钮触发从头重跑", [event["message"] for event in events])
            self.assertEqual(telegram.answers[-1][1], "已从头重跑")
            self.assertIn("已从头重跑", telegram.messages[-1][1])
            queued_submission = submission_store.find_by_id(int(row["id"]))
            self.assertEqual(queued_submission["workflow_phase"], "share_sync_submitted")
            self.assertEqual(queued_submission["own_share_file_id"], "old-folder")
            self.assertEqual(queued_submission["own_share_code"], "old-share")

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

            self.assertEqual(len(telegram.rich_messages), 1)
            message = telegram.rich_messages[-1][1].to_plain()
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
