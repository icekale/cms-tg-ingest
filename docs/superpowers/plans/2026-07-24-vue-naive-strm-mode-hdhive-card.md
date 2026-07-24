# Vue Naive Admin、STRM 模式与 HDHive 卡片实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有 Python SSR、TaskStore、CMS/115/Emby 工作流和 Telegram 入口的前提下，增加可安全切换的共享/直链 STRM 模式、`/app` Vue 管理壳，以及带海报、积分和解锁时间的 HDHive 订阅反馈。

**Architecture:** 用一个小型 `strm_mode` 策略模块统一新旧任务的模式推断，TaskRunner 根据任务模式选择共享或直链阶段流转；共享模式继续使用现有自有分享和清理门槛，直链模式只验证 CMS 普通同步生成的 STRM，不执行 115 源文件清理。现有 `WebApp` 继续负责认证和 POST 操作，新增只读 JSON API 和静态 `/app` 入口；Vue 只读取这些 API，HDHive 卡片由后端生成并通过 Telegram `sendPhoto`/`sendMessage` 发送。

**Tech Stack:** Python 3.12 标准库、SQLite、现有 `TaskStore`/`HdhiveSubscriptionStore`、Vue 3、Vite、Naive UI、Node 22 Alpine 构建阶段、Python `unittest`、npm lockfile。

---

## 文件地图

- Create: `app/strm_mode.py` — `shared`/`direct` 常量、旧任务兼容推断、锁定规则。
- Create: `app/workflows/direct.py` — CMS 普通同步的 TaskRunner 工作流；只负责直链模式，不调用 115 自有分享和源文件清理。
- Create: `app/web_api.py` — `/api/v1/` 的只读序列化和脱敏函数。
- Create: `app/hdhive_cards.py` — TMDB 详情缓存、订阅成功事件和 Telegram 卡片文本/图片选择。
- Create: `frontend/package.json`, `frontend/package-lock.json`, `frontend/index.html`, `frontend/vite.config.js`, `frontend/src/main.js`, `frontend/src/api.js`, `frontend/src/router.js`, `frontend/src/styles.css`, `frontend/src/layouts/AppLayout.vue`, `frontend/src/views/Overview.vue`, `frontend/src/views/Tasks.vue`, `frontend/src/views/TaskDetail.vue`, `frontend/src/views/Quality.vue`, `frontend/src/views/Health.vue`, `frontend/src/views/Hdhive.vue` — 轻量 Vue/Naive UI 管理壳和页面。
- Create: `frontend/THIRD_PARTY_NOTICES.md` — `zclzone/vue-naive-admin`/Naive UI 的来源和 MIT 声明。
- Modify: `app/config.py`, `.env.example` — STRM 默认模式和前端路径配置。
- Modify: `app/models.py`, `app/task_runner.py`, `app/task_store.py`, `app/task_bridge.py` — 模式元数据、默认设置、直链终态和旧任务兼容。
- Modify: `app/media/strm.py` — 暴露直链 STRM 验证函数，复用现有 `/d/` 检查和路径白名单。
- Modify: `app/workflows/self_share.py` — 共享模式锁定和模式保护，不改变现有整理、改名、分类逻辑。
- Modify: `app/clients/hdhive.py`, `app/hdhive_subscription_store.py`, `app/hdhive_subscriptions.py` — 实际/估算积分、解锁时间、任务号和检查摘要。
- Modify: `app/media/classify.py` — TMDB 详情字段规范化，供卡片复用。
- Modify: `app/web.py` — 静态 `/app`、只读 API、STRM 默认/任务模式 POST 路由，保留全部旧 SSR/POST 路由。
- Modify: `bridge.py`, `app/telegram_ui.py` — 订阅成功事件、Telegram `sendPhoto`、HTML 卡片和 HDHive 任务号关联。
- Modify: `Dockerfile`, `README.md`, `PRODUCT.md`, `docs/dockerhub-overview.md` — 多阶段构建、中文使用说明和第三方声明。
- Test: `tests/test_strm_mode.py`, `tests/test_direct_workflow.py`, `tests/test_web_api.py`, `tests/test_frontend.py`, `tests/test_hdhive_cards.py`。
- Modify: `tests/test_task_store.py`, `tests/test_task_runner.py`, `tests/test_bridge_task_engine.py`, `tests/test_hdhive_client.py`, `tests/test_hdhive_subscription_store.py`, `tests/test_hdhive_subscriptions.py`, `tests/test_hdhive_web.py`, `tests/test_telegram_client.py`, `tests/test_dockerfile.py`。

保留现有 `app/web.py` 的 `/`、`/quality`、`/health`、`/hdhive` 页面和所有旧 POST 路由；不删除现有 `WORKFLOW_MODE`、旧数据库字段或未关联的用户修改。

### Task 1: 建立 STRM 模式契约和旧任务兼容

**Files:**
- Create: `app/strm_mode.py`
- Modify: `app/config.py`, `.env.example`, `app/models.py`, `app/task_store.py`, `app/task_bridge.py`
- Test: `tests/test_strm_mode.py`, `tests/test_task_store.py`

- [ ] **Step 1: 先写失败测试，固定模式值、默认值和旧任务推断。**

在 `tests/test_strm_mode.py` 写入以下测试核心：

