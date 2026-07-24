# cms-tg-ingest

Cloud Media Sync（CMS）的 Telegram 自动入库外挂：把 115 分享、磁力、ED2K 或 HDHive 资源交给 Bot，自动完成 CMS 整理分类、STRM 生成、媒体库移动和 Emby 入库确认。共享 STRM 模式还会创建自有永久分享并在安全门槛满足后清理 115 源文件。

```text
115 分享/磁力/ED2K -> 115 接收或云下载 -> CMS 整理分类 -> 自有永久分享 -> 分享 STRM -> Emby 入库 -> 清理转存源
```

它只编排你已经拥有权限的 CMS、115、HDHive 和 Emby 工作流，不提供媒体资源，也不绕过任何服务的权限或风控机制。

## 你可以用它做什么

- **发链接自动入库**：Telegram 接收一条或多条 115 分享、磁力、ED2K 链接，自动去重、排队和处理。
- **CMS 优先**：整理、改名、TMDB 匹配和分类由 CMS 完成，外挂不会用低准确率模型覆盖 CMS 结果。
- **两种 STRM 模式**：shared 模式继续“只入库自有分享 STRM”，使用自己的 115 分享链接生成 STRM 并保留分享；`direct` 使用 CMS 普通同步生成的直链 STRM，不创建自有分享，也不清理源文件。
- **共享别名保护**：创建分享时使用中性别名，入库时恢复 CMS 的标准目录和文件名，降低 115 名称风控影响。
- **自动清理源文件**：自有永久分享验证通过后，可以删除 115 转存源，但不会取消永久分享。
- **Emby 结果确认**：刷新后检查媒体是否入库，并返回命中的媒体库名称。
- **任务可追踪**：TaskStore 记录接收、整理、识别、建分享、STRM、移动、Emby、清理等阶段。
- **低频和风控保护**：低频 115 调用，限制扫描预算和重试频率；检测到风控后进入冷却，不连续轰击 115。
- **HDHive 搜索与解锁**：复用 CMS 已授权的单个 HDHive 账号，按 TMDB 匹配影片/剧集、筛选网盘、单条或批量解锁。
- **HDHive 剧集订阅**：用 `/订阅 <HDHive剧集链接>` 创建订阅，按计划检查新集，费用未知或较高时等待确认。
- **Web 运维台**：查看队列、阶段耗时、健康状态、质量巡检和 HDHive 订阅。
- **Vue 管理台**：访问根路径 `/` 默认进入 `/app/`；旧 Python 页面和 POST 路由继续保留，旧版概览可从 `/legacy` 回退访问。

> 当前 HDHive 使用 CMS 中的一个 OAuth 授权账号。它支持该账号的免费次数/积分查询和扣费，但尚未实现多个 Telegram 用户分别绑定不同 HDHive 账号。

## 快速部署

### Unraid 推荐方式

1. 确认 CMS 已运行，并准备好 115 Cookie、待整理目录、STRM 根目录和媒体库路径。
2. 在 Unraid 的 `/mnt/user/appdata/cms-tg-ingest/.env` 写入配置。
3. 使用项目的 compose 配置，或在 Unraid Compose Manager 中创建 `cms-tg-ingest` 服务。
4. 拉取固定版本并启动：

```sh
docker pull icekale/cms-tg-ingest:0.2.16
docker compose up -d
```

当前项目的 Unraid 示例使用 `8788:8787`，所以 Web 地址通常是：

```text
http://<unraid-ip>:8788/
```

### Docker Compose 本地部署

```sh
git clone https://github.com/icekale/cms-tg-ingest.git
cd cms-tg-ingest
cp .env.example .env
# 编辑 .env 后启动
docker compose up -d --build
```

本地 compose 默认映射 `8787:8787`，访问：

```text
http://<host-ip>:8787/
```

生产环境建议固定版本号，不要盲目使用 `latest`。镜像支持 `linux/amd64` 和 `linux/arm64`。

## 使用前提

- 已部署并可访问 Cloud Media Sync。
- CMS 已有可用的 115 Cookie 和待整理目录。
- 已创建 Telegram Bot，并知道自己的 Telegram Chat/User ID。
- 已配置 STRM 目录和媒体库映射。
- 如需 Emby 入库确认，需提供 Emby 地址和 API Key。
- 如需 HDHive，先在 CMS `转存下载 -> 影巢账号` 完成 OAuth 授权。

## 最小配置

复制 `.env.example` 后，至少填写：

