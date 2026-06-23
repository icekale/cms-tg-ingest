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
        self.assertIn("`/status` 和 `/history` 优先读取 TaskStore", readme)
        self.assertIn("旧 SubmissionStore 记录为空时兜底显示", readme)
        self.assertIn("/status 会附带详情、重试、查 Emby、恢复 STRM、从头重跑按钮", readme)
        self.assertIn("/quality 会先执行 TaskStore 本地轻量巡检", readme)
        self.assertIn("Web 任务详情页提供重试、查 Emby、恢复 STRM、从头重跑按钮", readme)
        self.assertIn("Web `/quality` 页面只读取本地 TaskStore 和 STRM 文件", readme)
        self.assertIn("/health 会显示 TaskStore 本地队列健康", readme)
        self.assertIn("TaskEngine 开启时，新 self-share 链接不会回退到旧 start_status_poll 轮询路径", readme)
        self.assertNotIn("TaskStore 仍是旁路时间线", readme)

    def test_changelog_mentions_authoritative_runner(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("TaskStore authoritative runner", changelog)
        self.assertIn("Web 管理页读取 TaskStore", changelog)
        self.assertIn("Telegram 新链接接收回复", changelog)
        self.assertIn("`/status` 和 `/history` 优先读取 TaskStore", changelog)
        self.assertIn("Web 详情页支持懒回填旧 SubmissionStore 记录", changelog)
        self.assertIn("TG /status 增加任务操作按钮", changelog)
        self.assertIn("TaskStore 本地轻量巡检", changelog)
        self.assertIn("Web 任务详情页增加重试、查 Emby、恢复 STRM、从头重跑按钮", changelog)
        self.assertIn("Web /quality 增加 TaskStore 本地轻量巡检", changelog)
        self.assertIn("/health 增加 TaskStore 本地队列健康摘要", changelog)
        self.assertIn("TaskEngine 开启时禁止新 self-share 链接回退旧轮询路径", changelog)


if __name__ == "__main__":
    unittest.main()
