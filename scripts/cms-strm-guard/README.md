# CMS STRM 守卫（sitecustomize 补丁）

根治两个 CMS 侧问题：增量同步误删自有分享 STRM（线上案例：龙族 S03E06 两次被误删），
以及媒体库"先入库直链 strm、再用共享 strm 替换"的中间态。

## 问题根因

**问题一（删除）**：共享 STRM 模式下，cms-tg-ingest 在任务完成并通过 115 异步审核观察期后，会删除 115 转存源文件（设计行为，自有永久分享仍在，播放不受影响）。但 CMS 的增量同步会轮询 115 生活事件（`delete_file`），并按 fid 删除本地对应文件——它不感知"媒体库 strm 已被自有分享 `/s/` 链接接管"，于是把仍有效的 strm 一并删除，Emby 随即上报 `library.deleted`。

CMS 的生活事件消费在每次 `auto_organize` / `share115` 同步时顺带触发（独立 `SYNC_CRON` 常被禁用），所以误删可能在几小时后才发生，难以预料。

**问题二（直链中间态）**：CMS 的 `auto_organize` 会先把云文件落地为媒体库"直链 strm"（`/d/` 链接）。cms-tg-ingest 随后才生成自有分享 strm（`/s/`）并覆盖替换、删除直链——媒体库短暂出现又消失的 `/d/` 直链会被 Emby 扫到，且转存源删除后直链即失效。

## 方案：CMS 容器内加两个守卫

`sitecustomize.py` 以 Python 启动钩子注入 CMS 进程，monkey patch `MediaSync`：

1. **删除守卫**（`delete_local_file`）：删除本地 `.strm` 前读取文件内容，若指向自有分享链接（`/s/...` 模式）则**跳过删除**；
2. **直链拦截守卫**（strm 写入方法，先写后删）：写入媒体库 `.strm` 后读取内容，若指向直链（`/d/` 模式）且路径在媒体库根目录（`STRM_GUARD_LIBRARY_ROOTS`）内则**立即删除**——媒体库从始至终只出现 `/s/` 共享 strm。

两者共同保障：
- 转存源文件照常从 115 删除（不浪费空间）；
- 媒体库/分享目录中仍指向有效自有分享的 strm 永远不会被 CMS 误删；
- 直链（`/d/`）与普通文件的删除行为不变（直链拦截守卫只删媒体库内新写出的 /d/ strm）；
- 自有分享真正失效时，由 cms-tg-ingest 的 `self_share_health` 巡检负责清理。

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
         # 媒体库 strm 根目录（直链拦截守卫只在此目录内生效）；逗号分隔可多个。
         # 不设默认 /mnt/user/Unraid/strm/转存；显式设为空字符串可整体关闭直链拦截。
         - STRM_GUARD_LIBRARY_ROOTS=/mnt/user/Unraid/strm/转存
   ```

3. 重建容器：
   ```sh
   cd /boot/config/plugins/compose.manager/projects/CMS && docker compose up -d cloud-media-sync
   ```

4. 验证（容器日志应同时出现两个标记）：
   ```
   STRM-GUARD installed on MediaSync.delete_local_file
   STRM-GUARD direct-strm-suppressor installed on app.core.media_sync.create_strm_file
   ```

5. 建议同步关闭 cms-tg-ingest 侧的直链修复循环（它会在共享 strm 缺失时用 /d/ 直链
   把 strm 写回媒体库，与直链拦截守卫目标冲突）：
   ```
   MEDIA_STRM_REPAIR_ENABLED=false
   ```

## 回滚

删除 `config/patches/sitecustomize.py`、从 override 移除 `PYTHONPATH`（及
`STRM_GUARD_LIBRARY_ROOTS`），重启 CMS 容器即可；如需恢复直链兜底修复，同时把
`MEDIA_STRM_REPAIR_ENABLED` 设回 `true`。

## 安全设计

- 全程 `try/except`，任何异常只记日志，绝不阻断 CMS 启动或正常同步。
- 幂等：同一进程每个守卫只安装一次（`_strm_guard` / `_strm_direct_guard` 标记）。
- 惰性安装：后台线程轮询 `sys.modules` 等待 `app.core.media_sync` 加载后再注入，不主动 import，避免启动时序与依赖链问题。
- 删除守卫仅跳过 `/s/` 自有分享 strm 的删除；直链拦截守卫只删 `/d/` 直链且只在媒体库根目录内生效；其余行为不变。
- 守卫挂钩韧性：删除守卫找不到 `delete_local_file` 时保守匹配"名字同时含 delete+local"的方法；直链拦截守卫直接挂钩模块级 `create_strm_file(file_path, content)`（2026-08 版 CMS 实测确认所有 strm 文件——build_direct / save_file / save_video / sync_file_to_local——都经它写出），不存在时记 `STRM-GUARD NOT INSTALLED: ...` 明确日志，让 verify.sh / doctor / Web UI 能发现守卫失效，而不是静默放弃。
- marker 常量在 4 处保持同步（sitecustomize.py / verify.sh / doctor.py / web_api.py），自定义需同时修改。

## CMS 升级标准流程（固定版本 + 显式升级）

CMS 固定到基线版本（如 `0.4.9.1`）后，升级永远走 `update-cms.sh` 显式指定新版本：

```sh
# 1. 查看 CMS 新版本（Docker Hub tags，或 Web UI 的 CMS 版本检测面板）
# 2. 显式升级到新版本（自动：备份 compose → 改标签 → pull → 重建 → 验证守卫 → 失败自动回滚）
#    Unraid 上守卫文件不在 compose 目录里，需用 CMS_GUARD_FILE 指定：
CMS_GUARD_FILE=/mnt/user/appdata/cloud-media-sync/config/patches/sitecustomize.py \
  ./update-cms.sh /boot/config/plugins/compose.manager/projects/CMS 0.4.9.2
```

- `CMS_GUARD_FILE` 指向 `sitecustomize.py`（Unraid 上位于 `/mnt/user/appdata/cloud-media-sync/config/patches/`，与 compose 项目目录不同；不指定时脚本默认在 compose 目录下找，找不到会拒绝更新）。`update-cms.sh` 依赖同目录的 `verify.sh`，两个文件需一起部署。
- 不传版本号 = 保持当前标签重装一遍（用于验证守卫部署正确）。
- 守卫验证失败（CMS 结构变化）→ 自动回滚到旧版本并报错，绝不把守卫失效的 CMS 留在线上。
- 若新版改了内部方法名：先更新 `sitecustomize.py` 的挂钩逻辑 → 重新部署到 `config/patches/` → 再跑 `update-cms.sh <目录> <新版本>`。
- 日常监控：Web UI「本地健康」页的 CMS STRM 守卫 / CMS 直链拦截守卫标签、`doctor.py` 的 `cms_strm_guard` / `cms_direct_strm_guard` 检查、`verify.sh` 任选其一，守卫失效会明确标红/FAIL。

## 文件清单

- `sitecustomize.py` — Python 启动钩子，注入 CMS 进程包裹删除方法与直链 strm 写入方法。
- `verify.sh` — 一键检查两个守卫是否安装（本机或 SSH）。
- `update-cms.sh` — 安全升级 CMS（含守卫验证与自动回滚）。
