# cms-tg-ingest

`cms-tg-ingest` 是一个配合 Cloud Media Sync（CMS）使用的 Telegram 自动入库外挂。你把 115 分享链接发给 Telegram 机器人后，它会提交给 CMS，跟踪处理进度，并可按配置完成“转存 -> CMS 整理 -> 创建自己的 115 永久分享 -> CMS 分享同步生成 STRM -> 移动到 Emby 媒体库 -> 确认入库 -> 清理 115 转存源文件”的完整流程。

本项目不提供任何媒体资源，也不绕过 115、CMS、Emby 的权限机制；它只是自动化你已经拥有权限的个人工作流。

## 功能特性

- Telegram 机器人接收一条或多条裸 115 分享链接。
- 只允许指定 Telegram 用户或聊天 ID 使用。
- SQLite 本地历史记录、重复链接去重、`/status`、`/history`、`/metrics`、`/health`、`/quality`、`/clear_history`。
- 优先使用 CMS 识别结果；只有 CMS 识别不确定时才通过按钮请求人工确认分类。
- 可选 OpenAI 兼容接口作为分类兜底。
- 自分享 STRM 工作流：转存、等待 CMS 自动整理、创建自己的 115 永久分享、调用 CMS 分享同步、移动生成的 STRM。
- Emby 入库确认，并返回命中的媒体库名称。
- 可选在自有分享创建成功后删除 115 转存源文件，同时保留自己的 115 分享不取消。
- `doctor.py` 离线诊断配置和挂载路径。

## v0.2 Alpha：任务引擎和 Web 管理页

v0.2 引入任务引擎基础：每条链接会记录阶段、错误摘要、事件时间线和重试建议。启用 Web 管理页后，可以在浏览器查看任务列表、任务详情和从失败阶段触发重试。

```env
TASK_DB_PATH=/data/tasks.db
WEB_ENABLED=true
WEB_HOST=0.0.0.0
WEB_PORT=8787
WEB_TOKEN=
TASK_MAX_RETRIES=3
```

访问地址示例：`http://<unraid-ip>:8787/`。

## v0.2 Alpha.2：真实工作流任务时间线

v0.2 Alpha.2 将真实 Telegram/CMS 工作流进度写入 TaskStore。新的链接提交、CMS 状态、创建自有分享、分享同步、STRM 移动、Emby 确认和清理结果会出现在 Web 管理页中。

TaskStore 仍是旁路时间线，当前生产执行仍由既有稳定工作流和 SubmissionStore 驱动。Web 重试仍然是非破坏性操作：它记录重试意图和当前阶段，不会自动重复转存、删除、分享或移动文件。

## 快速开始

```sh
git clone https://github.com/icekale/cms-tg-ingest.git
cd cms-tg-ingest
cp .env.example .env
# 编辑 .env
docker compose up -d --build
```

运行诊断：

```sh
docker compose exec cms-tg-ingest python /app/doctor.py
```

本地测试：

```sh
python3 -m py_compile bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

## 必填配置

启动前先编辑 `.env`：

- `TG_BOT_TOKEN`：BotFather 创建的 Telegram bot token。
- `TG_ALLOWED_CHAT_ID`：只允许这个 Telegram 用户或聊天使用。
- `CMS_BASE_URL`：CMS Web 地址，例如 `http://192.168.1.10:9527`。
- `CMS_USERNAME` / `CMS_PASSWORD`：CMS 登录账号和密码。
- `DB_PATH`：一般保持 `/data/submissions.db`。

敏感信息必须只放在 `.env` 或挂载文件中，不要提交 `.env`。

## 推荐自分享工作流

建议配置：

```env
WORKFLOW_MODE=self_share_sync
P115_COOKIE_PATH=/config/115-cookies.txt
SELF_SHARE_RECEIVE_CID=
SELF_SHARE_STRM_ROOT=/mnt/user/Unraid/strm/share
SELF_SHARE_CMS_LOCAL_PATH=/media/share
SELF_SHARE_CLEANUP_AFTER_EMBY=true
SELF_SHARE_SOURCE_CLEANUP_PARENT_IDS=
MOVE_CONFLICT_POLICY=merge
```

完整流程：

