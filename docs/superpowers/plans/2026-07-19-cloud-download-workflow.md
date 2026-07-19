# 磁力与 ED2K 云下载工作流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Telegram Bot 接收合法的 `magnet:` 和 `ed2k://` 链接，使用 115 云下载落入待整理 CID，并复用现有 CMS 整理、自有分享 STRM、媒体库移动、Emby 确认和源文件清理流程。

**Architecture:** 在输入层增加独立 `MediaSource` 解析器；在 `P115WebClient` 增加一次提交、低频查询和输出定位接口；TaskStore 以 additive migration 保存来源类型和稳定去重键。TaskRunner 增加单一 `CLOUD_DOWNLOADING` 阶段，云下载完成后转入现有 `ORGANIZING`，后续流程保持现状，绝不回退直链 STRM 或旧轮询路径。

**Tech Stack:** Python 3.12 标准库、`sqlite3`、现有 `FormHttp`/`TaskRunner`/`SelfShareWorkflow`、`unittest`、Docker Compose。

---

## 实施前约束

- 不执行真实 115 云下载，直到本地 fake 全链路测试通过并明确进入真实验收阶段。
- 云下载提交是有副作用的 POST，不自动重试；查询 GET 遵循现有一次保守重试规则。
- 继续单 worker，不增加 115 并发和全盘扫描。
- 最终媒体库只接受当前任务自有分享 STRM，继续拒绝 `/d/` 直链。
- 所有任务产生的自有分享和源清理必须经过现有 Emby 确认保护。

## 文件映射

- Create: `app/media/sources.py`：统一识别 115 分享、磁力和 ED2K 输入。
- Modify: `bridge.py`：接入统一输入解析、创建 cloud TaskStore 任务、兼容回复和旧路径保护。
- Modify: `app/clients/p115.py`：115 云下载提交、状态和输出定位。
- Modify: `app/models.py`：来源字段和 `CLOUD_DOWNLOADING` 阶段。
- Modify: `app/task_store.py`：来源字段迁移、cloud upsert/查询和元数据持久化。
- Modify: `app/task_engine.py`：阶段名称、重试决策和 cloud 阶段列入可操作阶段。
- Modify: `app/workflows/self_share.py`：cloud 阶段和整理阶段分支。
- Modify: `app/config.py`、`.env.example`、`doctor.py`：轮询间隔、超时和配置诊断。
- Modify: `app/task_diagnostics.py`、`app/task_health.py`、`app/web.py`：显示来源和云下载等待原因。
- Test: `tests/test_media_sources.py`、`tests/test_p115_cloud_download.py`、`tests/test_task_store.py`、`tests/test_task_engine.py`、`tests/test_task_runner.py`、`tests/test_bridge_task_engine.py`、`tests/test_web_admin.py`。
- Modify: `README.md`、`CHANGELOG.md`：记录使用方式和安全边界。

### Task 1: 输入解析与稳定去重键

**Files:**
- Create: `app/media/sources.py`
- Create: `tests/test_media_sources.py`
- Modify: `bridge.py:200-230,2434-2590`

- [ ] **Step 1: 写失败测试**

在 `tests/test_media_sources.py` 增加以下行为测试：

```python
from app.media.sources import parse_media_sources

ED2K = "ed2k://|file|Example.mkv|10|ABCDEF0123456789ABCDEF0123456789|/"

def test_parse_media_sources_accepts_share_magnet_and_ed2k():
    sources = parse_media_sources(
        "https://115cdn.com/s/abc?password=1234\n"
        "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567\n"
        + ED2K
    )

    assert [source.source_type for source in sources] == ["share", "magnet", "ed2k"]
    assert sources[1].source_key == "btih:0123456789abcdef0123456789abcdef01234567"
    assert sources[2].source_key == "ed2k:abcdef0123456789abcdef0123456789:10"

def test_parse_media_sources_rejects_malformed_cloud_links():
    assert parse_media_sources("magnet:?dn=no-btih ed2k://|file|bad|x|bad|/") == []

def test_parse_media_sources_deduplicates_same_cloud_source():
    link = "MAGNET:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
    sources = parse_media_sources(link + "\n" + link.lower())
    assert len(sources) == 1
```

