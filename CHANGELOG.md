# Changelog

## 0.2.30 - 2026-07-26

- 修复 Emby 剧集查询未请求 `ProviderIds`，导致 HDHive 订阅无法识别已入库剧集并错误提示“Emby 集数检查不可用”的问题。

## 0.2.29 - 2026-07-26

- Web 管理台新增设置页，可统一选择自有分享 STRM、直链 STRM 或直接使用原始 115 分享生成 STRM，并显示程序版本。
- 新增原始分享工作流：跳过转存、CMS 整理、自有分享和 115 源文件清理，直接调用 CMS 分享同步并完成分类、移动和 Emby 入库。
- 115 分享创建处于异步处理时不再立即失败；可从处理中链接提取分享码，尚未返回分享码时改为低频等待重试。

## 0.2.28 - 2026-07-26

- 新任务保留 CMS 整理后的规范目录名，不再生成 `asset-*` 分享别名；历史别名任务继续兼容。
- 自有分享访问码支持 Web 设置，并按 Web、CMS、环境变量、默认值顺序解析；Web 读取只显示掩码和来源。
- 修复 legacy 流程未读取 TaskStore Web 访问码的问题，并以 115 更新分享接口返回的实际访问码为准。

## 0.2.27 - 2026-07-26

- 修复 CMS 返回业务成功但不返回任务 ID 时被错误标记为失败的问题，避免重复提交并支持后续恢复整理状态。
- 兼容 CMS 数字状态、重复任务记录和标题中的 TMDB ID，降低直链 STRM 分类与任务归属错误。
- 补充分类映射和旧的精确 TMDB 直链 STRM 恢复逻辑。

## 0.2.26 - 2026-07-26

- 文件名或接收标题中的显式 TMDB ID 作为任务身份依据，不能被错误的 CMS 识别结果或已生成目录覆盖。
- 115 接收后保存前后目录快照并验证本次新文件；无法唯一确认时不触发 CMS 整理，避免复用旧同名文件。
- CMS 整理目录、直链 STRM 和 Emby 路径在后续阶段执行 TMDB 一致性校验；不一致时停止建分享、移动和清理。
- 从头重跑时统一清除旧接收 ID、扫描游标、分享/移动/Emby/清理派生状态；Web、TG 和质量巡检入口只原子入队，由接收阶段统一清理跨库状态。
- 只有来源文件名明确带 TMDB ID 时才覆盖高置信 CMS 识别；115 待整理目录满页时不再假定快照完整，避免同名旧文件被误认成新任务。
- 质量重跑会清除旧的修复排队标记；多个客户端接收分享时串行执行快照、接收和结果解析，避免并发同名文件错配。

## 0.2.25 - 2026-07-26

- 限制 115 整理目录扫描预算，并持久化分页游标，避免重复扫描触发风控；风控响应继续交由任务冷却机制处理。
- 修复自有分享 STRM 维护任务在别名目录为空、移动失败或目录路径嵌套时的恢复逻辑。
- 任务重新处理时清理过期的整理目录和扫描游标，避免旧状态阻塞新任务。

## 0.2.24 - 2026-07-25

- Telegram `answerCallbackQuery` 遇到瞬时 HTTPS EOF 时自动重试一次；确认请求失败不会再把已完成的 HDHive 订阅检查误报为失败。
- Telegram Bot API 错误中的 Bot token 统一脱敏，避免错误消息或日志泄露凭据。

## 0.2.23 - 2026-07-25

- 兼容 HDHive 代理返回的 `OPENAPI_REAUTH_REQUIRED`，仍先交由 CMS 使用有效 OAuth 刷新令牌，再重试原请求。

## 0.2.22 - 2026-07-25

- HDHive OAuth 刷新失败时统一转换为可操作的错误提示，避免 Telegram 直接显示底层 `OpenAPI token refresh required`。
- 为 HDHive 令牌刷新增加进程内互斥，避免多个订阅/解锁请求同时刷新授权。

## 0.2.21 - 2026-07-25

- 115 自有分享增加异步审核观察期，确认风险或未知状态时保留源文件，不自动改名、重建或重复分享。
- 历史分享巡检改用批量状态查询；旧兼容路径和质量巡检不再绕过审核删除 115 源文件。

## 0.2.20 - 2026-07-25

- 新增每日 SQLite 在线备份，默认 `03:30`（`Asia/Shanghai`）保存 `submissions.db` 和 `tasks.db` 到 `/data/backups`，默认保留 14 天。
- `/health`、Web 健康 API 和 `doctor.py` 增加备份状态与配置检查；备份失败会在当天下一次调度 tick 重试。
- 将备份调度接入主进程生命周期，使用项目日志输出并在停机时等待线程退出。

## 0.2.19 - 2026-07-25

- HDHive 订阅支持多季识别、集数过滤、跳过 Emby 已有集数和 TMDB 完结判断。
- HDHive 订阅 Web/TG 状态补充智能判断结果、跳过原因和可恢复的完结状态。

## Unreleased