```env
TG_BOT_TOKEN=你的 Telegram Bot Token
TG_ALLOWED_CHAT_ID=你的 Telegram 用户或聊天 ID
CMS_BASE_URL=http://192.168.1.10:9527
CMS_USERNAME=你的 CMS 用户名
CMS_PASSWORD=你的 CMS 密码

WORKFLOW_MODE=self_share_sync
STRM_DEFAULT_MODE=shared
P115_COOKIE_PATH=/config/115-cookies.txt
SELF_SHARE_RECEIVE_CID=你的待整理目录 CID
SELF_SHARE_STRM_ROOT=/mnt/user/Unraid/strm/share
STRM_LIBRARY_MAP=华语电影=/mnt/user/Unraid/strm/转存/Movie/电影/华语电影,欧美电影=/mnt/user/Unraid/strm/转存/Movie/电影/欧美电影

TASK_ENGINE_ENABLED=true
TASK_DB_PATH=/data/tasks.db
WEB_ENABLED=true
WEB_HOST=0.0.0.0
WEB_PORT=8787
WEB_TOKEN=生成一个随机长字符串

EMBY_BASE_URL=http://192.168.1.10:8096
EMBY_API_KEY=你的 Emby API Key
```

敏感信息只放在 `.env` 或挂载文件中，不要提交到 GitHub、Docker Hub、截图或 issue。

## Telegram 怎么用

### STRM 模式

默认模式由 `STRM_DEFAULT_MODE` 控制：

- `shared`：CMS 整理完成后创建自有永久分享，使用 CMS 分享同步生成 STRM，移动入库并按原安全门槛清理源文件。
- `direct`：CMS 普通同步生成直链 STRM，校验后直接移动入库，绝不创建分享或删除 115 源文件。

Web 的 `/app/` 和 `/api/v1/settings/strm-mode` 可修改默认模式；任务进入建分享、STRM 或之后阶段后模式会锁定，避免同一任务半路切换导致直链/分享 STRM 混用。任务级模式优先于默认值。

### 普通入库

直接发送链接即可：

```text
https://115cdn.com/s/xxxx?password=abcd
magnet:?xt=urn:btih:...
ed2k://|file|example.mkv|10|ED2K_HASH_PLACEHOLDER|/
```

- 115 分享进入接收流程。
- 磁力和 ED2K 进入 115 云下载。
- 两者都会进入 CMS 整理、自有分享 STRM、Emby 和清理流程。
- 一条消息可以包含多个链接。

### 常用命令和按钮

| 操作 | 命令/按钮 | 作用 |
| --- | --- | --- |
| 搜索 HDHive | `/搜索` | 通过 TMDB 搜索电影或剧集，再查询 HDHive 资源 |
| 兼容旧命令 | `/hdhive_search` | `/搜索` 的旧命令名，继续支持 |
| 创建订阅 | `/订阅 https://hdhive.com/tv/<slug>` | 创建 HDHive 剧集订阅，不立即解锁 |
| 订阅管理 | `HDHive 订阅` 或 `/hdhive_subscriptions` | 查看、暂停、恢复、删除、立即检查和确认解锁 |
| 最近任务 | `/status` | 查看当前队列、阶段和操作按钮 |
| 历史记录 | `/history` | 查看最近已处理任务 |
| 健康检查 | `/health` | 查看容器、TaskStore、115 冷却和外部服务状态 |
| 质量巡检 | `/quality` | 查看本地 STRM 和任务问题 |
| 清理历史 | `/clear_history` | 清理已结束的本地历史，不删除媒体文件 |
| 帮助 | `/help` | 查看当前帮助文本 |

### HDHive 搜索与解锁

1. 点击 Telegram 菜单中的 `HDHive 搜索`，或发送 `/搜索`。
2. 输入片名、剧名或 TMDB ID。
3. 选择 TMDB 候选媒体。
4. 查看 HDHive 资源，默认筛选 `115`，也可以切换其他网盘。
5. 点击单条解锁，或选择多个资源后批量解锁。
6. 费用超过自动阈值或费用未知时，点击 `确认解锁`。
7. 成功的 115 链接会自动进入现有入库流程；其他网盘链接只返回给你，不会误提交到 115 流程。

HDHive 配置：

```env
HDHIVE_ENABLED=true
HDHIVE_PROXY_BASE_URL=https://authx.771885.xyz
HDHIVE_TOKEN_CONFIG_PATH=/config/cms-config/hdhive-openapi.json
HDHIVE_SEARCH_SESSION_TTL_SECONDS=900
HDHIVE_AUTO_UNLOCK_MAX_POINTS=20
```

OAuth 文件建议挂载整个 CMS 配置目录，而不是只挂载单个文件，确保 CMS 刷新 OAuth 后外挂能读取新文件：

```yaml
- /mnt/user/appdata/cloud-media-sync/config:/config/cms-config:ro
```

### HDHive 剧集订阅

