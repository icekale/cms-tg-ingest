# 自动巡检受控规则引擎设计

## 1. 背景

当前自动巡检已经能够扫描 TaskStore 和本地 STRM，但判定和修复动作仍主要由固定条件决定。生产数据中存在以下问题：

- 没有按任务的 `strm_mode` 判断，`direct` 模式会被误报为直链异常。
- `restore` 目前只是重新排队，并不保证真正恢复缺失目录或 STRM。
- 失效分享清理后的任务仍可能因目标目录不存在再次进入恢复计划。
- 失败任务缺少任务级冷却和尝试上限，可能重复入队。
- `QUALITY_AUTO_115_CHECK_LIMIT` 暴露在设置中，但没有完整约束规则计划的执行预算。

目标是建立一个受控、可解释、可人工接管的规则引擎，在不增加独立容器和外部数据库的前提下，统一自动巡检、Web 和 Telegram 的处理决策。

## 2. 目标与非目标

### 目标

- 规则判断基于任务实际 `strm_mode`、阶段、状态和已持久化元数据。
- 每个任务最多产生一个当前主规则，多个问题按优先级合并，避免同一任务重复入队。
- 自动动作只处理高置信、可逆或已有完整证据的问题。
- 高风险、证据不足和真实恢复能力不足的问题进入人工队列，不自动调用 115/CMS。
- 每个自动动作有任务级尝试次数、冷却时间和全局/规则级预算。
- Web/TG 可以查看规则、原因、冷却和历史，并对单任务进行人工干预。
- 重启、重复调度和并发执行不会丢失或重复接管状态。

### 非目标

- 不开放用户输入任意 Python、SQL 或脚本规则。
- 不新增独立规则服务、消息队列或外部数据库。
- 不让自动巡检直接删除 115 源文件或媒体库文件。
- 本版本不实现“缺失目录/STRM 的自动重建”；在没有真实恢复动作前，统一人工处理。

## 3. 规则模型

新增受控规则描述，规则实现为 Python 内置对象，不使用动态代码执行：

```text
QualityRule
  rule_id: str
  priority: int
  issue_codes: tuple[str, ...]
  risk_level: safe | guarded | manual
  auto_action: reprocess | restore | none
  manual_actions: tuple[str, ...]
  max_attempts: int
  cooldown_seconds: int
  matcher(task, issues) -> RuleMatch
```

`RuleMatch` 包含规则 ID、原因、证据摘要、动作和是否允许自动处理。规则评估必须是本地纯逻辑，不进行网络请求。

### 初始规则

| 规则 | 条件 | 自动动作 | 备注 |
| --- | --- | --- | --- |
| `terminal_invalid_share` | `invalid_share_cleaned`、源已删除或失效分享已确认 | 无 | 只提示，不重复恢复 |
| `unsafe_path` | 目标路径不在允许根目录 | 无 | 只人工处理 |
| `strm_mode_mismatch` | 文件类型与任务有效 `strm_mode` 不一致，且任务有完整来源/分享证据 | `reprocess` | `direct` 任务中的直链不是问题 |
| `missing_destination` | 目标目录不存在 | 无 | 当前没有真实 restore 实现 |
| `missing_strm` | 目标目录无 STRM | 无 | 当前没有真实 restore 实现 |
| `unexpected_strm` | STRM 链接不是任务期望类型，且证据完整 | `reprocess` | 受尝试次数和冷却限制 |
| `repeated_failure` | 相同规则达到自动尝试上限 | 无 | 转人工，不再每日自动重试 |
| `risk_controlled` | 任务或全局 115 风控冷却中 | 无 | 等待冷却或人工确认 |

规则按优先级从高到低评估。`terminal_invalid_share`、`unsafe_path` 和 `risk_controlled` 优先于所有修复规则。对于同一任务的多个文件问题，规则只生成一个计划，保留问题代码集合和有限样本路径。

## 4. 持久化状态

继续使用 TaskStore 的任务元数据和事件，不新增第二套任务表。新增字段统一使用 `quality_` 前缀：

- `quality_rule_id`
- `quality_rule_reason`
- `quality_rule_risk_level`
- `quality_issue_codes`
- `quality_manual_status`: `open | snoozed | ignored | resumed | resolved`
- `quality_repair_attempts`
- `quality_last_attempt_at`
- `quality_next_eligible_at`
- `quality_last_run_id`
- `quality_last_actor`
- `quality_rule_version`

现有 `quality_repair_queued`、`quality_repair_started_at` 和 `quality_repair_deadline_at` 保留兼容，迁移时转换到统一字段。旧任务没有这些字段时视为 `open` 且尝试次数为零。

