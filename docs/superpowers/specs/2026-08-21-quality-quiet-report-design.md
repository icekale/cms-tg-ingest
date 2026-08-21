# 质量巡检：身份模型 + 安静报表

## 背景

剧集 dest 按 TMDB 目录复用。每集任务的 115 分享经常只有新一集；`merge_self_share_strm_folder` 只覆盖源里同名文件，从不删除 dest 残留。这是对的：旧集 STRM 仍指向自己的分享，Emby 也能播。

0.2.50 让巡检用「该目录最新一条已搬迁分享」当唯一期望值。旧任务不再误报已被覆盖成新码的文件，但**最新一集任务**扫整个 dest 时，会把指向旧码的合法 STRM 打成 `unexpected_strm`。队列、Telegram、自动重跑堆在最新任务上。重跑再轮换分享码也清不掉旧文件（#368）。`validate_self_share_strm_merge` 已经允许 dest 里残留的其他 `/s/` 和 `/d/`，巡检应对齐。

## 目标

- 同一 dest+tmdb 下，任一仍有效的自有分享都算健康。
- 定时/手动巡检默认可扫、不修、不吵：不自动重跑/恢复/复检，不 claim 任务，不推 Telegram 操作队列，不因开启扫描而停掉 invalid-share probe，不自动删除任务记录。
- 真孤儿（分享码不属于该 dest+tmdb 任何仍有效身份）仍出现在 Web 报表；失效 STRM 预览删除保持人工确认。
- 身份模型换代后，7 天目录指纹缓存必须失效，避免旧误报冻住。

## 非目标

- 不在合并时删除 dest 残留 STRM。
- 不改 dest 按剧复用的目录布局。
- 不调用 HDHive 官网订阅接口，不改入库主路径。

## 扫描身份

- `SubmissionStore.live_self_share_identities(dest_path, tmdb_id)`：过滤与 `latest_self_share_identity` 相同（`self_share_sync`、`moved`、非 invalid/unavailable、同 dest、同 tmdb），返回全部 `(share_code, receive_code)`。
- `inspect_task_files` / `scan_task_quality`：STRM 命中任一仍有效 `/s/{code}_{receive}_` 即健康；`/d/` 仍按 `direct_strm`；全部不匹配才记 `unexpected_strm`。
- resolver 可返回一条或一组身份；任务自己的 `own_share_code` 始终计入。

## 安静报表

- `QUALITY_AUTO_ENABLED` 只表示是否定时扫描。
- `QUALITY_AUTO_REPAIR_ENABLED` 默认 false；关闭时 `_run_once_owned` 不执行 restore/reprocess/requeue、不 probe 直链、不复验分享、不退休任务记录。
- `allow_auto_reprocess` 默认 false。
- `QUALITY_UNFIXABLE_RETENTION_DAYS` 默认 0。
- Telegram `/quality` 只回一行计数，不带操作按钮；调度循环不发巡检告警。
- 开启质量扫描不再调用 `set_invalid_probe_enabled(True)` 去停 invalid-share probe。
- 扫描缓存前缀换代（`quality_dir_fp:live:`）。

## 验收

1. 同一 dest 两集两种仍有效分享 → 无 `unexpected_strm`。
2. 指向已不在 live 集合的 code → 仍报 `unexpected_strm`。
3. 自动巡检开启且未开修复 → 不入队重跑、不发 TG 按钮、不占 `claimed_by`。
4. quality 扫描开启时 invalid-share probe 仍可运行。
