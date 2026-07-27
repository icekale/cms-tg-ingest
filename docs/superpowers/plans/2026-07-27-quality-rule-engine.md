# 自动巡检受控规则引擎 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将自动巡检改造成按规则评估、按风险分流、可限次退避且支持 Web/TG 人工接管的受控任务系统。

**Architecture:** 保留现有 TaskStore、TaskRunner 和单容器结构。新增纯本地 `QualityRuleEngine` 负责从任务快照和 STRM 检查结果生成唯一主规则；`QualityAutomation` 负责预算和入队；Web/TG 只调用统一的 TaskStore CAS 操作，不直接调用 115、CMS 或文件删除接口。

**Tech Stack:** Python 3.12 标准库、SQLite TaskStore、现有 `http.server` Web API、Telegram Bot API、Vue/Naive UI 前端、Python `unittest`。

---

## 文件地图

- Create: `app/quality_rules.py`，内置规则、匹配结果、规则配置和规则引擎。
- Modify: `app/quality.py`，按任务有效 STRM 模式检查 STRM，保留问题文件样本。
- Modify: `app/quality_automation.py`，接入规则引擎、预算、冷却、人工状态和统一计划摘要。
- Modify: `app/task_store.py`，增加质量状态的原子读取/更新辅助方法，复用现有任务事件和 CAS 迁移。
- Modify: `app/web_api.py`，返回规则字段、聚合统计和人工操作结果。
- Modify: `app/web.py`，增加质量操作 POST 路由和旧版质量页的操作入口。
- Modify: `app/telegram_ui.py`、`bridge.py`，增加人工问题按钮、回调校验和 Telegram 反馈。
- Modify: `frontend/src/api.js`、`frontend/src/views/Quality.vue`，展示规则分组、冷却、动作按钮和确认操作。
- Modify: `tests/test_task_quality.py`、`tests/test_quality_automation.py`、`tests/test_task_store.py`、`tests/test_web_api.py`、`tests/test_web_admin.py`、相关 Bridge 测试，覆盖规则、状态、API、TG 和并发行为。
- Modify: `README.md`、`.env.example`、`CHANGELOG.md`，记录规则模式、人工操作和升级兼容说明。

## Task 1: 建立模式感知的规则引擎

**Files:**
- Create: `app/quality_rules.py`
- Modify: `app/quality.py`
- Test: `tests/test_quality_rules.py`
- Test: `tests/test_task_quality.py`

- [ ] **Step 1: Write failing tests for STRM mode and terminal safety**

在 `tests/test_quality_rules.py` 增加以下测试场景：

```python
def test_direct_mode_accepts_direct_strm(self):
    task = make_task(metadata={"strm_mode": "direct", "dest_path": str(dest)})
    issues = [QualityIssue("direct_strm", "发现直链 STRM", str(dest / "movie.strm"), task.id)]
    match = QualityRuleEngine().evaluate(task, issues)
    self.assertEqual(match.rule_id, "no_issue")

def test_shared_mode_routes_direct_strm_to_mode_mismatch(self):
    task = make_task(metadata={"strm_mode": "shared", "dest_path": str(dest), "own_share_code": "share"})
    issues = [QualityIssue("direct_strm", "发现直链 STRM", str(dest / "movie.strm"), task.id)]
    match = QualityRuleEngine().evaluate(task, issues)
    self.assertEqual(match.rule_id, "strm_mode_mismatch")
    self.assertEqual(match.auto_action, "reprocess")

def test_invalid_share_cleaned_is_manual_terminal_rule(self):
    task = make_task(metadata={"strm_mode": "shared", "invalid_share_cleaned": True})
    match = QualityRuleEngine().evaluate(task, [QualityIssue("missing_dest", "目标目录不存在", task_id=task.id)])
    self.assertEqual(match.rule_id, "terminal_invalid_share")
    self.assertFalse(match.auto_allowed)

def test_missing_destination_is_manual_until_restore_exists(self):
    task = make_task(metadata={"strm_mode": "shared", "dest_path": str(dest)})
    match = QualityRuleEngine().evaluate(task, [QualityIssue("missing_dest", "目标目录不存在", task_id=task.id)])
    self.assertEqual(match.rule_id, "missing_destination")
    self.assertEqual(match.auto_action, "none")
```

