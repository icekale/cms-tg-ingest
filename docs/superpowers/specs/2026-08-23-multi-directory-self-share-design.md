# 多目录自有分享支持设计

- 日期：2026-08-23
- 状态：已获用户确认，待实施计划
- 关联问题：任务 414 在 CMS 将一次接收拆入多个片库目录后持续等待

## 1. 背景

当前自有分享任务假设一次接收最终只对应一个 CMS 整理目录。`_resolve_intake_dest_folder` 已修复为能发现多个目录并进入安全冲突分支，但这只能防止无限等待，不能处理合法的多目录结果。

目标是让一个任务支持多个实际整理目录：每个目录独立识别、创建自有分享、提交 CMS 同步、生成独立 STRM 目录并完成 Emby 确认；全部目标完成后才统一清理接收源。

## 2. 已确认的产品规则

1. 一个任务可以拥有多个整理目标，不拆分为多个任务。
2. 每个整理目标创建一个独立的自有分享。
3. 每个自有分享提交独立的 CMS 同步任务。
4. 每个目标使用独立的本地 STRM 目录。
5. 每个目标独立解析 TMDB、标题、分类和 Emby 结果；目标之间可以有不同 TMDB ID。
6. 任一目标创建分享、验证、CMS 同步、STRM、移动或 Emby 阶段失败，整个任务进入 `NEEDS_ACTION`，所有接收源保留。
7. 只有所有目标全部完成且分享异步审核通过后，才统一清理接收源。
8. 不分享共同父目录，避免把任务之外的内容带入分享。
9. 现有单目录任务必须保持兼容。

## 3. 数据模型

不新增子任务。使用任务 metadata 保存多目录状态，并加入版本标记：

```json
{
  "multi_target_version": 1,
  "organized_targets": [
    {
      "target_id": "115-destination-id",
      "file_ids": ["file-id-1"],
      "folder": {
        "file_id": "115-destination-id",
        "file_name": "片库目录",
        "parent_id": "片库父目录"
      },
      "recognition": {
        "tmdb_id": "...",
        "title": "...",
        "category": "..."
      },
      "share": {
        "file_id": "115-destination-id",
        "code": "",
        "receive_code": "",
        "url": "",
        "status": "pending",
        "validation_status": "pending",
        "sync_status": "pending"
      },
      "strm": {
        "source_path": "",
        "dest_path": "",
        "status": "pending",
        "move_status": "pending",
        "emby_status": "pending"
      }
    }
  ]
}
```

### 3.1 目标发现

整理阶段收集全部接收文件，并建立 `destination_id -> file_ids` 分组。只有以下条件同时满足时才生成目标列表：

- 每个预期文件都已找到；
- 每个文件只能归属一个目标目录；
- 目标目录不是接收根、接收根子目录或季目录本身；
- 目标目录记录完整；
- 没有无法归属或相互冲突的文件。

单目录结果继续写入现有 `intake_identity.dest_id` 和旧 metadata 字段。多目录结果写入 `organized_targets`，并保留 `intake_identity.files/root_ids` 用于最终统一清理。

### 3.2 旧字段兼容

`submissions` 表中现有单值字段（`own_share_file_id`、`own_share_code`、`dest_path` 等）继续保留，单目录任务行为不变。多目录任务以 `organized_targets` 为权威状态；旧字段仅镜像第一个目标或提供兼容展示，不作为多目录清理和恢复的唯一依据。

### 3.3 操作幂等

所有外部操作 key 都必须包含任务 operation scope 和目标 ID：

- `create_share:<target_id>`
- `cms_share_sync:<target_id>:<share_code>`
- 目标级删除和恢复操作使用目标文件 ID

已成功的目标在重试或进程重启后跳过，禁止重复创建分享、重复提交同步或重复删除。

## 4. 阶段流转

现有任务阶段保持不变，每个阶段聚合处理全部目标。

### 4.1 Organizing