```python
import tempfile
import unittest
from pathlib import Path

from app.models import TaskStage, TaskStatus
from app.strm_mode import (
    effective_task_strm_mode,
    is_strm_mode_locked,
    normalize_strm_mode,
    next_stage_for_mode,
)
from app.task_store import TaskStore


class StrmModeTests(unittest.TestCase):
    def test_only_shared_and_direct_are_accepted(self):
        self.assertEqual(normalize_strm_mode("shared"), "shared")
        self.assertEqual(normalize_strm_mode("DIRECT"), "direct")
        with self.assertRaises(ValueError):
            normalize_strm_mode("delete-source")

    def test_direct_flow_skips_share_and_clean_stages(self):
        self.assertEqual(next_stage_for_mode(TaskStage.RECOGNIZING, "direct"), TaskStage.STRM_READY)
        self.assertEqual(next_stage_for_mode(TaskStage.EMBY_CONFIRMED, "direct"), None)
        self.assertEqual(next_stage_for_mode(TaskStage.RECOGNIZING, "shared"), TaskStage.SHARE_ALIAS_PREPARED)

    def test_mode_lock_starts_before_strm_side_effects(self):
        self.assertFalse(is_strm_mode_locked(TaskStage.RECOGNIZING))
        self.assertTrue(is_strm_mode_locked(TaskStage.STRM_READY))
        self.assertTrue(is_strm_mode_locked(TaskStage.SHARE_ALIAS_PREPARED))

    def test_old_metadata_and_legacy_workflow_mode_are_compatible(self):
        task = type("Task", (), {"metadata": {}, "current_stage": TaskStage.RECEIVED})()
        self.assertEqual(effective_task_strm_mode(task, default_mode="shared", legacy_workflow_mode="direct"), "direct")
        self.assertEqual(effective_task_strm_mode(task, default_mode="shared", legacy_workflow_mode="self_share_sync"), "shared")

    def test_default_mode_persists_in_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.db")
            self.assertEqual(store.get_default_strm_mode(), "shared")
            self.assertEqual(store.set_default_strm_mode("direct"), "direct")
            self.assertEqual(store.get_default_strm_mode(), "direct")
            with self.assertRaises(ValueError):
                store.set_default_strm_mode("remove")


if __name__ == "__main__":
    unittest.main()
```

再在 `tests/test_task_store.py` 增加任务元数据测试：第一次 `upsert_task("abc", "", "https://115cdn.com/s/abc?password=abcd", strm_mode="direct")` 必须写入 `metadata["strm_mode"]`；同一分享链接再次以 `strm_mode="shared"` 调用 `upsert_task("abc", "", "https://115cdn.com/s/abc?password=abcd", strm_mode="shared")` 不得覆盖已经选定的模式；没有该字段的旧任务必须能被读取。

- [ ] **Step 2: 运行测试，确认缺少模式接口。**

运行：

```sh
python3 -m unittest tests.test_strm_mode tests.test_task_store -q
```

预期：失败在 `app.strm_mode`、`TaskStore.get_default_strm_mode` 或 `upsert_task("abc", "", "https://115cdn.com/s/abc", strm_mode="direct")` 尚未存在，而不是 SQLite 初始化错误。

- [ ] **Step 3: 实现模式模块和 TaskStore 运行时设置。**

在 `app/strm_mode.py` 提供以下稳定接口：

```python
from __future__ import annotations

from typing import Any

from .models import TaskStage

STRM_MODES = ("shared", "direct")
STRM_MODE_LABELS = {"shared": "共享 STRM", "direct": "直链 STRM"}
_LOCKED_STAGES = frozenset({
    TaskStage.SHARE_ALIAS_PREPARED,
    TaskStage.OWN_SHARE_CREATED,
    TaskStage.SHARE_VALIDATED,
    TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.STRM_READY,
    TaskStage.CMS_DELETE_SETTLED,
    TaskStage.MOVED,
    TaskStage.EMBY_CONFIRMED,
    TaskStage.CLEANED,
})


def normalize_strm_mode(value: Any, default: str = "shared") -> str:
    candidate = str(value or "").strip().lower()
    if candidate not in STRM_MODES:
        if candidate:
            raise ValueError("STRM mode must be shared or direct")
        candidate = default
    if candidate not in STRM_MODES:
        raise ValueError("STRM default mode must be shared or direct")
    return candidate


def effective_task_strm_mode(task: Any, default_mode: str = "shared", legacy_workflow_mode: str = "") -> str:
    metadata = getattr(task, "metadata", {}) or {}
    selected = metadata.get("strm_mode")
    if selected:
        return normalize_strm_mode(selected)
    legacy = str(legacy_workflow_mode or metadata.get("workflow_mode") or "").strip().lower()
    if legacy == "self_share_sync":
        return "shared"
    if legacy == "direct":
        return "direct"
    return normalize_strm_mode(default_mode)


def is_strm_mode_locked(stage: TaskStage) -> bool:
    return stage in _LOCKED_STAGES


def next_stage_for_mode(stage: TaskStage, mode: str) -> TaskStage | None:
    mode = normalize_strm_mode(mode)
    if mode == "direct":
        return {
            TaskStage.RECEIVED: TaskStage.ORGANIZING,
            TaskStage.CLOUD_DOWNLOADING: TaskStage.ORGANIZING,
            TaskStage.ORGANIZING: TaskStage.RECOGNIZING,
            TaskStage.RECOGNIZING: TaskStage.STRM_READY,
            TaskStage.STRM_READY: TaskStage.MOVED,
            TaskStage.MOVED: TaskStage.EMBY_CONFIRMED,
            TaskStage.EMBY_CONFIRMED: None,
        }.get(stage)
    return {
        TaskStage.RECEIVED: TaskStage.ORGANIZING,
        TaskStage.CLOUD_DOWNLOADING: TaskStage.ORGANIZING,
        TaskStage.ORGANIZING: TaskStage.RECOGNIZING,
        TaskStage.RECOGNIZING: TaskStage.SHARE_ALIAS_PREPARED,
        TaskStage.SHARE_ALIAS_PREPARED: TaskStage.OWN_SHARE_CREATED,
        TaskStage.OWN_SHARE_CREATED: TaskStage.SHARE_VALIDATED,
        TaskStage.SHARE_VALIDATED: TaskStage.SHARE_SYNC_SUBMITTED,
        TaskStage.SHARE_SYNC_SUBMITTED: TaskStage.STRM_READY,
        TaskStage.STRM_READY: TaskStage.CMS_DELETE_SETTLED,
        TaskStage.CMS_DELETE_SETTLED: TaskStage.MOVED,
        TaskStage.MOVED: TaskStage.EMBY_CONFIRMED,
        TaskStage.EMBY_CONFIRMED: TaskStage.CLEANED,
    }.get(stage)
```

在 `TaskStore` 使用运行时键 `strm_default_mode`，新增 `get_default_strm_mode()` 和 `set_default_strm_mode(mode)`；设置写入前调用 `normalize_strm_mode`。扩展 `upsert_task` 为 `strm_mode: str | None = None`，插入时把有效模式写入 `metadata_json`，冲突更新时仅在原元数据没有 `strm_mode` 且调用方提供模式时补入，避免重复消息改变模式。默认值必须是 `shared`，历史任务不批量改写。

在 `Config` 增加：

```python
strm_default_mode: str = "shared"
frontend_dist_path: str = "/app/frontend/dist"
```

