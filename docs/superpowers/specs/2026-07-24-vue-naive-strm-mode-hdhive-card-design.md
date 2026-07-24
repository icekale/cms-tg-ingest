# Vue Naive Admin、STRM 模式与 HDHive 订阅卡片设计

## 背景

当前 Web UI 使用 Python 标准库服务端渲染，已经能够展示 TaskStore、质量巡检、本地健康和 HDHive 订阅，但页面缺少统一的轻量后台壳。当前入库工作流默认使用自有分享 STRM；用户还需要在 Web UI 中选择直链 STRM，并希望 HDHive 订阅结果以 Telegram 海报卡片形式反馈。

本设计覆盖三项变更：

1. 使用 `boyazuo/awesome-admin` 收录的 `zclzone/vue-naive-admin` 作为轻量前端结构参考和开源模板基础。
2. 增加安全可区分的 `shared`（共享 STRM）和 `direct`（直链 STRM）模式。
3. 增加接近现有截图的 HDHive 订阅资源卡片，并记录实际/估算积分和解锁时间。

## 已确认决策

- 模板：`vue-naive-admin`，MIT License。
- 前端上线：先使用 `/app`，现有 `/` SSR 页面保留为回退；验证后再考虑切换根路径。
- 后端：保留现有 Python `WebApp`、TaskStore、CMS/115/Emby 工作流和 POST 路由。
- STRM：全局默认模式 + 任务级覆盖；默认仍为共享 STRM。
- 直链模式：使用 CMS 普通同步，不创建自有永久分享，不执行共享模式的源文件自动清理。
- 共享模式：沿用当前自有分享、分享 STRM、Emby 确认和安全清理门槛。
- 海报：优先使用 TMDB，失败时发送纯文字卡片，不阻塞解锁和入库。
- 积分：优先记录接口实际消耗；接口未提供时记录资源预计积分并标记为估算。

## 目标

- 在 Web UI 中清楚地切换默认 STRM 模式，并在任务详情中查看/覆盖模式。
- 在不复制业务逻辑的前提下，为 Vue 页面提供概览、任务、健康、质量和 HDHive 订阅的只读数据。
- 让直链和共享任务的副作用明确可见，防止直链模式误删 115 源文件。
- 让订阅资源反馈包含封面、资源规格、积分、TMDB 信息、入库状态和可追踪任务。
- 保持现有 SSR 页面、POST 操作、WEB_TOKEN 行为和旧数据库兼容。

## 非目标

- 不一次性替换现有 `/` 页面。
- 不引入新的登录系统、WebSocket、自动高频轮询或前端状态服务。
- 不修改 CMS 的整理、改名、分类或 Emby 入库规则。
- 不让直链模式绕过现有路径校验、TMDB 错配保护或 Emby 确认流程。
- 不把完整模板示例、图表和无关后台模块全部带入项目。

## 架构

### 前端

在 `frontend/` 建立 Vue 3 + Vite 前端，采用 `vue-naive-admin` 的布局、路由、主题和 Naive UI 组件组织方式，只保留本项目需要的页面和组件：

- 应用壳：侧边导航、顶部状态、响应式布局和主题变量。
- 运行概览：需要关注、当前队列、阶段进度、等待原因、耗时和 115 调用统计。
- 任务详情：阶段时间线、STRM 模式、Emby 结果、普通操作和危险操作。
- HDHive 订阅：订阅状态、自动检查、资源统计、待确认资源和解锁记录。
- 质量巡检、本地健康：只读摘要，保留现有诊断信息入口。

### 后端

现有 Python `WebApp` 继续负责认证、页面回退和 POST 操作。新增 `/api/v1/` 只读接口，读取已有本地服务对象，不在页面渲染中增加无关外部请求：

```text
GET /api/v1/overview
GET /api/v1/tasks?limit=<n>
GET /api/v1/tasks/<id>
GET /api/v1/health
GET /api/v1/quality
GET /api/v1/hdhive/subscriptions
GET /api/v1/settings/strm-mode
```

Vue 端的暂停、恢复、立即检查、确认解锁、重试和恢复 STRM 等操作继续提交现有 POST 路由。所有 `/app` 和 API 请求复用现有 `WEB_TOKEN` 的 Cookie、查询参数和 `X-Web-Token` 校验。

### 部署

Dockerfile 增加 Node 构建阶段：安装锁定的前端依赖并生成静态文件，再复制到 Python 运行阶段。运行容器不常驻 Node 进程，不改变现有端口和数据挂载。前端构建失败时不应覆盖 Python 运行镜像的现有 SSR 能力。

## STRM 模式

### 模式定义

`shared`：

```text
115 接收 -> CMS 整理分类 -> 创建自有永久分享 -> 验证分享
-> CMS 分享同步 -> 验证分享 STRM -> 移动 -> Emby 确认 -> 清理源
```

`direct`：

```text
115 接收/普通 CMS 提交 -> CMS 普通同步 -> 验证直链 STRM
-> 按 CMS 分类移动 -> Emby 确认
```

直链模式不得执行“自有分享验证后删除转存源”的共享模式清理动作。两种模式都继续执行标题/TMDB/路径匹配、STRM 来源校验和 Emby 确认。

