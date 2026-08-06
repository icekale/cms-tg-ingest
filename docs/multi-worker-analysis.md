# 多 Worker 成本收益分析（结论：当前不引入）

> 2026-08-06 评审批次结论。结论基于代码结构分析与运行观测，不基于基准测试。
> 若未来引入，必须先落地"前提条件"一节中的三项改造。

## 现状（单 worker）

- `TaskRunner` 单线程循环 `run_once()`：`claim_next_runnable` 扫描至多 10 条到期任务，
  逐个 claim 并执行**一个阶段**，完成/推迟后释放，继续下一轮（`app/task_runner.py`）。
- defer 等待**不阻塞**：`next_run_at` 推迟后任务释放回队列，runner 立刻处理其他任务
  （`app/task_runner.py` 的 `_defer_*` 路径）。所有阶段 defer 有上限
  （`_STAGE_MAX_DEFER_COUNT`，30/20/30/20），不存在无界 head-of-line 阻塞。
- claim 采用乐观并发：`claim_token` + 心跳续租（15s，TTL 默认 5min）；结算时 token 不匹配
  则结果丢弃并告警（`_record_claimed_event`），不会把过期 worker 的结果写入冲突状态。
- 单进程内已有并行：Web 服务、HDHive 调度、质量巡检、备份调度均为独立线程。

## 瓶颈判断

| 约束 | 多 worker 能否缓解 | 依据 |
|------|-------------------|------|
| 115 风控冷却（默认 900s，**全局持久化**） | 否，反而加剧 | `115:risk_cooldown_until` runtime state；`_GLOBAL_115_LOCK_STAGES` 覆盖所有 115 阶段；请求速率翻倍更易触发 `P115RiskControlError` |
| 同 share 资源锁（`_lock_key`） | 否 | `claim_task_lock` 按 share 互斥，第二 worker 同样等待 |
| SQLite 单写 | 基本否 | `BEGIN IMMEDIATE` 串行化写入，本部署量级下非瓶颈 |
| 单阶段长任务（大文件云下载）占用 runner | **是（唯一收益点）** | 其他任务在下载期间无法推进 |

## 结论

- 量级判断：个人媒体桥每天十来个任务，延迟全部来自外部（115/HDHive/CMS/Emby），
  CPU 与 SQLite 均非瓶颈；多 worker 主要收益（并行跑长下载）恰好与 115 风控对撞。
- 收益小、成本不小：claim 续租失败目前仅告警不中止（#12）、锁扫描无 LIMIT（#13），
  两个 worker 同时处理同一任务会造成重复外部副作用；引入前必须补齐这两项 + 观测指标。
- **决策：维持单 worker。** 如果哪天真出现"一个长下载卡住所有小任务"的痛点，
  先用 `/api/v1/health` 的 `runner_active_*` 字段观察（见下），数据说话再决定。

## 可观测性（v0.2.73 起）

`/api/v1/health` 新增字段，用于回答"runner 是不是瓶颈"：

- `runner_active` / `runner_active_task_id` / `runner_active_stage` /
  `runner_active_since`：runner 当前正在处理的阶段及开始时间（15s 心跳写入，
  超过 90s 未刷新视为 idle）。
- `runner_last_claim_attempt_at`：最后一次尝试认领任务的时间（判断 runner 是否活着但无活干）。
- 前端"本地健康"页同步展示"Runner 当前"。

观察方法：持续几天看 `runner_active` 是否长期为 `true` 且 `pending_count` 堆积；
若两者同时成立才说明单 worker 是瓶颈，再按本文档前提条件规划多 worker。