`make_task` 使用现有 `TaskSnapshot` 构造方式，测试目录通过 `tempfile.TemporaryDirectory` 创建；测试必须先失败，原因是 `app.quality_rules` 不存在且当前 `quality.py` 未携带有效模式。

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```sh
python3 -m unittest tests.test_quality_rules tests.test_task_quality -v
```

Expected: FAIL with `ModuleNotFoundError` or missing `QualityRuleEngine`.

- [ ] **Step 3: Implement the pure rule engine**

在 `app/quality_rules.py` 定义：

```python
QUALITY_RULE_VERSION = "1"

@dataclass(frozen=True)
class QualityRuleMatch:
    rule_id: str
    priority: int
    risk_level: str
    reason: str
    issue_codes: tuple[str, ...]
    auto_action: str = "none"
    auto_allowed: bool = False
    manual_actions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

class QualityRuleEngine:
    def evaluate(self, task: TaskSnapshot, issues: Iterable[QualityIssue], *, config: dict[str, Any] | None = None) -> QualityRuleMatch:
        issue_rows = tuple(issues)
        codes = tuple(sorted({issue.code for issue in issue_rows}))
        metadata = task.metadata
        enabled = config or {}
        if metadata.get("invalid_share_cleaned") or metadata.get("source_deleted"):
            return QualityRuleMatch("terminal_invalid_share", 10, "manual", "任务已完成失效分享清理", codes, manual_actions=("view", "resume"))
        if not _metadata_paths_safe(task):
            return QualityRuleMatch("unsafe_path", 20, "manual", "任务路径不在允许根目录", codes, manual_actions=("view",))
        if _risk_controlled(task):
            return QualityRuleMatch("risk_controlled", 30, "manual", "115 当前处于风控冷却", codes, manual_actions=("view", "resume"))
        mode = effective_task_strm_mode(task)
        if mode == "direct" and "direct_strm" in codes:
            codes = tuple(code for code in codes if code != "direct_strm")
        if codes and _has_mode_mismatch(mode, codes) and _auto_enabled(enabled, "strm_mode_mismatch"):
            return QualityRuleMatch("strm_mode_mismatch", 40, "safe", "STRM 类型与任务模式不一致", codes, "reprocess", True, ("reprocess", "snooze", "ignore", "view"))
        if "missing_dest" in codes:
            return QualityRuleMatch("missing_destination", 50, "manual", "目标目录不存在，当前没有真实恢复动作", codes, manual_actions=("reprocess", "snooze", "ignore", "view"))
        if "missing_strm" in codes:
            return QualityRuleMatch("missing_strm", 60, "manual", "目标目录没有 STRM，当前没有真实恢复动作", codes, manual_actions=("reprocess", "snooze", "ignore", "view"))
        if codes and _has_mode_mismatch(mode, codes) and _auto_enabled(enabled, "unexpected_strm"):
            return QualityRuleMatch("unexpected_strm", 70, "guarded", "STRM 链接不是任务期望类型", codes, "reprocess", True, ("reprocess", "snooze", "ignore", "view"))
        if _attempt_limit_reached(task, enabled):
            return QualityRuleMatch("repeated_failure", 80, "manual", "相同规则已达到自动尝试上限", codes, manual_actions=("reprocess", "resume", "view"))
        if not codes:
            return QualityRuleMatch("no_issue", 1000, "safe", "未发现质量问题", ())
        return QualityRuleMatch("manual_required", 900, "manual", "问题缺少安全自动处理规则", codes, manual_actions=("snooze", "ignore", "view"))
```

同文件定义 `_metadata_paths_safe()`、`_risk_controlled()`、`_has_mode_mismatch()`、`_auto_enabled()` 和 `_attempt_limit_reached()` 五个纯函数；它们只读取任务 metadata、规则配置和问题代码，分别负责路径边界、风控标记、模式/问题代码匹配、规则开关和尝试上限判断。

实现顺序固定为 `terminal_invalid_share`、`unsafe_path`、`risk_controlled`、`strm_mode_mismatch`、`missing_destination`、`missing_strm`、`unexpected_strm`、`repeated_failure`、`no_issue`。有效模式通过现有 `effective_task_strm_mode(task)` 获取；`direct` 任务的 `direct_strm` 不产生问题，`shared`/`source_shared` 按现有 STRM 内容标记规则判断。

