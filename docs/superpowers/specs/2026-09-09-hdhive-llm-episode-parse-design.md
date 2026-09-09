# HDHive 订阅 LLM 季集识别

日期：2026-09-09  
状态：已确认，待实现计划

## 背景

HDHive Open API 通常不返回 `season_number` / `episode_number`。订阅检查用标题、备注和单季 TMDB 默认季做正则解析。多季剧且文本没有季号时，会故意标成 `unparsed`，避免把「更新至01集」猜成 S01。

v0.5.23 已经能从「第二季 + 更新至07集」这类文案抽出 `S02E01-S02E07`。仍有一批资源正则覆盖不到，但标题或备注里能看出集数，人可以结合剧名和 TMDB 判断是第几季。

项目里已有 OpenAI 分类 / 身份识别兜底（`OPENAI_CLASSIFY_ENABLED`、同一套 Key / Base URL / 模型、高置信与建议阈值）。本功能复用这条链路，不新开账号体系。

## 目标

- 正则解析失败、且标题或备注带有季集线索时，同步调用现有 OpenAI，识别季集。
- 允许结合剧名和 TMDB 季列表**推断季号**；集数必须能从资源文本落地，不能由模型按「当前正播到第几集」改写。
- 高置信且季号落在 TMDB 季列表时，按现有检查路径自动过滤 / 解锁 / 入队。
- 能解析但不满足自动入队条件时，写入季集并进入已有的 `pending_confirmation`，等人在 HDHive 页确认。
- 模型关闭、超时、失败时，检查不中断，条目保持 `unparsed`。

## 非目标

- 不改 `episode_keys()` 的确定性解析，不把 HTTP 放进正则函数。
- 不为 HDHive 另建一套 LLM 配置、账号或开关；沿用 `OPENAI_CLASSIFY_ENABLED` + API Key。
- 不改 HDHive 页面布局；待确认列表继续显示 `skip_reason`。
- 不新增多一列「LLM 元数据」表字段。
- 不做后台异步识别队列。
- 不把解锁 URL、积分账号、Cookie、Token 送给模型。
- 不放宽特殊集默认跳过、积分确认、Emby 去重和集数过滤。

## 已确认决策

- 季号：允许用剧名 / TMDB 推断。
- 入队：分层。高置信 + 季号在 TMDB 季列表 → 自动入队；其余能解析的 → 待确认。
- 触发：只有文本含季集线索且正则失败才调用。
- 时机：订阅检查过程中同步调用，且发生在按集分组之前。

## 架构

### 纯函数

新增 `app/hdhive_episode_llm.py`，不发网络请求：

- `has_episode_clue(text) -> bool`
- `decide_llm_episode(result, tmdb_season_numbers, high_confidence, suggest_confidence) -> auto | pending | reject`
- 把模型 JSON 收成 `EpisodeKey` 元组（校验集数、范围、季号）

### 模型调用

在现有 `OpenAIClassifier` 增加 `parse_hdhive_episode(...)`，风格对齐 `identify_media` / `classify_media`：

- `enabled` 为假时不请求
- JSON schema 强制输出
- 实例级短缓存（成功 / 失败都缓存）
- 本方法单独使用较短超时，避免一次订阅检查被拖死

### 检查接入

`HdhiveSubscriptionService` 增加可选依赖 `episode_parser`（duck typing：`enabled` + `parse_hdhive_episode`）。`create_hdhive_subscription_service` 把已有 `openai_classifier` 传进去。未注入或未启用时，行为与 v0.5.23 完全一致。

`episode_keys()` 保持只做正则。

## 触发

对每条 115 资源，先算 `episode_keys(resource, default_season=...)`。仅当结果为空，且把 `title`、`remark`、`episode_key`、`episode_code` 拼起来的文本满足 `has_episode_clue`，且 `episode_parser.enabled`，才调用模型。

线索（大小写不敏感，命中任一即可）：

- `更新至`
- `第` + 数字或中文数字 + `集`
- `全` + 数字 + `集`
- `EP` / `E` + 数字（需与字母数字边界，避免误伤分辨率里的裸数字）
- `S` + 数字（同样要边界）
- `Season` + 数字

不触发：

- 纯剧名、空备注、没有任何上述标记
- 正则已经解析成功
- OpenAI 未启用
- 本订阅本次检查已达到调用上限

「最后生还者 第二季」且备注为空：只有中文季号、没有集数线索，**不调用**。  
「最后生还者 第二季」+「更新至07集」：有线索；若正则已能解析则仍不调用。

## 数据流

在 `check()` 里，扫描 115 资源并分组之前：

1. 正则解析。
2. 需要兜底则调用 `parse_hdhive_episode`，输入仅含：
   - 订阅剧名、`tmdb_id`
   - TMDB 季列表（`season_number`、`episode_count`、季名；查失败则为空列表）
   - 资源 `title`、`remark`、`episode_key`、`episode_code`
