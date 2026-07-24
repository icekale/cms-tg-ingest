# cms-tg-ingest

Cloud Media Sync（CMS）的 Telegram 自动入库外挂。把 115 分享、磁力、ED2K 或 HDHive 资源发给 Bot，自动完成 CMS 整理分类、STRM 生成、媒体库移动和 Emby 入库确认。

```text
链接 -> 115 接收/云下载 -> CMS 整理 -> shared/direct STRM -> 媒体库 -> Emby
```

## 核心能力

- Telegram 支持裸链接、多链接、任务状态和按钮式运维。
- CMS 优先完成整理、改名、TMDB 匹配和分类。
- 支持 `shared` 共享 STRM 和 `direct` 直链 STRM；任务锁定后不允许切换，避免混用。
- 共享别名保护：创建分享时使用中性别名，入库时恢复 CMS 的标准目录和文件名。
- TaskStore 记录每个阶段、等待原因、失败、重试和耗时。
- 115 调用有频率限制、扫描预算和风控冷却。
- 支持磁力、ED2K 云下载，随后按 STRM 模式进入对应流程；direct 模式不会创建分享或清理源文件。
- 支持 Emby 刷新、入库确认和媒体库名称反馈。
- 支持 HDHive 搜索、网盘筛选、单条/批量解锁和剧集订阅。

## 快速开始

```sh
git clone https://github.com/icekale/cms-tg-ingest.git
cd cms-tg-ingest
cp .env.example .env
# 填写 Telegram、CMS、115、STRM 和 Emby 配置
docker compose up -d
```

固定版本部署：

```sh
docker pull icekale/cms-tg-ingest:0.2.16
```

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

## 前提与安全

- 已部署可访问的 CMS，并准备好 115 Cookie、待整理目录和 STRM/媒体库映射。
- Telegram Bot 通过 `TG_ALLOWED_CHAT_ID` 限制使用者。
- Emby 确认需要 `EMBY_BASE_URL` 和 `EMBY_API_KEY`。
- 启用 Web 管理台时必须设置随机 `WEB_TOKEN`，不要让局域网端口以匿名方式暴露。
- 本项目不提供媒体资源，也不绕过 115、CMS、HDHive 或 Emby 的权限机制。
- 不要公开 `.env`、115 Cookie、Telegram Token、Emby API Key 或 HDHive OAuth 文件。

完整配置、工作流、故障排查和回滚说明请查看 [GitHub README](https://github.com/icekale/cms-tg-ingest)。