扫描并分组所有预期文件。缺文件时继续等待；发现多个完整目标时生成 `organized_targets`。目标列表生成前不创建任何自有分享。

### 4.2 Recognizing

逐目标解析 TMDB、标题和分类，结果写入目标自身的 `recognition`。任一目标无法识别则整任务 `NEEDS_ACTION`，不进入创建分享阶段。

### 4.3 Alias / Own Share Created

逐目标保留别名并通过目标级 journal operation 创建独立自有分享。已完成目标不重复创建。任一目标无法安全恢复时整任务 `NEEDS_ACTION`。

### 4.4 Share Validated / CMS Sync Submitted

逐目标验证分享并提交 CMS 同步。所有目标都成功后阶段才完成。任何异常或不确定结果都进入 `NEEDS_ACTION`，源文件保留。

### 4.5 STRM Ready / Moved / Emby Confirmed

每个目标从自己的分享生成 STRM 源目录，并计算独立的本地目标路径。路径为空、重叠或无法安全确认时进入 `NEEDS_ACTION`。所有目标完成后才推进阶段。

### 4.6 Cleaned

所有目标通过异步分享审核、STRM 移动和 Emby 确认后，统一核对并删除接收根。现有 journal 删除和恢复保护继续使用；任一根目录不在允许清理父目录时立即人工处理。

## 5. 安全的继续整理动作

当前任务 414 已经处于 `NEEDS_ACTION`，而且已有成功的 `receive_share` operation。为避免使用现有 `reprocess` 导致 operation generation 改变并重复接收，新增受保护的“继续整理”动作：

- 只允许未领取的整理阶段人工任务；
- 必须存在成功的 `receive_share` operation；
- 必须存在接收文件快照；
- 只把任务重新排入 `ORGANIZING`；
- 不清除接收结果，不改变 operation generation；
- 不适用于已经创建自有分享或进入后续清理阶段的任务；
- 使用 compare-and-set 状态转换，状态变化时拒绝执行。

部署多目录版本后，任务 414 通过此动作重新进入整理，不重复接收 115 分享。

## 6. 接口与展示

任务 API/UI 增加：

- 目标目录列表；
- 每个目标的 TMDB、分类、分享、CMS 同步、STRM、移动和 Emby 状态；
- 聚合任务状态和目标失败原因；
- 仅对满足安全条件的任务显示“继续整理”。

旧单目录字段和接口响应继续保留，避免破坏现有客户端。

## 7. 测试计划

先写失败回归测试，再实现：

1. 一个接收任务拆入两个目录时，整理阶段生成两个完整目标；
2. 两个目标可以拥有不同 TMDB 和分类；
3. 每个目标只创建一次自有分享；
4. 每个目标只提交一次 CMS 同步；
5. 任一目标失败时任务进入 `NEEDS_ACTION`，接收源不删除；
6. 已完成目标在重试和进程重启后跳过；
7. 每个目标生成独立 STRM 路径，路径重叠会被拒绝；
8. 全部目标完成后才统一清理接收根；
9. “继续整理”不会重复 `receive_share`；
10. 现有全部单目录测试和 operation recovery 测试继续通过；
11. API/UI 能正确展示多目标状态。

## 8. 非目标

- 不把多个目录合并到共同父目录分享；
- 不自动拆分为多个任务；
- 不在任一目标失败时做部分源清理；
- 不修改已经成功的单目录任务数据；
- 不通过直接生产数据库修改来恢复任务 414。

## 9. 发布与恢复

实现完成后：

1. 完成本地回归、相关测试和全量测试；
2. 发布新版本并构建多架构镜像；
3. 备份远端数据、环境和 compose 配置后部署；
4. 用只读健康检查确认容器、TaskRunner 和保护守卫正常；
5. 通过受保护的“继续整理”动作处理任务 414；
6. 检查 operation 记录，确认没有重复接收、重复分享或错误删除；
7. 若多目录任何阶段失败，保留 `NEEDS_ACTION` 和全部源文件，不强行自动修复。