新增 `rule_config()` 返回内置规则的默认值；规则配置只包含布尔开关、整数预算和冷却秒数，不执行动态表达式。

- [ ] **Step 4: Make local STRM inspection mode-aware**

修改 `app/quality.py`：

- 从 `app.strm_mode` 导入 `effective_task_strm_mode`。
- `inspect_task_files()` 增加 `expected_mode: str = "shared"` 参数。
- 当 `expected_mode == "direct"` 时，`/d/` 内容不产生 `direct_strm`；当期望分享模式且内容是 `/d/` 时保留 `direct_strm`。
- 当期望直链模式而内容不是 `/d/` 时产生 `unexpected_strm`。
- 保留当前路径安全、空目录和分享 marker 校验。
- `scan_task_quality()` 对每个任务传入 `effective_task_strm_mode(task)`，并保持现有 `QualityIssue` 对外字段兼容。

- [ ] **Step 5: Run focused tests and full quality tests**

Run:

```sh
python3 -m unittest tests.test_quality_rules tests.test_task_quality -v
```

Expected: all new mode/terminal tests and existing task-quality tests pass.

- [ ] **Step 6: Commit the rule engine foundation**

```sh
git add app/quality_rules.py app/quality.py tests/test_quality_rules.py tests/test_task_quality.py
git commit -m "feat: add mode-aware quality rules"
```

## Task 2: 持久化规则状态、冷却和预算

**Files:**
- Modify: `app/task_store.py`
- Modify: `app/quality_automation.py`
- Test: `tests/test_task_store.py`
- Test: `tests/test_quality_automation.py`

- [ ] **Step 1: Write failing tests for state transitions and idempotency**

增加测试：

- `quality_state` 默认 `open`、尝试次数为 `0`。
- 两个并发 `execute_plan()` 只有一个返回 `queued`，另一个返回 `task_busy`。
- 达到规则尝试上限后返回 `repeated_failure`，第二天不自动再次入队。
- `snooze` 在截止时间前不入队，`resume` 清除冷却并允许重新评估。
- `invalid_share_cleaned` 的任务不会进入 `restore`。

- [ ] **Step 2: Run tests and verify the current implementation fails**

```sh
python3 -m unittest tests.test_task_store tests.test_quality_automation -v
```

Expected: FAIL because the new quality metadata and operations do not exist.

- [ ] **Step 3: Add TaskStore quality state helpers**

在 `app/task_store.py` 增加以下边界方法，全部使用现有连接锁和 `record_event`/`compare_and_set_transition`：

- `quality_state(task_id: int) -> dict[str, Any]`：读取并补齐默认质量字段，不写库。
- `update_quality_state(task_id: int, *, expected_updated_at: float, patch: dict[str, Any], message: str, actor: str) -> TaskSnapshot | None`：使用更新版本 CAS 写入 metadata 并记录事件。
- `mark_quality_snoozed(task_id: int, until: float, actor: str) -> TaskSnapshot | None`：写入 `quality_manual_status=snoozed`、截止时间和操作者。
- `mark_quality_ignored(task_id: int, actor: str) -> TaskSnapshot | None`：写入 `quality_manual_status=ignored` 并记录忽略事件。
- `resume_quality(task_id: int, actor: str) -> TaskSnapshot | None`：清除人工抑制、冷却和当前规则尝试状态，重新进入 `open`。

方法只更新 `metadata_json`、错误/阶段所需字段和事件，不直接删除 STRM 或修改 submissions。`quality_state()` 对旧任务补齐默认值但不写库。

- [ ] **Step 4: Integrate rule evaluation and task budgets**

修改 `QualityAutomation`：

- 持有 `QualityRuleEngine` 和由 TaskStore runtime state 加载的规则覆盖配置。
- `_run_once_owned()` 扫描任务后逐任务生成一个 `QualityRuleMatch`，再生成一个 `QualityRepairPlan`。
- `restore` 规则默认不自动计划；仅保留人工动作。
- 自动计划前检查 `quality_manual_status`, `quality_next_eligible_at`, `quality_repair_attempts`, `p115_risk_cooldown_until`, `invalid_share_cleaned` 和 `submission_id`。
- `strm_mode_mismatch`/`unexpected_strm` 只有在任务有有效 `submission_id` 或完整源分享信息时允许自动 `reprocess`。
- 每个规则维护 `max_attempts` 和 `cooldown_seconds`；默认 `reprocess` 每任务最多 2 次，冷却采用 `min(base * 2 ** attempts, 7 days)`。
- 每次自动计划消耗规则预算；`QUALITY_AUTO_MAX_TASKS` 限制所有动作，`QUALITY_AUTO_115_CHECK_LIMIT` 限制需要 115/CMS 的动作，不能只展示不执行。
- 摘要增加 `rule_counts`、`manual_count`、`cooldown_count`、`budget_used`，兼容旧摘要中不存在的字段。