- 115 自有分享增加异步审核观察期：默认在 10 分钟、1 小时、6 小时和 24 小时检查点全部通过后才清理转存源。
- 115 分享创建不再忽略平台警告；确认违规、未知状态、风控或网络错误时保留源文件，不自动改名、重建或重复分享。
- 历史失效分享巡检改用一次批量分享列表和短期缓存，降低 115 调用频率与风控概率。
- 旧 SubmissionStore 兼容路径和质量巡检清理入口不再绕过异步审核删除 115 源文件；自动清理统一由 TaskRunner 审核阶段负责。
- 新增 `/订阅 <HDHive剧集链接>` 命令，明确创建 HDHive 剧集订阅。
- Telegram HDHive 搜索命令新增中文别名 `/搜索`，原 `/hdhive_search` 继续兼容。
- 修复 HDHive Web 管理页在调度器完成一次运行后读取最近运行摘要时的异常。
- 修复 Cloudflare 浏览器签名拦截 HDHive 代理请求的问题。
- 改为挂载 CMS 配置目录，确保 OAuth 刷新替换文件后外挂读取到最新令牌。

## 0.2.16 - 2026-07-24

- Web 管理台支持局域网免 Token 模式；`WEB_TOKEN` 留空时直接访问，配置后仍启用鉴权。
- 115 风控冷却持久化到 TaskStore，重启或更换 TaskRunner 后继续遵守冷却窗口。
- HDHive 解锁增加 SQLite 原子抢占和过期状态恢复，避免并发重复解锁或永久停在 `unlocking`。
- Emby API Key 改用 `X-Emby-Token` 请求头，并在 HTTP 错误 URL 中脱敏敏感查询参数。

## 0.2.17 - 2026-07-24

- 支持局域网免 Token Web 模式；`WEB_TOKEN` 留空时可直接访问，配置后仍启用鉴权。

## 0.2.18 - 2026-07-24

- TaskRunner 写入阶段结果时增加阶段、Worker claim 和更新时间 CAS，避免旧结果覆盖用户重跑或新阶段。
- 115 客户端增加短期 GET 去重缓存，任何变更请求会立即清空缓存，降低重复查询和风控概率。
- 保留重启时释放固定 Worker 未完成 claim 的断点恢复逻辑。

## 0.2.8 - 2026-07-24

- 增加基于 CMS OAuth 授权账号的 HDHive Telegram 搜索、网盘筛选、单条/批量解锁和 115 自动入库。
- 增加费用阈值确认、无效资源禁选、非 115 链接不误提交和 HDHive 健康状态。
- `doctor.py` 在 HDHive 开启但 OAuth 配置文件未挂载时直接报告具体路径。
- 增加 HDHive 剧集订阅、每日调度和 Web 管理页。
- 修复 115 云下载文件经 CMS 改名后无法定位整理目录的问题。
- 增加“限制分享”风控提示识别和订阅数据库健康检查。

## 0.2.7 - 2026-07-20

- 增加每日质量巡检调度，默认时间 `02:50`，支持 IANA 时区、单次任务上限和 115 检查预算。
- 质量修复通过 TaskStore 原子 lease 接入现有 TaskRunner，避免重复重跑和并发抢占。
- 增加缺失/直链 STRM 自动恢复与重跑规划；风控、未知分享状态和不安全路径只记录并通知，不自动删除。
- 增加自有分享、STRM、媒体库、Emby 和最新成功事件的清理门槛，并支持崩溃 lease 恢复和幂等清理。
- Web `/quality` 增加自动巡检状态、立即运行、设置保存和恢复环境默认操作。

## 0.2.6 - 2026-07-19

- 兼容 115 离线任务真实状态码：`0/1` 等待或下载中、`2` 完成、`-1` 失败，并读取 `wp_path_id` 作为目标父目录。

## 0.2.5 - 2026-07-19

- 修复 115 云下载提交协议：使用 `lixian.115.com` RSA 加密接口和 Android 客户端 UA，支持磁力/ED2K 正确进入云下载。
- 云下载提交响应缺少任务身份时，通过任务列表按源哈希或完整链接精确回查，避免误认其他任务。
- 云下载状态查询改用 115 离线任务列表接口。

## 0.2.4 - 2026-07-19

- 支持 Telegram 接收磁力和 ED2K 链接，统一进入 115 云下载、CMS 整理、自有分享 STRM、Emby 和清理流程。
- 云下载状态查询默认至少间隔 30 秒；任务身份按 `info_hash`/任务 ID 精确匹配，失败或超时不会清理源文件。
- Web/TG 增加“115 云下载”阶段显示，doctor 会拒绝不安全的云下载轮询配置。
- 增加 fake 115/CMS/Emby 全链路测试，验证最终 STRM 使用自有分享而不是直链。

## 0.2.3 - 2026-07-19

- 修复 Telegram `getUpdates` 遇到 `RemoteDisconnected` 时未进入底层重试的问题。
- 连续断开时将其归类为瞬时网络波动，避免健康的轮询线程被记录成错误堆栈。

