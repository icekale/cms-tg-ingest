import os
import unittest
from unittest.mock import patch

from app.config import Config, parse_review_checkpoints


class ReviewConfigTests(unittest.TestCase):
    def test_review_checkpoints_require_strict_order_and_end_at_grace(self):
        self.assertEqual(
            parse_review_checkpoints("600,3600,21600,86400", 86400),
            (600, 3600, 21600, 86400),
        )
        with self.assertRaises(ValueError):
            parse_review_checkpoints("600,600,86400", 86400)
        with self.assertRaises(ValueError):
            parse_review_checkpoints("600,3600", 86400)

    def test_from_env_carries_review_window_values(self):
        env = {
            "TG_BOT_TOKEN": "token",
            "TG_ALLOWED_CHAT_ID": "chat",
            "CMS_BASE_URL": "http://cms",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "password",
            "SELF_SHARE_REVIEW_GRACE_SECONDS": "86400",
            "SELF_SHARE_REVIEW_CHECKPOINTS_SECONDS": "600,3600,21600,86400",
            "SELF_SHARE_REVIEW_LIST_CACHE_SECONDS": "300",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()

        self.assertEqual(config.self_share_review_grace_seconds, 86400)
        self.assertEqual(config.self_share_review_checkpoints_seconds, (600, 3600, 21600, 86400))
        self.assertEqual(config.self_share_review_list_cache_seconds, 300)


if __name__ == "__main__":
    unittest.main()