删除当前“`quality_repair_queued` 仅在少数阶段阻止重复入队”的判断，改为统一读取质量状态；保留旧字段迁移和 TaskRunner 清理兼容。

- [ ] **Step 5: Run focused and full backend tests**

```sh
python3 -m unittest tests.test_task_store tests.test_quality_automation -v
python3 -m unittest discover -s tests -p 'test*.py' -q
```

Expected: focused tests and the full suite pass with no external network dependency.

- [ ] **Step 6: Commit persistence and scheduler changes**

```sh
git add app/task_store.py app/quality_automation.py tests/test_task_store.py tests/test_quality_automation.py
git commit -m "feat: add quality rule state and cooldowns"
```

## Task 3: Web API 和旧版 Web 人工操作

**Files:**
- Modify: `app/web_api.py`
- Modify: `app/web.py`
- Test: `tests/test_web_api.py`
- Test: `tests/test_web_admin.py`

- [ ] **Step 1: Write failing API tests**

覆盖以下接口行为：

- `GET /api/v1/quality` 返回 `rule_id`、`risk_level`、`manual_status`、`attempts`、`next_eligible_at` 和 `available_actions`。
- `POST quality/action/execute` 只接受合法 `task_id`、`rule_id` 和允许的具体动作，重复提交返回 `409` 或 `task_busy`。
- `snooze`、`ignore`、`resume` 需要任务存在并写入事件。
- 规则不允许的动作返回 `409`，不改变任务状态。
- 以上操作不能触发 115/CMS 客户端调用。

- [ ] **Step 2: Run focused API tests and verify failure**

```sh
python3 -m unittest tests.test_web_api tests.test_web_admin -v
```

Expected: FAIL because the quality payload and action routes are not implemented.

- [ ] **Step 3: Extend `api_quality()` with rule data**

修改 `app/web_api.py`：

- 接收 `quality_automation` 的规则评估结果或由服务提供的 `quality_view()`。
- 按任务聚合问题，返回规则、风险、人工状态、尝试次数、冷却时间和动作列表。
- 对没有 TaskStore 任务的孤立问题返回 `manual_required`，动作只允许查看。
- 保持 `count` 和 `items[].code/message/detail` 兼容旧前端。

- [ ] **Step 4: Add authenticated action handlers**

在 `app/web.py` 的 POST 分发中增加：

```python
quality/action/execute
quality/action/snooze
quality/action/ignore
quality/action/resume
```

请求体包含 `task_id`、`rule_id` 和 `action`；`action` 必须属于规则返回的 `available_actions`，人工缺失目录/STRM 只能选择显式 `reprocess`。所有动作调用 `QualityAutomation.manual_action()`，由该方法验证任务快照、规则、路径、状态和 CAS；Web 层不直接操作文件、115 或 CMS。

- [ ] **Step 5: Add old Web quality controls**

质量页按 `auto_eligible`、`manual_required`、`snoozed`、`ignored` 分组；每个任务显示规则、证据、尝试次数和下一次时间。execute/ignore 等危险或改变状态的操作保留确认框，质量页刷新后重新读取 TaskStore。

- [ ] **Step 6: Run Web tests and commit**

```sh
python3 -m unittest tests.test_web_api tests.test_web_admin -v
git add app/web_api.py app/web.py tests/test_web_api.py tests/test_web_admin.py
git commit -m "feat: add quality web interventions"
```

## Task 4: Telegram 人工队列和回调

**Files:**
- Modify: `app/telegram_ui.py`
- Modify: `bridge.py`
- Test: `tests/test_telegram_ui.py`
- Test: `tests/test_bridge_v02_integration.py` 或现有 Telegram 回调测试文件

- [ ] **Step 1: Write failing callback tests**

覆盖：

