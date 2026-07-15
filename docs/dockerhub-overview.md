# cms-tg-ingest

Cloud Media Sync（CMS）的 115 分享自动入库外挂。把一个或多个 115 分享链接发给 Telegram 机器人，程序会自动完成 CMS 整理分类、自建永久分享、分享 STRM 生成、媒体库移动、Emby 入库确认和 115 转存源清理。

`115 分享链接 -> CMS 整理分类 -> 自有永久分享 -> 分享 STRM -> Emby 入库 -> 清理转存源`

## 核心能力

- Telegram 支持裸链接、多链接、任务状态和运维按钮。
- 优先采用 CMS 的整理与分类结果，不用模型覆盖已识别分类。
- 只允许自有分享 STRM 入库，拒绝直链和错误分享码 STRM。
- 共享别名保护降低名称风险，本地仍恢复标准目录和剧集文件名。
- 实际探测 STRM 播放端点，新版本验证失败时保留媒体库现有文件。
- TaskStore 记录每个阶段、耗时、等待原因、失败和重试状态。
- 限制 115 查询频率与扫描预算，遇到风控后自动进入冷却。

## 快速开始

```bash
git clone https://github.com/icekale/cms-tg-ingest.git
cd cms-tg-ingest
cp .env.example .env
# 编辑 .env 后启动
docker compose up -d
```

也可以直接拉取多架构镜像：

```bash
docker pull icekale/cms-tg-ingest:latest
```

支持 `linux/amd64` 和 `linux/arm64`。

## 使用前提

- 已部署并可访问 Cloud Media Sync。
- 已配置 115 Cookie、待整理目录和媒体库路径映射。
- 已创建 Telegram Bot，并设置允许访问的用户或聊天 ID。
- 如需自动确认入库，需提供可访问的 Emby 地址和 API Key。

## 安全边界

本项目不提供媒体资源，也不绕过 115、CMS 或 Emby 的权限机制。它只自动化你已经拥有权限的个人媒体库工作流。请勿公开 `.env`、115 Cookie、Telegram Token 或 Emby API Key。

完整配置、流程说明和故障排查请查看 [GitHub 文档](https://github.com/icekale/cms-tg-ingest)。
