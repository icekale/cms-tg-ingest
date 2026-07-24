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


if __name__ == "__main__":
    unittest.main()
