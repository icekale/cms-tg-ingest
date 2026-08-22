# Telegram 结构化报表改为 Rich Messages

日期：2026-08-22  
状态：已实施

## 背景

Telegram Bot API 10.1 增加了 `sendRichMessage`：用 `InputRichMessage.blocks` 发标题、表格、折叠详情。cms-tg-ingest 的 Bot 现在全部走 `sendMessage` 纯文本，报表用 `|` 拼接，手机上难扫。

产品原则仍是「清爽、可信、少打扰」。这次只把已经存在的结构化报表变得更好读，不增加推送次数，不改键盘协议。

## 目标

- 选定报表改为内部 `RichDocument`，经 `sendRichMessage` 的 `blocks` 发出。
- `sendRichMessage` 因富文本本身不可用而失败时，回退为现有 `sendMessage` + `to_plain()`，键盘仍在。
- 短确认、callback、`/quality` 一行摘要、自动巡检频率保持不变。

## 非目标

- 不上 `sendRichMessageDraft`、思考块、媒体、地图、公式。
- 不使用 `InputRichMessage` 的 `markdown` / `html` 字段。
- 不给旧 `sendMessage` 加 `parse_mode`。
- 不改 Web UI、键盘 `callback_data`、完结通知文案、HDHive 搜索点选、海报。
- 不改「少打扰」：`/quality` 仍只回 `format_quality_scan_summary` 一行；调度循环不因这次改动多推。
- 不引入 python-telegram-bot。
- 不新增 Bot 命令。

## 架构

不把 Telegram 全套 `InputRichBlock*` 搬进仓库。只做一个小的内部文档模型。

```
format_* 报表函数  →  RichDocument
                         │
     TelegramClient.send_rich_message()
           ├─ to_blocks() → POST /sendRichMessage
           └─ 富文本失败 → to_plain() → POST /sendMessage
```

短确认继续 `send_message`。键盘挂在消息上，不进文档模型。

## 组件

### `app/telegram_rich.py`

不依赖 `bridge`。

- `RichDocument`：有序列表。`to_blocks()` 编成 Bot API `blocks`；`to_plain()` 编成可读纯文本。没有块时为假值。
- 块类型仅四种：`Heading`、`Paragraph`、`Table`、`Details`。
- 文本：`str`，或 `Bold` / `Code`（可嵌在段落和单元格里）。
- 工厂：`heading()`、`paragraph()`、`table()`、`details()`、`bold()`、`code()`。

`to_blocks()` 映射：

| 内部 | Bot API `type` | 约定 |
|---|---|---|
| `Heading` | `heading` | 报表标题 `size=3`（1 最大、6 最小） |
| `Paragraph` | `paragraph` | `text` 为字符串或 RichText |
| `Table` | `table` | `is_bordered=true`，`is_striped=true`；表头行 `is_header=true`，`align=left`，`valign=top`。单元格只允许 `str` / `Bold` / `Code`，禁止再塞 `Details` 或其它块 |
| `Details` | `details` | `is_open` 默认 false |
| `Bold` | RichText `bold` | `{"type":"bold","text":"..."}` |
| `Code` | RichText `code` | `{"type":"code","text":"..."}` |

表格最多 20 列、20 行数据（另加 1 行表头）。超出的行收进文档末尾一个 `Details`，摘要为 `还有 N 条`，内容为被裁行的 `to_plain()`。

`to_plain()` 稳定规则：

- `Heading` / `Paragraph`：各占一行。
- `Table`：每行 `列1 | 列2 | …`，与现在报表风格接近。
- `Details`：先写摘要行，内部块每行前加两个空格。
- `Bold` / `Code`：只输出其文本，不加 Markdown 记号。

### `TelegramClient`

新增：

- `send_rich_message(chat_id, document, reply_markup=None)`
- `edit_rich_message(chat_id, message_id, document, reply_markup=None)`

`send_rich_message` 在 `document` 为假值时不发请求。否则 POST `/sendRichMessage`：

```json
{
  "chat_id": "...",
  "rich_message": {
    "blocks": [],
    "skip_entity_detection": true
  },
  "reply_markup": {}
}
```

`edit_rich_message` 走 `/editMessageText`，带 `rich_message`，同样 `skip_entity_detection=true`。第一期调用点几乎用不到：`/quality` 刷新仍用现有 `edit_message_text`。

### 改返回 `RichDocument` 的函数

