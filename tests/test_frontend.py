import json
import struct
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

    def test_media_vault_brand_assets_exist_with_expected_dimensions(self):
        brand_dir = ROOT / "frontend/public/brand"
        logo = brand_dir / "logo-mark.svg"
        favicon = brand_dir / "favicon-32.png"
        touch_icon = brand_dir / "apple-touch-icon.png"

        self.assertTrue(logo.is_file())
        self.assertTrue(favicon.is_file())
        self.assertTrue(touch_icon.is_file())

        svg = logo.read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 64 64"', svg)
        self.assertIn("#4c5fd5", svg)
        self.assertNotIn("#1D4ED8", svg)
        self.assertIn('aria-label="媒体仓"', svg)

        def png_dimensions(path):
            payload = path.read_bytes()[:24]
            self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
            return struct.unpack(">II", payload[16:24])

        self.assertEqual(png_dimensions(favicon), (32, 32))
        self.assertEqual(png_dimensions(touch_icon), (180, 180))

    def test_vue_admin_wires_media_vault_logo_and_icons(self):
        index = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        shell = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
        styles = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")

        for expected in (
            "%BASE_URL%brand/logo-mark.svg",
            "%BASE_URL%brand/favicon-32.png",
            "%BASE_URL%brand/apple-touch-icon.png",
            'name="theme-color" content="#4c5fd5"',
            'name="theme-color" media="(prefers-color-scheme: dark)" content="#10121a"',
        ):
            self.assertIn(expected, index)

        self.assertIn("brandLogoUrl", shell)
        self.assertIn("import.meta.env.BASE_URL", shell)
        self.assertIn('class="brand-logo"', shell)
        self.assertIn(':src="brandLogoUrl"', shell)
        self.assertIn('alt=""', shell)
        self.assertNotIn('<span class="brand-mark">CMS</span>', shell)
        self.assertIn(".brand-logo", styles)
        self.assertNotIn(".brand-mark", styles)
        self.assertIn('aria-label="主导航"', shell)
        self.assertIn("navIcons", shell)
        self.assertIn("collapse-mode", shell)
        self.assertIn("max-width: 860px", shell)
        self.assertIn("max-width: 520px", shell)
        self.assertIn('class="theme-toggle nav-toggle"', shell)
        self.assertIn('aria-controls="primary-nav"', shell)
        self.assertIn("collapsedWidth", shell)
        self.assertIn("nav-backdrop", shell)
        self.assertIn(".issue-card-actions .n-button", styles)
        self.assertIn(".subscription-row .n-button", styles)
        self.assertIn(".desktop-table .n-button", styles)
        self.assertIn("min-height: 44px", styles)
        self.assertNotIn("repeat(3, minmax(0, 1fr))", styles)
        self.assertIn("--media-score:", styles)
        self.assertIn("var(--media-score)", styles)
        self.assertNotIn("color: #ffd766", styles)

    def test_vue_admin_footer_uses_product_signature_layout(self):
        shell = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
        styles = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")

        self.assertIn('class="app-footer"', shell)
        self.assertIn('class="footer-signature"', shell)
        self.assertIn('class="footer-product"', shell)
        self.assertIn("115 · CMS · Emby 工作流", shell)
        self.assertIn('class="footer-version"', shell)
        self.assertIn(':src="brandLogoUrl"', shell)
        self.assertIn(".footer-signature", styles)
        self.assertIn(".footer-version", styles)
        self.assertIn("@media (max-width: 520px)", styles)
        self.assertIn(".footer-caption", styles)
        self.assertIn("display: none", styles)

    def test_vue_admin_scrolls_long_pages_while_footer_stays_in_view(self):
        styles = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")

        for expected in (
            ".admin-shell { height: 100vh;",
            ".main-column { height: calc(100vh - 60px); min-height: 0;",
            ".main-column > .n-layout-scroll-container",
            ".content-wrap { flex: 1 1 auto; min-height: 0;",
            ".content-wrap > .n-layout-scroll-container",
            ".app-footer { flex: 0 0 auto;",
        ):
            self.assertIn(expected, styles)

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
        for control in ("run", "settings", "reset"):
            self.assertIn(control, quality)
        self.assertIn("立即巡检", quality)
        self.assertNotIn("批量修复", quality)
        self.assertNotIn("window.confirm", quality)
        self.assertIn("n-popconfirm", quality)
        self.assertIn("QUALITY_CLEANUP_CONFIRM", quality)
        self.assertIn("var(--warning)", quality)
        self.assertNotIn("var(--danger)", quality)
        self.assertIn("Promise.all", quality)
        self.assertIn("qualityRuns()", quality)
        for control in ("pause", "resume", "delete", "check"):
            self.assertIn(control, hdhive)
        self.assertIn("hdhiveSubscriptionAction", api)
        self.assertIn("hdhiveSubscriptionFilter", api)
        self.assertIn("hdhiveCreateSubscription", api)
        self.assertIn("hdhiveItemConfirm", api)
        self.assertNotIn("window.confirm", task_detail)
        self.assertNotIn("window.confirm", hdhive)
        self.assertNotIn("window.confirm", settings)
        self.assertIn("n-popconfirm", task_detail)
        self.assertIn("n-popconfirm", hdhive)
        self.assertIn("n-popconfirm", settings)
        self.assertIn("关闭观察", settings)
        for control in ("confirm", "run", "settings"):
            self.assertIn(control, hdhive)
        for control in ("episode_filter", "已完结", "设置集数过滤", "资源状态", "diagnosis", "waitForHdhiveJob", "添加订阅", "hdhiveCreateSubscription"):
            self.assertIn(control, hdhive)
        self.assertNotIn("未据此跳过资源", hdhive)
        for mode in ("shared", "direct", "source_shared"):
            self.assertIn(mode, settings)
        self.assertIn("设置", shell)
        self.assertIn("cms-tg-ingest", shell)
        self.assertIn("version", shell)
        self.assertIn("cmsVersionUpgrade", api)
        self.assertIn("waitForCmsJob", settings)
        self.assertIn("升级", settings)
        self.assertNotIn("升级指引（在宿主机执行）", settings)
        health = (ROOT / "frontend/src/views/Health.vue").read_text(encoding="utf-8")
        self.assertIn("<h2>等待原因</h2>", health)
        self.assertIn("<h2>最近问题</h2>", health)
        self.assertNotIn("<h3>", health)
        theme = (ROOT / "frontend/src/themeOverrides.js").read_text(encoding="utf-8")
        self.assertIn("primaryColorHover: '#3e4ec4'", theme)


if __name__ == "__main__":
    unittest.main()
