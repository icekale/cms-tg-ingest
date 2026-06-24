import ast
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
