import unittest

from app.telegram_ui import menu_keyboard
from bridge import MENU_BUTTONS


class TelegramMenuTests(unittest.TestCase):
    def test_persistent_menu_is_search_status_subscribe(self):
        self.assertEqual(
            menu_keyboard()["keyboard"],
            [
                [{"text": "🔍 搜索"}],
                [{"text": "📋 最近任务"}, {"text": "📺 订阅"}],
            ],
        )

    def test_menu_aliases_map_new_and_old_labels(self):
        self.assertEqual(MENU_BUTTONS["🔍 搜索"], "/搜索")
        self.assertEqual(MENU_BUTTONS["📺 订阅"], "/hdhive_subscriptions")
        self.assertEqual(MENU_BUTTONS["📋 最近任务"], "/status")
        self.assertEqual(MENU_BUTTONS["HDHive 搜索"], "/搜索")
        self.assertEqual(MENU_BUTTONS["HDHive 订阅"], "/hdhive_subscriptions")
        self.assertEqual(MENU_BUTTONS["📊 统计"], "/metrics")
        self.assertEqual(MENU_BUTTONS["❓ 帮助"], "/help")


if __name__ == "__main__":
    unittest.main()