- `/quality` 只展示高优先级 `manual_required` 和 `auto_eligible` 任务。
- 回调数据包含短格式 action、task ID 和规则版本，不把 URL、Cookie 或路径放入 callback data。
- 非白名单用户、未知 task ID、过期规则版本和重复点击均被拒绝。
- execute、snooze、ignore、resume 回调返回简短中文结果并刷新键盘。

- [ ] **Step 2: Run focused tests and verify failure**

```sh
python3 -m unittest tests.test_telegram_ui tests.test_bridge_v02_integration -v
```

Expected: FAIL because quality action callbacks are not present.

- [ ] **Step 3: Add compact quality keyboard**

在 `app/telegram_ui.py` 增加 `quality_manual_keyboard(rows, limit=8)`；每个 callback 使用 `quality:<action>:<task_id>:<rule_version>`，长度控制在 Telegram callback data 限制内。显示文本使用规则名称、风险和冷却，不发送完整路径。

- [ ] **Step 4: Route callbacks through `QualityAutomation.manual_action()`**

在 `bridge.py` 的 callback 分发中校验白名单和 chat ID，解析 action/task/rule version，调用统一服务；成功后 `answerCallbackQuery` 并编辑原消息，失败只反馈原因，不吞掉状态冲突。

- [ ] **Step 5: Run Telegram tests and commit**

```sh
python3 -m unittest tests.test_telegram_ui tests.test_bridge_v02_integration -v
git add app/telegram_ui.py bridge.py tests/test_telegram_ui.py tests/test_bridge_v02_integration.py
git commit -m "feat: add Telegram quality interventions"
```

