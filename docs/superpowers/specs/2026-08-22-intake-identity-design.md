# 用文件身份绑定整理目录

日期：2026-08-22  
状态：已批准，待实现

## 背景

整理阶段用标题 / TMDB 搜索猜 dest。115 上分享 fid、接收夹 cid、片库 cid、视频 fid 是四套 ID。CMS 会新建 `C-` 目录、把文件挪走、把空接收夹丢进冗余。按名字命中的目录不必包含这次接收的文件。

0.4.14 用「目录必须包含 received 根目录 ID」修补电视剧绑错旧 dest。`received_file_ids` 实际是分享侧 fid。电影整夹入库后，真 dest 被拒，任务无限等待 CMS。测试 mock 把四套 ID 写成同一个字符串，所以全绿。

## 目标

- 接收后为每个任务写一份 `intake_identity`：这次拥有哪些接收根、哪些视频文件。
- 整理只根据这些视频文件的当前位置绑定 `dest_id`。标题 / TMDB 只用于命名和分类。
- 清理只删仍在待整理 / 冗余等源目录里的 `root_ids`，永远不删 `dest_id`。
- 测试夹具必须让分享 fid、接收夹 cid、片库 cid、视频 fid 互不相同。

## 非目标

- 不自己搬文件替代 CMS `auto_organize`。
- 不拆现有 15 个阶段，不多 worker。
- 不改 STRM 合并、Emby 刷新、分享探测的业务规则（除非它们误用了所有权 ID）。
- 不在实现完成前手改生产 #402 的 dest。

## 数据

任务 metadata 增加 `intake_identity`。写入后，历史 ID 只追加 `dest_id`，不改 `root_ids` / `files`。

```json
{
  "intake_identity": {
    "root_ids": ["3501165876508362266"],
    "files": [
      {"id": "3501165876877461022", "name": "拆弹专家.2017.2160p....mkv"}
    ],
    "dest_id": "3501166168859739769"
  }
}
```

| 字段 | 写入时机 | 含义 |
|---|---|---|
| `root_ids` | 接收完成 | 待整理里这次出现的顶层 cid（整夹或单文件） |
| `files` | 接收后立刻列目录 | 视频文件：`id` 为网盘 fid，`name` 为当时文件名 |
| `dest_id` | 第一次用 `files[].id` 定位成功 | 含这些视频的片库目录 |

现有 `received_file_ids` 仍表示分享侧快照，只用于接收恢复。它不参与绑 dest，不当清理目标，不当「未整理源」判断。

`received_items` 仍保存接收根的名字和 cid，供展示和 TMDB hint。所有权以 `intake_identity` 为准。

重处理：清空 `dest_id`，按当前 115 状态重列 `files`；`root_ids` 以这次重接收结果为准。

## 接收后快照

对每个接收根：

- 根是视频文件 → 收入 `files`。
- 根是目录 → `list_files` 一层。子项是视频则收入 `files`；子项是季文件夹（名匹配 `Season ##` / `第.季`）再列一层，只收视频。

视频后缀与现有识别相同：`.mkv` `.mp4` `.ts` `.iso` `.avi` `.mov` `.wmv` `.m2ts`。字幕、nfo、海报不进 `files`。

列不出任何 `files`（空目录或 115 失败）→ organizing 之前 defer，不准用标题绑 dest。

## 整理：用文件找 dest

仍触发 CMS `auto_organize`。然后：

1. 对每个 `files[]`，用记下的 `name` 搜索或列出候选目录子项，**只认 `p115_item_id` 等于 `files[].id` 的记录**。该记录的 cid 是当前父目录。
2. 父目录名匹配 `Season ##` / `第.季` → 候选 dest 是再上一级；否则候选 dest 就是该父目录。
3. 全部已定位的文件必须落到**同一个** dest。写入 `intake_identity.dest_id`，并照旧写入 submission 的 `own_share_file_id`（分享仍建在 dest 上）。
4. `dest_id` 已存在且所有 `files[].id` 仍在其下（直接子项或季文件夹下）→ 不再搜索。

等和停：

- 任一 `files[].id` 还找不到 → defer「等待 CMS 整理完成」。
- 搜到同 TMDB 旧目录，但不含这次任何 `files[].id` → 丢掉，即使名字完全匹配。
- `root_ids` 还在待整理，或已空并在冗余 → 不能当 dest。
- 文件落到两个不同片库根 → `needs_action`，不猜。

不再用 `received_file_ids ∪ received_items` 做 containment。`rejected_organized_file_ids` 不再承担所有权；可留作调试。

标题 / TMDB 解析仍可用于分类和展示，发生在 dest 绑定之后，或与绑定并行但不覆盖 `dest_id`。

## 清理：只删接收根

允许删除的父目录集合 `cleanup_parents`：

- `SELF_SHARE_RECEIVE_CID`（待整理）
- `source_cleanup_parent_ids`
- CMS `auto_organize_excluded_parent_ids`（冗余 / 失败 / 待整理等源侧目录）

分类片库父目录（`organized_scan_parent_ids` / 华语电影等）不在此集合。

规则：

- 只对 `root_ids` 发删除。`root_id == dest_id` 则跳过并记清理完成（空 file_id）。
- 当前 parent 必须 ∈ `cleanup_parents`。否则 `needs_action`，不删。
- `root_id` 已经不在，或不在 `cleanup_parents` 下 → 记清理完成，不补搜同名目录。
- 残留扫描只扫 `cleanup_parents`，排除 `dest_id` 和所有 `files[].id`。
- `cleaned` 不把 `own_share_file_id` 当删除目标。
- `cms_delete_settled` 只等本次发出的清理 ID 离开 CMS 索引；`dest_id` 留在索引里算正常。

0.4.14 的 `_is_library_dest_cleanup_target` 推断删除。改为：不在 `root_ids` 里的一律不删。

## 错误处理

- 115 风控或列目录失败 → defer，不改 `intake_identity`。
- 快照或定位未完成 → defer，不准用标题凑合绑。
- 文件散在两个片库根，或要删的 root 父目录不是 `cleanup_parents` → `needs_action`。

## 测试

夹具里分享 fid、接收夹 cid、片库 cid、视频 fid 必须两两不同。下列三张图缺一张不准合：

1. **电影整夹**：待整理一个夹；CMS 建 `C-…` 并把 mkv 挪进去；空夹进冗余。必须绑 `C-`；清理只删空夹。
2. **剧，季文件夹并入旧 dest**：旧剧根已在；新季文件进这个根。必须绑旧 dest，不删剧根。
3. **同剧第二条链接**：第二条任务自己的 `files` 进了已有 dest。其 `dest_id` 可以等于旧 dest；清理只删自己的 `root_ids`。

另测：同 TMDB 旧目录不含这次 `files` → 不绑定；`files` 尚未出现 → defer。

## 生产

#402 等这套合上、镜像发布后再按新规则跑，不先手写 dest。
