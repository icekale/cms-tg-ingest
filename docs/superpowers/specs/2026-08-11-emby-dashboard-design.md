# Emby 看板设计

## 背景

Overview 首页的「最近入库」媒体墙已经上线（v0.2.95，历史任务海报按需补齐）。用户希望把媒体墙升级成参考 TgtoDrive「Emby 看板」的完整仪表盘形态：数据概览 + 我的媒体库分类 + 最近入库海报流，并放在独立菜单页。同时提供登录页优化（另行 spec）。

现有基础设施：

- `app/clients/emby.py` 已有 `EmbyClient`：`recent_items()`、媒体库条目、按 tmdb 查找、刷新等能力，配置来自 `EMBY_BASE_URL`/`EMBY_API_KEY`（Unraid .env 已配，指向 `http://<emby-host>:8096`）。
- Emby API 能力已实测：`/Items/Counts` 返回 `MovieCount=1089 / SeriesCount=365 / EpisodeCount=16419`；`/Users` 返回用户 `icekale`；**PlaybackReporting 插件未安装（404）**，播放统计类数据不可得。
- 前端已有设计 token（`styles.css` 亮/暗两套 CSS 变量）、`api.js`、侧栏 `App.vue`、骨架屏与降级约定。

## 目标

- 新增独立的「Emby 看板」菜单页，展示：数据概览（统计卡）、我的媒体库（分类海报条）、最近入库（海报流）。
- Emby API key 不出容器：后端聚合端点返回完整可加载的海报 URL。
- 海报点击跳转 Emby Web 详情页（新标签）。
- 主题跟随全站（暗色优先，亮色完整适配）。
- 不引入播放统计插件、不内嵌播放器、不做搜索/无限滚动。

## 架构

方案 A（已确认）：后端聚合端点 + 新 Vue 页面。

```
EmbyBoard.vue ──GET /api/v1/emby/dashboard──▶ web.py ─▶ EmbyClient(容器内 EMBY_API_KEY) ─▶ Emby
   ◀── {available, stats, libraries[], recent[]} ◀──
点击海报 → window.open(emby_base + /web/#/details?id=<emby_item_id>, "_blank")
```

## 后端

### 新端点 `GET /api/v1/emby/dashboard`

在 `app/web_api.py` 新增 `api_emby_dashboard(emby)`；`app/web.py` 注册路由（Emby 未配置/未启用时返回 `{"available": false, "reason": "emby_not_configured"}`，200）。

复用现有 `EmbyClient`（由 bridge 传入 `WebApp`，构造时透传——与 `hdhive_service`/`cms_version_checker` 同模式；若 `emby.enabled` 为 False 则不注册端点实体，直接返回 `available:false`）。

响应结构：

```json
{
  "available": true,
  "emby_base": "http://<emby-host>:8096",
  "stats": {
    "movie_count": 1089,
    "series_count": 365,
    "episode_count": 16419,
    "library_count": 13
  },
  "libraries": [
    {
      "name": "Strm欧美电影",
      "count": 456,
      "poster_url": "http://<emby-host>:8096/emby/Items/<id>/Images/Primary?maxHeight=280&apiKey=..."
    }
  ],
  "recent": [
    {
      "id": "<emby_item_id>",
      "name": "权力的游戏前传：龙族",
      "type": "Series",
      "year": 2022,
      "rating": 8.2,
      "genres": ["剧情", "奇幻"],
      "poster_url": "http://<emby-host>:8096/emby/Items/<id>/Images/Primary?maxHeight=420&apiKey=..."
    }
  ]
}
```

数据来源：

- `stats`：`/Items/Counts`（movie/series/episode）+ `len(media_folders)`。
- `libraries`：`/Library/MediaFolders` → 每库取一代表条目海报（`/Users/{user}/Items?ParentId=<id>&Limit=1&SortBy=DateCreated,SortOrder=Descending`），并用 `ParentId=<id>&Recursive=true&Limit=0` 取该库总条数。并发用 `ThreadPoolExecutor(max_workers=4)`。
- `recent`：现有 `emby.recent_items(20)`，每条取 `Id`/`Name`/`Type`/`ProductionYear`/`CommunityRating`/`Genres`，海报 URL 用 `/Images/Primary?maxHeight=420`。

