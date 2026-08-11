# cms-tg-ingest

Cloud Media Sync（CMS）的 Telegram 自动入库外挂。把 115 分享、磁力、ED2K 或 HDHive 资源发给 Bot，自动完成 CMS 整理分类、STRM 生成、媒体库移动和 Emby 入库确认。

```text
链接 -> 115 接收/云下载 -> CMS 整理 -> shared/direct STRM -> 媒体库 -> Emby
```

## 核心能力

- Telegram 支持裸链接、多链接、任务状态和按钮式运维。
- CMS 优先完成整理、改名、TMDB 匹配和分类。
- 支持 `shared` 共享 STRM 和 `direct` 直链 STRM；任务锁定后不允许切换，避免混用。
- 自有分享保留 CMS 的标准目录名，历史 `asset-*` 分享继续兼容，不自动改名。
- TaskStore 记录每个阶段、等待原因、失败、重试和耗时。
- 115 调用有频率限制、扫描预算和风控冷却。
- 支持磁力、ED2K 云下载，随后按 STRM 模式进入对应流程；direct 模式不会创建分享或清理源文件。
- 磁力和 ED2K 始终锁定 shared STRM；云下载输出支持多文件，并在移动中断后幂等恢复。
- 支持 Emby 刷新、入库确认和媒体库名称反馈。
- 支持 HDHive 搜索、网盘筛选、单条/批量解锁和剧集订阅。
- 质量巡检支持 Web/Telegram 人工队列，展示规则、风险、尝试次数和脱敏证据，并支持确认后执行、重跑、暂缓、忽略和恢复评估。
- **Emby 看板**：数据概览（电影/剧集/集数/媒体库数）、我的媒体库（各库代表海报 + 数量）、最近入库海报流，点击直达 Emby 详情/播放；Emby API Key 只在服务端使用。
- **暗色模式**：Web 管理台跟随系统深浅色，顶栏可手动切换并记住选择；登录页同步适配。
- **CMS 版本远程检测**：设置页「立即检查」对比本地 CMS 与 Docker Hub 最新 tag，发现新版直接提示。

## 5 分钟部署

固定版本镜像：

```sh
docker pull icekale/cms-tg-ingest:0.3.1
```

### 完整 Docker Compose

下面的配置可以直接使用 Docker Hub 镜像，不需要本地构建。将其保存为 `docker-compose.yml`，并在同一目录创建 `.env`：

```yaml
services:
  cms-tg-ingest:
    image: icekale/cms-tg-ingest:0.3.1
    container_name: cms-tg-ingest
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "8787:8787"
    volumes:
      - ./data:/data
      # STRM 源目录和媒体库目录，必须与 .env 中的路径一致。
      - /mnt/user/Unraid/strm:/mnt/user/Unraid/strm:rw
      # CMS 导出的 115 Cookie。
      - /mnt/user/appdata/cloud-media-sync/config/115-cookies.txt:/config/115-cookies.txt:ro
      # CMS 在线数据库，用于识别整理后的目录。
      - /mnt/user/appdata/cloud-media-sync/config/cms-online.db:/cms/cms-online.db:ro
      # CMS 配置目录，用于读取并跟随 OAuth 刷新后的 HDHive 授权文件。
      - /mnt/user/appdata/cloud-media-sync/config:/config/cms-config:ro
      # Docker Socket（CMS 守卫检查与镜像拉取需要，可去掉以增强隔离）。
      - /var/run/docker.sock:/var/run/docker.sock:ro
    healthcheck:
      test: ["CMD", "python", "/app/doctor.py", "--quiet"]
      interval: 5m
      timeout: 20s
      retries: 2
      start_period: 30s
```