- [ ] **Step 2: 运行测试确认失败**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_media_sources -v`

预期：FAIL，因为 `app.media.sources` 和 `parse_media_sources` 尚不存在。

- [ ] **Step 3: 实现最小解析器**

定义不可变 `MediaSource`：`source_type`、`source_key`、`raw_url`、`display_name`。只接受 `magnet` 和完整 ED2K 格式；磁力以 BTIH 规范化去重，ED2K 以 hash+size 去重。保留现有 115 分享解析逻辑的输出兼容转换。

在 `bridge.py` 保留 `extract_share_links` 兼容导出，将新输入入口改为 `parse_media_sources`。TaskEngine 开启时，share 继续执行现有分支，cloud source 走后续 `CLOUD_DOWNLOADING` 入队；TaskEngine 关闭时对 cloud source 回复明确配置错误，不进入旧流程。

- [ ] **Step 4: 运行测试确认通过**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_media_sources -v`

预期：所有解析、拒绝和去重测试 PASS；随后运行现有 `tests.test_bridge_v02_integration`，确认 115 分享输入没有回归。

- [ ] **Step 5: 提交**

```bash
git add app/media/sources.py tests/test_media_sources.py bridge.py
git commit -m "feat: parse magnet and ed2k sources"
```

### Task 2: TaskStore 来源字段与云阶段模型

**Files:**
- Modify: `app/models.py`
- Modify: `app/task_store.py`
- Modify: `app/task_engine.py`
- Test: `tests/test_task_store.py`、`tests/test_task_engine.py`

- [ ] **Step 1: 写失败测试**

覆盖旧库迁移、cloud 去重、阶段展示和重试：

```python
def test_legacy_task_store_migrates_source_columns_and_backfills_share_key():
    store = TaskStore(legacy_db_path)
    task = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
    assert task.source_type == "share"
    assert task.source_key == "share:abc:1234"

def test_cloud_task_is_idempotent_by_source_key():
    first = store.upsert_cloud_task("ed2k:hash:10", ED2K, chat_id="464100862")
    second = store.upsert_cloud_task("ed2k:hash:10", ED2K, chat_id="464100862")
    assert first.id == second.id
    assert second.current_stage == TaskStage.CLOUD_DOWNLOADING

def test_cloud_downloading_is_displayed_and_retryable():
    assert stage_display_name(TaskStage.CLOUD_DOWNLOADING) == "115 云下载"
    assert decide_retry(cloud_failed_task).stage == TaskStage.CLOUD_DOWNLOADING
```

- [ ] **Step 2: 运行测试确认失败**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_task_store tests.test_task_engine -v`

预期：FAIL，因为缺少来源列、cloud upsert 和新阶段。

- [ ] **Step 3: 增量实现 schema 和模型**

在 `TaskStage` 增加 `CLOUD_DOWNLOADING`，在成功流中将其下一阶段设为 `ORGANIZING`。TaskStore 初始化时添加 `source_type TEXT NOT NULL DEFAULT 'share'`、`source_key TEXT NOT NULL DEFAULT ''`，对旧行回填 `share:<share_code>:<receive_code>`，创建 `(source_type, source_key)` 唯一索引。

新增 `TaskSnapshot.source_type/source_key`，保留 `share_code/receive_code` 兼容字段。新增 `upsert_cloud_task(source_key, url, chat_id)`、`find_task_by_source(source_type, source_key)`，并让 `upsert_task` 的旧 share key 明确写入 `source_type=share`。

- [ ] **Step 4: 运行测试确认通过**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_task_store tests.test_task_engine -v`

