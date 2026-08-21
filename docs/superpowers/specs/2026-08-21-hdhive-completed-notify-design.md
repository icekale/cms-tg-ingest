# HDHive 订阅完结通知

日期：2026-08-21  
状态：已实施

## 背景

订阅检查已经能把剧集标成 `completed`：TMDB 状态为 `Ended` / `Canceled`，且过滤后的预期集都处于 `enqueued`、`emby_exists` 或 `filtered`。每日调度只跑 `active` 订阅，完结后本来就不会再自动检查。

缺的是状态刚变成完结时没有人知道。解锁入队有 Telegram 卡片；完结没有对等通知。Web 列表已有「已完结」，够用。

## 目标

- 订阅**第一次**从非完结变成 `completed` 时，向该订阅的 `chat_id` 发一条 Telegram 文本。
- 已是 `completed` 时再手动检查，不重发。
- 通知失败只记日志，不让这次检查失败，也不回滚完结状态。
- 完结判定、每日跳过、Web「已完结 / 恢复」保持现状。

## 非目标

- 不放宽、不收紧 `completion_state`。
- 不改 Web 质量页、不新增完结详情页、不发海报、不带按钮。
- 不新增「已通知」数据库字段。
- 不在一夜多部完结时合并成一条摘要。
- 不处理入库任务完结、Emby 刮削或媒体清理。

## 行为

`HdhiveSubscriptionService.check()` 在现有完结写入之后：

1. 记住进入 `check()` 时的 `subscription.status`。
2. 现有逻辑若决定完结，照旧 `set_status(..., "completed")`。
3. 仅当**进入时不是** `completed`，且本次写成了 `completed`，才调用 `on_subscription_completed(subscription, summary)`。
4. `resume` 把订阅拉回 `active` 后，若再次检查又完结，视为新的一次翻转，再通知一次。这是有意的：用户主动恢复后应知道系统仍判完结。

调度器继续只检查 `active`。完结订阅不会因为每日任务再发消息。

## 文案

纯文本，无按钮、无海报。标题用订阅片名，缺省用 TMDB ID：

```text
#12 剧名 已完结。
TMDB Ended，预期 10 集均已入队、已在 Emby 或已过滤。
之后不再每日检查。可在订阅里点恢复。
```

- `Ended` / `Canceled` 原样写入，缺省写 `未知`。
- 集数用本次 `summary["expected"]`。
- 不写分享链接、积分或任务号。

格式函数放在 `app/hdhive_cards.py`，纯函数，便于单测。

## 接线

- `HdhiveSubscriptionService` 增加可选 `on_subscription_completed`，与 `on_item_enqueued` 同级。
- `bridge.py` 里用订阅 `chat_id` 调 `telegram.send_message`。未配置 Telegram 或 `chat_id` 为空则跳过发送。
- 回调抛错：catch、打日志、检查结果仍返回 `subscription_status=completed`。

## 验收

1. 活跃订阅首次达到完结条件 → 状态 `completed`，Telegram 恰好 1 条。
2. 已完结订阅再 `check()` → 仍是 `completed`，不再发消息。
3. 未完结（缺集、待确认、TMDB 未知、Emby 查询失败）→ 不发消息。
4. 回调抛错 → 检查成功完结，测试能看到日志调用，不要求回滚。
5. Web / Telegram 订阅列表仍显示「已完结」，恢复按钮不变。
