# Web 实时日志系统设计

## 背景

当前程序通过 Python `logging` 输出到 Docker 标准输出，排查问题需要查看 `docker logs`。CMS 已有独立的实时日志页面，支持级别筛选、关键字过滤、最近行数和断线重连。需要在不影响 TaskRunner、Telegram、115 或 CMS 调用的前提下，提供类似体验，并保留有限的本地历史日志。

## 目标

- 在新 Vue 管理台增加“实时日志”页面。
- 支持实时查看、关键字过滤、重要/错误/全部筛选、1000/2000/5000 行选择和手动重连。
- 日志同时保留在 Docker stdout、内存缓冲区和本地轮转文件中。
- 本地日志总量控制在约 100 MB：当前文件 20 MB，加 4 个备份文件。
- 容器重启后恢复最近最多 5000 行，便于查看上一次运行的启动和故障信息。
- 在 Web、文件和 stdout 三个出口统一脱敏敏感信息。

## 非目标

- 不代理或读取 CMS 的 `/logs` 页面；本功能只展示本程序日志。
- 不把日志写入 TaskStore 或新增日志数据库表。
- 不在本版本提供删除磁盘日志、下载日志包、按任务建立审计索引或 Telegram 日志推送。
- 不改变现有 `LOG_LEVEL` 的语义，也不增加业务调用频率。

## 用户体验

### 导航与页面

在 Vue 管理台侧边栏增加“实时日志”，路由为 `/app/logs`。页面沿用现有 Naive UI 外壳和 CMS 的信息布局：

- 顶部显示“已连接”或“连接失败”状态。
- 默认筛选为“重要”、默认显示 1000 行。
- 筛选项为“重要 / 错误 / 全部”；关键字搜索不区分大小写。
- 行数选项为 1000、2000、5000。
- “重连”关闭现有连接并按当前条件重新获取快照；“清空”只清空当前页面内存，不删除服务器文件。
- 最新日志置于顶部。用户已滚动查看旧内容时，新日志到达不强制改变视口。
- INFO、WARNING、ERROR/CRITICAL、DEBUG 采用不同颜色；多行异常堆栈保持等宽文本和换行。

### 筛选语义

- `main`（重要）：INFO、WARNING、ERROR、CRITICAL。
- `ERROR`（错误）：ERROR、CRITICAL。
- `all`（全部）：DEBUG 及以上所有记录。

## 后端架构

### 日志管线

新增独立的日志模块，提供以下组件：

1. 脱敏 Formatter：先格式化 `LogRecord`，再移除 Bot Token、Cookie、密码、访问码、API Key、Authorization 和敏感 URL 查询参数。
2. 轮转文件 Handler：默认写入 `/data/logs/cms-tg-ingest.log`，单文件上限 20 MiB，保留 4 个备份；目录不存在时自动创建。
3. 有界内存 LogHub：保存最近 5000 条结构化记录，并通过条件变量向 SSE 订阅者广播。单条记录包含递增 ID、时间、级别、logger 名称和已脱敏文本。
4. stdout Handler：保留现有 Docker 日志行为，使用相同脱敏 Formatter。

`bridge.py` 启动时只配置一次上述 Handler，保留现有 `LOG_LEVEL` 配置。测试和重复初始化不能叠加 Handler。文件写入失败时记录一次内部状态并继续提供 stdout 和内存日志，不得使业务线程抛出异常。

### 重启恢复

初始化 LogHub 时，从当前日志文件和必要的较旧轮转文件按时间倒序读取，恢复最近 5000 行；解析失败的历史行按普通文本显示，不阻断启动。内存 ID 在每次进程启动时重新开始，客户端不依赖跨重启的 ID 连续性。

### SSE 接口

新增只读接口：

```text
GET /api/v1/logs/stream?filter_type=main&lines=1000&keyword=
```

接口沿用现有 Web Token/Cookie 鉴权。响应使用 `text/event-stream`，并发送以下事件：

- `snapshot`：连接建立后发送符合筛选条件的历史记录。
- `log`：新日志记录，携带事件 ID。
- `heartbeat`：定期注释/事件，保持连接和代理存活。
- `gap`：慢客户端导致内存队列丢弃旧记录时，提示前端重新连接获取快照。

服务端限制 `filter_type`、行数和关键字长度；非法参数返回 400。每个客户端使用独立有界队列，慢客户端不得阻塞 LogHub、文件 Handler 或业务日志调用。HTTP Handler 对 SSE 连接负责清理订阅和断开资源；普通 JSON/HTML 路由行为保持不变。

## 前端数据流

新增 `frontend/src/views/Logs.vue` 和日志 API/解析辅助模块。页面打开时建立 EventSource；收到 `snapshot` 替换内容，收到 `log` 追加到顶部，收到 `gap` 显示提示并自动重连。筛选条件或行数变化时关闭旧连接并建立新连接。组件卸载时必须关闭连接。

由于浏览器 EventSource 不支持自定义 Header，鉴权使用已有 HttpOnly Cookie；在局域网免 Token 配置下直接连接。不得把 Web Token 拼入 SSE URL。

## 安全与异常处理

- 脱敏发生在所有输出出口之前，避免 stdout、文件和 Web 之间出现安全差异。
- SSE 只返回已脱敏文本和有限元数据，不返回日志文件绝对路径、环境变量或请求凭据。
- 文件打开、轮转、历史解析、客户端断开和广播异常均局部处理；任何单点失败都不能停止 Runner。
- 日志模块自身不得通过同一 logger 无限记录异常，内部故障使用一次性状态或 stderr 防止递归。
- 保留现有 Docker healthcheck、Runner heartbeat 和 `/api/v1/health` 语义。

## 测试验收

### Python

- Handler 配置幂等，不重复输出或重复写文件。
- 20 MiB 文件轮转并保留 4 个备份，超过数量的文件被淘汰。
- 敏感字段在文件、stdout Formatter 和 SSE 文本中均被脱敏。
- 重启恢复最近 5000 行，损坏历史行不阻断启动。
- `main`、`ERROR`、`all`、关键字和行数限制正确。
- SSE 首次快照、实时广播、心跳、断开清理、慢客户端 gap 行为正确。
- 日志目录不可写时仍能正常启动并保留内存/stdout日志。
- 现有 Web 鉴权和所有现有 Python 测试不回归。

### Vue

- 页面显示连接状态、筛选、行数、重连和清空行为。
- snapshot/log/gap 事件解析正确；筛选切换会关闭旧 EventSource。
- 组件卸载关闭连接，日志更新不会破坏用户滚动位置。

### 部署验收

- Compose 不新增媒体或数据库挂载；现有 `/data` 挂载承载日志目录。
- `docker logs` 和 Web 页面都能看到启动日志。
- 容器重启后页面能恢复最近日志。
- `/api/v1/health` 返回 2xx，`runner_heartbeat_stale=false`，且日志系统异常不会产生 TaskRunner/CMS/115 错误。

## 成功标准

用户可以在新 Web UI 中像使用 CMS `/logs` 一样实时查看和筛选本程序日志；日志文件自动限制在约 100 MB；重启后可恢复近期记录；日志故障、慢客户端或非法查询都不会影响现有入库工作流。
