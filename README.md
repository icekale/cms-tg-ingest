# cms-tg-ingest

`cms-tg-ingest` 是一个给 Cloud Media Sync（CMS）加自动化能力的 Telegram 入库外挂，适合已经在用 115、STRM 和 Emby 的个人媒体库场景。你把 115 分享链接发给 Telegram 机器人后，它可以自动完成接收、CMS 整理、自建永久分享、分享同步生成 STRM、移动到媒体库、刷新并确认 Emby 入库、清理 115 转存源这一整套流程。

它的目标很朴素：把“拿到别人的 115 分享链接后，要手动转存、整理、再分享、生成 STRM、挪目录、刷 Emby”的重复操作变成一次发送链接。整个过程优先相信 CMS 的整理和分类结果；外挂只负责串联流程、补足状态追踪、做必要的安全校验和失败恢复。

本项目不提供任何媒体资源，也不绕过 115、CMS、Emby 的权限机制；它只自动化你已经拥有权限的个人工作流。请不要把 `.env`、115 cookie、Telegram token、Emby API key、OpenAI API key 提交到仓库或截图公开。

## 适合谁

- 已经部署 CMS，并希望通过 Telegram 提交 115 分享链接。
- 使用 STRM + Emby 管理媒体库，希望减少手动移动目录和刷新媒体库的操作。
- 想用自己的 115 永久分享生成 STRM，而不是依赖别人分享或直链 STRM。
- 希望任务卡住时能在 TG/Web 上看到阶段、错误、重试、恢复和从头重跑按钮。

## 功能特性

- **裸链接入库**：Telegram 机器人接收一条或多条 115 分享链接，自动去重并创建任务。
- **白名单访问**：只允许指定 Telegram 用户或聊天 ID 使用。
- **自分享 STRM 工作流**：转存到待整理目录、等待 CMS 自动整理、创建自己的 115 永久分享、调用 CMS 分享同步、移动生成的 STRM。
- **CMS 优先分类**：优先使用 CMS 整理后的目录和分类结果；只有 CMS 识别不确定时才通过按钮请求人工确认分类。
- **Emby 入库确认**：刷新后检查媒体是否进入 Emby，并返回命中的媒体库名称。
- **TaskStore 任务引擎**：SQLite 记录任务阶段、时间线、错误、重试次数和运行状态，支持 `/status`、`/history`、`/metrics`、`/health`、`/quality`、`/clear_history`。
- **TG/Web 运维按钮**：查看详情、重试当前阶段、查 Emby、恢复 STRM、从头重跑。
- **本地质量巡检**：检查缺失 STRM、直链 STRM、目标目录异常等问题；巡检只读本地 TaskStore 和 STRM 文件，不扫描 115。
- **共享别名保护**：CMS 完成整理和分类后，外挂用中性名称创建 115 分享，并用 canonical manifest 在本地恢复 CMS 标准目录名；115 风险标记只作为预警，最终以分享状态和 STRM 实际播放验证为准。
- **安全清理 115 空间**：在 `TASK_ENGINE_ENABLED=true` 的 TaskRunner 路径中，自己的永久分享状态验证通过后即可删除 115 转存源，不会取消自己的 115 永久分享；后续 STRM 只使用自己的分享链接生成。
- **115 压力保护**：整理文件夹查找使用分层搜索早停和整理目录扫描预算；遇到 115 风控冷却会暂停新的 115/CMS 全局阶段，避免连续重试。
- **离线诊断**：`doctor.py` 可检查配置、挂载路径、数据库质量和常见部署问题。
- **可选兜底识别**：可接入 OpenAI 兼容接口，但默认思路仍是尽量依赖 CMS 分类。

## v0.2 Alpha.2：TaskStore 接管新链接

v0.2 authoritative 任务引擎让真实 Telegram/CMS 工作流的新自分享链接默认由 TaskStore authoritative runner 执行：Telegram 收到链接后创建 task 并返回 task ID，随后 TaskRunner 推进完整阶段：接收、整理、识别、建分享、生成 STRM、移动、Emby、清理。

Web 管理页读取 TaskStore，因此卡在哪个阶段、最近错误和 Web 重试结果都能在管理页看到。Telegram 新链接接收回复在 authoritative 模式下可返回 TaskStore task ID 和当前阶段；`/status` 和 `/history` 优先读取 TaskStore，旧 SubmissionStore 记录为空时兜底显示。/status 会附带详情、重试、查 Emby、恢复 STRM、从头重跑按钮；/quality 会先执行 TaskStore 本地轻量巡检。Web 任务详情页打开旧任务 ID 时会懒回填历史 SubmissionStore 记录，便于从同一个任务页查看旧流程状态。Web 任务详情页提供重试、查 Emby、恢复 STRM、从头重跑按钮；Web `/quality` 页面只读取本地 TaskStore 和 STRM 文件，不扫描 115。/health 会显示 TaskStore 本地队列健康。TaskEngine 开启时，新 self-share 链接不会回退到旧 start_status_poll 轮询路径；如果 TaskStore 不可用，任务会直接失败并提示配置问题，避免误触发旧普通流程。