预期：迁移、去重、阶段展示和重试测试 PASS；全量 TaskStore/TaskRunner 测试无回归。

- [ ] **Step 5: 提交**

```bash
git add app/models.py app/task_store.py app/task_engine.py tests/test_task_store.py tests/test_task_engine.py
git commit -m "feat: add cloud download task stage"
```

### Task 3: 115 云下载客户端

**Files:**
- Modify: `app/clients/p115.py`
- Create: `tests/test_p115_cloud_download.py`

- [ ] **Step 1: 写失败测试**

使用已有 fake HTTP，验证提交参数、状态归一化、输出父 CID 校验和 POST 不重复：

```python
def test_cloud_download_add_sends_target_cid_and_empty_savepath():
    client = P115WebClient("cookie", http=FakeHttp({"state": True, "data": {"info_hash": "HASH"}}))
    result = client.cloud_download_add(ED2K, "3298928530653445613")
    assert client.http.calls[0].data == {
        "url": ED2K,
        "wp_path_id": "3298928530653445613",
        "savepath": "",
    }
    assert result["info_hash"] == "HASH"

def test_cloud_download_status_maps_completed_and_failed():
    assert normalize_cloud_status({"status": 11}) == "completed"
    assert normalize_cloud_status({"status": 12}) == "running"
    assert normalize_cloud_status({"status": 9}) == "failed"

def test_cloud_download_output_rejects_wrong_parent_cid():
    try:
        validate_cloud_output({"parent_id": "999"}, target_cid="3298928530653445613")
    except RuntimeError:
        pass
    else:
        raise AssertionError("wrong parent CID must be rejected")
```

- [ ] **Step 2: 运行测试确认失败**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_p115_cloud_download -v`

预期：FAIL，因为云下载方法和状态辅助函数不存在。

- [ ] **Step 3: 实现客户端方法**

在 `P115WebClient` 内复用 `_request`，提交 `https://clouddownload.115.com/lixianssp/?ac=add_task_url` 的 `url/wp_path_id/savepath`；状态查询优先使用 `?ac=get_user_task` 的 `info_hash`，必要时用 `task_lists` 做精确匹配。将 115 状态 9/11/12 等映射为统一状态，并把 `info_hash/task_id/name/file_id/cid/parent_id` 归一化。

仅在返回结果明确表示提交成功且有可追踪身份时返回成功；失败时抛出可分类异常。所有请求继续走 `_rate_limit`、`request_count` 和风控异常转换。

- [ ] **Step 4: 运行测试确认通过**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_p115_cloud_download tests.test_http_clients -v`

预期：所有云下载客户端测试 PASS，POST 仍只有一次调用，GET 重试行为保持不变。

- [ ] **Step 5: 提交**

```bash
git add app/clients/p115.py tests/test_p115_cloud_download.py
git commit -m "feat: add 115 cloud download client"
```

### Task 4: Config、TaskRunner 与 self-share workflow

**Files:**
- Modify: `app/config.py`、`.env.example`
- Modify: `app/task_runner.py`
- Modify: `app/workflows/self_share.py`
- Modify: `bridge.py`
- Test: `tests/test_task_runner.py`、`tests/test_bridge_task_engine.py`、`tests/test_self_share_workflow.py`

- [ ] **Step 1: 写失败测试**

覆盖首次提交只调用一次、轮询 defer、完成转 organizing、cloud 跳过再次 receive：

```python
def test_cloud_stage_submits_once_then_defers():
    result = workflow.run_stage(cloud_task_without_identity)
    assert result.outcome == StageOutcome.DEFER
    assert p115.add_calls == [(ED2K, RECEIVE_CID)]

def test_cloud_stage_completed_enqueues_organizing_metadata():
    result = workflow.run_stage(cloud_task_with_completed_identity)
    assert result.outcome == StageOutcome.COMPLETE
    assert result.metadata["received_file_ids"] == ["folder-id"]

