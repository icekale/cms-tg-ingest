import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendTests(unittest.TestCase):
    def test_vue_admin_shell_has_expected_routes_and_build_contract(self):
        package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
        self.assertIn("vue", package["dependencies"])
        self.assertIn("naive-ui", package["dependencies"])
        self.assertIn("build", package["scripts"])
        router = (ROOT / "frontend/src/router.js").read_text(encoding="utf-8")
        for route in ("/overview", "/tasks", "/quality", "/health", "/hdhive", "/logs", "/settings"):
            self.assertIn(route, router)
        self.assertIn("base: '/app/'", (ROOT / "frontend/vite.config.js").read_text(encoding="utf-8"))

    def test_vue_admin_exposes_realtime_logs_route_and_lifecycle_controls(self):
        router = (ROOT / "frontend/src/router.js").read_text(encoding="utf-8")
        shell = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
        page = (ROOT / "frontend/src/views/Logs.vue").read_text(encoding="utf-8")
        helper = (ROOT / "frontend/src/logView.js").read_text(encoding="utf-8")

        self.assertIn("/logs", router)
        self.assertIn("实时日志", shell)
        for text in ("重要", "错误", "全部", "1000", "2000", "5000", "重连", "清空"):
            self.assertIn(text, page)
        self.assertIn('maxlength="100"', page)
        self.assertIn("slice(0, 100)", page)
        for label in ("日志关键字", "实时日志输出"):
            self.assertIn(f'aria-label="{label}"', page)
        for label in ("日志级别", "日志行数"):
            self.assertIn(f":input-props=\"{{ 'aria-label': '{label}' }}\"", page)
        self.assertNotIn('n-select v-model:value="filterType" aria-label=', page)
        self.assertNotIn('n-select v-model:value="lineLimit" aria-label=', page)
        self.assertIn('tabindex="0"', page)
        self.assertIn("onBeforeUnmount", page)
        self.assertIn("preservedScrollTop", page)
        self.assertIn("withCredentials: true", helper)
        self.assertNotIn("WEB_TOKEN", helper)

    def test_vue_log_selects_use_filterable_input_props_for_accessible_trigger(self):
        page = (ROOT / "frontend/src/views/Logs.vue").read_text(encoding="utf-8")

        self.assertIn('<n-select v-model:value="filterType" filterable :input-props="{ \'aria-label\': \'日志级别\' }" :options="filterOptions" style="width: 120px" />', page)
        self.assertIn('<n-select v-model:value="lineLimit" filterable :input-props="{ \'aria-label\': \'日志行数\' }" :options="lineOptions" style="width: 120px" />', page)

    def test_vue_admin_exposes_migrated_operational_controls(self):
        api = (ROOT / "frontend/src/api.js").read_text(encoding="utf-8")
        task_detail = (ROOT / "frontend/src/views/TaskDetail.vue").read_text(encoding="utf-8")
        quality = (ROOT / "frontend/src/views/Quality.vue").read_text(encoding="utf-8")
        hdhive = (ROOT / "frontend/src/views/Hdhive.vue").read_text(encoding="utf-8")
        overview = (ROOT / "frontend/src/views/Overview.vue").read_text(encoding="utf-8")
        settings = (ROOT / "frontend/src/views/Settings.vue").read_text(encoding="utf-8")
        shell = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")

        self.assertIn("taskAction", api)
        for action in ("retry", "emby", "restore", "reprocess"):
            self.assertIn(action, task_detail)
        self.assertIn("clearHistory", api)
        self.assertIn("clearHistory", overview)
        self.assertIn("setOwnShareReceiveCode", api)
        self.assertIn("clearOwnShareReceiveCode", api)
        self.assertIn("setSelfShareReceiveCid", api)
        self.assertIn("setSelfShareReview", api)
        self.assertIn("待整理目录", settings)
        self.assertIn("分享审核观察", settings)
        self.assertIn("分享访问码", settings)
        self.assertIn('type="password"', settings)
        for control in ("fix", "run", "settings", "reset"):
            self.assertIn(control, api)
            self.assertIn(control, quality)
        for control in ("pause", "resume", "delete", "check"):
            self.assertIn(control, hdhive)
        self.assertIn("hdhiveSubscriptionAction", api)
        self.assertIn("hdhiveSubscriptionFilter", api)
        self.assertIn("hdhiveItemConfirm", api)
        for control in ("confirm", "run", "settings"):
            self.assertIn(control, hdhive)
        for control in ("episode_filter", "已完结", "emby_skip_unavailable", "设置集数过滤"):
            self.assertIn(control, hdhive)
        for mode in ("shared", "direct", "source_shared"):
            self.assertIn(mode, settings)
        self.assertIn("设置", shell)
        self.assertIn("cms-tg-ingest", shell)
        self.assertIn("version", shell)


if __name__ == "__main__":
    unittest.main()