```env
TASK_ENGINE_ENABLED=true
TASK_DB_PATH=/data/tasks.db
WEB_ENABLED=true
WEB_HOST=0.0.0.0
WEB_PORT=8787
WEB_TOKEN=
TASK_WORKER_INTERVAL_SECONDS=5
TASK_MAX_RETRIES=3
```

访问地址示例：`http://<unraid-ip>:8787/`。如需回滚到旧的 SubmissionStore + 轮询路径，设置 `TASK_ENGINE_ENABLED=false`；该旧路径是兼容回滚路径，不提供 TaskRunner 的同等清理顺序保证。

## 后端结构

后端按职责拆分为配置、外部客户端、媒体文件操作、分类识别和自分享工作流；`bridge.py` 只保留为启动入口和兼容层。新 self-share 链接由 TaskRunner/TaskStore 推进真实执行状态，避免回退到旧轮询路径。

### 运行稳定性

TaskRunner 会记录每个阶段的等待原因、等待次数和下一次检查时间。`/status` 和 Web `/health` 会显示这些本地状态；长时间等待会进入 `NEEDS_ACTION`，方便从当前阶段安全重试。STRM 等待使用本地目录条件检查，不增加 115 扫描频率。

115 查询会优先按 TMDB/标题候选词逐个搜索，一旦找到高置信整理目录就停止后续搜索；搜索索引未命中时才进入整理目录扫描，扫描有预算上限，避免大树反复遍历。任意 115 API 返回“操作过于频繁 / 风控 / 限制接收”等提示时，TaskRunner 会进入 115 风控冷却，把当前任务标记为需要人工稍后重试，并在冷却结束前暂停新的 115/CMS 全局阶段。

自分享最终 STRM 必须来自自己的 115 永久分享；移动前会校验 `.strm` 内容包含自己的 `/s/<own_share_code>_<receive_code>_` marker，拒绝 `/d/` 直链 STRM，并通过 Range 请求验证 CMS 跳转后的媒体端点可访问。CMS 普通同步直链 STRM 最多只作为分类参考，不作为最终入库来源。新分享尚未完成这些验证时，已有媒体库 STRM 不会被替换。

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
P115_MIN_REQUEST_INTERVAL_SECONDS=2
P115_RISK_COOLDOWN_SECONDS=900
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
4. 外挂保存 CMS 标准目录、分类和 TMDB 信息，用中性 `asset-*` 别名创建你自己的 115 永久分享，提取码默认 `1212`。
5. 外挂检查分享状态；若 115 标记名称风险，只在当前任务目录内尝试一次中性视频文件名，不扫描整个网盘。`have_vio_file` 只记录为风险提示，不会单独判定分享失效。
6. 分享验证通过后，外挂删除 115 转存源但保留永久分享，并触发 CMS 消化删除事件。
7. 外挂调用 CMS 分享同步，使用你自己的分享链接生成 STRM；CMS 把结果写入 `SELF_SHARE_STRM_ROOT`。
8. 外挂校验 STRM 分享码并实际探测播放端点，再按 manifest 恢复 CMS 标准目录名和剧集文件名。
9. CMS 删除事件落库后，外挂把 STRM 文件夹移动或合并到目标媒体库目录，避免后续同步误删刚入库的 STRM。
10. 外挂通过 Emby API 确认媒体已入库，并返回媒体库名称。
11. 如果配置了 `SELF_SHARE_SOURCE_CLEANUP_PARENT_IDS`，外挂还会在这些 115 父目录中删除同一任务的接收阶段残留文件。

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
`P115_MIN_REQUEST_INTERVAL_SECONDS` 用于限制外挂访问 115 API 的频率，账号被风控后建议临时调到 `3` 或 `5`。`P115_RISK_COOLDOWN_SECONDS` 控制 115 风控冷却时长，默认 `900` 秒。

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
- `/status`：查看最近任务；在任务引擎模式下优先显示 TaskStore 当前阶段，并附带详情、重试、查 Emby、恢复 STRM、从头重跑按钮。已完成的国产电视、外国电视和番剧会显示“追更”：重新接收当前外部分享，生成新的自有分享 STRM 并合并到现有剧集目录。
- `/history`：查看更长历史；在任务引擎模式下优先显示 TaskStore，旧记录为空时回退到 SubmissionStore。
- `/metrics`：查看统计。
- `/health`：健康检查；在任务引擎模式下会附带 TaskStore 本地队列健康摘要。
- `/quality`：查看需要处理的问题记录；在任务引擎模式下先执行 TaskStore 本地轻量巡检，不扫描 115。
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
- `SELF_SHARE_CLEANUP_AFTER_EMBY=true` 在 `TASK_ENGINE_ENABLED=true` 的 TaskRunner 路径中会在自己的永久分享状态验证通过后立即删除 115 转存源，不会取消你自己的永久分享；旧 SubmissionStore + 轮询路径是兼容回滚路径，不提供同等清理顺序保证。变量名保留历史兼容。
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