3. `decide_llm_episode` 决定 `auto` / `pending` / `reject`。
4. `auto` 与 `pending` 都用识别出的键作为分组键，并写入 `normalized_episode_key`。同一集的 1080 / 2160 因此仍能合并，后续继续选最优、过滤、Emby 去重。
5. `reject` 或未调用：分组键仍退回 slug / 原始 `episode_key`，`normalized_episode_key` 为空，现有逻辑标 `unparsed`。

`pending` 在进入解锁循环前调用 `mark_item_pending`，原因固定为：

```text
LLM 识别待确认：S02E01-S02E07
```

已有解锁循环会跳过 `pending_confirmation`。用户在现有 HDHive 待确认列表点确认后，带 `confirmed_item_id` 再检查，使用已写入的 `normalized_episode_key` 走解锁 / 入队，不需要新 API。

`auto` 不改状态，当作正则成功。若随后积分超过自动解锁阈值，仍走现有积分待确认，不覆盖 LLM 结果。

特殊集（`S00`）识别成功后仍走现有默认跳过；用户把 `S00` 写进过滤范围时才处理。

## 模型约定

输出 schema（strict）：

| 字段 | 约束 |
| --- | --- |
| `season` | 整数，`>= 0` |
| `episode_start` | 整数，`>= 1` |
| `episode_end` | 整数，`>= episode_start`，且 `end - start <= 200` |
| `confidence` | 0–1 |
| `reason` | 短说明 |
| `evidence` | 必须是标题或备注里出现过的原文片段 |

系统提示要点：

- 集数必须能从 `evidence` 对应的资源文本读出。备注写「更新至07集」则 `episode_end` 必须是 7，不得改成 TMDB 的总集数或「当前正播集」。
- 文本没有季号时，可以用剧名和 TMDB 季名 / 季号推断 `season`。
- 无法同时给出合法季和集时，不要编造；由调用方把缺字段或空 `evidence` 判为 `reject`。

单集：`episode_start == episode_end`，格式化成 `SxxExx`。  
连续范围：格式化成现有的 `SxxEaa-SxxEbb`。

## 分层落地

沿用现有阈值，不新增环境变量：

- 高置信：`openai_high_confidence`，默认 `0.75`
- 建议：`openai_suggest_confidence`，默认 `0.45`

判定：

| 条件 | 结果 |
| --- | --- |
| 键合法，`confidence >= 高置信`，且 `season` 落在 TMDB 季号集合 | `auto` |
| 键合法，`confidence >= 建议`，但不满足上一行 | `pending` |
| 其余（低置信、空 evidence、非法范围、模型失败） | `reject` → `unparsed` |

TMDB 季号集合：该次 lookup 返回的 `seasons[].season_number`（整数，含 0）。lookup 失败或列表为空时集合为空，因此**不会 `auto`**，最多 `pending`。

## 失败与限流

- 单次订阅检查最多 **8** 次实际 HTTP 调用（缓存命中不计）。超出后剩余资源保持 `unparsed`，下次检查再试。
- `parse_hdhive_episode` 超时 **20 秒**。超时、HTTP 错误、非 JSON、schema 失败：该条 `reject`，检查继续。
- 成功缓存 TTL **6 小时**；失败缓存 TTL **30 分钟**，便于手动「立即检查」在短故障后重试。
- 缓存键：`tmdb_id` + `resource_slug` + `title/remark/episode_key/episode_code` 的稳定哈希。
- 失败结果同样缓存，避免定时任务对同一条反复打满上限。
- 单条异常只记警告日志：`subscription_id`、`resource_slug`、识别出的季集、置信度。不写 API Key、解锁 URL、Cookie。
- 一条资源的 LLM 失败不影响同一次检查里其他资源，也不让整个订阅检查失败。

## 测试

不打真实模型。用假 `episode_parser` 断言调用次数、入参不含密钥、出参如何改变状态。

必须覆盖：

1. 多季剧 +「更新至07集」+ 标题能看出第二季：高置信且季在 TMDB 列表 → `S02E01-S02E07`，可入队，不是 `unparsed`。
2. 同样文案但置信度不够，或季号不在 TMDB 列表 → 写入季集，`pending_confirmation`，不解锁。
3. 无季集线索 → 不调用，保持 `unparsed`。
4. 正则已能解析 → 不调用。
5. 解析器关闭 / 超时 / 乱返回 → 检查不中断，条目 `unparsed`。
6. 同一 slug + 同一文案第二次检查 → 缓存命中，不再请求。
7. 超过 8 次调用上限 → 其余 `unparsed`。
8. 识别为 `S00E01` 且未把特殊集纳入过滤 → 现有默认跳过。
9. 现有 `test_multi_season_tmdb_does_not_guess_seasonless_updated_through` 仍然成立（无标题季号、无 LLM 或 LLM 未启用）。
10. 日志和序列化结果不含 API Key、解锁 URL。

## 兼容

- OpenAI 关闭时，订阅检查与 v0.5.23 行为一致。
- 已存在的 `unparsed` 行仍按 `resource_slug` upsert；下次检查识别成功后就地更新 `normalized_episode_key` 和状态，不必删订阅。
- 不改数据库 schema。
