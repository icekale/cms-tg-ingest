"""sitecustomize.py — CMS self-share STRM guards (root fix for #373 + direct-STRM suppression).

背景
----
cms-tg-ingest 在共享 STRM 模式下，任务完成后会删除 115 转存源文件（设计行为，
自有永久分享仍在，播放不受影响）。但 CMS 的增量同步会消费 115 的 delete_file
生活事件，并删除本地对应文件——它不知道媒体库里的 strm 已被自有分享（/s/ 链接）
接管，于是把仍然有效的媒体库 strm 一起删掉（线上案例：龙族 S03E06 两次被误删）。

同时，CMS 的 auto_organize 会先把云文件落地为媒体库"直链 strm"（/d/ 链接），
随后 cms-tg-ingest 才用自有分享 strm（/s/ 链接）接管并删除直链 strm。媒体库里
短暂出现又消失的 /d/ 直链会被 Emby 扫到，且直链在转存源删除后即失效，体验很差。

本补丁在 CMS 容器内安装两个守卫（都 monkey patch app.core.media_sync）：
1. 删除守卫（MediaSync.delete_local_file）：删除本地 .strm 前读取文件内容，
   若指向自有分享链接（/s/ 模式）则跳过删除。
2. 直链拦截守卫（模块级 create_strm_file(file_path, content)）：所有 strm 文件
   都由该函数写出（build_direct / save_file / save_video / sync_file_to_local
   均调用它）。先原样调用原函数（不破坏 CMS 状态机），再校验 content 与落盘路径：
   若指向直链（/d/ 模式）且路径在媒体库根目录（STRM_GUARD_LIBRARY_ROOTS）内，
   立即删除该文件（"先写后删"），让媒体库从始至终只出现 /s/ 共享 strm。

这样：
- 转存源文件照常从 115 删除（不浪费空间）；
- 媒体库/分享目录中仍指向有效自有分享的 strm 永远不会被 CMS 误删；
- 直链（/d/）与普通文件的删除行为不变；
- 媒体库不再出现"先直链后共享再删除"的中间态；
- 自有分享真正失效时，由 cms-tg-ingest 的 self_share_health 巡检负责清理。

安装方式（Unraid Compose Manager）
----------------------------------
1. 把本文件放到 /mnt/user/appdata/cloud-media-sync/config/patches/sitecustomize.py
   （/config/patches 已挂载进 CMS 容器）。
2. 在 CMS 的 docker-compose.override.yml 给 cloud-media-sync 加环境变量：
       PYTHONPATH: /cms/cms-api:/config/patches
       STRM_GUARD_LIBRARY_ROOTS: /mnt/user/Unraid/strm/转存
   （STRM_GUARD_LIBRARY_ROOTS 是媒体库 strm 根目录，逗号分隔可多个；
   直链拦截守卫只在此目录内生效，分享目录不受影响。不设则默认
   /mnt/user/Unraid/strm/转存；显式设为空字符串可整体关闭直链拦截。）
3. 重建/重启 cloud-media-sync 容器。
   Python 解释器启动时会自动执行 sitecustomize.py；守卫会轮询等待
   app.core.media_sync 加载完成后安装（不 import、无启动时序风险）。

验证：CMS 容器日志应同时出现
   "STRM-GUARD installed on MediaSync.delete_local_file" 和
   "STRM-GUARD direct-strm-suppressor installed on app.core.media_sync.create_strm_file"。
回滚：删除 sitecustomize.py 并从 override 移除 PYTHONPATH（及
STRM_GUARD_LIBRARY_ROOTS），重启容器即可。

安全设计
--------
- 全程 try/except，任何异常只记日志，绝不影响 CMS 启动与正常同步。
- 幂等：同一进程每个守卫只安装一次（_strm_guard / _strm_direct_guard 标记）。
- 惰性安装：后台线程轮询 sys.modules 等待 app.core.media_sync 加载后再注入，
  不主动 import，避免启动时序与依赖链问题。
- 直链拦截守卫只删 /d/ 直链且只在媒体库根目录内生效；/s/ 共享 strm、非 strm、
  媒体库外的文件一律不动。
- 守卫挂钩韧性：删除守卫找不到 delete_local_file 时保守匹配"名字同时含
  delete+local"的方法；直链守卫直接挂钩模块级 create_strm_file（2026-08 版
  实测确认存在），不存在时记 STRM-GUARD NOT INSTALLED 明确日志，让
  verify.sh / doctor / Web UI 能发现守卫失效，而不是静默放弃。
"""
import logging
import os
import re
import sys
import threading
import time

logger = logging.getLogger("cms.strm-guard")

_SELF_SHARE_URL_RE = re.compile(r"/s/[A-Za-z0-9]+_[A-Za-z0-9]+_\d+")
_DIRECT_LINK_MARK = "/d/"

# 直链拦截守卫要 patch 的模块级写入函数：所有 strm 文件都经
# app.core.media_sync.create_strm_file(file_path, content) 写出
# （build_direct / save_file / save_video / sync_file_to_local 均调用它）。
# CMS 升级后若此函数被移除或改名，守卫会记 NOT INSTALLED 明确日志。
_DIRECT_STRM_WRITER = "create_strm_file"


def _library_roots() -> list[str]:
    raw = os.environ.get("STRM_GUARD_LIBRARY_ROOTS", "/mnt/user/Unraid/strm/转存").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _within_library_roots(path: str) -> bool:
    roots = _library_roots()
    if not roots or not path:
        return False
    try:
        target = os.path.realpath(path)
    except Exception:
        return False
    for root in roots:
        try:
            base = os.path.realpath(root)
        except Exception:
            continue
        if not base:
            continue
        try:
            if os.path.commonpath([target, base]) == base:
                return True
        except ValueError:
            continue
    return False


