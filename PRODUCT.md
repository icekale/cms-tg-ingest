# Product

## Register

product

## Users

个人或家庭媒体库维护者，已经在使用 Cloud Media Sync、115、STRM、Emby 和 Telegram bot。用户通常只是偶尔打开 Web UI，快速确认自动入库流程是否正常、有没有任务卡住，以及是否需要执行低风险修复。

## Product Purpose

cms-tg-ingest 的 Web UI 是 Telegram 入库外挂的轻量运维控制台。它不承担复杂配置中心角色，而是把任务状态、异常处理、健康巡检和质量修复用更清晰的方式呈现出来。成功标准是：用户三秒内知道系统是否健康；一分钟内能定位卡住的任务；危险或重型操作不会被误触发。

Telegram 侧还可以复用 CMS 已授权的 HDHive 账号完成 TMDB 搜索、网盘筛选和资源解锁。该功能只把成功的 115 链接交给现有入库流程，不改变 CMS 的整理分类，也不替其他网盘执行入库。

## HDHive 剧集订阅

用户直接发送 `https://hdhive.com/tv/<slug>` 创建订阅，而不是立即解锁。系统每天按 `HDHIVE_SUBSCRIPTION_TIME`（默认 `01:30`，时区由 `HDHIVE_SUBSCRIPTION_TIMEZONE` 控制）检查新集；费用未知或超过阈值时进入待确认，用户点击“确认解锁”后才会继续。订阅由 `HDHIVE_SUBSCRIPTION_AUTO_ENABLED` 控制，可在 Web `/hdhive` 或 Telegram 菜单中暂停、恢复、删除和立即检查。

## Brand Personality

清爽、可信、少打扰。界面应该像一个安静可靠的家庭媒体库助手：默认克制，只在需要关注时明确提醒，不用花哨效果制造存在感。

## Anti-references

不要做成传统 NAS 插件后台、路由器管理页或密密麻麻的参数表。避免把底层日志、原始错误、过多按钮和诊断文本直接堆在首页。避免高频刷新、复杂图表、重型前端框架和增加 115/CMS 压力的功能。

## Design Principles

1. 默认极简，异常优先：首页只回答“现在是否正常、哪里需要处理”。
2. CMS/TaskStore 优先可信：Web UI 展示已有状态，不主动做额外扫描或昂贵请求。
3. 渐进披露：普通用户看卡片和简短说明，诊断信息放到详情页或二级页面。
4. 操作谨慎：重试、恢复、重跑、清理历史等操作要有明确文案和确认。
5. 轻依赖、易部署：优先使用现有 Python server-rendered HTML/CSS，不引入复杂前端栈。

## Accessibility & Inclusion

目标达到 WCAG AA 级别的基础可读性：足够的文字对比度、清晰焦点状态、移动端可用、按钮命名明确。默认减少动效；如加入动效，只用于状态反馈且保持短促克制。