`.env` 至少填写以下内容；完整变量和注释见 [GitHub `.env.example`](https://github.com/icekale/cms-tg-ingest/blob/main/.env.example)：

```env
TG_BOT_TOKEN=从BotFather获取的Token
TG_ALLOWED_CHAT_ID=你的Telegram用户或聊天ID
CMS_BASE_URL=http://192.168.1.10:9527
CMS_USERNAME=你的CMS用户名
CMS_PASSWORD=你的CMS密码
P115_COOKIE_PATH=/config/115-cookies.txt
SELF_SHARE_RECEIVE_CID=待整理目录CID
SELF_SHARE_STRM_ROOT=/mnt/user/Unraid/strm/share
SELF_SHARE_OWN_SHARE_PASSWORD=1212
STRM_LIBRARY_MAP=华语电影=/mnt/user/Unraid/strm/转存/Movie/电影/华语电影,欧美电影=/mnt/user/Unraid/strm/转存/Movie/电影/欧美电影
STRM_DEFAULT_MODE=shared
TASK_ENGINE_ENABLED=true
WEB_ENABLED=true
# Web 认证（推荐用户名密码，与 WEB_TOKEN 二选一）
WEB_USERNAME=admin
WEB_PASSWORD=change-me
WEB_TOKEN=
BACKUP_ENABLED=true
BACKUP_TIME=03:30
BACKUP_TIMEZONE=Asia/Shanghai
BACKUP_DIR=/data/backups
BACKUP_RETENTION_DAYS=14
EMBY_BASE_URL=http://192.168.1.10:8096
EMBY_API_KEY=你的Emby_API_Key
EMBY_USER_ID=你的Emby用户ID（可选）
# 可选：媒体墙海报/评分与历史任务海报补齐
TMDB_API_KEY=你的TMDB v3 API Key
```

`TG_BOT_TOKEN` 和密码只放在 `.env`，不要写进 Compose、Dockerfile 或公开 issue。`TG_ALLOWED_CHAT_ID` 用于限制只有指定 Telegram 用户可以操作 Bot。

自有分享访问码可在 Web“当前任务”页设置。新任务按“Web 设置 -> CMS `share_115_sync` 配置 -> `SELF_SHARE_OWN_SHARE_PASSWORD` -> `1212`”解析；读取接口只显示掩码和来源，已有分享不会被批量修改。

待整理目录可在新版 Web UI“设置”中修改。Web 保存值优先于 `SELF_SHARE_RECEIVE_CID`，写入 TaskStore 并在重启后保留；点击“使用环境配置”可恢复 `.env` 值。它同时控制 115 转存和云下载的目标目录。

启动和查看日志：

```sh
mkdir -p data
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f cms-tg-ingest
```

### 实时日志

- 默认 Compose 使用 `8787:8787` 时，打开 `http://<host-ip>:8787/app/logs`；Unraid 改为 `8788:8787` 时，打开 `http://<unraid-ip>:8788/app/logs`。页面支持重要/错误/全部、关键字和 1000/2000/5000 行筛选。
- “清空”只清空当前浏览器内容，不删除磁盘日志。
- 日志同时输出到 `docker logs` 和 `/data/logs/cms-tg-ingest.log`；当前文件达到 20 MiB 后轮转，保留 4 个备份。
- 容器继续使用现有 `./data:/data` 挂载，不需要增加日志 volume；重启后恢复最近最多 5000 行。
- 配置 `WEB_TOKEN` 时，先通过 `/app/?token=...` 建立 HttpOnly Cookie，再进入 `/app/logs`；EventSource URL 不携带 Token。
- `/api/v1/logs/stream` 是仅供页面读取实时流的内部只读 SSE 端点。
- 日志页支持级别、关键字和来源（logger）过滤；慢客户端丢行时页面会提示并自动重连。
- AI 分析接口：`GET /api/v1/logs/analyze` 返回结构化摘要（错误/告警统计、重复模式、修复提示与最近条目），供外部 AI 分析和调用管理 API 修复。
- CMS 版本检测：`CMS_VERSION_CHECK_ENABLED=true` 后定时探测 CMS 版本，新版本出现时 Telegram 通知并标记 `update_ready`；`CMS_UPDATE_IMAGE` + `CMS_AUTO_PULL_ENABLED=true` 可自动拉取镜像，容器切换在宿主机执行 `scripts/update-cms-container.sh`。
- Web 设置页新增“CMS 版本更新”配置（开关、频率、镜像、容器、Socket、自动拉取），保存后优先于 `.env`。「立即检查」会对比本地 CMS 版本与 Docker Hub 最新 tag（`CMS_UPDATE_IMAGE` 需填完整镜像名如 `imaliang/cloud-media-sync:latest`），发现新版直接提示。

首次部署检查：

```sh
docker compose config
docker compose ps
docker compose exec cms-tg-ingest python /app/doctor.py
curl http://127.0.0.1:8787/api/v1/health
```

容器显示 `healthy`、健康接口中的 `runner_heartbeat_stale` 为 `false`，并且 Bot 能回复 `/help`，才算部署完成。

Unraid 使用时建议把端口改为 `8788:8787`，访问 `http://<unraid-ip>:8788/app/`。如果 STRM、Cookie 或 CMS 数据库路径不同，请同步修改 `volumes` 和 `.env` 中的路径；不要把 `.env` 或 Cookie 提交到 GitHub/Docker Hub。

本地 compose 默认使用 `8787:8787`。Unraid 推荐映射 `8788:8787`，访问 `http://<unraid-ip>:8788/` 会默认进入 Vue 管理台（实际页面为 `/app/`）；旧版概览保留在 `/legacy`。镜像支持 `linux/amd64` 和 `linux/arm64`。

## Telegram 使用

- 直接发送 115 分享、磁力或 ED2K 链接：进入自动入库流程。
- `/搜索`：通过 TMDB 匹配影片/剧集，筛选 HDHive 资源并解锁；旧命令 `/hdhive_search` 继续兼容。
- `/订阅 https://hdhive.com/tv/<slug>`：创建 HDHive 剧集订阅。
- `HDHive 订阅` 或 `/hdhive_subscriptions`：管理订阅、立即检查和确认解锁。
- `/status`、`/health`、`/quality`、`/history`：查看任务和本地健康状态。

## HDHive 配置

不需要单独申请 HDHive OpenAPI Key。先在 CMS `转存下载 -> 影巢账号` 完成 OAuth 授权，再启用：

```env
HDHIVE_ENABLED=true
HDHIVE_PROXY_BASE_URL=https://authx.771885.xyz
HDHIVE_TOKEN_CONFIG_PATH=/config/cms-config/hdhive-openapi.json
HDHIVE_SEARCH_SESSION_TTL_SECONDS=900
HDHIVE_AUTO_UNLOCK_MAX_POINTS=20
HDHIVE_SUBSCRIPTION_AUTO_ENABLED=true
HDHIVE_SUBSCRIPTION_TIME=01:30
HDHIVE_SUBSCRIPTION_TIMEZONE=Asia/Shanghai
```

挂载整个 CMS 配置目录，确保 OAuth 刷新后读取到新文件：

```yaml
- /mnt/user/appdata/cloud-media-sync/config:/config/cms-config:ro
```

HDHive 搜索默认筛选 `115`。费用未知或超过阈值时停在待确认状态，需要点击 `确认解锁`；只有成功的 115 链接会自动进入 CMS 整理、自有分享 STRM 和 Emby 入库流程，其他网盘链接不会误提交到 115 流程。

成功解锁记录会保存实际或估算积分、解锁时间和关联 Task ID，并通过 Telegram 发送海报卡片；TMDB 海报不可用时自动降级为文字通知。

直接发送 HDHive 剧集页面也可以创建订阅：

```text
https://hdhive.com/tv/<slug>
```

订阅不会立即解锁。程序每天按 `01:30`（`Asia/Shanghai`）检查新增资源；费用未知或超过阈值时等待 `确认解锁`。Web 管理页为 `/hdhive`，可以查看 OAuth 状态、下次检查时间和订阅统计。

订阅支持智能判断：集数过滤示例为 `S01E01-S01E10,S02`，默认跳过 `S00` 特殊集；多季资源按季集编号识别，Emby 已有集数会跳过。程序根据 TMDB 完结状态和季集数量标记已完结订阅；无法解析季集、费用未知或超过自动阈值的资源不自动解锁，完结订阅可手动恢复。

### SQLite 数据库备份

默认每天 `03:30`（`Asia/Shanghai`）在线备份 `/data/submissions.db` 和 `/data/tasks.db` 到 `/data/backups`，保留 14 天。由于 Compose 已将 `./data` 映射到 `/data`，不需要额外挂载目录。最近一次结果可在 `/health` 或 Web `/api/v1/health` 查看。

```sh
docker compose exec cms-tg-ingest sh -c 'ls -lh /data/backups'
```

恢复时先停止容器、备份当前文件，再将同一时间戳的 `submissions-*.db` 和 `tasks-*.db` 快照复制回 `data/submissions.db`、`data/tasks.db`，最后启动容器。备份不包含 Cookie、CMS 配置或媒体文件；失败会在健康状态中显示，并在当天下一次调度 tick 重试。

## 更新、回滚和排障

固定版本更新：

```sh
# 先备份 ./data、.env 和挂载配置
docker compose pull
docker compose up -d --no-build
docker compose ps
```

回滚时将 Compose 的镜像改回上一版本，例如 `icekale/cms-tg-ingest:0.2.19`，然后执行：

```sh
docker compose pull
docker compose up -d --no-build
```

常见问题：

- Bot 无回复：检查 `TG_BOT_TOKEN`、`TG_ALLOWED_CHAT_ID` 和容器日志。
- Web 无法访问：确认宿主机端口；Unraid 默认使用 `8788:8787`。
- 容器不健康：检查 `/data`、STRM、115 Cookie、CMS 数据库和 CMS 配置目录挂载。
- HDHive 授权失效：在 CMS `转存下载 -> 影巢账号` 重新授权，并保留 `/config/cms-config` 挂载。
- 直链/共享模式不符：检查 `STRM_DEFAULT_MODE`；任务进入后续阶段后模式会锁定。
- 任务变慢：先看 `/health` 的 115 冷却和阶段等待原因，不要连续重试。

## 前提与安全

- 已部署可访问的 CMS，并准备好 115 Cookie、待整理目录和 STRM/媒体库映射。
- Telegram Bot 通过 `TG_ALLOWED_CHAT_ID` 限制使用者。
- Emby 确认需要 `EMBY_BASE_URL` 和 `EMBY_API_KEY`。
- Web 管理台支持两种认证：推荐 `WEB_USERNAME` + `WEB_PASSWORD`（登录页签发 7 天会话 Cookie）；也可以设置随机 `WEB_TOKEN` 用共享令牌。局域网内可以留空直接访问，通过公网/反向代理暴露时**必须**启用其中一种。
- 本项目不提供媒体资源，也不绕过 115、CMS、HDHive 或 Emby 的权限机制。
- 不要公开 `.env`、115 Cookie、Telegram Token、Emby API Key 或 HDHive OAuth 文件。

完整配置、工作流、故障排查和回滚说明请查看 [GitHub README](https://github.com/icekale/cms-tg-ingest)。