def test_cloud_organizing_does_not_receive_share_again():
    runner.run_once()
    assert p115.receive_share_calls == []
    assert cms.organize_calls == 1
```

- [ ] **Step 2: 运行测试确认失败**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_task_runner tests.test_bridge_task_engine tests.test_self_share_workflow -v`

预期：FAIL，因为没有 cloud 阶段分支、配置字段和来源分支。

- [ ] **Step 3: 实现配置和工作流**

在配置中增加 `cloud_download_poll_interval_seconds`，默认 `30` 且最小值为 `30`；增加 `cloud_download_timeout_seconds`，默认 `86400`。`handle_update` 创建 cloud TaskStore/SubmissionStore 兼容记录并入队 `CLOUD_DOWNLOADING`。

在 `SelfShareWorkflow.run_stage` 增加 cloud 阶段：首次提交写入任务身份并 defer，queued/running 低频 defer，completed 验证目标 CID并返回 received metadata，failed/timeout 返回 `NEEDS_ACTION`。整理阶段仅对 share source 调用 `receive_share_to_cid`，cloud source 使用已保存的输出文件夹。

将 cloud 阶段加入 TaskRunner 的 115 全局锁、重试阶段和所有用户可操作阶段，但不创建第二个 worker。

- [ ] **Step 4: 运行测试确认通过**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_task_runner tests.test_bridge_task_engine tests.test_self_share_workflow -v`

预期：cloud 新测试和所有旧 self-share 测试 PASS；验证 cloud 失败不会调用 cleanup。

- [ ] **Step 5: 提交**

```bash
git add app/config.py .env.example app/task_runner.py app/workflows/self_share.py bridge.py tests/test_task_runner.py tests/test_bridge_task_engine.py tests/test_self_share_workflow.py
git commit -m "feat: run cloud downloads through self-share workflow"
```

### Task 5: Web/TG 状态、诊断和文档

**Files:**
- Modify: `app/task_diagnostics.py`、`app/task_health.py`、`app/web.py`
- Modify: `bridge.py`
- Modify: `doctor.py`、`README.md`、`CHANGELOG.md`
- Test: `tests/test_web_admin.py`、`tests/test_task_health.py`、`tests/test_doctor.py`、`tests/test_docs_task_engine.py`

- [ ] **Step 1: 写失败测试**

验证 Web/TG 标题显示云下载来源、等待原因、超时信息和配置提示：

```python
def test_task_detail_shows_cloud_download_source_and_wait_reason():
    html = render_task_detail(cloud_running_task)
    assert "115 云下载" in html
    assert "云下载" in html
    assert "下次检查" in html

def test_doctor_reports_cloud_download_interval_below_safe_floor():
    report = doctor.run_checks(env={**valid_env, "CLOUD_DOWNLOAD_POLL_INTERVAL_SECONDS": "5"}, filesystem=fs)
    assert "30" in report.to_text()
```

- [ ] **Step 2: 运行测试确认失败**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_web_admin tests.test_task_health tests.test_doctor tests.test_docs_task_engine -v`

预期：FAIL，因为阶段名称、来源摘要、配置检查和文档尚不存在。

- [ ] **Step 3: 实现状态与文档**

让 `stage_display_name`、Task diagnostics、TG `/status`、Web 任务详情和 `/health` 使用 TaskStore 的 `source_type` 与云下载 metadata。保留敏感信息过滤，只展示任务类型、状态、等待时长、下次检查和 115 调用计数。

在 doctor 校验轮询间隔不低于 30 秒、超时为正数且 TaskEngine 开启时提示 cloud source 要求；README 增加磁力/ED2K 使用方式、配置和不会自动清理失败源的说明；CHANGELOG 记录新功能。

