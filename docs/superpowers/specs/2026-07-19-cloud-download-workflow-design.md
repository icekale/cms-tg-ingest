# 磁力与 ED2K 云下载入库设计

**日期：** 2026-07-19  
**状态：** 待评审  
**目标：** 让 Telegram Bot 接收 `magnet:` 和 `ed2k://` 链接，通过 115 云下载落入现有待整理目录，然后复用 CMS 整理、自有分享 STRM、媒体库移动、Emby 确认和源文件清理流程。

## 背景与边界

当前程序只把 115 分享链接作为输入。`P115WebClient` 能接收分享并创建自有分享，但没有云下载任务接口；`TaskRunner` 的第一阶段也默认调用 `receive_share_to_cid`。因此磁力/ED2K 不能直接套用现有分享输入路径。

115 当前客户端库已经验证具备以下能力：

- `clouddownload_task_add_url` 支持 HTTP、HTTPS、FTP、磁力链和电驴链接。
- `wp_path_id` 可指定云下载目标目录 ID。
- `savepath` 可指定目标目录下的相对路径。
- `clouddownload_task` / `clouddownload_task_list` 可查询任务状态。

本设计只新增磁力和 ED2K 输入，不开放任意 HTTP/FTP 下载；最终入库仍然只接受当前任务创建的自有分享 STRM，不接受 `/d/` 直链 STRM。

## 目标流程

```text
Telegram 输入 magnet/ED2K
        |
        v
解析并去重，创建 cloud_download 任务
        |
        v
115 云下载提交（wp_path_id = SELF_SHARE_RECEIVE_CID）
        |
        v
低频轮询云下载状态
        |----------------------失败/超时--------------------> NEEDS_ACTION
        v
云下载完成，确认任务文件夹位于待整理 CID
        |
        v
CMS 整理和分类（复用现有 organizing/recognizing）
        |
        v
创建自有永久分享并验证
        |
        v
CMS 分享同步生成 STRM
        |
        v
验证自有分享 STRM，移动到 CMS 分类对应媒体库
        |
        v
Emby 刷新并确认媒体库
        |
        v
删除 115 已整理源文件，保留自有分享
```

云下载任务目录位于 `SELF_SHARE_RECEIVE_CID` 下。提交时不使用默认云下载根目录；`savepath` 为空时保留 115 的任务文件夹行为，程序只允许在该父目录内追踪和整理。

## 方案

### 输入层

新增统一的媒体输入解析器，保留现有 `extract_share_links` 兼容行为，并新增：

- `magnet:`：要求存在 BTIH 标识，保留原始磁力链接用于提交；以规范化 BTIH 作为去重键。
- `ed2k://`：要求符合 `|file|name|size|hash|/` 结构，使用规范化文件哈希和大小作为去重键；保留完整链接用于提交。
- 输入允许一条消息包含多个链接，按行和空白分隔；不识别的文本继续忽略。

统一结果包含 `source_type`、`source_key`、`raw_url` 和可选的显示名称。已有 115 分享输入继续走 `share` 分支，不改变其去重和回复格式。

### TaskStore 与兼容记录

TaskStore 通过 SQLite 增量迁移增加 `source_type` 和 `source_key` 字段，并建立唯一索引：

- 旧任务回填为 `source_type=share`，`source_key=<share_code>:<receive_code>`。
- 云下载任务使用 `source_type=cloud_download`，`source_key` 使用规范化后的源哈希，不把完整磁力链接塞进 `share_code`。
- 原始链接仍保存在现有 `url` 字段，不能在日志、TG 回复或错误中泄露 Cookie/API key。
- 为复用现有 `SelfShareWorkflow` 的兼容记录，创建内部 SubmissionStore 行；内部兼容 key 不作为用户标题，也不影响现有 115 分享记录。

新增 `TaskStage.CLOUD_DOWNLOADING`，其展示名为“115 云下载”。云下载任务的初始状态为该阶段，不进入旧轮询路径；`TASK_ENGINE_ENABLED=false` 时明确回复“磁力/ED2K 需要启用 TaskEngine”，不偷偷回退。

### 115 云下载客户端

在 `P115WebClient` 增加三个最小方法：

1. `cloud_download_add(url, target_cid)`
   - POST `https://clouddownload.115.com/lixianssp/?ac=add_task_url`。
   - 发送 `url`、`wp_path_id=target_cid`，`savepath` 保持为空。
   - 解析并保存 `info_hash`、云下载任务 ID、任务名称和目标 CID；无法确认任务身份时返回失败，不盲目再次 POST。
2. `cloud_download_status(task_identity)`
   - 优先按 `info_hash` 查询单个任务，必要时使用任务列表做精确匹配。
   - 统一返回 `queued`、`running`、`completed`、`failed`、`unknown`，并保留原始状态用于诊断。
3. `cloud_download_output(task_identity)`
   - 云下载完成后提取输出文件夹/文件 ID、名称、父 CID。
   - 必须验证父 CID等于 `SELF_SHARE_RECEIVE_CID` 或其受控子目录；拿不到唯一输出时进入人工处理，不扫描整个网盘。

所有云下载 API 调用纳入现有 P115 请求计数、最小间隔和风控冷却。提交 POST 不自动重试；状态查询 GET 遵循现有安全重试规则。

### TaskRunner 阶段行为

`CLOUD_DOWNLOADING` 每次执行只做一件明确的事：

