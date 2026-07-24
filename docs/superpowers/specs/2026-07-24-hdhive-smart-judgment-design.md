# HDHive 剧集智能判断设计

日期：2026-07-24  
状态：已确认，待实施计划

## 目标

增强现有 HDHive 剧集订阅检查逻辑，在不提高 115/HDHive 并发、不改变普通 115 入库链路的前提下，实现：

- 多季资源的统一季集识别；
- 默认排除特殊集，并支持订阅级集数范围过滤；
- 查询 Emby 已有的季集，避免重复解锁和重复提交 CMS；
- 根据 TMDB 的剧集状态和已知集数判断订阅是否完结；
- 在 TG/Web 中反馈每次检查的跳过、待确认、解锁和完结原因。

## 范围与边界

本功能只作用于 `HdhiveSubscriptionService` 的电视剧订阅检查，不改变：

- 普通 115 分享、磁力、ED2K 入库任务；
- CMS 整理、分类、改名和 STRM 生成规则；
- 共享 STRM/直链 STRM 的选择；
- 已经进入 TaskStore 的任务；
- 115 文件删除和 Emby 刷新流程。

成功解锁后仍只调用现有 intake 回调，把 115 分享链接交给现有任务引擎。智能判断层不会直接操作 115 文件，不会复制 CMS 整理逻辑，也不会删除已存在的媒体。

## 用户可见规则

### 季集识别

资源集数按以下优先级识别：

1. HDHive 返回的 `season_number` + `episode_number`；
2. HDHive 返回的 `episode_key` / `episode_code`；
3. 标题中的 `S01E02`、`S1E2` 等模式；
4. 无法识别时保留资源为 `unparsed`，不自动解锁。

统一格式为大写 `S01E02`。`S00` 视为特殊集，默认跳过；用户显式将 `S00` 放入过滤范围时才允许处理。

同一季同一集的候选仍按现有规则选择：有效性优先、分辨率最高、积分最低。不同季之间独立选择，不会把 `S01E01` 和 `S02E01` 合并。

### 集数过滤

每条订阅增加可选的 `episode_filter`，空值表示处理全部可识别的正常集。支持以下格式：

```text
S01E01-S01E10
S01E01,S01E03,S02E01-S02E03
S02
```

规则：

- 单集按精确匹配；
- 范围只允许在同一季内，按集数闭区间匹配；
- `S02` 表示该季全部正常集；
- 混合表达式按逗号拆分后取并集；
- 格式错误时拒绝保存过滤条件，并保留原条件；
- 过滤范围之外的资源写入 `filtered` 统计，但不改变资源为已解锁或失败。

过滤器是订阅级配置，不影响其他订阅；默认空值保持当前行为。

### Emby 已有集数跳过

当 Emby 已启用且订阅有 TMDB TV ID 时：

1. 查询 Emby 中 TMDB ID 对应的 Series；
2. 查询该 Series 的 Episodes，读取 `ParentIndexNumber` 和 `IndexNumber`；
3. 转换成统一的 `SxxExx` 集数集合；
4. 已存在的集标记为 `emby_exists`，不调用 HDHive 解锁、不进入 intake；
5. 资源详情和运行摘要显示已存在的集数。

Emby 未配置、找不到对应 Series 或查询失败时，不假设集数已存在，继续执行正常解锁，并记录 `emby_skip_unavailable` 原因。Emby 查询失败不会把订阅置为完结。

### 电视剧完结

TMDB 详情中的状态按以下规则解释：

- `Ended`、`Canceled`：完结候选；
- `Returning Series`、`In Production`、`Planned`、`Pilot`：未完结；
- 缺失或未知：未知，不自动完结。

只有同时满足以下条件，订阅才标记为 `completed`：

1. TMDB 状态为完结状态；
2. TMDB 提供了可用的季集数量，且每个应处理的集都已处于 `enqueued`、`emby_exists` 或 `filtered`；缺少资源、普通解锁失败、提交失败和待确认状态都会阻塞完结，保留后续重试机会；
3. 没有高费用/未知费用待确认资源；
4. 没有无法识别集数的资源阻塞判断。

完结订阅停止每日自动检查，但保留“恢复”操作。恢复后仍按相同规则检查，适合 TMDB 数据修正或用户手动补集。`active`、`paused`、`error` 外新增 `completed` 状态；删除订阅不删除已有任务和媒体文件。

## 架构

### 纯规则模块

新增 `app/series_rules.py`，只包含可测试的纯函数：

- `parse_episode_key(value) -> EpisodeKey | None`；
- `normalize_episode_key(value) -> str`；
- `parse_episode_filter(value) -> EpisodeFilter`；
- `episode_filter_matches(filter, episode_key) -> bool`；
- `is_special_episode(episode_key) -> bool`；
- `completion_state(tmdb_status, discovered, blocked) -> str`。