def _install_guard(ms_cls) -> bool:
    original = getattr(ms_cls, "delete_local_file", None)
    if original is None:
        # CMS 更新可能改了方法名；保守匹配"名字同时含 delete 和 local"的方法，
        # 只 patch 一个，避免误伤其它删除路径。
        candidates = [
            name
            for name in dir(ms_cls)
            if "delete" in name.lower() and "local" in name.lower()
        ]
        if len(candidates) == 1:
            original = getattr(ms_cls, candidates[0], None)
            patched_name = candidates[0]
        else:
            logger.warning(
                "STRM-GUARD NOT INSTALLED: MediaSync.delete_local_file 不存在，"
                "候选=%s（CMS 结构可能已变，请更新本守卫）",
                candidates,
            )
            return False
    else:
        patched_name = "delete_local_file"
    if getattr(original, "_strm_guard", False):
        return False

    def delete_local_file_guarded(self, *args, **kwargs):
        # 签名透传：不假设 CMS 的方法签名。若能在 args 里取到 item_db 且其
        # local_path 是 /s/ 自有分享 strm，则跳过删除；否则原样调用原方法，
        # 保证 CMS 更新后即使签名变化，删除流程也绝不因守卫崩溃。
        local_path = ""
        try:
            item_db = args[0] if args else kwargs.get("item_db")
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
        return original(self, *args, **kwargs)

    delete_local_file_guarded._strm_guard = True
    setattr(ms_cls, patched_name, delete_local_file_guarded)
    # 保持标准 marker（verify.sh / doctor.py / web_api.py 均按此检测），
    # 附加实际方法名便于排查。若想自定义 marker，需同步修改这 4 处。
    logger.warning(
        "STRM-GUARD installed on MediaSync.delete_local_file (patched=%s)", patched_name
    )
    return True


def _install_direct_strm_guard(mod) -> bool:
    """直链拦截守卫：strm 写出后校验，/d/ 直链且落在媒体库根目录则立即删除（先写后删）。

    Patch 模块级 create_strm_file(file_path, content) —— 2026-08 版 CMS 实测确认
    所有 strm 文件（build_direct / save_file / save_video / sync_file_to_local）
    都经此函数写出，是唯一咽喉；file_path 与 content 直接从参数获取，无需猜签名。
    """
    original = getattr(mod, _DIRECT_STRM_WRITER, None)
    if original is None or not callable(original):
        logger.warning(
            "STRM-GUARD direct-strm-suppressor NOT INSTALLED: app.core.media_sync.%s "
            "不存在（CMS 结构可能已变，请更新本守卫）",
            _DIRECT_STRM_WRITER,
        )
        return False
    if getattr(original, "_strm_direct_guard", False):
        return False

    def direct_strm_suppressor(file_path, content, *args, **kwargs):
        # 先写后删：先原样调用原函数（不破坏 CMS 状态机与返回值），再校验落盘内容。
        result = original(file_path, content, *args, **kwargs)
        try:
            path = os.fspath(file_path)
            text = str(content or "")
            if not path.lower().endswith(".strm"):
                return result
            if _DIRECT_LINK_MARK not in text:
                return result
            if not _within_library_roots(path):
                return result
            try:
                os.unlink(path)
                logger.warning(
                    "STRM-GUARD direct-strm-suppressor removed direct STRM: %s", path
                )
            except OSError:
                logger.warning(
                    "STRM-GUARD direct-strm-suppressor failed to remove %s", path
                )
        except Exception:
            logger.exception("STRM-GUARD direct-strm-suppressor error")
        return result

    direct_strm_suppressor._strm_direct_guard = True
    setattr(mod, _DIRECT_STRM_WRITER, direct_strm_suppressor)
    # 独立 marker：verify.sh / doctor.py / web_api.py 按此检测；自定义需同步 4 处。
    logger.warning(
        "STRM-GUARD direct-strm-suppressor installed on app.core.media_sync.%s "
        "(patched=%s)",
        _DIRECT_STRM_WRITER,
        _DIRECT_STRM_WRITER,
    )
    return True


def _worker() -> None:
    # 冷启动时 app.core.media_sync 尚未加载；轮询 sys.modules 等待，不主动 import，
    # 避免依赖链（telebot/urllib3 等）与启动时序问题。最多等 10 分钟；超时前
    # 找不到模块会记明确失败日志，让 verify.sh/doctor 能区分"已安装"和"尝试过但失败"。
    deadline = time.time() + 600
    saw_module = False
    while time.time() < deadline:
        try:
            mod = sys.modules.get("app.core.media_sync")
            if mod is not None:
                saw_module = True
                ms_cls = getattr(mod, "MediaSync", None)
                if ms_cls is None:
                    logger.warning("STRM-GUARD NOT INSTALLED: app.core.media_sync 无 MediaSync 类")
                    return
                _install_guard(ms_cls)
                _install_direct_strm_guard(mod)
                return  # 两个守卫的安装结果都已记日志
        except Exception:
            logger.exception("STRM-GUARD worker error")
        time.sleep(1.0)
    if not saw_module:
        logger.warning(
            "STRM-GUARD NOT INSTALLED: 600s 内未加载 app.core.media_sync"
            "（模块未导入或 CMS 结构已变），守卫未生效"
        )


try:
    threading.Thread(target=_worker, name="cms-strm-guard", daemon=True).start()
except Exception:
    logger.exception("STRM-GUARD failed to start worker")
