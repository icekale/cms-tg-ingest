# Telegram 富消息体验优化设计

日期：2026-08-29
状态：已确认，待实施

## 目标

在不改变任务、订阅、解锁、重试和权限状态机的前提下，使用 Telegram Rich Message 改善机器人长消息的可读性和操作连续性。

现有项目已经具备 `RichDocument`、标题/段落/表格/详情块、`sendRichMessage` 发送、编辑回退和纯文本降级能力。本次工作是在这套能力上扩展业务调用点，而不是引入新的消息框架。

## 范围

### 改为 RichDocument 的消息

- `/status`、`/history`、`/metrics`、`/health`
- TaskStore 任务列表、任务详情、等待原因和失败摘要
- HDHive 搜索候选、资源列表、账号/扣费确认、解锁结果
- HDHive 订阅列表、订阅检查结果
- 质量巡检结果
- 长错误摘要和需要用户继续操作的结果消息

### 保持纯文本的消息

- `answerCallbackQuery` 的短反馈
- “已开始”“已取消”“请发送……”等短提示
- 115 接收成功后的简短确认
- Rich Message 发送失败时的等价纯文本回退

短消息不为了形式统一而强行包装成 RichDocument。

## 非目标

- 不改变任务、订阅、HDHive 解锁、重试、清理或权限状态机。
- 不修改 callback data、inline keyboard、权限校验或消息业务语义。
- 不引入新的 Telegram 消息框架或第三方依赖。
- 不使用 Rich Message 媒体、地图、公式、思考块或草稿接口。
- 不在消息中暴露分享码、receive code、access token、refresh token、解锁链接或其他敏感凭据。
- 不把所有单行通知机械迁移到富消息。

## 架构

沿用现有调用链：

```text
业务状态/结果
    -> formatter（app/telegram_ui.py 或 bridge.py）
    -> RichDocument
    -> TelegramClient.send_rich_message()
    -> Rich 不可用时 send_message(document.to_plain())
```

职责边界：

- `app/telegram_rich.py` 负责最小文档模型和 blocks/plain text 序列化。
- `app/telegram_ui.py` 负责状态、历史、指标、TaskStore、质量和订阅视图的结构化 formatter。
- `bridge.py` 负责搜索候选、HDHive 资源、解锁结果和少量事件结果的结构化展示。
- `TelegramClient` 只负责 API 请求、回退和错误识别，不加入业务判断。

## 消息布局

Rich 消息按需要采用以下顺序：

```text
标题
摘要段落
数据表格
可选详情块
现有 inline keyboard
```

组件约定：

- `heading` 用于消息标题和分组标题。
- `paragraph` 用于摘要、失败原因和下一步说明。
- `table` 用于任务、资源和统计数据的横向比较。
- `details` 用于单个任务、订阅的等待原因、操作记录和错误详情。
- `bold` / `code` 只强调状态、任务编号和不敏感标识。

表格列数、每个单元格长度和详情文本都要有边界，确保手机端内容不会无限横向扩展。长原因优先进入详情块或摘要，不把完整异常堆进表格。

## 数据流与兼容性

- formatter 返回 `RichDocument`，已有 keyboard 原样传给发送层。
- RichDocument 为空时不发送请求，保持当前空结果行为。
- 发送 Rich Message 成功时只调用 `/sendRichMessage`。
- Telegram 返回 Rich 格式错误、未知方法或不支持 Rich 时，发送一条 `document.to_plain()` 的普通消息，并保留 keyboard。
- 网络错误不额外发送第二条消息，避免重复刷屏；沿用现有异常处理和日志脱敏。
- 编辑 Rich Message 失败时回退现有 `edit_message_text`，不新发消息。
- 旧 Telegram 能力和测试替身必须显式实现 `send_rich_message`；缺失能力不静默当成普通 `send_message`。
- 所有 Rich 和纯文本结果沿用现有截断及敏感信息脱敏规则。

## 错误处理

富消息格式失败属于展示能力问题，只触发展示回退，不改变业务结果。

业务异常仍由原调用点负责：

- HDHive 授权、资源、积分和解锁错误继续显示原有可操作提示。
- 任务和订阅失败继续保留原状态、重试入口和人工处理入口。
- Telegram 网络错误只记录并交给现有上层处理，不制造第二条内容相同的消息。

## 实施文件

优先限制在以下文件：

- `app/telegram_rich.py`：仅在已有模型不足时补充最小能力。
- `app/telegram_ui.py`：扩展现有 formatter。
- `bridge.py`：替换选定的长消息发送点。
- 对应的 `tests/test_telegram_rich.py`、`tests/test_telegram_client.py`、`tests/test_quality_telegram.py`、`tests/test_hdhive_bridge.py`、`tests/test_bridge_v02_integration.py` 和相关 bridge 测试。

不为一次性消息新建抽象层。

## 验证

必须通过：

- formatter 的标题、表格、详情块、空数据和长文本边界测试。
- Rich API 成功发送测试。
- Rich 格式错误/未知方法回退为一条纯文本测试。
- 网络失败不重复发送测试。
- 编辑 Rich 失败回退为编辑普通文本测试。
- HDHive、任务操作、订阅和质量流程保留 keyboard/callback 的集成测试。
- `python3 -m unittest discover -s tests -p 'test*.py' -q`
- `python3 -m compileall -q app bridge.py doctor.py`
- `git diff --check`

## 验收标准

1. 长列表和可比较数据在 Telegram 中通过标题、表格和详情块呈现。
2. 原有 inline keyboard 和 callback 行为不变。
3. Rich Message 不可用时，用户仍收到一条内容等价的纯文本消息。
4. 网络故障不因回退逻辑制造重复消息。
5. 所有敏感凭据和解锁链接继续不出现在机器人消息或新增日志中。
6. 完整回归测试和静态检查通过。