- 没有远端任务身份：提交一次云下载，写入身份和 `cloud_status=queued`，返回 defer。
- 有身份且状态为 queued/running：写入状态和等待原因，按 `CLOUD_DOWNLOAD_POLL_INTERVAL_SECONDS` defer；默认不低于 30 秒。
- 状态为 completed：验证输出目录在待整理 CID 下，写入 `received_file_ids`、`received_title`、`cloud_completed_at`，完成当前阶段并进入现有 `ORGANIZING`。
- 状态为 failed/unknown 超过超时：转入 `NEEDS_ACTION`，保留云下载任务身份，不自动再次提交。

云下载阶段的默认最长等待为 24 小时，可通过 `CLOUD_DOWNLOAD_TIMEOUT_SECONDS` 调整。超时只停止本程序轮询，不删除云下载结果或媒体库文件。

`ORGANIZING` 根据 `source_type` 分支：

- `share`：保持现有接收分享逻辑。
- `cloud_download`：跳过 `receive_share_to_cid`，使用云下载阶段保存的输出文件夹继续 CMS 整理检查。

后续 `RECOGNIZING`、自有分享、分享同步 STRM、移动、Emby 和清理均复用现有实现，并继续以 CMS 分类为权威来源。

### 清理与安全边界

- 云下载完成后不能立即删除源；必须等待自有分享验证、STRM 播放验证、媒体库移动和 Emby 确认。
- `CLEANED` 只删除当前任务已确认的 115 整理文件/文件夹，保留自有永久分享。
- 不自动清空 115 云下载任务列表，避免把云下载任务记录和源文件误删混为一谈。
- 云下载失败、无法取得唯一输出、输出落在错误 CID、名称匹配不唯一时均进入 `NEEDS_ACTION`，不删除任何文件。
- 不提高全局并发；云下载输入与 115 分享输入仍由单一 TaskRunner 串行处理。

## 配置与用户体验

默认复用现有配置：

```env
SELF_SHARE_RECEIVE_CID=3298928530653445613
CLOUD_DOWNLOAD_POLL_INTERVAL_SECONDS=30
CLOUD_DOWNLOAD_TIMEOUT_SECONDS=86400
```

Telegram 直接发送磁力/ED2K 后立即回复任务号和“115 云下载”阶段；`/status`、Web 任务详情和健康摘要显示：

- 输入类型：磁力或 ED2K
- 云下载任务身份和当前状态
- 已等待时长、下次检查时间和超时剩余时间
- 115 本阶段/累计调用次数和风控冷却状态

最终完成通知沿用现有格式，并包含目标路径和 Emby 媒体库名称。

## 失败处理

| 情况 | 处理 |
| --- | --- |
| 115 云下载提交失败 | 记录错误并进入 `NEEDS_ACTION`，不自动重复 POST |
| 云下载任务排队/下载中 | 低频 defer，显示等待原因 |
| 云下载任务失败 | `NEEDS_ACTION`，保留任务身份，允许人工重试当前云下载阶段 |
| 24 小时未完成 | `NEEDS_ACTION`，不删除云下载源 |
| 完成但输出目录不在待整理 CID | `NEEDS_ACTION`，禁止全盘扫描 |
| 输出文件夹匹配多个 | `NEEDS_ACTION`，要求人工确认 |
| CMS 整理/分类失败 | 复用现有阶段重试和人工分类按钮 |
| 自有分享/STRM/Emby 任一验证失败 | 复用现有保护逻辑，禁止清理源 |

## 测试计划

### 单元测试

- 解析合法 magnet、合法 ED2K、大小写 scheme、多个链接和非法输入。
- 源类型/源键去重，旧 TaskStore/SubmissionStore 数据迁移不变。
- `cloud_download_add` 发送 `wp_path_id`、不重复 POST，并正确保存任务身份。
- 云下载状态映射 queued/running/completed/failed/unknown。
- 云下载完成输出目录必须位于目标 CID；越界或多匹配进入 `NEEDS_ACTION`。
- TaskRunner 只在首次执行提交一次，轮询 defer 后完成时进入 `ORGANIZING`。
- cloud 任务跳过再次接收分享，share 任务保持原行为。
- 云下载失败和超时不会触发清理；完整后续流程仍要求自有分享 STRM 和 Emby 确认。

### 集成测试

- 用 fake P115/CMS/Emby 跑完整 cloud path：云下载完成、CMS 分类、自有分享 STRM、移动、Emby、清理。
- 现有 115 分享回归测试全部通过。
- Docker 构建、doctor、TaskStore 旧数据库迁移和健康页回归通过。

### 真实验收

使用用户提供的 ED2K 样本：

```text
ed2k://|file|爱在记忆消逝前.The.Leisure.Seeker.2017.BCORE.WEB-DL.2160p.HEVC.DV.HDR.DTS-HD.MA.5.1.11Audios-LGNB@oSpecialCN.mkv|66417791661|<32-hex-ed2k-hash>|/
```

验收条件：任务进入 `succeeded/cleaned`；输出在 CMS 分类对应媒体库；STRM 内容包含当前自有分享 marker；Emby 返回条目和媒体库；115 云下载源文件已删除且自有分享仍有效。

## 兼容、回滚与发布

- 115 分享链接行为不变。
- 数据库只做 additive migration，不删除旧列和旧记录。
- 新功能只在 TaskEngine 模式启用；`TASK_ENGINE_ENABLED=false` 仍保留旧分享兼容路径，但不支持磁力/ED2K。
- 先完成本地测试和 fake 全链路，再发布新版本镜像；真实 ED2K 验收成功后才更新默认 `latest`。