海报 URL 拼接统一函数 `emby_image_url(emby_base, item_id, *, max_height, api_key)`，apiKey 只出现在服务端拼好的完整 URL 里。

### 缓存

进程内 60 秒 TTL（`time.monotonic()` + 模块级字典），与 `check_cms_strm_guard` 的 60 秒缓存模式一致；页面「刷新」按钮强制绕过缓存（端点接受 `?refresh=1`）。

### 错误处理

- Emby 未配置 → `available:false, reason="emby_not_configured"`。
- Emby 请求超时/失败 → `available:false, reason="emby_unreachable"`（记录日志，不 500）。
- 单个媒体库查询失败 → 跳过该库，其余正常返回。
- 单条 recent 缺海报 → 前端降级文字卡（复用现有 media-card fallback）。

## 前端

### 路由与菜单

- `router.js` 新增 `/emby-board` → `EmbyBoard.vue`（懒加载）。
- `App.vue` 菜单在「质量巡检」之后插入 `{ label: 'Emby 看板', key: '/emby-board' }`。

### `EmbyBoard.vue` 布局

- 页头：标题「Emby 看板」+ 副标题 + 「刷新」按钮。
- **数据概览**：4 张统计卡（电影 / 剧集 / 总集数 / 媒体库数），复用 `.stat-card`/`.stat-label`/`.stat-value`，`tabular-nums`。
- **我的媒体库**：横向滚动条——每库一张代表海报卡（2:3）+ 库名 + 数量徽标，复用 `.media-card` 视觉体系。
- **最近入库**：横向海报流（同 Overview 媒体墙视觉：海报 + 标题 + 评分徽标 + 类型标签），点击跳 Emby 详情。
- 空态：`available:false` → 引导文案（"未配置 Emby，请在 .env 设置 EMBY_BASE_URL / EMBY_API_KEY" 或 "Emby 不可达"）；加载中骨架屏。
- 主题：暗色优先——海报区靠留白与 `--surface` 分层，无边框；亮色完整适配（token 驱动）。

### api.js

新增 `embyDashboard: (refresh = false) => request(\`emby/dashboard\${refresh ? '?refresh=1' : ''}\`)`。

## 测试

- `app/web_api.py` 单测（mock `EmbyClient`）：
  - 统计/媒体库/最近入库字段齐全且结构正确。
  - `available:false`（emby 为 None / enabled=False）。
  - 缓存命中（第二次调用不重打 Emby）、`refresh=1` 绕过缓存。
  - 单库失败跳过、Emby 异常降级为 `unreachable`。
- 前端 `api.test.js`：`embyDashboard` 路径与 `?refresh=1`。
- 回归：现有 1453 后端 + 26 前端测试全部通过。

## 明确不做（边界）

- 不做播放统计卡（PlaybackReporting 插件未安装，数据不可得）。
- 不内嵌播放器、不做搜索/无限滚动/详情抽屉。
- 不把 Emby apiKey 下发到浏览器。
- 登录页优化单独走一个 spec（本次不混入）。

## 涉及文件

- 后端：`app/web_api.py`（`api_emby_dashboard` + 海报 URL 函数）、`app/web.py`（路由 + `emby` 注入）、`bridge.py`（透传 `emby` 客户端）、`app/clients/emby.py`（若需补取库条数/代表海报的小方法）。
- 前端：`frontend/src/views/EmbyBoard.vue`（新增）、`frontend/src/router.js`、`frontend/src/App.vue`、`frontend/src/api.js`、`frontend/src/styles.css`（如需要少量看板专属样式）。
- 测试：`tests/test_web_api.py`、`frontend/test/api.test.js`。
