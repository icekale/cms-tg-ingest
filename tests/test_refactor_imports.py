import ast
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class RefactorImportTests(unittest.TestCase):
    def test_config_module_exports_core_config_types(self):
        from app.config import Config, MoveConfig, MovePlan, SelfShareConfig

        self.assertEqual(Config.__name__, "Config")
        self.assertEqual(MoveConfig.__name__, "MoveConfig")
        self.assertEqual(MovePlan.__name__, "MovePlan")
        self.assertEqual(SelfShareConfig.__name__, "SelfShareConfig")

    def test_client_modules_export_clients(self):
        from app.clients.cms import CmsClient
        from app.clients.emby import EmbyClient
        from app.clients.http import FormHttp, HttpJson
        from app.clients.p115 import P115WebClient

        self.assertEqual(CmsClient.__name__, "CmsClient")
        self.assertEqual(EmbyClient.__name__, "EmbyClient")
        self.assertEqual(FormHttp.__name__, "FormHttp")
        self.assertEqual(HttpJson.__name__, "HttpJson")
        self.assertEqual(P115WebClient.__name__, "P115WebClient")

    def test_media_classify_module_exports_core_helpers(self):
        from app.media.classify import final_category_for_move, normalize_text

        self.assertEqual(normalize_text("J-杰克・莱恩-2018"), "j杰克莱恩2018")
        self.assertEqual(final_category_for_move({"category_choice": "外国电视"}, {}), "外国电视")

    def test_media_strm_module_exports_core_helpers(self):
        from app.media.strm import MovePlan, has_strm_file, validate_self_share_strm_source

        self.assertEqual(MovePlan.__name__, "MovePlan")
        self.assertFalse(has_strm_file(Path("/path/that/does/not/exist")))
        self.assertEqual(validate_self_share_strm_source(Path("/path/that/does/not/exist"), {}), "")

    def test_workflow_module_exports_self_share_workflows(self):
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow, SelfShareWorkflow

        self.assertEqual(SelfShareWorkflow.__name__, "SelfShareWorkflow")
        self.assertEqual(BridgeSelfShareTaskWorkflow.__name__, "BridgeSelfShareTaskWorkflow")

    def test_app_modules_do_not_import_bridge(self):
        app_root = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for path in app_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path.relative_to(app_root.parent)}:{alias.name}"
                        for alias in node.names
                        if alias.name == "bridge" or alias.name.startswith("bridge.")
                    )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == "bridge" or module.startswith("bridge."):
                        offenders.append(f"{path.relative_to(app_root.parent)}:{module}")
        self.assertEqual(offenders, [])

    def test_bridge_keeps_compatibility_exports(self):
        import bridge
        from app.clients.p115 import P115WebClient
        from app.config import Config, MoveConfig, SelfShareConfig
        from app.media.strm import category_for_self_share_row
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow

        self.assertIs(bridge.Config, Config)
        self.assertIs(bridge.MoveConfig, MoveConfig)
        self.assertIs(bridge.SelfShareConfig, SelfShareConfig)
        self.assertIs(bridge.P115WebClient, P115WebClient)
        self.assertIs(bridge.category_for_self_share_row, category_for_self_share_row)
        self.assertIs(bridge.BridgeSelfShareTaskWorkflow, BridgeSelfShareTaskWorkflow)

    def test_telegram_ui_exports_formatters_and_bridge_compat(self):
        import bridge
        from app.telegram_ui import format_history, format_status, task_action_keyboard

        self.assertIs(bridge.format_history, format_history)
        self.assertIs(bridge.format_status, format_status)
        self.assertIs(bridge.task_action_keyboard, task_action_keyboard)

    def test_legacy_polling_exports_start_status_poll_and_bridge_compat(self):
        import bridge
        from app.legacy_polling import start_status_poll

        self.assertIs(bridge.start_status_poll, start_status_poll)

    def test_legacy_polling_start_status_poll_signature(self):
        from app.legacy_polling import start_status_poll

        signature = inspect.signature(start_status_poll)
        expected_names = [
            "cms",
            "telegram",
            "chat_id",
            "store",
            "row",
            "status_poll_seconds",
            "status_poll_interval",
            "emby",
            "move_config",
            "openai_classifier",
            "tmdb_resolver",
            "self_share_workflow",
            "cleanup_client",
            "task_store",
        ]
        self.assertEqual(list(signature.parameters), expected_names)
        for name in expected_names[:7]:
            self.assertIs(signature.parameters[name].default, inspect.Parameter.empty)
        for name in expected_names[7:]:
            self.assertIsNone(signature.parameters[name].default)
            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_legacy_polling_prefers_main_bridge_impl_without_importing_bridge(self):
        from app.legacy_polling import start_status_poll

        calls = []
        fake_main = types.SimpleNamespace(
            _start_status_poll_impl=lambda *args, **kwargs: calls.append((args, kwargs))
        )
        original_bridge = sys.modules.pop("bridge", None)
        original_main = sys.modules.get("__main__")
        sys.modules["__main__"] = fake_main
        try:
            with patch("builtins.__import__", side_effect=AssertionError("bridge should not be imported")):
                start_status_poll("cms", "telegram", "chat", "store", {"id": 1}, 1, 1)
        finally:
            if original_bridge is not None:
                sys.modules["bridge"] = original_bridge
            else:
                sys.modules.pop("bridge", None)
            if original_main is not None:
                sys.modules["__main__"] = original_main
            else:
                sys.modules.pop("__main__", None)

        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[:7], ("cms", "telegram", "chat", "store", {"id": 1}, 1, 1))
        self.assertEqual(kwargs["task_store"], None)

    def test_legacy_polling_accepts_old_poll_kwargs_without_exposing_them(self):
        from app.legacy_polling import start_status_poll

        calls = []
        fake_bridge = types.SimpleNamespace(
            _start_status_poll_impl=lambda *args, **kwargs: calls.append((args, kwargs))
        )
        with patch.dict(sys.modules, {"bridge": fake_bridge}):
            start_status_poll("cms", "telegram", "chat", "store", {"id": 1}, max_seconds=1, interval=2)

        self.assertEqual(len(calls), 1)
        args, _kwargs = calls[0]
        self.assertEqual(args[:7], ("cms", "telegram", "chat", "store", {"id": 1}, 1, 2))
        self.assertNotIn("max_seconds", inspect.signature(start_status_poll).parameters)
        self.assertNotIn("interval", inspect.signature(start_status_poll).parameters)


if __name__ == "__main__":
    unittest.main()