人工操作必须通过 TaskStore 的 compare-and-set 过渡完成，并记录事件消息、操作者、规则 ID 和操作结果。忽略或暂缓只抑制该任务的自动规则，不删除问题证据。

## 5. 执行流程

自动巡检分为四个阶段：

1. **本地扫描**：读取任务快照和允许根目录下的 STRM，只产生问题，不调用 115、CMS 或 Emby。
2. **规则评估**：按优先级合并每个任务的问题，计算自动/人工、冷却和跳过原因。
3. **预算入队**：仅对允许自动处理的计划入队。预算同时受 `QUALITY_AUTO_MAX_TASKS`、规则级上限、`QUALITY_AUTO_115_CHECK_LIMIT` 和全局 115/CMS 锁限制。
4. **结果归档**：记录扫描、自动入队、人工待处理、冷却、跳过和失败数量，并保存规则摘要。

自动修复的任务在入队前再次读取最新快照，检查任务状态、路径、风险冷却、人工状态和规则版本。任何检查失败都以 `skipped` 结束，不覆盖用户刚发生的任务状态。

任务执行成功后清理规则尝试和排队标记；进入失败或 `NEEDS_ACTION` 后增加尝试次数并计算指数退避。达到上限后标记 `manual_required`，除非用户执行“恢复自动处理”，否则后续巡检不再自动重试。

## 6. 人工干预

Web 和 Telegram 使用同一组 TaskStore 操作：

- **立即执行**：必须显式选择规则允许的动作。安全规则默认执行 `reprocess`；缺失目录/STRM 只能在人工确认后选择“从头重跑”，不能伪装成 `restore`；仍执行路径、状态和资源锁检查。
- **暂缓 24 小时**：保留问题，设置 `quality_manual_status=snoozed` 和下次检查时间。
- **忽略本次**：设置 `ignored`，不删除文件；只有“恢复自动处理”才重新进入规则评估。
- **恢复自动处理**：清除人工抑制和冷却，重新评估当前问题。
- **查看详情**：展示规则、证据、最近尝试、下一次可执行时间和完整任务时间线。

所有危险或会调用 CMS/115 的操作必须二次确认。人工操作不直接执行外部调用，只负责入队，由 TaskRunner 统一执行。

## 7. API 与界面

扩展现有 `/api/v1/quality`：

- 返回每个任务的 `rule_id`、`risk_level`、`auto_status`、`manual_status`、`attempts`、`next_eligible_at` 和 `available_actions`。
- 返回按规则聚合的数量和最近一次运行的预算消耗。

新增任务级 POST 操作，具体 URL 沿用现有 Web API 命名风格：

- `quality/action/execute`
- `quality/action/snooze`
- `quality/action/ignore`
- `quality/action/resume`

Web 质量页按“自动修复 / 需人工 / 暂缓 / 已忽略”分组，人工按钮只在规则允许时显示。Telegram `/quality` 显示高优先级人工任务，并使用回调按钮执行上述操作。

## 8. 安全与并发

- 规则评估不进行外部网络访问。
- 所有自动和人工入队都使用 TaskStore CAS，防止 Web、TG、调度器重复接管。
- 115/CMS 阶段继续使用现有全局锁和风控冷却；巡检不得绕过锁。
- 巡检不删除源文件；失效分享清理仍由现有安全检查流程负责。
- 路径必须通过现有允许根目录校验，不能因人工操作绕过路径边界。
- 规则版本写入任务，规则升级后只重新评估未完成或人工恢复的任务，不自动重置历史人工决定。

## 9. 测试与验收

新增或更新测试覆盖：

- `direct`、`shared`、`source_shared` 三种模式的 STRM 判定。
- 已清理失效分享不会生成恢复计划。
- 缺失目录/STRM 默认只进入人工队列。
- 规则优先级和同任务多问题合并。
- 尝试上限、指数退避、冷却和人工恢复。
- Web/TG 操作的 CAS、重复点击和重启持久化。
- 115/CMS 调用预算和全局锁不被绕过。
- 规则版本迁移和旧任务兼容。

验收指标：本地巡检零 115 调用；直接模式不再报告直链异常；自动计划只包含证据完整的规则；已清理任务不再重复入队；人工操作可在任务时间线中追溯。

## 10. 发布策略

分两步发布：

1. 先发布规则评估、只读 API 和人工状态，默认所有新规则保持 `manual`，观察一次完整巡检。
2. 仅开放 `strm_mode_mismatch` 和 `unexpected_strm` 的安全自动动作，其他规则保持人工模式；确认无误后再调整 Web 设置中的规则开关和预算。

旧的自动巡检运行状态、任务元数据和媒体文件不删除。升级失败时回滚镜像即可恢复旧执行逻辑。
