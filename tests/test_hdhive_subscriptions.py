import unittest
from unittest.mock import patch

from app.config import Config
from app.hdhive_subscriptions import HdhiveUrlError, parse_hdhive_tv_url


class HdhiveSubscriptionUrlTests(unittest.TestCase):
    def test_parse_hdhive_tv_url_accepts_hdhive_tv_pages(self):
        parsed = parse_hdhive_tv_url(
            "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5"
        )

        self.assertEqual(parsed.slug, "542a1c1fe6ac4a5aab152369079596b5")
        self.assertEqual(parsed.url, "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5")

    def test_parse_hdhive_tv_url_rejects_other_hosts_and_paths(self):
        for value in (
            "https://evil.example/tv/542a1c1fe6ac4a5aab152369079596b5",
            "https://hdhive.com/movie/542a1c1fe6ac4a5aab152369079596b5",
            "https://hdhive.com/tv/short",
            "https://hdhive.com/tv/not-a-valid-slug!",
        ):
            with self.subTest(value=value):
                with self.assertRaises(HdhiveUrlError):
                    parse_hdhive_tv_url(value)

    def test_subscription_schedule_defaults_and_env_overrides(self):
        required = {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms.test",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "password",
        }
        with patch.dict("os.environ", required, clear=True):
            defaults = Config.from_env()
        self.assertTrue(defaults.hdhive_subscription_auto_enabled)
        self.assertEqual(defaults.hdhive_subscription_time, "01:30")
        self.assertEqual(defaults.hdhive_subscription_timezone, "Asia/Shanghai")

        with patch.dict(
            "os.environ",
            {**required, "HDHIVE_SUBSCRIPTION_TIME": "03:15", "HDHIVE_SUBSCRIPTION_TIMEZONE": "UTC"},
            clear=True,
        ):
            overridden = Config.from_env()
        self.assertEqual(overridden.hdhive_subscription_time, "03:15")
        self.assertEqual(overridden.hdhive_subscription_timezone, "UTC")


if __name__ == "__main__":
    unittest.main()