| 函数 | 结构 |
|---|---|
| `format_status` | 标题「最近任务」+ 表（任务 / 状态 / 错误）；无记录时只有一段「暂无记录。…」 |
| `format_taskstore_status` | 标题「TaskStore 最近任务」+ 表（# / 任务 / 阶段 / 状态 / 错误）；每个进行中任务在**表后**跟一个 `Details`（摘要 `#id 任务名`，正文为等待与可观测信息）。单元格只放行内文字。无任务时返回空文档 |
| `format_history` | 标题「最近历史」+ 表（# / 任务 / 分类 / 移动 / Emby）；失败摘要、入库摘要各一段 |
| `format_taskstore_history` | 标题「TaskStore 最近历史」+ 表（# / 任务 / 阶段 / 状态 / 分类 / 媒体库 / 路径）；无任务时返回空文档 |
| `format_metrics` | 标题「任务统计」+ 键值表（项 / 值） |
| `format_health` | 标题「健康检查」+ 表（组件 / 状态 / 说明）；`format_taskstore_health` 原文放 `Details`「TaskStore」 |
| `format_hdhive_subscriptions` | 标题「HDHive 剧集订阅」+ 调度说明段 + 表（# / 剧名 / 状态 / 来源）；有过滤/检查/诊断/错误的订阅在**表后**各跟一个 `Details`（摘要 `#id 剧名`）。待确认数单独一段。无订阅时只有一段「暂无 HDHive 剧集订阅。」 |
| `format_quality_report` | 标题「质量巡检：发现疑似错配」+ 表（# / 任务 / Emby / 问题）；无问题时只有一段「最近任务未发现明显错配。」 |
| `format_task_quality_report` | 标题「TaskStore 轻量巡检」+ 表（# / 任务 / 问题）；无问题时只有一段「TaskStore 轻量巡检：未发现本地 STRM 问题。」 |
| `format_quality_manual_report` | 标题「质量巡检：N 项需要关注」+ 表（# / 任务 / 规则 / 风险 / 状态 / 原因 / 尝试）；无问题时只有一段「质量巡检：当前没有需要人工处理的问题。」 |
| `_quality_attention_message` | 标题「质量巡检需要关注」+ 一段（含 `run_id` 与扫描/问题/失败/跳过计数）+ 表（# / 任务 / 原因），最多 10 行失败/跳过 |

`format_hdhive_subscription_view` 改为返回 `(RichDocument, keyboard)`。`format_health` 的 OK/FAIL 状态用 `Bold`。

`bridge.format_status` 等仍是 `telegram_ui` 同名函数的别名（`tests/test_refactor_imports.py` 保持）。

### 仍返回 `str`

- 一行摘要：`format_quality_scan_summary`
- 短确认/标签：`format_task_label`、`format_task_snapshot`、`format_task_intake_reply`、`format_hdhive_subscription_completed`、`format_hdhive_candidate_label`、`format_hdhive_account`
- 内部拼串：`format_counts`、`format_failure_summary`、`format_library_summary`、`truncate_text`、`truncate_end`、`format_task_health`、`format_taskstore_health`
- HDHive 交互：`format_hdhive_resources`、候选列表正文、`format_subscription_check_message`

## 发送与回退

**富文本失败**（才回退）：

- `HttpRequestError.status_code` 为 400 或 404；或
- Telegram JSON `ok=false` 且 `error_code` 为 400 或 404；或
- 错误描述（小写）含 `unknown method`、`method not found`，或同时含 `bad request` 与 `rich` / `block`。

**不回退，原样抛出**：`Cannot reach`、超时、EOF、429、5xx、其它未列出的错误。

回退：`LOG.warning`（URL/token 走现有脱敏）→ `send_message(chat_id, document.to_plain(), reply_markup)`。回退再失败则抛出，与今天 `send_message` 失败相同。不重试 `sendRichMessage`。

`edit_rich_message`：描述含 `message is not modified` 视为成功。富文本失败则 `edit_message_text(..., document.to_plain(), ...)`。不另发一条新消息（避免和 quality 队列重复刷屏）。

网络瞬时错误的判定沿用 `TelegramClient._is_transient_telegram_error`，不在回退集合里。

## 调用点

改为 `send_rich_message` 的路径：

- `/status`：`format_taskstore_status` 或 `format_status`
- `/history`：`format_taskstore_history` 或 `format_history`
- `/metrics`：`format_metrics`
- `/health`：`format_health`
- HDHive 订阅列表：`format_hdhive_subscription_view`
- `/quality` 在无 `quality_automation` 时的 `format_task_quality_report` / `format_quality_report`
- `notify_quality_run` 的 `_quality_attention_message`

不改：

- `send_quality_manual_queue`（仍 `format_quality_scan_summary` + `edit_message_text` / `send_message`）
- 一句话确认、callback `answer_callback_query`、`send_photo`
- HDHive 搜索、选资源、解锁确认
- 完结通知 `format_hdhive_subscription_completed`

## 测试

不调用真 Bot。

1. **`tests/test_telegram_rich.py`**  
   标题+表格编出 `heading` / `table`；`to_plain()` 含关键词；空文档为假；21 行表截断并带「还有 N 条」。

2. **`tests/test_telegram_client.py`**  
   成功只请求 `/sendRichMessage`，body 含 `blocks` 与 `skip_entity_detection`。模拟 400/404 或 unknown method 时改请求 `/sendMessage`，text 为 `to_plain()`，`reply_markup` 仍在。模拟 `Cannot reach` / EOF **不**出现 `/sendMessage`。

3. **报表测试**  
   旧关键词断言改为 `document.to_plain()`（例如「最近任务」「质量巡检」）。新断言检查 `to_blocks()` 含 `table`。`format_quality_scan_summary` 仍返回 `str`。

4. **假客户端**  
   测试用 `FakeTelegram` 必须实现 `send_rich_message`。未实现则 `AttributeError`，禁止静默当成 `send_message`。实现方可把 `document.to_plain()` 记入现有 `messages` 列表，以便旧集成测试少改。

## 验收

1. `/status`、`/history`、`/metrics`、`/health`、HDHive 订阅列表走 `sendRichMessage`，消息含表格或标题块，原键盘仍在。
2. `/quality` 在质量自动化开启时仍只回一行，无操作按钮；`edit_message_text` 路径不变。
3. `sendRichMessage` 返回 400 时用户仍能收到内容等价的纯文本，且只有一条。
4. 网络超时不会先富文本再纯文本连发两条。
5. 短确认（「已取消 HDHive 操作。」等）仍是 `sendMessage`。
6. 完结通知、海报、Web 质量页行为不变。
7. `uv run python -m unittest` 相关测试通过。
