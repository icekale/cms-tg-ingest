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
        self.assertIn("Web 管理页读取 TaskStore", readme)
        self.assertIn("Telegram 新链接接收回复", readme)
        self.assertIn("现有 `/status` 仍面向 SubmissionStore 兼容视图", readme)
        self.assertNotIn("TaskStore 仍是旁路时间线", readme)
        self.assertNotIn("Telegram 状态命令读取同一个 TaskStore 状态", readme)

    def test_changelog_mentions_authoritative_runner(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("TaskStore authoritative runner", changelog)
        self.assertIn("Web 管理页读取 TaskStore", changelog)
        self.assertIn("Telegram 新链接接收回复", changelog)
        self.assertIn("`/status` 仍保留 SubmissionStore 兼容语义", changelog)
        self.assertNotIn("Telegram 状态读取同一 TaskStore 状态", changelog)


if __name__ == "__main__":
    unittest.main()
