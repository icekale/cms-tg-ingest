# GitHub 与 Docker Hub 中文介绍设计

## 目标

让第一次打开 GitHub 或 Docker Hub 的用户在首屏快速理解：`cms-tg-ingest` 是 CMS 的 115 分享自动入库外挂，入口是 Telegram，最终产物是自有分享 STRM 和 Emby 入库结果。

## 统一定位

短描述统一为：

> CMS 115 分享自动入库外挂：TG 裸链接、自有分享 STRM、Emby 确认与安全清理。

## GitHub README

- 保留项目名，重写开头两段，先说明输入、处理流程和最终结果。
- 首屏增加一行紧凑流程：`115 分享链接 -> CMS 整理分类 -> 自有永久分享 -> 分享 STRM -> Emby 入库 -> 清理转存源`。
- 核心保障压缩为四点：CMS 优先分类、只入库分享 STRM、共享别名保护、低频 115 调用。
- 详细功能、配置、路径映射和安全说明继续保留，避免破坏现有文档链接和部署说明。

## Docker Hub Overview

- 使用独立的精简中文 Overview，不完整复制长 README。
- 内容包含产品用途、核心流程、主要保障、快速启动命令、必需依赖和 GitHub 文档链接。
- 不包含用户实例的 IP、CID、Token、Cookie、API Key 或其他私有配置。

## 平台元信息

- GitHub repository description 与 Docker Hub short description 使用统一定位。
- GitHub homepage 指向 Docker Hub 镜像页，Docker Hub Overview 指向 GitHub 仓库。

## 验证

- README 文档测试和敏感信息测试通过。
- GitHub API 返回新的 description/homepage。
- Docker Hub API 返回新的 short description，并且 Overview 包含新版核心流程。
