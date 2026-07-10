# Changelog

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