直接发送 HDHive 剧集页面链接也可以创建订阅：

```text
https://hdhive.com/tv/<slug>
```

更明确的写法是：

```text
/订阅 https://hdhive.com/tv/<slug>
```

订阅不会立即解锁。程序每天按配置时间检查新增资源，默认每天 `01:30`（`Asia/Shanghai`）。每一集只选择一个最佳的 115 资源；费用未知或超过阈值时进入待确认状态，点击 `确认解锁` 后才会继续。

```env
HDHIVE_SUBSCRIPTION_AUTO_ENABLED=true
HDHIVE_SUBSCRIPTION_TIME=01:30
HDHIVE_SUBSCRIPTION_TIMEZONE=Asia/Shanghai
```

Web 管理页：

```text
http://<unraid-ip>:8788/hdhive
```

新版管理台（根路径会自动跳转到这里）：

```text
http://<unraid-ip>:8788/app/
```

API 位于 `/api/v1/overview`、`/api/v1/tasks`、`/api/v1/health`、`/api/v1/quality` 和 `/api/v1/hdhive`，并提供任务操作、质量修复/设置、历史清理和 HDHive 订阅操作的 POST 接口。它们沿用 Web Token 认证，不返回 Cookie、访问令牌或 115 分享密码。

可查看 OAuth 状态、订阅来源、最近/下次检查、发现/入队/待确认/失败统计，并执行暂停、恢复、删除、立即检查和确认解锁。

## 完整工作流

1. Telegram 收到 115 分享、磁力或 ED2K 链接。
2. 115 分享进入接收目录，磁力/ED2K 进入云下载；随后按任务 STRM 模式执行 shared 或 direct 流程。
3. 外挂等待 CMS 完成整理、改名、TMDB 匹配和分类。
4. 外挂保存 CMS 的标准目录和分类，创建自己的中性名称永久分享。
5. 自有分享状态验证通过后，删除 115 转存源，但不取消自有永久分享。
6. 调用 CMS 分享同步，使用自己的分享链接生成 STRM。
7. 校验 STRM 使用自己的分享码，并实际探测播放端点。
8. 按 CMS 分类把 STRM 文件夹移动或合并到媒体库。
9. 调用 Emby 刷新并确认入库，返回命中的媒体库名称。
10. 任务完成后保留自己的永久分享，清理已确认的转存残留。

**重要安全门槛**：只有在自有分享、STRM marker、媒体库路径、Emby 入库和 TaskStore 成功事件均满足条件时，程序才会执行安全清理；风控、未知状态、路径不明确或验证失败都会保留源文件。

在 `TASK_ENGINE_ENABLED=true` 的 TaskRunner 路径中，自己的永久分享状态验证通过后立即删除 115 转存源；后续 STRM 只使用自己的分享链接生成。旧 SubmissionStore + 轮询路径是兼容回滚路径，不提供同等清理顺序保证。

## TaskStore 和 Web 管理

### v0.2 Alpha.2：TaskStore 接管新链接

v0.2 的任务引擎让真实 Telegram/CMS 工作流的新自分享链接默认由 TaskStore authoritative runner 执行。Telegram 收到链接后创建 task，并按以下阶段推进：接收、整理、识别、建分享、生成 STRM、移动、Emby、清理。

这意味着 **TaskStore 接管新链接**，不再只是旁路时间线：

- Web 管理页读取 TaskStore，显示当前阶段、等待原因、重试次数和最近错误。
- Telegram 新链接接收回复会返回任务 ID和当前阶段。
- `/status` 和 `/history` 优先读取 TaskStore，旧 SubmissionStore 记录为空时兜底显示。
- /status 会附带详情、重试、查 Emby、恢复 STRM、从头重跑按钮。
- Web 任务详情页提供重试、查 Emby、恢复 STRM、从头重跑按钮。
- Vue 任务详情页和旧任务详情页都保留这些操作，并显示事件时间线、阶段耗时和 115 调用统计。
- /health 会显示 TaskStore 本地队列健康、worker 心跳和 115 风控冷却。
- /quality 会先执行 TaskStore 本地轻量巡检。
- Web `/quality` 页面只读取本地 TaskStore 和 STRM 文件；Vue 和旧版页面都支持本地巡检、自动巡检设置和修复入队，不扫描 115。
- TaskEngine 开启时，新 self-share 链接不会回退到旧 start_status_poll 轮询路径。

关键设置：

```env
TASK_ENGINE_ENABLED=true
TASK_DB_PATH=/data/tasks.db
WEB_ENABLED=true
WEB_HOST=0.0.0.0
WEB_PORT=8787
TASK_WORKER_INTERVAL_SECONDS=5
TASK_MAX_RETRIES=3
```

