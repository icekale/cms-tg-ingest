import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendTests(unittest.TestCase):
    def test_vue_admin_shell_has_expected_routes_and_build_contract(self):
        package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
        self.assertIn("vue", package["dependencies"])
        self.assertIn("naive-ui", package["dependencies"])
        self.assertIn("build", package["scripts"])
        router = (ROOT / "frontend/src/router.js").read_text(encoding="utf-8")
        for route in ("/overview", "/tasks", "/quality", "/health", "/hdhive"):
            self.assertIn(route, router)
        self.assertIn("base: '/app/'", (ROOT / "frontend/vite.config.js").read_text(encoding="utf-8"))

    def test_vue_admin_exposes_migrated_operational_controls(self):
        api = (ROOT / "frontend/src/api.js").read_text(encoding="utf-8")
        task_detail = (ROOT / "frontend/src/views/TaskDetail.vue").read_text(encoding="utf-8")
        quality = (ROOT / "frontend/src/views/Quality.vue").read_text(encoding="utf-8")
        hdhive = (ROOT / "frontend/src/views/Hdhive.vue").read_text(encoding="utf-8")
        overview = (ROOT / "frontend/src/views/Overview.vue").read_text(encoding="utf-8")

        self.assertIn("taskAction", api)
        for action in ("retry", "emby", "restore", "reprocess"):
            self.assertIn(action, task_detail)
        self.assertIn("clearHistory", api)
        self.assertIn("clearHistory", overview)
        for control in ("fix", "run", "settings", "reset"):
            self.assertIn(control, api)
            self.assertIn(control, quality)
        for control in ("pause", "resume", "delete", "check"):
            self.assertIn(control, hdhive)
        self.assertIn("hdhiveSubscriptionAction", api)
        self.assertIn("hdhiveItemConfirm", api)
        for control in ("confirm", "run", "settings"):
            self.assertIn(control, hdhive)


if __name__ == "__main__":
    unittest.main()
