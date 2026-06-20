# Changelog

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