`Config.from_env()` 读取 `STRM_DEFAULT_MODE`；若该变量不存在，再检查 `WORKFLOW_MODE` 是否真的出现在环境中，`self_share_sync` 映射为 `shared`，`direct` 映射为 `direct`；两个环境变量都未显式设置时使用 `shared`。`Config.workflow_mode` 保留原字段和旧默认值供旧分支兼容，但 TaskRunner 的新模式选择使用 `strm_default_mode`。读取 `FRONTEND_DIST_PATH`，不因前端目录缺失而阻止 Python 服务启动。在 `.env.example` 增加：

```env
STRM_DEFAULT_MODE=shared
FRONTEND_DIST_PATH=/app/frontend/dist
```

把 `task_bridge.record_submission_event` 和 `ensure_task_for_link` 接受的 `strm_mode` 写入任务元数据；不从分享 URL、标题或 TMDB ID 猜测模式。

- [ ] **Step 4: 运行模式和存储测试。**

运行：

```sh
python3 -m unittest tests.test_strm_mode tests.test_task_store tests.test_bridge_task_engine -q
```

预期：新增模式测试和原有 TaskStore/TaskRunner 测试全部通过。

- [ ] **Step 5: 提交模式契约切片。**

```sh
git add app/strm_mode.py app/config.py app/models.py app/task_store.py app/task_bridge.py .env.example tests/test_strm_mode.py tests/test_task_store.py
git commit -m "feat: add shared and direct strm mode contract"
```

### Task 2: 接入直链 TaskRunner 流程并锁定模式

**Files:**
- Create: `app/workflows/direct.py`
- Modify: `app/models.py`, `app/task_runner.py`, `app/workflows/self_share.py`, `app/media/strm.py`, `bridge.py`
- Test: `tests/test_direct_workflow.py`, `tests/test_task_runner.py`, `tests/test_bridge_task_engine.py`
- Test: `tests/test_media_sources.py`

- [ ] **Step 1: 写失败测试，先验证直链不会建分享或清理源文件。**

使用临时 `TaskStore`、假的 CMS、假的 `SubmissionStore`、临时 STRM 源目录和假的 Emby 客户端，覆盖以下断言：

```python
def test_direct_workflow_submits_cms_and_never_creates_own_share_or_deletes_source():
    workflow, cms, p115, source, emby, task = make_direct_workflow_with_one_strm()

    run_until_terminal(workflow, task)

    assert cms.add_share_down_calls == [task.url]
    assert p115.receive_calls == []
    assert p115.create_share_calls == []
    assert p115.delete_calls == []
    assert task_store.find_task(task.id).current_stage == TaskStage.EMBY_CONFIRMED
    assert source.exists()


def test_direct_workflow_uses_cms_category_and_records_direct_source():
    workflow, _cms, _p115, _source, _emby, task = make_direct_workflow_with_one_strm(category="欧美电影")

    run_until_terminal(workflow, task)

    final = task_store.find_task(task.id)
    assert final.metadata["strm_mode"] == "direct"
    assert final.metadata["direct_strm"] is True
    assert final.metadata["category_final"] == "欧美电影"
    assert final.metadata.get("cleanup_status", "") == ""
```

在 `tests/test_task_runner.py` 增加 `TaskRunner._apply_result` 的模式流转测试：直链从 `RECOGNIZING` 直接进入 `STRM_READY`，`EMBY_CONFIRMED` 成功后不再入队 `CLEANED`；共享模式保持原有全阶段顺序。

- [ ] **Step 2: 运行新增测试，确认当前实现会错误地进入共享阶段。**

运行：

```sh
python3 -m unittest tests.test_direct_workflow tests.test_task_runner -q
```

预期：失败在直链工作流不存在、或当前 TaskRunner 把 `RECOGNIZING` 转到 `SHARE_ALIAS_PREPARED`。

- [ ] **Step 3: 实现最小直链工作流。**

在 `app/workflows/direct.py` 提供 `DirectTaskWorkflow`，构造参数固定为 `cms`, `submission_store`, `task_store`, `move_config`, `emby`, `strm_stable_seconds` 和可注入的 `now`。`run_stage` 只处理 `RECEIVED`、`ORGANIZING`、`RECOGNIZING`、`STRM_READY`、`MOVED`、`EMBY_CONFIRMED`：

1. `RECEIVED` 调用一次 `cms.add_share_down(task.url)`，用返回的 CMS 任务号写入 `submission_store` 和 `cms_task_id`，返回 `StageResult.complete("已提交 CMS 普通同步")`；重试时如果已有 `cms_task_id`，只复用记录，不再次提交。
2. `ORGANIZING` 用 `cms.get_share_down_detail(cms_task_id)`；非终态返回 `StageResult.defer("等待 CMS 整理完成", 15)`，失败返回 `StageResult.failed`，成功更新 submission 状态并进入下一阶段。
3. `RECOGNIZING` 只读取 CMS/submission 已保存的 `recognition_json`、`category_final` 和 TMDB ID；分类为空时返回 `StageResult.needs_action("CMS 尚未给出媒体分类")`，不调用 OpenAI，不创建人工分类猜测；有分类时将 `strm_mode=direct`、`direct_strm=True` 写入元数据。
4. `STRM_READY` 使用 `find_strm_source_dir`/`find_recent_direct_library_strm_source_dir` 及新增的 `validate_direct_strm_source` 直链标记检查源目录；目录不存在或不稳定时 `defer`，发现共享 STRM 或 TMDB ID 不匹配时 `failed`；成功时写入 `source_path`、`direct_strm=True` 并将模式标记为已锁定。
5. `MOVED` 复用 `plan_strm_move` 和 `execute_strm_move` 的路径白名单、冲突策略和稳定性检查，成功写入 `dest_path`、`move_status`、`category_final`，不调用任何 cleanup helper。
6. `EMBY_CONFIRMED` 沿用现有 TMDB/路径匹配和刷新接口；匹配到媒体库时写入 `emby_parent`、`emby_item_id`、`emby_status=confirmed` 并完成该任务，暂未匹配时 `defer`。该阶段完成后由模式流转返回 `None`，因此任务终态为 `SUCCEEDED/EMBY_CONFIRMED`，不会伪造“清理完成”。