- [ ] **Step 4: 运行测试确认通过**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_web_admin tests.test_task_health tests.test_doctor tests.test_docs_task_engine -v`

预期：Web/TG/doctor/docs 测试 PASS，现有页面布局和安全鉴权测试不回归。

- [ ] **Step 5: 提交**

```bash
git add app/task_diagnostics.py app/task_health.py app/web.py bridge.py doctor.py README.md CHANGELOG.md tests/test_web_admin.py tests/test_task_health.py tests/test_doctor.py tests/test_docs_task_engine.py
git commit -m "feat: expose cloud download task observability"
```

### Task 6: 完整回归与本地 fake 验收

**Files:**
- Test: `tests/test_bridge_task_engine.py`、`tests/test_task_runner.py`、`tests/test_task_store.py`、`tests/test_p115_cloud_download.py`
- Verify: `Dockerfile`、`.env.example`、`docker-compose.yml`

- [ ] **Step 1: 增加完整 fake cloud path 测试**

使用 fake P115/CMS/Emby 按顺序返回：cloud queued、cloud completed、CMS organized、own share valid、share STRM ready、moved、Emby confirmed、cleanup deleted；断言最终 TaskStore 为 `CLEANED/SUCCEEDED`、source folder 被删除、自有分享 metadata 保留，并断言中间没有 direct `/d/` STRM。

- [ ] **Step 2: 运行定向红绿测试**

运行：`/opt/homebrew/bin/python3 -m unittest tests.test_bridge_task_engine tests.test_task_runner tests.test_task_store tests.test_p115_cloud_download -v`

预期：所有 cloud path 和旧 share path 测试 PASS。

- [ ] **Step 3: 运行完整验证**

```bash
/opt/homebrew/bin/python3 -W error::ResourceWarning -m unittest discover -s tests -v
/opt/homebrew/bin/python3 -m compileall -q app bridge.py doctor.py
git diff --check
docker build --pull=false -t cms-tg-ingest:cloud-download-check .
```

预期：全量测试零失败、编译和 diff 检查通过、Docker 构建成功；不执行真实 115/CMS/Emby 任务。

- [ ] **Step 4: 提交**

```bash
git add tests/test_bridge_task_engine.py tests/test_task_runner.py tests/test_task_store.py tests/test_p115_cloud_download.py
git commit -m "test: cover cloud download end to end"
```

### Task 7: 真实 ED2K 验收、发布和部署

**Files:**
- Verify: production TaskStore、Docker Hub、Unraid container

- [ ] **Step 1: 发布前确认**

确认工作树干净、PR CI 通过、镜像本地构建通过；只在用户明确要求真实验收后使用提供的 ED2K 样本，避免在实现阶段误触发云下载。

- [ ] **Step 2: 发布版本**

更新 `app/__init__.py`、`CHANGELOG.md` 版本号，提交并推送 PR；CI 通过后合并 main，创建 `v0.3.0` 标签，等待 Docker Hub/GHCR 多架构构建成功。

- [ ] **Step 3: 更新 Unraid**

在 `/mnt/user/appdata/cms-tg-ingest` 执行 `docker compose pull cms-tg-ingest` 和 `docker compose up -d --no-build cms-tg-ingest`。不覆盖 `.env`、`data`、115 cookie 和媒体挂载。

- [ ] **Step 4: 真实验收 ED2K**

从用户 Telegram 账号发送已提供的 ED2K 链接；只读 TaskStore 追踪 `CLOUD_DOWNLOADING → ORGANIZING → ... → CLEANED`。核对云下载目标父 CID、CMS 分类、自有分享 STRM marker、Emby 媒体库、目标目录和 115 源清理；失败时停止，不人工删除文件。

- [ ] **Step 5: 生产健康检查**

验证容器 `running/healthy`，Web `/`、`/health`、`/quality` 为 `200`，健康页显示 TaskRunner active；检查没有 `Polling loop failed`、TaskRunner stage exception 或 cloud source 越界日志。