1. 你把 115 分享链接发给 Telegram bot。
2. 外挂用 115 接口把外部分享接收到 `SELF_SHARE_RECEIVE_CID` 指定的待整理目录，不提交 CMS 普通同步。
3. 外挂触发 CMS 自动整理，并找到整理后的 115 文件夹。
4. 外挂为该文件夹创建你自己的 115 永久分享，提取码默认 `1212`。
5. 外挂调用 CMS 分享同步，使用你自己的分享链接生成 STRM。
6. CMS 把 STRM 写入 `SELF_SHARE_STRM_ROOT`。
7. 外挂把 STRM 文件夹移动或合并到目标媒体库目录。
8. 外挂通过 Emby API 确认媒体已入库，并返回媒体库名称。
9. 如果启用清理，外挂会在自有分享创建成功后删除 115 转存源文件或源文件夹，不取消你自己的永久分享。
10. 如果配置了 `SELF_SHARE_SOURCE_CLEANUP_PARENT_IDS`，外挂还会在这些 115 父目录中删除同一任务的接收阶段残留文件。

## 路径映射

容器内路径必须和 `.env` 保持一致。Unraid 常见挂载示例：

```yaml
volumes:
  - ./data:/data
  - /mnt/user/Unraid/strm:/mnt/user/Unraid/strm:rw
  - /mnt/user/appdata/cloud-media-sync/config/115-cookies.txt:/config/115-cookies.txt:ro
```

`CMS_PARENT_CID_CATEGORY_MAP` 用于把 CMS 整理后的 115 父目录 CID 映射到分类。这个值和个人 115 目录强相关；不配置时禁用父目录分类推断。

`SELF_SHARE_RECEIVE_CID` 是 CMS 自动整理监听的 115 待整理目录 CID；必须配置为你自己的待整理目录，不要配置媒体库或根目录。

```env
CMS_PARENT_CID_CATEGORY_MAP=3260485903797190075=欧美电影,3254119954860998447=外国电视
```

`STRM_LIBRARY_MAP` 使用逗号分隔的 `分类=绝对路径`：

```env
STRM_LIBRARY_MAP=欧美电影=/mnt/user/Unraid/strm/转存/Movie/电影/欧美电影,外国电视=/mnt/user/Unraid/strm/转存/TV
```

程序只会移动配置允许的源目录和媒体库目录下的 STRM 文件夹。

## Telegram 命令

- 直接发送 `https://115cdn.com/s/...?...`：提交并处理链接。
- `/help`：查看帮助。
- `/status`：查看最近任务。
- `/history`：查看更长历史。
- `/metrics`：查看统计。
- `/health`：健康检查。
- `/quality`：查看需要处理的问题记录。
- `/clear_history`：清理已完成的本地历史。

## 镜像

发布版本会自动构建多架构镜像。可直接拉取 GHCR 或 Docker Hub：

```sh
docker pull ghcr.io/icekale/cms-tg-ingest:0.1.0
docker pull icekale/cms-tg-ingest:0.1.0
```

支持平台：`linux/amd64`、`linux/arm64`。

如果你 fork 后想发布自己的 Docker Hub 镜像，在 GitHub 仓库 Secrets 中配置：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名或命名空间。
- `DOCKERHUB_TOKEN`：Docker Hub access token。

打 tag 发布：

```sh
git tag v0.1.0
git push origin v0.1.0
```

## 诊断

容器内运行：

```sh
python /app/doctor.py
/app/scripts/diagnostics.sh
```

诊断脚本会自动脱敏环境变量名中包含 `TOKEN`、`PASSWORD`、`KEY`、`COOKIE`、`SECRET` 的值。公开提交 issue 前仍建议你自己再检查一遍。

## 安全说明

- 不要公开 `.env`、115 cookie、Telegram token、Emby API key、OpenAI API key。
- 生产环境建议固定版本号，不建议盲目使用 `latest`。
- 批量使用前，先用一个小体量链接测试完整流程。
- `SELF_SHARE_CLEANUP_AFTER_EMBY=true` 会在自有分享创建成功后删除 115 转存源，不会取消你自己的永久分享；变量名保留历史兼容。
- `SELF_SHARE_SOURCE_CLEANUP_PARENT_IDS` 是额外清理白名单，只会扫描你显式配置的 115 父目录 CID；不要配置整个媒体库根目录。
- 本项目依赖 CMS、115、Telegram、Emby 等第三方接口，这些服务的行为可能变化。

## 开发

```sh
python3 -m py_compile bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

当前运行时不依赖第三方 Python 包。

## 许可证

MIT。