不在直链类中导入 `bridge.py`，避免循环依赖；需要的媒体路径函数从 `app.media.strm` 导入，submission 接口用现有对象的最小方法调用。在 `app/media/strm.py` 增加 `validate_direct_strm_source(source: Path) -> str`：目录不存在返回明确错误，目录没有 STRM 返回明确错误，每个 STRM 内容不包含 `/d/` 时返回“发现非直链 STRM”，全部通过时返回空字符串；其内部调用现有 `iter_strm_files` 和 `_strm_has_direct_link`，不放宽源路径白名单。

- [ ] **Step 4: 让 TaskRunner 和共享工作流识别模式。**

把 `app/models.py` 的 `next_stage_after_success(stage)` 改成保留兼容默认值的 `next_stage_after_success(stage, strm_mode="shared")`，内部调用 `next_stage_for_mode`。在 `TaskRunner._apply_result` 使用 `effective_task_strm_mode(task)` 决定下一阶段；模式未锁定时只允许从任务元数据读取，不能根据阶段自动切换。直链 `TaskRunner` 使用 `DirectTaskWorkflow`，共享使用现有 `BridgeSelfShareTaskWorkflow`。

在 `bridge.run_forever`：

- 任务引擎开启时，即使 `WORKFLOW_MODE=direct` 也创建 `DirectTaskWorkflow`，不要求 P115 client；`self_share_sync` 仍创建 P115 和共享工作流。
- 用一个只负责按 `effective_task_strm_mode(task)` 委派的 `ModeRoutingWorkflow` 或等价路由对象给同一个 TaskRunner，避免重复 worker。
- TaskStore 入队时把当前默认模式固定写入 `metadata["strm_mode"]`；HDHive 入队复用同一入口，不另行决定模式。
- 任务进入 `is_strm_mode_locked` 的阶段后拒绝模式覆盖；“从头重跑”先清理旧阶段运行元数据，再把新模式写入任务元数据。

共享工作流增加一道断言：只有 `shared` 模式才可执行 `OWN_SHARE_CREATED`、`SHARE_VALIDATED`、`SHARE_SYNC_SUBMITTED` 和 cleanup；若任务元数据为 `direct`，返回 `StageResult.failed("直链模式禁止进入共享阶段", error_type="strm_mode_mismatch")`，从而不会误删源文件。

- [ ] **Step 5: 运行工作流回归测试。**

运行：

```sh
python3 -m unittest tests.test_direct_workflow tests.test_task_runner tests.test_bridge_task_engine tests.test_self_share_workflow tests.test_invalid_share_cleanup -q
```

预期：直链测试验证无自有分享、无源清理、终态停在 Emby；共享测试继续验证自有分享、共享 STRM 和安全清理门槛。

- [ ] **Step 6: 提交工作流切片。**

```sh
git add app/workflows/direct.py app/models.py app/task_runner.py app/workflows/self_share.py bridge.py tests/test_direct_workflow.py tests/test_task_runner.py tests/test_bridge_task_engine.py
git commit -m "feat: route direct strm tasks without source cleanup"
```

### Task 3: 增加只读 API、模式设置 POST 和 `/app` 静态入口

**Files:**
- Create: `app/web_api.py`
- Modify: `app/web.py`, `app/config.py`
- Test: `tests/test_web_api.py`, `tests/test_web_admin.py`

- [ ] **Step 1: 写失败测试，固定认证和 JSON 契约。**

在 `tests/test_web_api.py` 建立带临时 TaskStore、HDHive store 和假的服务对象的 `WebApp`，验证：

```python
def test_api_requires_the_same_web_token_as_html_pages():
    app = make_app(web_token="secret")
    status, _, body = app.handle_request("GET", "/api/v1/overview", {}, b"")
    assert status == 403
    status, headers, body = app.handle_request("GET", "/api/v1/overview?token=secret", {}, b"")
    assert status == 303
    assert headers["Location"] == "/api/v1/overview"
    assert "cms_web_token=" in headers["Set-Cookie"]


def test_read_only_api_returns_tasks_without_secret_share_passwords():
    app = make_app(web_token="secret", task_url="https://115cdn.com/s/abc?password=abcd")
    headers = {"X-Web-Token": "secret"}
    status, _, body = app.handle_request("GET", "/api/v1/tasks?limit=10", headers, b"")
    payload = json.loads(body)
    assert status == 200
    assert payload["items"][0]["strm_mode"] == "shared"
    assert "password=abcd" not in body.decode()


def test_strm_mode_settings_and_task_override_obey_lock():
    app, task = make_app(web_token="secret")
    headers = {"X-Web-Token": "secret"}
    status, _, _ = app.handle_request("POST", "/settings/strm-mode", headers, b"mode=direct")
    assert status == 303
    status, _, _ = app.handle_request("POST", f"/task/{task.id}/strm-mode", headers, b"mode=direct")
    assert status == 303
    app.store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.RUNNING, "locked")
    status, _, body = app.handle_request("POST", f"/task/{task.id}/strm-mode", headers, b"mode=shared")
    assert status == 409
    assert "模式已锁定" in body.decode()
```

API 路由至少覆盖 `/api/v1/overview`、`/api/v1/tasks?limit=n`、`/api/v1/tasks/<id>`、`/api/v1/health`、`/api/v1/quality`、`/api/v1/hdhive/subscriptions` 和 `/api/v1/settings/strm-mode`。所有输出均为 JSON，任务详情中的 URL 使用 `redact_share_url` 删除 `password` 查询参数；API 不返回 Telegram token、CMS 密码、115 cookie、HDHive OAuth token 或完整解锁 URL。

- [ ] **Step 2: 运行 API 测试，确认当前只返回 SSR/404。**

运行：

```sh
python3 -m unittest tests.test_web_api -q
```

预期：失败在 `/api/v1/*` 未路由、直链模式 POST 未实现或静态 `/app` 返回 404。

- [ ] **Step 3: 实现 API 序列化和静态文件服务。**

在 `app/web_api.py` 提供 `api_overview(store: TaskStore) -> dict[str, object]`、`api_tasks(store: TaskStore, limit: int) -> dict[str, object]`、`api_task_detail(store: TaskStore, task_id: int, submission_store: object | None) -> dict[str, object] | None`、`api_health(store: TaskStore, task_engine_enabled: bool) -> dict[str, object]`、`api_quality(store: TaskStore, automation: object | None) -> dict[str, object]`、`api_hdhive(service: object | None, scheduler: object | None) -> dict[str, object]` 和 `redact_share_url(url: str) -> str`。这些函数直接返回前文规定的 JSON 字典；不存在的任务返回 `None`，调用方转换为 404。

