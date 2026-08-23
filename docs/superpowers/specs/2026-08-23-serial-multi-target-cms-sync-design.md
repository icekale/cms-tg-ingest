# 多目标 CMS 分享同步串行化设计

## 问题

CMS 的 `/api/sync/share115` 接口是异步任务入口。多目标任务连续提交多个请求时，接口都可能返回“创建任务成功”，但 CMS 的后台处理可能只实际完成其中一个目标。任务 journal 只能证明请求已接受，不能证明 STRM 源已生成。

## 设计

在多目标 `share_sync_submitted` 阶段使用确定性 target 顺序：

1. 预校验所有目标身份、分享和已有 operation。
2. 找到第一个尚未有成功 `cms_share_sync` operation 的目标，只为它创建并提交一个 operation，然后 defer 当前阶段。
3. 对已提交目标，只接受其自有分享 STRM 源目录已出现且包含 STRM；源目录未出现时 defer，不提交后续目标。
4. 所有目标 operation 成功且所有源目录均可验证后，阶段才完成并进入 `strm_ready`。
5. 已成功 operation 永远复用；异常或不确定 operation 继续进入既有人工安全路径。

这样不会重复 receive、create share 或成功的 CMS sync 请求，同时避免 CMS 异步任务互相覆盖。

## 验证

增加回归测试证明：第一次阶段调用只提交第一个目标；目标源出现后第二次调用才提交第二个目标；已有成功 operation 不会再次调用 CMS。保留单目录和旧字段行为。