## 0.2.2 - 2026-07-19

- 外部 HTTP 的 GET/HEAD 请求在网络错误、超时、408、425、429 和 5xx 时最多保守重试一次；POST 不自动重试，避免重复接收、建分享或提交任务。
- TaskRunner 增加独立本地心跳，长时间等待或外部阶段执行时不会误报心跳停止；Web `/health` 会标记真正 stale 的任务引擎。
- `doctor.py` 增加 Web 未配置 `WEB_TOKEN` 和遗留 `TASK_MAX_CONCURRENT` 的非阻断安全提示。
- `.env.example` 移除当前不生效的 `TASK_MAX_CONCURRENT` 配置，继续保持单 worker 和 115 低频调用策略。

- 增加共享别名层：CMS 标准名称与 115 分享名称解耦，媒体库仍恢复标准目录和剧集文件名。
- 增加分享别名准备、分享验证、CMS 删除落库三个 TaskStore 阶段，并在 Web/TG 中显示等待原因。
- `have_vio_file` 改为风险提示；分享仍可访问时不会误删 STRM，第二级中性文件名后停止自动改名。
- 分享 STRM 移动前增加实际 Range 播放探测，验证失败会保留媒体库现有版本。
- 修复源文件提前删除后任务被旧状态桥误判为全部完成的问题。
- 修复别名分享的 STRM 恢复和搁置任务修复会落入 `asset-*` 媒体库目录的问题。

## 0.2.0-alpha.2 - 2026-07-10

- 增加任务等待原因、等待次数和下一次检查时间展示，长时间等待会转入 NEEDS_ACTION 方便人工恢复。
- 优化本地 STRM 就绪检查，缩短分享同步后等待时间，同时不增加 115 扫描频率。
- 继续拆分 `bridge.py` 中的 Telegram UI 和旧轮询逻辑，降低后续维护成本。
- 启用 TaskStore authoritative runner：真实工作流进度写入 TaskStore，新自分享链接由 TaskStore 创建任务并由 TaskRunner 推进真实工作流。
- 后端内部拆分为 config、service clients、media STRM operations、classification、self-share workflows。
- `bridge.py` 保持可执行入口和兼容 facade。
- 强化自分享 STRM 安全校验，禁止 direct `/d/` STRM 作为最终媒体库输出。
- Web 管理页读取 TaskStore，失败阶段可见，Web 重试会重新排队实际任务；Telegram 新链接接收回复在 authoritative 模式下可返回 TaskStore task ID 和当前阶段，`/status` 和 `/history` 优先读取 TaskStore。
- TG /status 增加任务操作按钮：详情、重试、查 Emby、恢复 STRM、从头重跑；/quality 增加 TaskStore 本地轻量巡检，不扫描 115。
- Web 任务详情页增加重试、查 Emby、恢复 STRM、从头重跑按钮；Web /quality 增加 TaskStore 本地轻量巡检，不扫描 115。
- /health 增加 TaskStore 本地队列健康摘要，Web /health 只读取本地 TaskStore，不扫描 115。
- Web 详情页支持懒回填旧 SubmissionStore 记录，打开旧任务 ID 时会同步成 TaskStore 任务后展示。
- TaskEngine 开启时禁止新 self-share 链接回退旧轮询路径，TaskStore 不可用会直接失败提示配置问题。
- SubmissionStore 保留为兼容、审计和修复元数据；可用 `TASK_ENGINE_ENABLED=false` 回滚到旧执行路径。
- 自分享 TaskRunner 创建自己的 115 永久分享后立即清理 115 转存源；分享同步 STRM 源目录会提前拒绝直链或非预期分享码 STRM。
- 自分享等待默认从 90 秒缩短到 15 秒，分享 STRM 移动稳定等待最多 5 秒；CMS 自动整理每条任务只触发一次，等待期间不反复刺激普通同步。
- 已完成任务会重新校验目标 STRM 是否匹配当前自有分享；单集恢复要求指定集文件存在，避免旧 STRM 被误判为成功。
- 单集恢复只覆盖当前集的同名 STRM，不再删除同季其他集；恢复完成后会重新刷新对应 Emby 媒体库。

## 0.2.0-alpha.1 - Unreleased

- 增加任务状态机和任务事件表。
- 增加轻量 Web 管理页，用于查看任务和从失败阶段重试。
- 增加 v0.2 配置项：`TASK_DB_PATH`、`WEB_ENABLED`、`WEB_PORT`、`WEB_TOKEN`、`TASK_MAX_RETRIES`。
- 增加任务质量检查模块，识别直链 STRM、缺失目标目录和缺失 STRM。

## 0.1.0 - Unreleased

- Initial GitHub-ready release candidate.
- Telegram ingestion for 115 share links.
- CMS self-share workflow support.
- STRM folder move/merge and Emby confirmation.
- Optional OpenAI-compatible classification fallback.
- Offline `doctor.py` diagnostics.
- Docker Compose example and CI workflow.