函数只读取已有对象；`api_tasks` 的每个任务返回 `id,title,status,current_stage,category,tmdb_id,strm_mode,mode_locked,cleanup_policy,created_at,updated_at,wait_reason`，`api_task_detail` 额外返回时间线和非敏感路径。`api_hdhive` 返回订阅、每个 item 的 `estimated_points/spent_points/points_source/unlocked_at/task_id/status` 和最近运行摘要，旧字段缺失时使用 `null` 或 `"-"`，不猜造数据。

在 `WebApp.handle_request` 的认证之后加入：

- `GET /app` 返回 `FRONTEND_DIST_PATH/index.html`；`GET /app/<relative>` 只允许解析到该目录内的文件，缺失文件返回 404，禁止路径穿越。
- 上述 `/api/v1/*` 路由返回 `Content-Type: application/json; charset=utf-8`，解析 `limit` 时限制在 `1..100`，非法值返回 400。
- `POST /settings/strm-mode` 解析 `mode`，调用 `TaskStore.set_default_strm_mode`，成功 303 到 `/app/`，非法值返回 400。
- `POST /task/<id>/strm-mode` 只在任务存在且 `is_strm_mode_locked(task.current_stage)` 为假时写入元数据；锁定时 409；成功 303 到 `/task/<id>`。
- 现有 POST `/task/<id>/reprocess` 读取可选的 `mode`，只有 `shared/direct` 才把模式写入重跑元数据；无参数时保留原模式。

`GET /app` 使用与 `/` 相同的 Cookie、查询参数和 `X-Web-Token` 校验；认证查询参数仍然只用于设置 Cookie 后 303，避免 token 长期出现在 Vue 请求 URL。

- [ ] **Step 4: 运行 Web 回归测试。**

运行：

```sh
python3 -m unittest tests.test_web_api tests.test_web_admin tests.test_hdhive_web -q
```

预期：旧 SSR 页面和 POST 路由保持原断言，新 API、Cookie、脱敏和模式锁定测试通过。

- [ ] **Step 5: 提交后端 Web 边界切片。**

```sh
git add app/web_api.py app/web.py app/config.py tests/test_web_api.py tests/test_web_admin.py tests/test_hdhive_web.py
git commit -m "feat: expose authenticated read-only web api"
```

### Task 4: 记录 HDHive 实际/估算积分、时间和 TaskStore 任务号

**Files:**
- Modify: `app/clients/hdhive.py`, `app/hdhive_subscription_store.py`, `app/hdhive_subscriptions.py`, `bridge.py`
- Test: `tests/test_hdhive_client.py`, `tests/test_hdhive_subscription_store.py`, `tests/test_hdhive_subscriptions.py`, `tests/test_hdhive_bridge.py`

- [ ] **Step 1: 写失败测试，覆盖 API 实际积分、免费和估算三种来源。**

在 fake unlock response 中分别使用以下字段组合：

```python
{"slug": "api-cost", "success": True, "full_url": "https://115cdn.com/s/a?password=1111", "spent_points": 7}
{"slug": "free", "success": True, "full_url": "https://115cdn.com/s/b?password=2222", "already_owned": True}
{"slug": "estimated", "success": True, "full_url": "https://115cdn.com/s/c?password=3333"}
```

断言：第一条 `points_source == "api"`、`spent_points == 7`；第二条 `points_source == "free"`、`spent_points == 0`；第三条 `points_source == "estimated"`、`spent_points == estimated_points`。三条都写入非空 `unlocked_at`，且 `mark_item_enqueued` 后保留 `task_id`。

在 `tests/test_hdhive_subscription_store.py` 中先用旧 schema 手工建表，只含已有列，再初始化 `HdhiveSubscriptionStore`；断言 `_ensure_columns` 可重复增加新列且第二次初始化不报错。

- [ ] **Step 2: 运行测试，确认新字段尚未存在。**

运行：

```sh
python3 -m unittest tests.test_hdhive_client tests.test_hdhive_subscription_store tests.test_hdhive_subscriptions -q
```

预期：失败在 `HdhiveUnlockItem` 缺少实际积分、store dataclass 缺少新字段或旧 schema 没有迁移。

- [ ] **Step 3: 实现字段迁移和解锁结果解析。**

给 `HdhiveUnlockItem` 增加 `spent_points: int | None = None` 和 `unlocked_at: str = ""`，`_unlock_item` 从 `spent_points/cost/deducted_points/points` 依次读取实际消耗，从 `unlocked_at/unlock_time` 读取接口时间。保留原构造位置参数兼容，新增字段放在默认参数之后。

在 `hdhive_subscription_items` 追加并迁移：

```sql
estimated_points INTEGER;
spent_points INTEGER;
points_source TEXT NOT NULL DEFAULT '';
unlocked_at REAL;
```

`HdhiveSubscriptionItem` 同名字段增加默认兼容读取；`upsert_item` 把资源 `unlock_points` 同时保存为 `estimated_points`，旧记录没有新值时保持 `NULL`。新增 `mark_item_unlocked(item_id, spent_points, points_source, unlocked_at)`，`mark_item_enqueued(item_id, task_id)` 只更新状态/任务号，不覆盖已记录的积分和时间。

在 `HdhiveSubscriptionService.check()` 解锁成功后使用以下确定规则：

```python
if result.already_owned:
    spent_points, points_source = 0, "free"
elif result.spent_points is not None:
    spent_points, points_source = result.spent_points, "api"
elif selected.unlock_points is not None:
    spent_points, points_source = selected.unlock_points, "estimated"
else:
    spent_points, points_source = None, ""
```

若实际接口时间为空，使用成功处理时的 `time.time()`；只有成功拿到 115 分享链接后才写 `unlocked_at`。`SubscriptionCheckResult` 增加不可变 `deliveries` 元组；每个 delivery 至少包含 `subscription_id,item_id,episode_key,resource_slug,title,share_size,pan_type,resolution,source,subtitle,estimated_points,spent_points,points_source,unlocked_at,task_ids`。`enqueue_links` 的返回值统一解析成 TaskStore ID 列表；当前 callback 没有返回值时，从每条 115 分享链接反查 `TaskStore.find_task_by_share_key`，反查不到则保存空列表而不伪造任务号。

