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

    def test_readme_documents_cleanup_after_share_validation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("在 `TASK_ENGINE_ENABLED=true` 的 TaskRunner 路径中", readme)
        self.assertIn("自己的永久分享状态验证通过后立即删除 115 转存源", readme)
        self.assertIn("后续 STRM 只使用自己的分享链接生成", readme)
        self.assertIn("旧 SubmissionStore + 轮询路径是兼容回滚路径", readme)
        self.assertNotIn("只会在 STRM 已移动且 Emby 确认入库后删除", readme)

    def test_readme_leads_with_current_product_workflow(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(
            "115 分享/磁力/ED2K -> 115 接收或云下载 -> CMS 整理分类 -> 自有永久分享 -> 分享 STRM -> Emby 入库 -> 清理转存源",
            readme,
        )
        self.assertIn("共享别名保护", readme)
        self.assertIn("只入库自有分享 STRM", readme)
        self.assertIn("低频 115 调用", readme)

    def test_env_example_scopes_cleanup_safety_to_task_engine(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("TASK_ENGINE_ENABLED=true", env)
        self.assertIn("Task engine path deletes the 115 source immediately after your own permanent share is created", env)
        self.assertIn("legacy SubmissionStore path keeps compatibility behavior", env)
        self.assertNotIn("Task engine path cleans only after own share, STRM move/library, and Emby confirmation", env)

    def test_docs_describe_115_pressure_guards(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("P115_RISK_COOLDOWN_SECONDS=900", env)
        self.assertIn("115 风控冷却", readme)
        self.assertIn("分层搜索早停", readme)
        self.assertIn("整理目录扫描预算", readme)


if __name__ == "__main__":
    unittest.main()
