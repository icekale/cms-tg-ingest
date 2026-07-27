# 云下载移动后立即触发 CMS 整理

## 目标

115 云下载的真实媒体文件移动到当前待整理目录成功后，在同一个任务阶段立即调用 CMS 自动整理，减少等待下一轮调度的延迟。

## 设计

- `P115WebClient.resolve_cloud_download_output()` 继续负责定位真实媒体项并移动到待整理 CID；移动返回成功后才进入 CMS 触发逻辑。
- `_stage_cloud_downloading()` 创建提交记录和完整云下载元数据后调用 `cms.run_auto_organize()`。
- CMS 调用成功时，将提交记录的 `workflow_phase` 写为 `auto_organize_submitted`，任务完成并进入现有 `ORGANIZING` 阶段。
- CMS 调用失败时返回低频 `defer`，保留云下载身份和已移动文件元数据；重试只调用 CMS，不重复云下载，不删除 115 文件。
- 既有 `ORGANIZING` 阶段识别 `auto_organize_submitted`，因此成功触发后不会重复提交 CMS 整理。

## 验收

- 云下载完成并移动后，当前阶段调用一次 `run_auto_organize()`。
- CMS 触发失败时任务保持可重试，115 云下载提交次数仍为一次。
- 现有云下载、分享、STRM、Emby 和清理流程测试全部通过。