## Task 5: Vue 质量页和规则设置

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/views/Quality.vue`
- Test: `tests/test_frontend.py`

- [ ] **Step 1: Write failing frontend contract tests**

在 `tests/test_frontend.py` 检查构建入口和页面源码包含：规则风险、冷却、人工状态、执行/暂缓/忽略/恢复动作，以及确认提示；并检查 API 方法使用上述四个 POST 路由。

- [ ] **Step 2: Run the contract test and verify failure**

```sh
python3 -m unittest tests.test_frontend -v
```

Expected: FAIL because the Vue quality page only exposes the old bulk actions.

- [ ] **Step 3: Add API methods and task-level action cards**

在 `frontend/src/api.js` 增加：

```javascript
qualityAction: (action, taskId, ruleId, selectedAction = action) => request(`quality/action/${action}`, {
  method: 'POST',
  body: JSON.stringify({ task_id: taskId, rule_id: ruleId, action: selectedAction })
})
```

在 `frontend/src/views/Quality.vue`：

- 用标签区分自动、人工、暂缓、忽略。
- 展示规则、风险级别、尝试次数、冷却截止时间和问题样本。
- execute/ignore/resume 使用 `window.confirm`；snooze 直接调用并显示结果。
- 操作成功后重新加载质量数据，失败显示后端返回的中文原因。
- 保留现有“立即巡检”和“修复”入口，但将批量修复明确标记为只处理规则允许的项目。

- [ ] **Step 4: Build frontend and commit**

```sh
npm ci --prefix frontend
npm run build --prefix frontend
python3 -m unittest tests.test_frontend -v
git add frontend/src/api.js frontend/src/views/Quality.vue tests/test_frontend.py
git commit -m "feat: expose quality rule controls in Vue"
```

Expected: Vite exits `0`; only existing chunk-size warning may remain。

## Task 6: 规则配置、迁移和文档

**Files:**
- Modify: `app/config.py`
- Modify: `app/quality_automation.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_quality_automation.py`
- Test: `tests/test_docs_v02.py`

- [ ] **Step 1: Write failing configuration tests**

覆盖默认配置：

- 自动巡检仍由 `QUALITY_AUTO_ENABLED` 控制。
- 新规则配置默认安全：`strm_mode_mismatch` 和 `unexpected_strm` 可自动；`missing_destination`、`missing_strm`、`terminal_invalid_share` 为人工。
- 规则覆盖 JSON 非法、未知 rule ID、负预算和超过上限时拒绝并保留上一份配置。
- 旧 `quality_auto_last_summary` 缺少新字段时读取为零值，不导致启动失败。

- [ ] **Step 2: Implement runtime rule settings**

使用现有 `quality_auto_overrides` runtime state 扩展为：

```json
{
  "quality_auto_enabled": true,
  "quality_auto_max_tasks": 5,
  "quality_auto_115_check_limit": 3,
  "rules": {
    "strm_mode_mismatch": {"enabled": true, "max_attempts": 2, "cooldown_seconds": 86400},
    "unexpected_strm": {"enabled": true, "max_attempts": 2, "cooldown_seconds": 86400},
    "missing_destination": {"enabled": false},
    "missing_strm": {"enabled": false}
  }
}
```

解析时只接受已知 rule ID 和整数范围；任何非法更新抛出 `ValueError`，不写入部分结果。Web 设置页读取并保存同一份结构。

- [ ] **Step 3: Add backward-compatible migration**

启动时将旧 `quality_repair_queued`、`quality_repair_started_at`、`quality_repair_deadline_at` 映射到新字段；不改写所有历史任务，只在首次评估或人工操作时按 CAS 补齐，避免升级时大批量写库和触发巡检。

- [ ] **Step 4: Update docs and tests**

在 `.env.example`、`README.md` 和 `CHANGELOG.md` 说明：

- 规则默认行为和 `02:50` 调度。
- Web/TG 手动操作流程。
- `missing_*` 默认只人工处理。
- 升级不删除历史任务、STRM 或 115 源文件。

Run:

```sh
python3 -m unittest tests.test_quality_automation tests.test_docs_v02 -v
git diff --check
git add app/config.py app/quality_automation.py .env.example README.md CHANGELOG.md tests/test_quality_automation.py tests/test_docs_v02.py
git commit -m "docs: document quality rule policies"
```

## Task 7: 全量验证、观测和分阶段发布

**Files:**
- Modify: `app/__init__.py`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Test: all `tests/test*.py`

- [ ] **Step 1: Run all local verification**

```sh
git diff --check
python3 -m compileall -q app bridge.py doctor.py
python3 -m unittest discover -s tests -p 'test*.py' -q
npm ci --prefix frontend
npm run build --prefix frontend
docker build --pull=false -t cms-tg-ingest:quality-rule-engine-check .
```

Expected: Python suite passes, Vite exits `0`, Docker image builds successfully.

- [ ] **Step 2: Enable safe rules in shadow/manual mode first**

发布时先让规则评估和 API 可见，`strm_mode_mismatch`、`unexpected_strm` 的自动动作由 Web 设置控制，其他规则保持人工。确认第一次完整巡检的 `rule_counts`、`manual_count`、`budget_used` 与本地预期一致后，再打开安全规则自动入队。

- [ ] **Step 3: Verify production without destructive operations**

```sh
curl -fsS http://127.0.0.1:8788/api/v1/health
curl -fsS http://127.0.0.1:8788/api/v1/quality
docker exec cms-tg-ingest python /app/doctor.py --quiet
docker logs --since=10m cms-tg-ingest
```

检查：容器 healthy、`runner_heartbeat_stale=false`、115 冷却状态可见、质量摘要有规则计数和预算、没有 TaskRunner 异常。第一次只读巡检不手动触发大批量自动修复。

- [ ] **Step 4: Publish fixed version and keep rollback point**

创建数据和 `.env` 备份，推送 GitHub tag，发布 Docker Hub `amd64/arm64` 固定版本，Unraid Compose 只更新镜像标签。验证失败时回滚到上一固定版本，不删除 `/data`、媒体挂载或 115 Cookie。

- [ ] **Step 5: Commit the release metadata**

```sh
git add app/__init__.py CHANGELOG.md README.md
git commit -m "release: publish quality rule engine"
```

## Plan Self-Review

- Spec coverage: 规则模型对应 Task 1；状态、冷却和预算对应 Task 2；Web/TG 人工操作对应 Tasks 3-5；配置迁移对应 Task 6；安全发布和验证对应 Task 7。
- No dynamic rule execution: the plan only accepts known rule IDs and typed JSON overrides.
- Type consistency: `QualityRuleMatch` is produced by `QualityRuleEngine.evaluate()`, consumed by `QualityAutomation`; `manual_action()` is the single entry point for Web and Telegram.
- Safety consistency: missing paths remain manual because `restore()` is not a real recovery implementation; automatic reprocess is limited by mode, evidence, attempts, cooldown and global locks.
- Migration consistency: old quality metadata is retained and translated lazily; no historical database rewrite or media deletion is part of the plan.
