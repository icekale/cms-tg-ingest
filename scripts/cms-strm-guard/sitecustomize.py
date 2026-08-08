"""sitecustomize.py — CMS self-share STRM delete guard (root fix for #373).

背景
----
cms-tg-ingest 在共享 STRM 模式下，任务完成后会删除 115 转存源文件（设计行为，
自有永久分享仍在，播放不受影响）。但 CMS 的增量同步会消费 115 的 delete_file
生活事件，并删除本地对应文件——它不知道媒体库里的 strm 已被自有分享（/s/ 链接）
接管，于是把仍然有效的媒体库 strm 一起删掉（线上案例：龙族 S03E06 两次被误删）。

本补丁在 CMS 容器内对 `MediaSync.delete_local_file` 加守卫：删除本地 .strm 前
读取文件内容，若指向自有分享链接（/s/ 模式）则跳过删除。这样：
- 转存源文件照常从 115 删除（不浪费空间）；
- 媒体库/分享目录中仍指向有效自有分享的 strm 永远不会被 CMS 误删；
- 直链（/d/）与普通文件的删除行为不变；
- 自有分享真正失效时，由 cms-tg-ingest 的 self_share_health 巡检负责清理。

安装方式（Unraid Compose Manager）
----------------------------------
1. 把本文件放到 /mnt/user/appdata/cloud-media-sync/config/patches/sitecustomize.py
   （/config/patches 已挂载进 CMS 容器）。
2. 在 CMS 的 docker-compose.override.yml 给 cloud-media-sync 加环境变量：
       PYTHONPATH: /cms/cms-api:/config/patches
3. 重建/重启 cloud-media-sync 容器。
   Python 解释器启动时会自动执行 sitecustomize.py；守卫会轮询等待
   app.core.media_sync 加载完成后安装（不 import、无启动时序风险）。

验证：CMS 容器日志出现 "STRM-GUARD installed on MediaSync.delete_local_file"。
回滚：删除 sitecustomize.py 并从 override 移除 PYTHONPATH，重启容器即可。

安全设计
--------
- 全程 try/except，任何异常只记日志，绝不影响 CMS 启动与正常同步。
- 幂等：同一进程只安装一次（_strm_guard 标记）。
- 只跳过 /s/ 自有分享 strm 的删除；直链、非 strm、无 /s/ 链接的文件照常删除。
"""
import logging
import re
import sys
import threading
import time

logger = logging.getLogger("cms.strm-guard")

_SELF_SHARE_URL_RE = re.compile(r"/s/[A-Za-z0-9]+_[A-Za-z0-9]+_\d+")


def _install_guard(ms_cls) -> bool:
    original = getattr(ms_cls, "delete_local_file", None)
    if original is None or getattr(original, "_strm_guard", False):
        return False

    def delete_local_file_guarded(self, item_db):
        local_path = ""
        try:
            local_path = str(getattr(item_db, "local_path", "") or "")
        except Exception:
            pass
        if local_path.lower().endswith(".strm"):
            try:
                with open(local_path, "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(512)
                if _SELF_SHARE_URL_RE.search(head):
                    logger.warning("STRM-GUARD skip delete self-share STRM: %s", local_path)
                    return None
            except OSError:
                pass
            except Exception:
                pass
        return original(self, item_db)

    delete_local_file_guarded._strm_guard = True
    setattr(ms_cls, "delete_local_file", delete_local_file_guarded)
    logger.warning("STRM-GUARD installed on MediaSync.delete_local_file")
    return True


def _worker() -> None:
    # 冷启动时 app.core.media_sync 尚未加载；轮询 sys.modules 等待，不主动 import，
    # 避免依赖链（telebot/urllib3 等）与启动时序问题。最多等 10 分钟。
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            mod = sys.modules.get("app.core.media_sync")
            if mod is not None:
                ms_cls = getattr(mod, "MediaSync", None)
                if ms_cls is not None and _install_guard(ms_cls):
                    return
        except Exception:
            logger.exception("STRM-GUARD worker error")
        time.sleep(1.0)


try:
    threading.Thread(target=_worker, name="cms-strm-guard", daemon=True).start()
except Exception:
    logger.exception("STRM-GUARD failed to start worker")
