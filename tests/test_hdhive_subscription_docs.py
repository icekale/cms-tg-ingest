from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HdhiveSubscriptionDocsTests(unittest.TestCase):
    def test_user_docs_explain_tv_url_subscription_and_schedule(self):
        documents = [
            ROOT / "README.md",
            ROOT / "PRODUCT.md",
            ROOT / "docs/dockerhub-overview.md",
        ]
        for path in documents:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("HDHIVE_SUBSCRIPTION_AUTO_ENABLED", text)
                self.assertIn("HDHIVE_SUBSCRIPTION_TIME", text)
                self.assertIn("HDHIVE_SUBSCRIPTION_TIMEZONE", text)
                self.assertIn("hdhive.com/tv/<slug>", text)
                self.assertIn("01:30", text)
                self.assertIn("费用未知", text)
                self.assertIn("确认解锁", text)

    def test_user_docs_explain_subscription_does_not_immediately_unlock(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("直接发送 HDHive 剧集页面链接", readme)
        self.assertIn("创建订阅", readme)
        self.assertIn("每天", readme)


if __name__ == "__main__":
    unittest.main()