如需回滚，设置 `TASK_ENGINE_ENABLED=false` 并重启；旧 SubmissionStore + 轮询路径是兼容回滚路径，不提供 TaskRunner 的同等清理顺序保证。

## 质量巡检

首次部署建议保持关闭，手动完成一次完整入库测试后再开启：

```env
QUALITY_AUTO_ENABLED=true
QUALITY_AUTO_TIME=02:50
QUALITY_AUTO_TIMEZONE=Asia/Shanghai
QUALITY_AUTO_MAX_TASKS=50
QUALITY_AUTO_115_CHECK_LIMIT=3
```

质量巡检只读取本地 TaskStore 和 STRM 文件，检查缺失 STRM、直链 STRM、异常目录和需要恢复的任务。它不扫描整个 115 网盘。风控、未知分享状态和路径不安全时只记录问题，不自动删除文件。

## 路径与风控配置

容器内路径必须和 `.env` 保持一致：

```yaml
volumes:
  - ./data:/data
  - /mnt/user/Unraid/strm:/mnt/user/Unraid/strm:rw
  - /mnt/user/appdata/cloud-media-sync/config/115-cookies.txt:/config/115-cookies.txt:ro
  - /mnt/user/appdata/cloud-media-sync/config/cms-online.db:/cms/cms-online.db:ro
  - /mnt/user/appdata/cloud-media-sync/config:/config/cms-config:ro
```

`CMS_PARENT_CID_CATEGORY_MAP` 用于把 CMS 整理后的 115 父目录映射到分类，`STRM_LIBRARY_MAP` 用于把分类映射到媒体库目录。两者都和个人目录结构相关，不能直接复制别人的 CID。

建议保留这些保护：

```env
P115_MIN_REQUEST_INTERVAL_SECONDS=2
P115_RISK_COOLDOWN_SECONDS=900
MOVE_CONFLICT_POLICY=merge
STRM_STABLE_SECONDS=30
```

115 风控后，程序会暂停新的全局 115/CMS 阶段，避免连续重试。整理目录查找使用分层搜索早停和整理目录扫描预算，降低频繁扫描风险。

## 更新、诊断和回滚

更新固定版本：

```sh
docker compose pull
docker compose up -d
```

查看日志和健康状态：

```sh
docker compose logs --tail=200 -f cms-tg-ingest
docker compose exec cms-tg-ingest python /app/doctor.py
docker compose exec cms-tg-ingest python /app/doctor.py --quiet
```

如果任务卡住：

1. 先看 `/status` 或 Web 当前队列，确认卡在哪个阶段。
2. 看 `/health` 的 115 风控冷却和 worker 心跳。
3. 不要连续点击重试；等待冷却结束后再重试当前阶段。
4. 只有确认状态安全时，才使用“从头重跑”或质量修复。

回滚到上一版本：

```sh
docker compose down
docker pull icekale/cms-tg-ingest:0.2.11
# 将 compose 的 image 改为 0.2.11
docker compose up -d
```

## 开发与发布

本地测试：

```sh
python3 -m py_compile bridge.py doctor.py
python3 -m unittest discover -s tests -q
```

发布版本通过 GitHub Actions 构建并推送 GHCR 和 Docker Hub：

```sh
git tag v0.2.16
git push origin v0.2.16
```

如果 fork 后要发布自己的 Docker Hub 镜像，在 GitHub Secrets 中配置：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名或命名空间。
- `DOCKERHUB_TOKEN`：Docker Hub access token。

镜像：

```sh
docker pull icekale/cms-tg-ingest:0.2.16
docker pull icekale/cms-tg-ingest:latest
```

## 安全说明

- 不要公开 `.env`、115 Cookie、Telegram Bot Token、Emby API Key、OAuth 文件或 OpenAI/TMDB Key。
- 启用 Web 管理台时必须设置随机 `WEB_TOKEN`；不要把管理端口以匿名方式暴露。
- HDHive 当前使用 CMS 已授权的单个账号，不要把 OAuth 配置文件复制到公共目录。
- 生产环境固定版本号，不建议盲目使用 `latest`。
- 批量操作前先用一个小体量资源测试完整链路。
- `SELF_SHARE_CLEANUP_AFTER_EMBY=true` 的安全清理依赖 TaskStore authoritative runner；旧回滚路径不提供同等清理顺序保证。

## 相关链接

- GitHub：[github.com/icekale/cms-tg-ingest](https://github.com/icekale/cms-tg-ingest)
- Docker Hub：[hub.docker.com/r/icekale/cms-tg-ingest](https://hub.docker.com/r/icekale/cms-tg-ingest)
- Web 管理页：`http://<unraid-ip>:8788/`