检查摘要增加 `points_total` 和 `points_by_source`，自动巡检和手动检查使用相同计数，CMS 入队失败时 item 状态为 `failed`、保留解锁时间和积分、delivery 标记 `enqueue_error`，绝不把失败说成已入库。

- [ ] **Step 4: 运行 HDHive 数据回归测试。**

运行：

```sh
python3 -m unittest tests.test_hdhive_client tests.test_hdhive_subscription_store tests.test_hdhive_subscriptions tests.test_hdhive_bridge -q
```

预期：新字段、旧数据库迁移、Task ID 和积分来源全部通过，原有自动解锁阈值和幂等测试不变。

- [ ] **Step 5: 提交 HDHive 数据切片。**

```sh
git add app/clients/hdhive.py app/hdhive_subscription_store.py app/hdhive_subscriptions.py bridge.py tests/test_hdhive_client.py tests/test_hdhive_subscription_store.py tests/test_hdhive_subscriptions.py tests/test_hdhive_bridge.py
git commit -m "feat: record hdhive unlock cost time and task id"
```

### Task 5: 增加 TMDB 详情缓存和 Telegram 海报卡片

**Files:**
- Create: `app/hdhive_cards.py`
- Modify: `app/media/classify.py`, `app/hdhive_subscriptions.py`, `bridge.py`, `app/telegram_ui.py`
- Test: `tests/test_hdhive_cards.py`, `tests/test_telegram_client.py`, `tests/test_hdhive_bridge.py`

- [ ] **Step 1: 写失败测试，固定卡片字段、降级和状态真实性。**

在 `tests/test_hdhive_cards.py` 使用 fake TMDB resolver 和 fake Telegram，覆盖：

```python
def test_success_delivery_prefers_poster_and_labels_estimated_points():
    card = build_hdhive_card(delivery_with(points_source="estimated", spent_points=8), tmdb_ok=True)
    assert card.photo_url.endswith("/poster.jpg")
    assert "消耗积分：8 积分（估算）" in card.caption
    assert "🕒 时间：" in card.caption
    assert "📁 分类：外国电视" in card.caption


def test_tmdb_failure_returns_text_card_without_blocking_delivery():
    card = build_hdhive_card(delivery_with(points_source="free", spent_points=0), tmdb_ok=False)
    assert card.photo_url == ""
    assert "TMDB：255358" in card.caption
    assert "未知" in card.caption


def test_failed_enqueue_is_not_rendered_as_success():
    text = render_hdhive_delivery_status(delivery_with(enqueue_error="CMS unavailable"))
    assert "HDHive 解锁成功" in text
    assert "CMS 入队失败" in text
    assert "执行整理入库中" not in text
```

在 `tests/test_telegram_client.py` 增加 fake HTTP 断言：`send_photo` POST 到 `/sendPhoto`，传 `chat_id/photo/caption/parse_mode=HTML` 和可选键盘；`send_message(chat_id, text, parse_mode="HTML")` 只在传入时带 parse mode。

- [ ] **Step 2: 运行测试，确认当前只有纯文本 sendMessage。**

运行：

```sh
python3 -m unittest tests.test_hdhive_cards tests.test_telegram_client -q
```

预期：失败在卡片 builder 和 `TelegramClient.send_photo` 不存在。

- [ ] **Step 3: 实现 TMDB 缓存和卡片 builder。**

在 `app/hdhive_cards.py` 定义：

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class HdhiveCard:
    caption: str
    photo_url: str = ""
    reply_markup: dict | None = None
```

同时实现 `TmdbDetailsCache(store, resolver, ttl_seconds=86400)` 及其 `lookup(media_type, tmdb_id, share_name)` 方法；实现 `build_hdhive_card(delivery, tmdb=None) -> HdhiveCard`、`render_hdhive_delivery_status(delivery) -> str` 和 `send_hdhive_delivery(telegram, chat_id, delivery, tmdb_cache, reply_markup=None) -> None`。它们的输入、输出和失败行为按下面的约束固定，不使用隐式全局状态。

实现约束：

- 缓存键严格为 `tmdb:<media_type>:<tmdb_id>`，值为 JSON，TTL 24 小时；缓存命中不发 TMDB 请求，失败结果也只短暂缓存 10 分钟，避免每日巡检重复打空请求。
- 扩展 `TmdbApiResolver._normalize_details` 保留 `poster_path, backdrop_path, overview, release_date/first_air_date, production_countries/origin_country, genres, credits.cast` 和 `keywords`；海报地址只由 `https://image.tmdb.org/t/p/w500` 加 `poster_path` 组成，不接受接口返回的任意协议/域名。
- caption 使用 Telegram HTML 转义，最长 1024 字符；超长简介、主演、标签分别截断到 180、120、100 字符。显示“免费”“n 积分”或“n 积分（估算）”，真实时间按 `Asia/Shanghai` 格式化。
- 资源信息按“资源名、季集/分辨率/网盘、积分、分享者、大小、字幕、TMDB、地区、分类、主演、标签、115 链接数量、时间、简介”顺序输出。
- `enqueue_error` 时仍显示 HDHive 解锁成功、实际积分和 Task ID（若有），另加“CMS 入队失败：原因”，不显示“执行整理入库中”。待确认和解锁失败仍走现有纯文本/按钮分支，不调用成功卡片。

`TelegramClient` 增加 `send_photo(chat_id, photo, caption, reply_markup=None, parse_mode="HTML")`，并让 `send_message` 接受可选 `parse_mode`；所有 HTTP payload 不写入日志。`send_hdhive_delivery` 先尝试 `send_photo`，photo 为空或 Telegram 返回失败时调用 `send_message`，图片失败不能回滚 HDHive 解锁或 CMS 任务。

把 `HdhiveSubscriptionService` 的 delivery callback 接到 `bridge.py`：手动“立即检查”、确认解锁和每日 scheduler 都复用同一个回调，保证状态显示一致；不在卡片中输出完整 115 分享 URL、HDHive token 或 OAuth 信息。`app/telegram_ui.py` 只保留按钮文本和订阅摘要，成功卡片交给新 builder，避免两套积分格式。

- [ ] **Step 4: 运行卡片和 Telegram 回归测试。**

运行：

```sh
python3 -m unittest tests.test_hdhive_cards tests.test_telegram_client tests.test_hdhive_bridge tests.test_hdhive_subscriptions -q
```

预期：海报优先、文字降级、积分来源、时间、CMS 入队失败和 HTML 转义测试通过。

