import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TaskEngineDocsTests(unittest.TestCase):
    def test_env_example_documents_authoritative_task_engine_settings(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("TASK_ENGINE_ENABLED", env)
        self.assertIn("TASK_DB_PATH", env)

    def test_readme_documents_taskstore_authoritative_new_links(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("TaskStore 接管新链接", readme)
        self.assertNotIn("TaskStore 仍是旁路时间线", readme)

    def test_changelog_mentions_authoritative_runner(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("TaskStore authoritative runner", changelog)


if __name__ == "__main__":
    unittest.main()
