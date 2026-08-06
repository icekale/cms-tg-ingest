# 失效 STRM 清理动作 — Dry-Run 设计（v0.2.75 已实现）

> 背景：质量巡检线上实例 24 个问题全部来自任务 #368 — dest 目录 `Q-...-[tmdb=94997]`
> 内残留 22 个指向旧分享 code 的 STRM（`unexpected_strm`）+ 2 个直链（`direct_strm`）。
> 根因：转存目录按剧集文件夹复用，每集任务重新分享整个文件夹；`merge_self_share_strm_folder`
> （`app/media/strm.py`）只覆盖"源中有同名文件"的项，**从不删除目标目录中无对应的旧 STRM**，
> 旧分享失效后这些文件成为死链。巡检正确识别了死链，但此前没有任何清理路径（只能 SSH 手动删）。

## 实现状态（v0.2.75 起，至 v0.2.79）

- `QUALITY_STRM_CLEANUP_ENABLED`（默认 false）开关；开启后质量页每行出现“失效 STRM”按钮。
- `POST /api/v1/quality/cleanup/dry-run`（body `{task_id}`）→ 候选清单；`POST /api/v1/quality/cleanup/run`（body `{task_id, paths}`）→ 逐文件复检后删除。
- 实现位置：`QualityAutomation.stale_strm_candidates` / `cleanup_stale_strm`（`app/quality_automation.py`），存活判定 `TaskStore.list_live_share_codes`（`app/task_store.py`）。
- 每文件一条 `task_operations`（type=`quality_strm_cleanup`）；删除后任务问题清空且 `manual_required` 时自动恢复评估。
- 直链（`/d/`）文件不参与；执行前按新鲜扫描复检（文件变化/分享复活 → 跳过）。
- v0.2.77：dry-run 支持 `check_shares`（对候选分享码逐个调用 115 `inspect_share`，缓存+上限 20 码），每条候选标注 `share_state`（valid/invalid/unknown）；执行默认 `allow_alive=false`——分享在 115 仍有效的文件会被保护跳过（防误删断链，线上 #368 教训），需显式 `allow_alive=true` 才删；前端对有效分享的文件默认不勾选并标注“分享仍有效”。
- v0.2.78：直链 STRM 自动探测——定时巡检时对 `/d/` 文件调用 CMS `probe_strm_url` 确认是否死链，结果写入任务元数据（24h 冷却），确认失效的升级为 `dead_direct_link` 规则（high，可忽略/可清理）；web 页面零外部调用（只读元数据）；清理动作可删除“已确认失效”的直链文件，未确认的直链仍不参与。

## 目标

提供一个**先预览、后执行**的失效 STRM 清理能力：把"目录中引用已失效分享的 STRM 文件"列出来，
用户确认后删除；绝不误删当前任务或其他有效任务正在使用的文件。

## 安全判定（删除条件必须全部满足）

对 dest 目录中每个 `.strm` 文件，提取其引用的分享 code（正则 `/s/([A-Za-z0-9]+)_[A-Za-z0-9]*_`）：

1. **不是当前任务自己的分享**：code != 当前任务的 `own_share_code`。
2. **该 code 不属于任何"存活"任务**：TaskStore 中不存在
   `own_share_code == code` 且 `share_validation_status in {valid, pending}`
   且未归档、未 `invalid_share_cleaned`/`source_deleted` 的任务。
   - 存活任务 = 可能仍在被 Emby 使用的分享 → 绝不删。
3. **文件内容不含 `/d/`**：直链文件一律留给人工作判断，自动清理不碰。
4. **路径安全**：文件在 `allowed_roots` 内、解析后无 `..` 逃逸、且 `is_file()`。
5. **目录指纹刷新**：执行前绕过 scan cache 重新扫描（防止与巡检缓存不同步）。

## 执行形态

- **Dry-run（默认）**：`POST /api/v1/quality/cleanup/dry-run` 或按钮"预览失效 STRM"——
  只返回候选清单 `[{path, share_code, owning_task_id?, task_title?, reason}]`，
  不写任何文件。候选按"无存活任务引用 → 已清理任务引用"两级排序。
- **执行**：`POST /api/v1/quality/cleanup/run`（body: `{paths: [...]}`，**必须显式传确认路径**，
  不接受"全选即删"）——逐文件删除前再次复检条件 1-5，任一项变化则跳过并记录。
- **每文件一条操作日志**：`task_operations` 记录 `operation_type=quality_strm_cleanup`、
  path、share_code、`dry_run` 标记，删除前落盘操作记录（与现有 journaled 移动一致）。
- **回滚**：不提供文件级恢复（STRM 是文本，可从仍在分享中的源重建）；删除前把文件内容
  写入日志（文本 200 字符内）以便溯源。执行后任务重扫，若任务已无 issue 且此前为
  `manual_required`，自动恢复评估（等同用户点击 resume 的效果）。
- **配置开关**：`QUALITY_STRM_CLEANUP_ENABLED`（默认 `false`），Web 设置页同步开关；
  关闭时两个端点都返回 409。

## 边界与不做的事

- 不自动执行：只提供 UI 动作，巡检绝不主动删文件（保持"巡检只读"原则）。
- 不处理 `direct_strm`（直链可能是其他流程的合法输入，需要人工确认）。
- 不改 workflow 的文件夹布局（每集共享同一 tmdb 文件夹）——那是更大的行为变更，
  单独评估；本清理动作是它的兜底。
- 不跨任务清理：候选只从"当前任务的 dest 目录"出发，避免影响无关目录。

## 验收标准

1. 线上 #368 场景：dry-run 精确列出 22 个 `unexpected_strm` 文件 + 原因"无存活任务引用/旧分享"，
   不含当前任务自己的文件。
2. 两个任务共享同一 dest 时（如 S03E07 与 S03E08 不同分享码），清理只删除"无存活引用"的文件，
   另一个任务仍有效的文件保留。
3. 全部文件删除后重扫：任务 issue 清零，`manual_required` 自动恢复评估为 no_issue。
4. 执行中途文件被改动（mtime/内容变化）→ 复检失败 → 跳过该文件并记录。
