# CMS STRM 删除守卫（sitecustomize 补丁）

根治 CMS 增量同步误删自有分享 STRM 的问题（线上案例：龙族 S03E06 两次被误删）。

## 问题根因

共享 STRM 模式下，cms-tg-ingest 在任务完成并通过 115 异步审核观察期后，会删除 115 转存源文件（设计行为，自有永久分享仍在，播放不受影响）。但 CMS 的增量同步会轮询 115 生活事件（`delete_file`），并按 fid 删除本地对应文件——它不感知"媒体库 strm 已被自有分享 `/s/` 链接接管"，于是把仍有效的 strm 一并删除，Emby 随即上报 `library.deleted`。

CMS 的生活事件消费在每次 `auto_organize` / `share115` 同步时顺带触发（独立 `SYNC_CRON` 常被禁用），所以误删可能在几小时后才发生，难以预料。

## 方案：CMS 容器内加删除守卫

`sitecustomize.py` 以 Python 启动钩子注入 CMS 进程，对 `MediaSync.delete_local_file` 包一层守卫：

- 删除本地 `.strm` 前读取文件内容，若指向自有分享链接（`/s/...` 模式）则**跳过删除**；
- 直链（`/d/`）、普通文件、非 strm 的删除行为**完全不变**；
- 自有分享真正失效时，由 cms-tg-ingest 的 `self_share_health` 巡检负责清理（现有能力）。

## 安装（Unraid Compose Manager）

1. 把 `sitecustomize.py` 复制到 CMS 挂载的 config 目录：
   ```sh
   cp sitecustomize.py /mnt/user/appdata/cloud-media-sync/config/patches/sitecustomize.py
   ```

2. 在 CMS 的 `docker-compose.override.yml` 的 `cloud-media-sync` 服务下加环境变量：
   ```yaml
   services:
     cloud-media-sync:
       environment:
         - PYTHONPATH=/cms/cms-api:/config/patches
   ```

3. 重建容器：
   ```sh
   cd /boot/config/plugins/compose.manager/projects/CMS && docker compose up -d cloud-media-sync
   ```

4. 验证（容器日志应出现）：
   ```
   STRM-GUARD installed on MediaSync.delete_local_file
   ```

## 回滚

删除 `config/patches/sitecustomize.py`、从 override 移除 `PYTHONPATH`，重启 CMS 容器即可。

## 安全设计

- 全程 `try/except`，任何异常只记日志，绝不阻断 CMS 启动或正常同步。
- 幂等：同一进程只安装一次（`_strm_guard` 标记）。
- 惰性安装：后台线程轮询 `sys.modules` 等待 `app.core.media_sync` 加载后再注入，不主动 import，避免启动时序与依赖链问题。
- 仅跳过 `/s/` 自有分享 strm 的删除；其余删除行为不变。
- 守卫挂钩韧性：若 `delete_local_file` 不存在（CMS 更新改方法名），会保守匹配"名字同时含 delete+local"的方法；找不到时记 `STRM-GUARD NOT INSTALLED: ...` 明确日志，让 verify.sh / doctor / Web UI 能发现守卫失效，而不是静默放弃。
- marker 常量在 4 处保持同步（sitecustomize.py / verify.sh / doctor.py / web_api.py），自定义需同时修改。

## CMS 升级标准流程（固定版本 + 显式升级）

CMS 固定到基线版本（如 `0.4.9.1`）后，升级永远走 `update-cms.sh` 显式指定新版本：

```sh
# 1. 查看 CMS 新版本（Docker Hub tags，或 Web UI 的 CMS 版本检测面板）
# 2. 显式升级到新版本（自动：备份 compose → 改标签 → pull → 重建 → 验证守卫 → 失败自动回滚）
./update-cms.sh /boot/config/plugins/compose.manager/projects/CMS 0.4.9.2
```

- 不传版本号 = 保持当前标签重装一遍（用于验证守卫部署正确）。
- 守卫验证失败（CMS 结构变化）→ 自动回滚到旧版本并报错，绝不把守卫失效的 CMS 留在线上。
- 若新版改了内部方法名：先更新 `sitecustomize.py` 的挂钩逻辑 → 重新部署到 `config/patches/` → 再跑 `update-cms.sh <目录> <新版本>`。
- 日常监控：Web UI「本地健康」页的 CMS STRM 守卫标签、`doctor.py` 的 `cms_strm_guard` 检查、`verify.sh` 任选其一，守卫失效会明确标红/FAIL。

## 文件清单

- `sitecustomize.py` — Python 启动钩子，注入 CMS 进程包裹删除方法。
- `verify.sh` — 一键检查守卫是否安装（本机或 SSH）。
- `update-cms.sh` — 安全升级 CMS（含守卫验证与自动回滚）。