### 选择规则

- TaskStore 设置保存 `default_strm_mode`，值只能是 `shared` 或 `direct`。
- 新任务把默认模式写入任务元数据 `strm_mode`。
- 任务进入 STRM 阶段前可以在任务详情覆盖模式。
- 任务一旦进入 STRM 阶段，模式锁定；切换必须使用“从头重跑为直链/共享”。
- Web 选择器显示安全提示：共享模式可在验证后清理源；直链模式保留源文件。
- 旧任务没有模式时，根据已有 `workflow_mode` 和元数据兼容推断，不修改历史成功结果。

## HDHive 资源卡片

### 数据来源

- HDHive 资源接口：资源名称、季集、网盘、大小、分辨率、字幕、积分和有效性。
- TMDB 详情：海报、年份、地区、类型、主演、标签和简介。
- TaskStore：提交后的任务号、阶段和入库结果。

TMDB 详情按 `media_type + tmdb_id` 缓存，避免订阅每日巡检重复请求。TMDB 海报 URL 不可用时只退化展示文字，不影响 HDHive 解锁。

### Telegram 展示

成功解锁并提交 CMS 后，优先调用 `sendPhoto` 发送海报，caption 使用 HTML 转义和长度限制；无海报时调用 `sendMessage`。内容顺序如下：

```text
HDHIVE 资源获取成功
资源名：<title>
<季集> <分辨率> <网盘>

💰 消耗积分：免费 / <n> 积分 / <n> 积分（估算）
👤 分享者：<source>
📦 大小：<share_size>
💬 字幕：<subtitle>
🆔 TMDB：<tmdb_id>
🌍 地区：<region>
📁 分类：<genre/category>
🎬 主演：<cast>
🏷 标签：<tags>
🔗 115 链接：1 个
🕒 时间：<unlocked_at>

📖 简介：<truncated_overview>

✅ CMS 接收指令，执行整理入库中
```

状态必须真实对应：

- 待确认：显示费用和“确认解锁”按钮，不显示成功。
- 解锁失败：显示失败原因，不提交 CMS。
- 解锁成功但 CMS 入队失败：显示 HDHive 成功、CMS 入队失败，并保留可重试入口。
- TMDB 失败：显示资源和任务信息，封面/详情字段显示“未知”。

按钮使用现有回调体系扩展任务详情、订阅管理和立即检查，不在卡片中暴露敏感凭据或完整 OAuth 信息。

### 解锁记录

扩展 `hdhive_subscription_items`：

- `estimated_points`：资源预计积分。
- `spent_points`：实际消耗积分或估算值。
- `points_source`：`api`、`estimated` 或 `free`。
- `unlocked_at`：成功解锁时间。
- `task_id`：关联的 TaskStore 任务 ID。

扩展订阅运行摘要，累计保存本次检查的发现、入队、失败、待确认和积分消耗。Web 订阅页新增“解锁记录”表，展示剧集、资源、积分、积分来源、解锁时间、任务号和状态；旧记录缺失字段时显示 `-`，不回填虚假的时间或积分。

如果 HDHive 解锁响应没有实际积分字段，先使用资源预计积分并明确标记为 `estimated`。后续代理增加实际扣费字段时直接记录 `api` 来源。必要时可在解锁前后读取账户积分作为一致性校验，但不把额外账户查询作为成功解锁的必要条件。

## 测试与验证

### Python

- API 的 WEB_TOKEN 认证、只读响应结构和不存在资源的错误响应。
- STRM 默认模式、任务继承、模式锁定和从头重跑模式覆盖。
- 直链模式不触发共享清理；共享模式保持现有清理门槛。
- HDHive 积分字段解析、免费/实际/估算来源和时间落库。
- 订阅卡片状态分支、TMDB 失败降级、sendPhoto/sendMessage 选择。
- 数据库迁移在旧数据库上可重复执行。

### 前端

- `/app` 路由、导航、模式选择器和权限失败页面。
- 订阅资源卡片、积分/时间展示、待确认和失败状态。
- 任务详情的共享/直链提示和危险操作确认。
- 360px、768px、1440px 下无横向溢出；按钮、状态色和键盘焦点可用。

### 回归

```sh
python3 -m unittest tests.test_hdhive_subscriptions tests.test_hdhive_web tests.test_web_admin -q
python3 -m unittest discover -s tests -q
git diff --check
```

部署前用一个免费 HDHive 剧集资源验证：订阅创建、资源卡片、积分/时间记录、CMS 入队、共享或直链模式、Emby 确认和源文件保护。

## 回滚

- `/` SSR 页面和所有旧 POST 路由始终保留。
- Vue 构建失败或 `/app` 不可用时，用户继续使用 `/`。
- 直链/共享模式写入的是新增设置和任务元数据，不破坏旧任务。
- 数据库迁移只增加字段和表，不删除历史数据。

## 许可证与第三方声明

前端采用 `zclzone/vue-naive-admin` 的 MIT 授权代码/结构时，在仓库中保留其许可证和第三方声明；本项目自有代码继续使用现有许可证。不得复制与本项目无关的示例账号、演示数据或第三方密钥。

