# Changelog

## 0.2.0-alpha.2 - Unreleased

- 启用 TaskStore authoritative runner：真实工作流进度写入 TaskStore，新自分享链接由 TaskStore 创建任务并由 TaskRunner 推进真实工作流。
- Web 管理页读取 TaskStore，失败阶段可见，Web 重试会重新排队实际任务；Telegram 新链接接收回复在 authoritative 模式下可返回 TaskStore task ID 和当前阶段，现有 `/status` 仍保留 SubmissionStore 兼容语义。
- SubmissionStore 保留为兼容、审计和修复元数据；可用 `TASK_ENGINE_ENABLED=false` 回滚到旧执行路径。

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
