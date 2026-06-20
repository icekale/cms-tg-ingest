from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V02DocsTests(unittest.TestCase):
    def test_readme_documents_web_admin_and_task_engine(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("任务引擎", readme)
        self.assertIn("Web 管理页", readme)
        self.assertIn("WEB_PORT", readme)
        self.assertIn("TASK_DB_PATH", readme)

    def test_env_example_contains_v02_settings(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("TASK_DB_PATH=/data/tasks.db", env)
        self.assertIn("WEB_ENABLED=true", env)
        self.assertIn("WEB_PORT=8787", env)
        self.assertIn("TASK_MAX_RETRIES=3", env)

    def test_changelog_mentions_v02_alpha(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("0.2.0-alpha.1", changelog)
        self.assertIn("任务状态机", changelog)


if __name__ == "__main__":
    unittest.main()