该模块不访问网络、数据库或文件系统，避免将季集解析和业务副作用混在一起。

### TMDB 数据

扩展现有 TMDB resolver 的 TV 详情标准化结果，保留：

- `status`；
- `number_of_seasons`；
- `number_of_episodes`；
- `seasons` 中的季号和已知集数。

若当前部署没有 TMDB API 凭据，则不主动发起额外网络扫描；完结状态保持未知，资源季集仍使用 HDHive 返回值和标题解析。已有 TMDB API/代理失败时沿用现有 fallback，不影响普通入库。

### Emby 查询

扩展 `EmbyClient`：

- `find_series_by_tmdb(tmdb_id)`：只查询 Series；
- `episode_keys_for_series(item_id)`：读取指定 Series 的 Episodes；
- `existing_episode_keys_by_tmdb(tmdb_id)`：组合上面两个操作并返回标准化集数集合。

查询使用现有 `X-Emby-Token` Header，保持 API Key 不出现在 URL、日志和错误消息中。接口返回空集合与接口失败区分记录，但都不会误判为“Emby 已有”。

### 订阅存储

在现有 `HdhiveSubscriptionStore` 的订阅表增加：

- `episode_filter TEXT NOT NULL DEFAULT ''`；
- 复用现有 `status` 存储 `completed`，不新增重复的智能状态列；
- `last_summary_json TEXT NOT NULL DEFAULT '{}'`。

资源表增加：

- `normalized_episode_key TEXT NOT NULL DEFAULT ''`；
- `skip_reason TEXT NOT NULL DEFAULT ''`。

已有数据库通过 `_ensure_columns` 增量迁移，不删除旧数据。资源状态新增 `filtered`、`emby_exists`、`unparsed`；已解锁/已入库的旧状态保持兼容。每次检查对同一订阅采用现有串行租约，避免重复解锁。

### 订阅检查流程

每次 `check(subscription_id)` 执行：

1. 读取订阅过滤条件并解析；
2. 查询 HDHive TV 资源，只保留 115；
3. 标准化季集，记录未解析资源；
4. 应用特殊集和订阅范围过滤；
5. 查询 Emby 已有集数（可用时）；
6. 对 Emby 已有集标记跳过；
7. 按集分组并选择最佳新资源；
8. 沿用现有积分阈值和确认逻辑解锁；
9. 将成功 115 分享链接交给现有 intake；
10. 读取 TMDB 完结状态，计算是否满足完结条件；
11. 保存摘要和状态，并通过现有通知回调反馈。

Emby 查询和 TMDB 详情均采用每次检查一次的结果缓存，不在 115 上做额外扫描。

## TG/Web 交互

### Telegram

保留现有订阅按钮，并在订阅详情/检查结果中显示：

- 过滤条件；
- 已有集数跳过数量；
- 特殊集/范围过滤数量；
- 待确认和未解析集数；
- 完结或继续追更状态。

新增“设置集数过滤”按钮，使用一次性文本输入；空输入清除过滤。错误格式只返回示例，不修改原值。无需新增复杂命令。

### Web

在现有 HDHive 订阅卡片增加：

- 集数过滤输入框和保存按钮；
- 智能判断状态；
- 最近一次跳过/待处理摘要；
- `completed` 状态和恢复按钮。

现有的暂停、恢复、检查、删除和确认解锁操作保持兼容；Vue API 与旧 Web 路由都提供同等能力。

## 错误与安全

- 季集无法解析：不自动解锁，标记 `unparsed`，避免把错误资源当成新集；
- 过滤格式错误：拒绝保存，不改变当前过滤器；
- Emby 查询失败：继续处理但不执行已有集数跳过，并在摘要中提示；
- TMDB 状态未知：订阅保持 active，不标记 completed；
- 高费用或未知费用：沿用待确认，不因为智能判断绕过费用保护；
- 已有 TaskStore 任务：继续使用现有去重，不重复提交 CMS；
- 所有新增日志/通知不输出 OAuth Token、Emby API Key 或完整授权敏感信息；
- 不新增 115 全盘扫描，不提高并发，不改变风控冷却策略。

## 验证

自动化测试覆盖：

- `S1E1`、`S01E01`、标题识别和无效集数；
- `S00` 默认过滤和显式放行；
- 单集、范围、整季和混合过滤器；
- 多季同号集不合并；
- Emby 已有集跳过、Emby 未配置和 Emby 查询失败；
- TMDB 完结/未完结/未知状态；
- 完结条件被待确认、未解析集阻塞；
- 订阅数据库迁移、状态保存和重启恢复；
- TG/Web 保存过滤器和展示检查摘要；
- 现有 HDHive、TaskStore、CMS/115 工作流全量回归测试。

真实环境验证先使用暂停订阅执行手动检查，确认季集、Emby 已有集和过滤结果正确，再恢复订阅启用实际自动解锁。
