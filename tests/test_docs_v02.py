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

    def test_readme_documents_alpha2_real_workflow_timeline(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("v0.2 Alpha.2", readme)
        self.assertIn("真实 Telegram/CMS 工作流", readme)
        self.assertIn("TaskStore 接管新链接", readme)
        self.assertNotIn("TaskStore 仍是旁路时间线", readme)

    def test_changelog_mentions_alpha2_taskstore_bridge(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("0.2.0-alpha.2", changelog)
        self.assertIn("真实工作流进度写入 TaskStore", changelog)

    def test_readme_documents_cleanup_after_own_share(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("在 `TASK_ENGINE_ENABLED=true` 的 TaskRunner 路径中", readme)
        self.assertIn("只会在 STRM 已移动且 Emby 确认入库后删除", readme)
        self.assertIn("旧 SubmissionStore + 轮询路径是兼容回滚路径", readme)
        self.assertNotIn("自有分享创建成功后删除 115 转存源", readme)

    def test_env_example_scopes_cleanup_safety_to_task_engine(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("TASK_ENGINE_ENABLED=true", env)
        self.assertIn("Task engine path cleans only after own share, STRM move/library, and Emby confirmation", env)
        self.assertIn("legacy SubmissionStore path keeps compatibility behavior", env)
        self.assertNotIn("cleanup runs after your own permanent 115 share is created", env)


if __name__ == "__main__":
    unittest.main()
