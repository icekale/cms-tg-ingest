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