- [ ] **Step 5: 提交 Telegram 卡片切片。**

```sh
git add app/hdhive_cards.py app/media/classify.py app/hdhive_subscriptions.py app/telegram_ui.py bridge.py tests/test_hdhive_cards.py tests/test_telegram_client.py tests/test_hdhive_bridge.py
git commit -m "feat: send hdhive subscription poster cards"
```

### Task 6: 建立轻量 `vue-naive-admin` 风格 `/app` 前端

**Files:**
- Create: `frontend/package.json`, `frontend/package-lock.json`, `frontend/index.html`, `frontend/vite.config.js`, `frontend/src/main.js`, `frontend/src/api.js`, `frontend/src/router.js`, `frontend/src/styles.css`, `frontend/src/layouts/AppLayout.vue`, `frontend/src/views/Overview.vue`, `frontend/src/views/Tasks.vue`, `frontend/src/views/TaskDetail.vue`, `frontend/src/views/Quality.vue`, `frontend/src/views/Health.vue`, `frontend/src/views/Hdhive.vue`, `frontend/THIRD_PARTY_NOTICES.md`
- Modify: `tests/test_frontend.py`

- [ ] **Step 1: 先写前端结构测试和构建命令。**

在 `tests/test_frontend.py` 通过 `pathlib` 检查以下文件存在，且源码包含关键接口：

```python
def test_frontend_declares_pinned_dependencies_and_app_base():
    package = json.loads((ROOT / "frontend/package.json").read_text())
    assert package["scripts"]["build"] == "vite build"
    assert package["dependencies"]["vue"].startswith("3.")
    assert package["dependencies"]["naive-ui"].startswith("2.")
    assert "vue-naive-admin" in (ROOT / "frontend/THIRD_PARTY_NOTICES.md").read_text()
    assert 'base: "/app/"' in (ROOT / "frontend/vite.config.js").read_text()


def test_frontend_has_mode_selector_and_subscription_unlock_fields():
    all_source = "".join(path.read_text() for path in (ROOT / "frontend/src").rglob("*") if path.is_file())
    assert "/api/v1/settings/strm-mode" in all_source
    assert "共享 STRM" in all_source and "直链 STRM" in all_source
    assert "spent_points" in all_source and "unlocked_at" in all_source
```

- [ ] **Step 2: 运行前端测试，确认当前没有前端目录。**

运行：

```sh
python3 -m unittest tests.test_frontend -q
```

预期：失败在 `frontend/package.json` 或 `frontend/src` 不存在。

- [ ] **Step 3: 生成锁定依赖和最小 Vue 壳。**

`frontend/package.json` 使用以下固定主版本和脚本，不复制模板中无关示例页面：

```json
{
  "private": true,
  "scripts": {"build": "vite build"},
  "dependencies": {
    "naive-ui": "2.41.0",
    "vue": "3.5.13",
    "vue-router": "4.5.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "5.2.1",
    "vite": "6.1.0"
  }
}
```

执行 `cd frontend && npm install --package-lock-only` 生成并提交 `package-lock.json`，再执行 `npm install` 只用于本地构建，不把 `node_modules` 纳入仓库。`vite.config.js` 设置 `base: "/app/"`，Vue plugin 和输出目录 `dist`。

前端只实现以下页面和组件：

- `AppLayout.vue`：Naive UI `NLayout`/`NMenu`/`NLayoutHeader`，侧栏导航“运行概览、任务、质量巡检、本地健康、HDHive 订阅”，响应式断点 768px，无渐变和大阴影。
- `api.js`：统一 `fetchJson(path, options)`；请求失败抛出包含 HTTP 状态的错误；POST 只调用已存在的模式设置/任务操作路由，不把 token 写进 localStorage。
- `Overview.vue`：并列展示“需要关注”和“当前队列”，读取 `/api/v1/overview`，显示阶段、等待原因、耗时、115 调用统计。
- `Tasks.vue`/`TaskDetail.vue`：读取任务列表/详情，显示 `共享 STRM`/`直链 STRM`、清理策略、八阶段时间线；任务未锁定时允许模式覆盖，锁定后显示“模式已锁定”，从头重跑时可选择新模式。
- `Quality.vue`/`Health.vue`：只读摘要和现有诊断入口，不复制后台修复逻辑。
- `Hdhive.vue`：显示订阅、状态、待确认资源和解锁记录表，列为剧集、资源、积分、积分来源、解锁时间、Task ID、状态；积分来源为 `estimated` 时明确显示“估算”。

海报卡片只在后端 Telegram 发送；Web 订阅页显示 `poster_url`（存在时）和文本字段，图片加载失败显示文本，不阻塞表格。每个页面只在 mounted 和按钮操作后刷新，按用户已确定的不高并发原则不使用高频轮询。

`THIRD_PARTY_NOTICES.md` 写明结构参考为 `zclzone/vue-naive-admin`、来源仓库 `https://github.com/zclzone/vue-naive-admin`、MIT License、Naive UI/Vue/Vite 版本和本项目没有复制示例账号/密钥；把完整 MIT 文本放入该文件，不使用模板演示数据。

- [ ] **Step 4: 构建并执行前端结构测试。**

运行：

```sh
cd frontend
npm ci
npm run build
cd ..
python3 -m unittest tests.test_frontend -q
```

预期：Vite 生成 `frontend/dist/index.html` 和带哈希的静态资源，测试通过，源码没有外部 CDN 地址和硬编码 token。

- [ ] **Step 5: 提交前端切片。**

```sh
git add frontend tests/test_frontend.py
git commit -m "feat: add lightweight vue admin app shell"
```

### Task 7: 多阶段 Docker 构建、中文文档和回滚保障

**Files:**
- Modify: `Dockerfile`, `.env.example`, `README.md`, `PRODUCT.md`, `docs/dockerhub-overview.md`, `tests/test_dockerfile.py`, `tests/test_hdhive_subscription_docs.py`
- Reference: `frontend/package-lock.json`, `frontend/THIRD_PARTY_NOTICES.md`

- [ ] **Step 1: 写失败测试，固定 Docker 构建和回退能力。**

在 `tests/test_dockerfile.py` 增加断言：Dockerfile 包含 `node:22-alpine` 构建阶段、`npm ci`、`npm run build`、`COPY --from=frontend-build`、Python 运行阶段和 `CMD ["python", "/app/bridge.py"]`；不包含运行时 Node server 命令。文档测试断言中文说明包含 `/app/`、`STRM_DEFAULT_MODE=shared`、`direct` 不清理源文件、HDHive 积分来源和 `npm ci`。

- [ ] **Step 2: 实现多阶段 Dockerfile。**

将 `Dockerfile` 改成以下结构，保持 Python 运行镜像和端口行为不变：

```dockerfile
FROM node:22-alpine AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-alpine
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY bridge.py doctor.py /app/
COPY app/ /app/app/
COPY scripts/ /app/scripts/
COPY --from=frontend-build /src/frontend/dist /app/frontend/dist
VOLUME ["/data"]
CMD ["python", "/app/bridge.py"]
```

Python `WebApp` 读取 `FRONTEND_DIST_PATH`；即使 `frontend-build` 失败，开发者仍可从源码运行 Python SSR，生产构建失败时不允许用空 `dist` 覆盖已有镜像。Docker 测试使用 `docker build --target frontend-build .` 和 `docker build .`；没有 Docker daemon 时至少执行 `npm ci`, `npm run build`, `python3 -m unittest tests.test_dockerfile -q` 并报告环境限制。

- [ ] **Step 3: 重写中文使用说明。**

在 `README.md`、`PRODUCT.md`、`docs/dockerhub-overview.md` 说明：

1. `/` 是原 Python SSR 回退，`/app/` 是新的 Vue 管理页；两者共用 `WEB_TOKEN`。
2. `STRM_DEFAULT_MODE=shared` 为安全默认；共享模式在分享验证、Emby 确认后才清理 115 源；`direct` 仅使用 CMS 普通同步并保留源文件。
3. Web 的全局模式和任务级模式覆盖只能在 STRM 阶段前修改，锁定后用从头重跑切换。
4. HDHive 订阅支持待确认、实际 API 积分、免费和估算积分；解锁记录保存解锁时间和 Task ID，TMDB/海报失败时退化为文字。
5. 提供 `docker compose up -d --build`、健康检查、升级前备份 `/data` 和回滚到 `/` 的准确步骤，不在文档中写入任何真实 token/cookie。

- [ ] **Step 4: 运行文档、Docker 和完整回归。**

运行：

```sh
python3 -m unittest tests.test_dockerfile tests.test_hdhive_subscription_docs -q
python3 -m unittest tests.test_hdhive_subscriptions tests.test_hdhive_web tests.test_web_admin -q
python3 -m unittest discover -s tests -q
git diff --check
```

预期：完整测试通过，`git diff --check` 无输出；Docker 构建在存在 daemon 时产出包含 `/app/frontend/dist` 的单容器镜像。

- [ ] **Step 5: 提交部署和文档切片。**

```sh
git add Dockerfile .env.example README.md PRODUCT.md docs/dockerhub-overview.md tests/test_dockerfile.py tests/test_hdhive_subscription_docs.py
git commit -m "docs: document vue app and strm mode deployment"
```

### Task 8: 端到端验收和发布前检查

**Files:**
- Modify: `tests/test_release_workflows.py`, `tests/test_task_diagnostics.py` only when a new assertion is required by the implemented API/metadata.
- Reference: all files from Tasks 1-7.

- [ ] **Step 1: 加入无外部服务的端到端测试。**

用 fake CMS、fake P115、fake Emby、fake TMDB 和 fake Telegram 在一个临时 `/data` 目录运行两条任务：

```python
def test_shared_and_direct_tasks_have_distinct_side_effects_and_observable_end_states():
    shared = run_fake_task(mode="shared")
    direct = run_fake_task(mode="direct")
    assert shared.current_stage == TaskStage.CLEANED
    assert direct.current_stage == TaskStage.EMBY_CONFIRMED
    assert shared.metadata["cleanup_status"] == "deleted"
    assert direct.metadata.get("cleanup_status", "") == ""
    assert direct.metadata["direct_strm"] is True
```

再通过 `WebApp.handle_request` 读取两个任务的 `/api/v1/tasks/<id>`，确认前端所需的模式、阶段、等待原因和清理策略都存在；通过 HDHive fake response 确认卡片的实际积分、时间、海报降级和 Task ID 与数据库一致。

- [ ] **Step 2: 做静态安全检查。**

运行：

```sh
rg -n "(TG_BOT_TOKEN|OPENAI_API_KEY|TMDB_API_KEY|HDHIVE.*TOKEN|cookie|sk-[A-Za-z0-9])" frontend app README.md PRODUCT.md docs Dockerfile
python3 -m unittest tests.test_secret_hygiene tests.test_frontend tests.test_web_api -q
```

预期：源码、前端和文档没有真实凭据；API 响应和日志测试不包含 115 访问码或 HDHive OAuth 信息。

- [ ] **Step 3: 执行最终验证并检查每个回滚点。**

运行：

```sh
python3 -m unittest discover -s tests -q
git diff --check
git status --short
```

预期：完整测试通过；未提交的只剩用户明确保留的变更；`/` SSR 和旧 POST 路由仍可用，`/app` 失效时不影响 Python 服务启动。

- [ ] **Step 4: 记录部署验收结果。**

在发布记录中填写一次免费 HDHive 剧集资源的实际结果：订阅创建、资源卡片、积分来源、解锁时间、CMS Task ID、共享/直链模式、Emby 媒体库名称和源文件是否保留。不要把真实分享密码、Telegram token、115 cookie 或 HDHive OAuth token 写进仓库、提交信息或公开文档。

## 计划自检

- 设计覆盖：STRM 全局默认/任务覆盖/阶段锁定、共享清理保护、直链普通同步、`/app` Vue 壳、只读 API、WEB_TOKEN、TMDB 缓存、海报降级、实际/估算/免费积分、解锁时间、Task ID、旧数据库迁移、Docker 回退和中文文档均有对应任务。
- 占位检查：每个代码步骤给出文件、接口、测试命令和预期结果；没有把“稍后处理”作为实现步骤。
- 类型一致性：模式名统一为 `shared`/`direct`；积分字段统一为 `estimated_points`、`spent_points`、`points_source`、`unlocked_at`；任务关联统一为 `task_id`；前端 API 路径统一使用 `/api/v1/`。
- 回归边界：只读 API 和 Vue 不复制业务逻辑；直链清理永远不调用 cleanup；共享工作流原有清理门槛和 `/` SSR 页面保留。
