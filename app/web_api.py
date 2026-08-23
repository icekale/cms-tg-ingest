from __future__ import annotations

import http.client
import json
import re
import socket
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from .background_jobs import redact_background_text
from .config import DEFAULT_OWN_SHARE_RECEIVE_CODE
from .hdhive_subscriptions import diagnose_subscription_check
from .logging_system import LogFilter, redact_text
from .media.strm import iter_strm_files
from .models import TaskSnapshot, TaskStage, TaskStatus
from .quality_rules import QUALITY_RULE_VERSION, QualityRuleEngine, quality_attempt_count
from .task_diagnostics import explain_task_slowness, format_stage_observability
from .task_health import build_task_health
from .quality import redact_quality_detail, scan_task_quality
from .strm_mode import effective_task_strm_mode
from .task_actions import available_lifecycle_actions, available_task_actions, task_termination_requested
from .task_store import TaskStore


_SENSITIVE_QUERY_KEYS = {
    "password",
    "passwd",
    "pwd",
    "code",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "share_password",
    "share_pwd",
    "hdhive_token",
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "bearer_token",
    "session_token",
    "csrf_token",
    "p115_cookie",
}
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|access_token|refresh_token|cookie|share[-_]?password|share[-_]?pwd|hdhive[-_]?token|api[-_]?key|authorization|auth_token|bearer_token|session_token|csrf_token|p115_cookie)\s*([:=])\s*([^\s&;,]+)"
)
_SENSITIVE_METADATA_KEYS = {
    "authorization",
    "cookie",
    "p115_cookie",
    "access_token",
    "refresh_token",
    "hdhive_token",
    "token",
    "receive_code",
    "own_share_receive_code",
    "password",
    "share_password",
    "share_pwd",
    "api_key",
    "apikey",
    "auth_token",
    "bearer_token",
    "session_token",
    "csrf_token",
    "own_share_url",
}
_SENSITIVE_METADATA_KEY_SUFFIXES = (
    "_token",
    "_api_key",
    "_cookie",
    "_password",
    "_passwd",
    "_pwd",
    "_secret",
)
_OWN_SHARE_RECEIVE_CODE_SOURCES = {"web", "cms", "env", "default"}


def _safe_error(value: Any) -> str:
    redacted = redact_background_text(redact_text(value or ""))
    redacted = redacted.replace("[REDACTED]", "***").replace("[redacted]", "***")
    return _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2***", redacted)


def _safe_url(value: str) -> str:
    """Keep links useful to the UI without returning share passwords or tokens."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    try:
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        hostname = ""
        port = None
    if not hostname:
        return ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    safe_netloc = hostname + (f":{port}" if port is not None else "")
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _SENSITIVE_QUERY_KEYS or _is_sensitive_metadata_key(key):
            query.append((key, "***"))
        else:
            query.append((key, item))
    encoded_query = "&".join(f"{quote(key)}={quote(item, safe='*')}" for key, item in query)
    return urlunsplit((parsed.scheme, safe_netloc, parsed.path, encoded_query, ""))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def task_display_title(task: Any) -> str:
    metadata = getattr(task, "metadata", {}) or {}
    organized = metadata.get("organized_folder")
    if isinstance(organized, dict):
        folder_name = str(organized.get("file_name") or "").strip()
        if folder_name:
            return folder_name
    for key in ("own_share_file_name", "dest_path", "source_path", "emby_path"):
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue
        if key.endswith("_path"):
            name = Path(value).name
            if name:
                return name
        return value
    title = str(getattr(task, "title", "") or "").strip()
    if title and not title.startswith(("http://", "https://")):
        return title
    return str(getattr(task, "share_code", "") or title or "-")


def _completion_drift_recommendation(mode: str) -> str:
    if mode == "shared":
        return "系统会尝试用自有分享 STRM 恢复；恢复后刷新 Emby。"
    return "请检查 STRM 内容和媒体库挂载，并让系统重新执行 Emby 入库确认。"


def completion_drift_for_task(task: TaskSnapshot) -> dict[str, str] | None:
    """Report a live filesystem mismatch without mutating the persisted task."""
    if task.status != TaskStatus.SUCCEEDED or task.current_stage != TaskStage.CLEANED:
        return None
    try:
        mode = effective_task_strm_mode(task)
    except ValueError:
        mode = "shared"
    recommendation = _completion_drift_recommendation(mode)
    dest_path = str(task.metadata.get("dest_path") or "").strip()
    if not dest_path:
        return {
            "code": "missing_dest",
            "message": "已入库但当前媒体目录缺失",
            "detail": "任务未保存目标媒体目录",
            "recommendation": recommendation,
        }
    try:
        destination = Path(dest_path)
        if not destination.is_dir():
            return {
                "code": "missing_dest",
                "message": "已入库但当前媒体目录缺失",
                "detail": "目标媒体目录不存在",
                "recommendation": recommendation,
            }
        strm_files = iter_strm_files(destination)
        try:
            first_file = next(strm_files)
        except StopIteration:
            first_file = None
        if first_file is None:
            return {
                "code": "missing_strm",
                "message": "已入库但当前媒体目录没有 STRM",
                "detail": "目标媒体目录中未找到 STRM 文件",
                "recommendation": recommendation,
            }
        expected_code = str(task.metadata.get("own_share_code") or "").strip()
        receive_code = str(task.metadata.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        expected_marker = f"/s/{expected_code}_{receive_code}_" if expected_code else "/s/"
        has_unexpected = False
        for path in (first_file, *strm_files):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                has_unexpected = True
                continue
            if mode == "direct":
                matches = "/d/" in text
            elif mode == "source_shared":
                matches = "/s/" in text
            else:
                matches = expected_marker in text
            has_unexpected = has_unexpected or not matches
        if not has_unexpected:
            return None
        return {
            "code": "unexpected_strm",
            "message": "已入库但 STRM 内容与任务模式不匹配",
            "detail": "目标媒体目录中的 STRM 不是预期来源",
            "recommendation": recommendation,
        }
    except (OSError, RuntimeError):
        return {
            "code": "missing_dest",
            "message": "已入库但当前媒体目录缺失",
            "detail": "目标媒体目录无法访问",
            "recommendation": recommendation,
        }
    return None


def _safe_container_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _safe_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_safe_container_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _safe_url(value)
        return _safe_error(value)
    return value


def _is_sensitive_metadata_key(value: Any) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_METADATA_KEYS or normalized.endswith(_SENSITIVE_METADATA_KEY_SUFFIXES)


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if _is_sensitive_metadata_key(key):
            continue
        result[str(key)] = _safe_container_value(value)
    return result


def _safe_api_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key == "own_share_receive_code" and isinstance(item, dict):
                configured = bool(item.get("configured"))
                source = str(item.get("source") or "").strip().lower()
                result[key] = {
                    "configured": configured,
                    "masked": "****" if configured else "",
                    "source": source if source in _OWN_SHARE_RECEIVE_CODE_SOURCES else "",
                }
                continue
            if normalized_key in {"emby_credentials", "tmdb_credentials"} and isinstance(item, dict):
                # 凭据 payload 里的 key 已是脱敏值（integration_credentials 的
                # masked_payload），不能再被下方敏感 key 规则整值替换成 ***。
                result[key] = _safe_api_value({k: v for k, v in item.items() if k != "api_key" and k != "bearer_token"})
                for secret_key in ("api_key", "bearer_token"):
                    if secret_key in item:
                        result[key][secret_key] = str(item[secret_key] or "")
                continue
            if _is_sensitive_metadata_key(normalized_key):
                result[key] = "***"
                continue
            result[key] = _safe_api_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_api_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _safe_url(value)
        return _safe_error(value)
    return value


def _organized_target_summary(task: TaskSnapshot) -> list[dict[str, str]]:
    raw_targets = task.metadata.get("organized_targets")
    if not isinstance(raw_targets, list):
        return []
    summary = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            continue
        folder = raw.get("folder") if isinstance(raw.get("folder"), dict) else {}
        recognition = raw.get("recognition") if isinstance(raw.get("recognition"), dict) else {}
        share = raw.get("share") if isinstance(raw.get("share"), dict) else {}
        strm = raw.get("strm") if isinstance(raw.get("strm"), dict) else {}
        summary.append(
            {
                "target_id": str(raw.get("target_id") or ""),
                "folder_name": str(folder.get("file_name") or ""),
                "tmdb_id": str(recognition.get("tmdb_id") or ""),
                "category": str(recognition.get("category") or ""),
                "share_status": str(share.get("status") or ""),
                "sync_status": str(share.get("sync_status") or ""),
                "strm_status": str(strm.get("status") or ""),
                "move_status": str(strm.get("move_status") or ""),
                "emby_status": str(strm.get("emby_status") or ""),
            }
        )
    return summary


def serialize_task(
    task: TaskSnapshot,
    *,
    now: float | None = None,
    lifecycle_actions_enabled: bool = True,
    max_retries: int = 3,
    include_completion_drift: bool = False,
    task_store: TaskStore | None = None,
) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    elapsed, p115_calls = format_stage_observability(task)
    termination_requested = task_termination_requested(task)
    if lifecycle_actions_enabled:
        available_actions = sorted(
            available_lifecycle_actions(task) | available_task_actions(task, max_retries=max_retries, store=task_store)
        )
    else:
        available_actions = []
    try:
        strm_mode = effective_task_strm_mode(task)
    except ValueError:
        # A single task with an invalid persisted strm_mode must not 500 the
        # whole task list / detail / health API surface.
        strm_mode = "shared"
    return _safe_api_value({
        "id": task.id,
        "title": task.title or task.share_code,
        "display_title": task_display_title(task),
        "source_type": task.source_type,
        "stage": _enum_value(task.current_stage),
        "status": _enum_value(task.status),
        "strm_mode": strm_mode,
        "category": task.category or task.metadata.get("category") or "",
        "tmdb_id": task.tmdb_id or task.metadata.get("tmdb_id") or "",
        "safe_url": _safe_url(task.url),
        "error": {"type": task.error_type, "summary": _safe_error(task.error_summary)},
        "retry_count": task.retry_count,
        "next_run_at": task.next_run_at,
        "claimed": bool(task.claimed_by),
        "available_actions": available_actions,
        "organized_targets": _organized_target_summary(task),
        "termination_requested": termination_requested,
        "why_slow": explain_task_slowness(task, now=current_time),
        "stage_elapsed": elapsed,
        "stage_p115_calls": p115_calls,
        "completion_drift": completion_drift_for_task(task) if include_completion_drift else None,
        "metadata": _safe_metadata(task.metadata),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    })


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    return _safe_api_value({
        "id": int(event.get("id") or 0),
        "stage": str(event.get("stage") or ""),
        "status": str(event.get("status") or ""),
        "message": _safe_error(event.get("message")),
        "error_type": str(event.get("error_type") or ""),
        "created_at": float(event.get("created_at") or 0),
    })


# 注意：此 marker 与 sitecustomize.py / verify.sh / doctor.py 保持一致；
# 若自定义 marker，需同步修改这 4 处。
CMS_STRM_GUARD_MARKER = "STRM-GUARD installed on MediaSync.delete_local_file"
CMS_DIRECT_STRM_GUARD_MARKER = "STRM-GUARD direct-strm-suppressor installed on"
CMS_OS_STRM_GUARD_MARKER = "STRM-GUARD os-level delete-protect installed on"


class _UnixDockerLogReader:
    """Read recent container logs through the Docker Engine API over a unix socket."""

    def __init__(self, socket_path: str = "/var/run/docker.sock", timeout: float = 3.0):
        self.socket_path = str(socket_path or "").strip()
        self.timeout = float(timeout or 3.0)

    def read_logs(self, container: str, tail: int = 100000) -> str:
        # tail 必须足够大：守卫 marker 只在容器启动时打印一次，CMS 日志跨天
        # 累积（约数千行/天），tail 太小会把 marker 挤出窗口造成假阳性。
        if not self.socket_path or not str(container or "").strip():
            return ""
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            conn = http.client.HTTPConnection("localhost", timeout=self.timeout)
            conn.sock = sock
            path = f"/containers/{quote(str(container), safe='')}/logs?stdout=1&stderr=1&tail={int(tail)}"
            conn.request("GET", path)
            response = conn.getresponse()
            status = int(response.status or 0)
            body = response.read()
            conn.close()
            if status != 200:
                return ""
            return body.decode("utf-8", "replace")
        except Exception:
            return ""
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


_cms_guard_cache: dict[str, Any] = {"at": 0.0, "value": None}
_cms_direct_guard_cache: dict[str, Any] = {"at": 0.0, "value": None}
_cms_os_guard_cache: dict[str, Any] = {"at": 0.0, "value": None}


def check_cms_strm_guard(
    *,
    workflow_mode: str = "",
    container: str = "cloud-media-sync",
    docker_socket: str = "/var/run/docker.sock",
    marker: str = CMS_STRM_GUARD_MARKER,
    log_reader: Any | None = None,
    cache_seconds: float = 60.0,
) -> dict[str, Any]:
    """Return the CMS self-share STRM delete-guard status for the health API.

    Never raises and never blocks the health endpoint: docker socket failures
    degrade to ``status="unknown"`` and the result is cached in-process.
    """
    if (workflow_mode or "direct") != "self_share_sync":
        return {
            "ok": True,
            "status": "not_applicable",
            "message": "guard only relevant for self_share_sync",
        }
    now = time.time()
    cache = _cms_guard_cache
    if cache["value"] is not None and now - cache["at"] < max(0.0, float(cache_seconds or 0)):
        return dict(cache["value"])

    reader = log_reader or _UnixDockerLogReader(socket_path=docker_socket)
    logs = reader.read_logs(container, tail=100000)
    if not logs:
        result = {
            "ok": True,
            "status": "unknown",
            "message": "无法读取 CMS 容器日志（docker socket 不可用或容器不存在）；守卫状态未知",
        }
    elif marker in logs:
        result = {"ok": True, "status": "installed", "message": "CMS STRM 删除守卫已安装"}
    else:
        result = {
            "ok": False,
            "status": "missing",
            "message": "CMS 容器日志未找到 STRM 删除守卫标记；CMS 更新可能导致守卫静默失效",
        }
    cache.update({"at": now, "value": result})
    return dict(result)


def check_cms_direct_strm_guard(
    *,
    workflow_mode: str = "",
    container: str = "cloud-media-sync",
    docker_socket: str = "/var/run/docker.sock",
    marker: str = CMS_DIRECT_STRM_GUARD_MARKER,
    log_reader: Any | None = None,
    cache_seconds: float = 60.0,
) -> dict[str, Any]:
    """Return the CMS direct-STRM suppression guard status for the health API.

    Same semantics as ``check_cms_strm_guard`` but for the /d/ direct-STRM
    suppression guard (media library only ever keeps /s/ self-share STRM).
    """
    if (workflow_mode or "direct") != "self_share_sync":
        return {
            "ok": True,
            "status": "not_applicable",
            "message": "guard only relevant for self_share_sync",
        }
    now = time.time()
    cache = _cms_direct_guard_cache
    if cache["value"] is not None and now - cache["at"] < max(0.0, float(cache_seconds or 0)):
        return dict(cache["value"])

    reader = log_reader or _UnixDockerLogReader(socket_path=docker_socket)
    logs = reader.read_logs(container, tail=100000)
    if not logs:
        result = {
            "ok": True,
            "status": "unknown",
            "message": "无法读取 CMS 容器日志（docker socket 不可用或容器不存在）；守卫状态未知",
        }
    elif marker in logs:
        result = {"ok": True, "status": "installed", "message": "CMS 直链 STRM 拦截守卫已安装"}
    else:
        result = {
            "ok": False,
            "status": "missing",
            "message": "CMS 容器日志未找到直链 STRM 拦截守卫标记；CMS 更新可能导致守卫静默失效",
        }
    cache.update({"at": now, "value": result})
    return dict(result)


def check_cms_os_strm_guard(
    *,
    workflow_mode: str = "",
    container: str = "cloud-media-sync",
    docker_socket: str = "/var/run/docker.sock",
    marker: str = CMS_OS_STRM_GUARD_MARKER,
    log_reader: Any | None = None,
    cache_seconds: float = 60.0,
) -> dict[str, Any]:
    """Return the CMS os-level STRM delete-protect guard status for the health API.

    os.remove/os.unlink 兜底守卫是方法级守卫的补充：CMS 增量同步（消费 115
    delete_file 生活事件）的本地删除未必经过 MediaSync.delete_local_file，
    os 级钩子覆盖一切删除路径。缺失即方法级守卫可能仍被旁路。
    """
    if (workflow_mode or "direct") != "self_share_sync":
        return {
            "ok": True,
            "status": "not_applicable",
            "message": "guard only relevant for self_share_sync",
        }
    now = time.time()
    cache = _cms_os_guard_cache
    if cache["value"] is not None and now - cache["at"] < max(0.0, float(cache_seconds or 0)):
        return dict(cache["value"])

    reader = log_reader or _UnixDockerLogReader(socket_path=docker_socket)
    logs = reader.read_logs(container, tail=100000)
    if not logs:
        result = {
            "ok": True,
            "status": "unknown",
            "message": "无法读取 CMS 容器日志（docker socket 不可用或容器不存在）；守卫状态未知",
        }
    elif marker in logs:
        result = {"ok": True, "status": "installed", "message": "CMS STRM 删除兜底守卫已安装"}
    else:
        result = {
            "ok": False,
            "status": "missing",
            "message": "CMS 容器日志未找到 STRM 删除兜底守卫标记；CMS 更新可能导致守卫静默失效",
        }
    cache.update({"at": now, "value": result})
    return dict(result)


def _reset_cms_guard_cache() -> None:
    _cms_guard_cache.update({"at": 0.0, "value": None})
    _cms_direct_guard_cache.update({"at": 0.0, "value": None})
    _cms_os_guard_cache.update({"at": 0.0, "value": None})


def serialize_health(
    store: TaskStore,
    *,
    enabled: bool = True,
    now: float | None = None,
    cms_guard: dict[str, Any] | None = None,
    cms_direct_guard: dict[str, Any] | None = None,
    cms_os_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    summary = build_task_health(store, enabled=enabled, now=current_time)
    backup = {"status": "never", "file_count": 0, "skipped_count": 0, "error": ""}
    backup_state = store.get_runtime_state("backup_last_result")
    if backup_state:
        try:
            parsed_backup = json.loads(str(backup_state.get("value") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_backup = {}
        if isinstance(parsed_backup, dict):
            backup = {
                "status": str(parsed_backup.get("status") or "unknown"),
                "file_count": len(parsed_backup.get("files") or []) if isinstance(parsed_backup.get("files"), list) else 0,
                "skipped_count": len(parsed_backup.get("skipped") or []) if isinstance(parsed_backup.get("skipped"), list) else 0,
                "error": _safe_error(str(parsed_backup.get("error") or ""))[:160],
                "finished_at": str(parsed_backup.get("finished_at") or ""),
            }
    return {
        "enabled": summary.enabled,
        "recent_count": summary.recent_count,
        "pending_count": summary.pending_count,
        "running_count": summary.running_count,
        "needs_action_count": summary.needs_action_count,
        "problem_count": summary.problem_count,
        "lock_wait_count": summary.lock_wait_count,
        "p115_cooldown_until": summary.p115_cooldown_until,
        "p115_cooldown_active": summary.p115_cooldown_until > current_time,
        "runner_heartbeat_at": summary.runner_heartbeat_at,
        "runner_heartbeat_stale": summary.runner_heartbeat_stale,
        "runner_state": summary.runner_state,
        "runner_active": summary.runner_active,
        "runner_active_task_id": summary.runner_active_task_id,
        "runner_active_stage": summary.runner_active_stage,
        "runner_active_since": summary.runner_active_since,
        "runner_last_claim_attempt_at": summary.runner_last_claim_attempt_at,
        "backup": backup,
        "wait_details": list(summary.wait_details),
        "latest_problem": (
            serialize_task(
                summary.latest_problem,
                now=current_time,
                lifecycle_actions_enabled=enabled,
                task_store=store,
            )
            if summary.latest_problem
            else None
        ),
        "latest_lock_wait": (
            serialize_task(
                summary.latest_lock_wait,
                now=current_time,
                lifecycle_actions_enabled=enabled,
                task_store=store,
            )
            if summary.latest_lock_wait
            else None
        ),
        "cms_strm_guard": cms_guard or None,
        "cms_direct_strm_guard": cms_direct_guard or None,
        "cms_os_strm_guard": cms_os_guard or None,
    }


def serialize_background_job(background_jobs: Any | None, *, prefix: str = "") -> dict[str, Any] | None:
    if background_jobs is None:
        return None
    snapshots = getattr(background_jobs, "list_snapshots", lambda: ())()
    matches = [snapshot for snapshot in snapshots if str(getattr(snapshot, "key", "")).startswith(prefix)]
    if not matches:
        return None
    snapshot = max(matches, key=lambda item: float(getattr(item, "queued_at", 0)))
    return {
        "description": redact_background_text(getattr(snapshot, "description", "")),
        "state": str(getattr(snapshot, "status", "")),
        "started_at": getattr(snapshot, "started_at", None),
        "finished_at": getattr(snapshot, "finished_at", None),
        "error": redact_background_text(getattr(snapshot, "error", "")),
    }


_LOG_REPAIR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("等待超时", "任务阶段等待超时：可在任务详情执行 retry/restore，或检查 Emby/CMS 与目标目录"),
    ("p115_risk_control", "115 风控或频率限制：等待冷却结束后再重试"),
    ("风控", "115 风控或频率限制：等待冷却结束后再重试"),
    ("Cannot reach", "网络瞬时错误：稍后重试即可"),
    ("SSL:", "网络瞬时错误（SSL）：稍后重试即可"),
    ("Network unreachable", "网络不可达：检查网络/代理后重试"),
    ("Task stage failed", "任务阶段失败：可在任务详情执行 retry"),
    ("Traceback", "程序异常：需要检查对应模块的异常堆栈"),
    ("Exception", "程序异常：需要检查对应模块"),
    ("Failed to", "外部操作失败：检查配置或稍后重试"),
)


def _log_repair_hints(entries: list[Any]) -> list[dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    for entry in entries:
        text = str(getattr(entry, "text", "") or "")
        for marker, hint in _LOG_REPAIR_PATTERNS:
            if marker in text:
                item = matches.setdefault(marker, {"marker": marker, "hint": hint, "count": 0, "sample": ""})
                item["count"] += 1
                if not item["sample"]:
                    item["sample"] = text[:160]
                break
    return sorted(matches.values(), key=lambda item: item["count"], reverse=True)[:10]


def api_log_analysis(
    log_hub: Any,
    *,
    lines: int = 500,
    since_seconds: int = 0,
    logger: str = "",
    keyword: str = "",
    level: str = "main",
) -> dict[str, Any]:
    normalized_lines = max(1, min(int(lines), 5000))
    normalized_since = max(0, min(int(since_seconds), 7 * 24 * 3600))
    normalized_level = str(level or "main") if str(level or "main") in {"main", "ERROR", "all"} else "main"
    normalized_logger = str(logger or "").strip()[:100]
    normalized_keyword = str(keyword or "").strip()[:100]
    spec = LogFilter(normalized_level, normalized_lines, normalized_keyword, normalized_logger)
    entries = tuple(log_hub.snapshot(spec))
    if normalized_since > 0:
        cutoff = time.time() - normalized_since
        entries = tuple(entry for entry in entries if entry.created_at >= cutoff)
    entries = entries[:normalized_lines]

    error_count = sum(1 for entry in entries if entry.level in {"ERROR", "CRITICAL"})
    warning_count = sum(1 for entry in entries if entry.level == "WARNING")
    logger_counts: dict[str, int] = {}
    repeated: dict[tuple[str, str, str], int] = {}
    for entry in entries:
        logger_counts[str(entry.logger or "root")] = logger_counts.get(str(entry.logger or "root"), 0) + 1
        key = (str(entry.level or ""), str(entry.logger or ""), str(entry.text or "")[:120])
        repeated[key] = repeated.get(key, 0) + 1
    top_loggers = sorted(logger_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    repeated_patterns = [
        {
            "level": level_name,
            "logger": logger_name,
            "prefix": text_prefix,
            "count": count,
        }
        for (level_name, logger_name, text_prefix), count in sorted(
            repeated.items(), key=lambda item: item[1], reverse=True
        )[:10]
    ]
    return {
        "generated_at": time.time(),
        "requested": {
            "lines": normalized_lines,
            "since_seconds": normalized_since,
            "logger": normalized_logger,
            "keyword": normalized_keyword,
            "level": normalized_level,
        },
        "summary": {
            "total": len(entries),
            "error_count": error_count,
            "warning_count": warning_count,
            "top_loggers": [{"logger": name, "count": count} for name, count in top_loggers],
            "repeated_patterns": repeated_patterns,
        },
        "repair_hints": _log_repair_hints(entries),
        "entries": [entry.payload() for entry in entries],
    }


def serialize_hdhive(
    service: Any | None,
    scheduler: Any | None = None,
    background_jobs: Any | None = None,
) -> dict[str, Any]:
    if service is None:
        return {"enabled": False, "subscriptions": [], "account": None, "schedule": {}, "background_job": None}
    subscriptions = []
    for subscription in service.list():
        item_rows = []
        for item in service.store.list_items(subscription.id):
            if is_dataclass(item):
                item_row = asdict(item)
                item_row.pop("unlocked_url", None)
                item_row["last_error"] = _safe_error(item_row.get("last_error"))
                item_rows.append(item_row)
        row = asdict(subscription) if is_dataclass(subscription) else {"id": subscription.id}
        row.pop("source_url", None)
        row.pop("chat_id", None)
        row["last_error"] = _safe_error(row.get("last_error"))
        row["items"] = item_rows
        try:
            summary = json.loads(str(row.get("last_summary_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        row["last_summary"] = summary if isinstance(summary, dict) else {}
        diagnosis = diagnose_subscription_check(row["last_summary"], item_rows)
        row["diagnosis"] = {
            "conclusion": diagnosis.conclusion,
            "counts": diagnosis.counts,
            "reasons": list(diagnosis.reasons),
        }
        row["completed"] = str(row.get("status") or "").lower() == "completed"
        subscriptions.append(row)
    account = None
    account_getter = getattr(getattr(service, "proxy", None), "account", None)
    if callable(account_getter):
        try:
            value = account_getter()
            account = {
                "nickname": str(getattr(value, "nickname", "")),
                "points": int(getattr(value, "points", 0)),
                "weekly_free_quota_remaining": int(getattr(value, "weekly_free_quota_remaining", 0)),
                "weekly_free_quota_unlimited": bool(getattr(value, "weekly_free_quota_unlimited", False)),
                "level": str(getattr(value, "level", "")),
                "is_blocked": bool(getattr(value, "is_blocked", False)),
                "is_forever_vip": bool(getattr(value, "is_forever_vip", False)),
            }
        except Exception as exc:
            account = {"error": _safe_error(str(exc)[:160])}
    schedule = {}
    if scheduler is not None:
        try:
            schedule = dict(scheduler.status_snapshot())
        except Exception as exc:
            schedule = {"error": _safe_error(str(exc)[:160])}
    return _safe_api_value({
        "enabled": True,
        "subscriptions": subscriptions,
        "account": account,
        "schedule": schedule,
        "background_job": serialize_background_job(background_jobs, prefix="hdhive:"),
    })


def serialize_hdhive_subscription(service: Any | None, subscription_id: int) -> dict[str, Any] | None:
    if service is None:
        return None
    payload = serialize_hdhive(service)
    return next(
        (row for row in payload.get("subscriptions", []) if int(row.get("id") or 0) == int(subscription_id)),
        None,
    )


def api_response(payload: Any, *, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(_safe_api_value(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, body


def api_tasks(
    store: TaskStore,
    *,
    limit: int = 100,
    now: float | None = None,
    lifecycle_actions_enabled: bool = True,
    max_retries: int = 3,
    media_enricher: Any | None = None,
    open_only: bool = False,
) -> dict[str, Any]:
    capped = max(1, min(int(limit), 500))
    if open_only:
        tasks = store.list_open_tasks()[:capped]
    else:
        tasks = store.list_recent_tasks(limit=capped)
    serialized = [
        serialize_task(
            task,
            now=now,
            lifecycle_actions_enabled=lifecycle_actions_enabled,
            max_retries=max_retries,
            task_store=store,
        )
        for task in tasks
    ]
    if media_enricher is not None:
        serialized = media_enricher(store, serialized)
    return {
        "items": serialized,
        "count": len(serialized),
    }


def api_task_detail(
    store: TaskStore,
    task_id: int,
    *,
    now: float | None = None,
    lifecycle_actions_enabled: bool = True,
    max_retries: int = 3,
) -> dict[str, Any] | None:
    task = store.find_task(task_id)
    if task is None:
        return None
    result = serialize_task(
        task,
        now=now,
        lifecycle_actions_enabled=lifecycle_actions_enabled,
        max_retries=max_retries,
        include_completion_drift=True,
        task_store=store,
    )
    result["events"] = [serialize_event(event) for event in store.list_events(task.id)]
    return result


def quality_items(
    store: TaskStore,
    *,
    limit: int = 100,
    quality_automation: Any | None = None,
    now: float | None = None,
    issues: list[Any] | None = None,
) -> list[dict[str, Any]]:
    allowed_roots = getattr(quality_automation, "allowed_roots", None)
    if not isinstance(allowed_roots, (list, tuple, set, frozenset)):
        allowed_roots = None
    if issues is None:
        share_identity_resolver = getattr(quality_automation, "share_identity_resolver", None)
        scan_kwargs = {
            "limit": max(1, min(int(limit), 500)),
            "allowed_roots": allowed_roots,
        }
        if callable(share_identity_resolver):
            scan_kwargs["share_identity_resolver"] = share_identity_resolver
        issues = scan_task_quality(store, **scan_kwargs)
    grouped: dict[int, list[Any]] = {}
    for issue in issues:
        grouped.setdefault(int(issue.task_id), []).append(issue)
    engine = getattr(quality_automation, "rule_engine", None)
    if not isinstance(engine, QualityRuleEngine):
        engine = QualityRuleEngine()
    config = getattr(quality_automation, "rule_config", None)
    if not isinstance(config, dict):
        config = None
    current_time = time.time() if now is None else float(now)
    items: list[dict[str, Any]] = []
    for issue in issues:
        task = store.find_task(int(issue.task_id)) if int(issue.task_id) > 0 else None
        if task is None:
            descriptor = {
                "rule_id": "manual_required",
                "rule_reason": "task_not_found",
                "risk_level": "high",
                "issue_codes": [issue.code],
                "manual_status": "manual_required",
                "attempts": 0,
                "next_eligible_at": 0,
                "available_actions": ["view"],
                "evidence": [issue.detail] if issue.detail else [],
                "auto_allowed": False,
                "rule_version": QUALITY_RULE_VERSION,
            }
        elif quality_automation is not None and callable(getattr(quality_automation, "quality_descriptor", None)):
            candidate = quality_automation.quality_descriptor(task, grouped[int(issue.task_id)], now=current_time)
            descriptor = candidate if isinstance(candidate, dict) else None
            if descriptor is None:
                match = engine.evaluate(task, grouped[int(issue.task_id)], config=config)
                descriptor = {
                    "rule_id": match.rule_id,
                    "rule_reason": match.reason,
                    "risk_level": match.risk_level,
                    "issue_codes": list(match.issue_codes),
                    "manual_status": "manual_required",
                    "attempts": quality_attempt_count(task),
                    "next_eligible_at": 0,
                    "available_actions": ["view"],
                    "evidence": list(match.evidence),
                    "auto_allowed": False,
                    "rule_version": QUALITY_RULE_VERSION,
                }
        else:
            match = engine.evaluate(task, grouped[int(issue.task_id)], config=config)
            descriptor = {
                "rule_id": match.rule_id,
                "rule_reason": match.reason,
                "risk_level": match.risk_level,
                "issue_codes": list(match.issue_codes),
                "manual_status": "manual_required",
                "attempts": quality_attempt_count(task),
                "next_eligible_at": 0,
                "available_actions": ["view"],
                "evidence": list(match.evidence),
                "auto_allowed": False,
                "rule_version": QUALITY_RULE_VERSION,
            }
        descriptor = dict(descriptor)
        descriptor["evidence"] = [redact_quality_detail(value) for value in descriptor.get("evidence", [])]
        items.append(
            {
                "task_id": issue.task_id,
                "title": issue.title or (task.title if task is not None else ""),
                "display_title": task_display_title(task) if task is not None else issue.title,
                "code": issue.code,
                "message": issue.message,
                "detail": redact_quality_detail(issue.detail),
                **descriptor,
            }
        )
    return items


def api_quality(
    store: TaskStore,
    *,
    limit: int = 100,
    quality_automation: Any | None = None,
    background_jobs: Any | None = None,
) -> dict[str, Any]:
    items = quality_items(store, limit=limit, quality_automation=quality_automation)
    rule_counts: dict[str, int] = {}
    task_keys: set[tuple[int, str]] = set()
    manual_count = 0
    cooldown_count = 0
    now = time.time()
    for item in items:
        key = (int(item["task_id"]), str(item["rule_id"]))
        if key not in task_keys:
            task_keys.add(key)
            rule_counts[str(item["rule_id"])] = rule_counts.get(str(item["rule_id"]), 0) + 1
            if str(item["manual_status"]) == "manual_required":
                manual_count += 1
            try:
                cooldown_count += float(item["next_eligible_at"] or 0) > now
            except (TypeError, ValueError):
                pass
    payload = {
        "count": len(items),
        "items": items,
        "rule_counts": rule_counts,
        "manual_count": manual_count,
        "cooldown_count": cooldown_count,
    }
    if quality_automation is not None:
        snapshot = quality_automation.status_snapshot()
        payload["automation"] = snapshot if isinstance(snapshot, dict) else {}
        payload["cleanup_enabled"] = bool(getattr(quality_automation, "strm_cleanup_enabled", False))
    payload["background_job"] = serialize_background_job(background_jobs, prefix="quality:run")
    return payload


def api_quality_runs(
    store: TaskStore,
    *,
    limit: int = 30,
    days: int = 30,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for row in store.list_quality_runs(limit=limit):
        try:
            rule_counts = json.loads(row.get("rule_counts_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            rule_counts = {}
        try:
            budget_used = json.loads(row.get("budget_used_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            budget_used = {}
        items.append(
            {
                "run_id": str(row.get("run_id") or ""),
                "run_date": str(row.get("run_date") or ""),
                "status": str(row.get("status") or ""),
                "started_at": float(row.get("started_at") or 0),
                "finished_at": float(row.get("finished_at") or 0),
                "scanned_count": int(row.get("scanned_count") or 0),
                "issue_count": int(row.get("issue_count") or 0),
                "planned_count": int(row.get("planned_count") or 0),
                "queued_count": int(row.get("queued_count") or 0),
                "failed_count": int(row.get("failed_count") or 0),
                "skipped_count": int(row.get("skipped_count") or 0),
                "manual_count": int(row.get("manual_count") or 0),
                "cooldown_count": int(row.get("cooldown_count") or 0),
                "rule_counts": rule_counts if isinstance(rule_counts, dict) else {},
                "budget_used": budget_used if isinstance(budget_used, dict) else {},
            }
        )
    trend: list[dict[str, Any]] = []
    for row in store.quality_run_trend(days=days):
        trend.append(
            {
                "run_date": str(row.get("run_date") or ""),
                "runs": int(row.get("runs") or 0),
                "scanned_count": int(row.get("scanned_count") or 0),
                "issue_count": int(row.get("issue_count") or 0),
                "planned_count": int(row.get("planned_count") or 0),
                "queued_count": int(row.get("queued_count") or 0),
                "failed_count": int(row.get("failed_count") or 0),
                "manual_count": int(row.get("manual_count") or 0),
                "cooldown_count": int(row.get("cooldown_count") or 0),
            }
        )
    return {"items": items, "trend": trend}


def build_cms_upgrade_hint(remote_version: str, container: str = "cms-tg-ingest") -> str:
    """Host-side upgrade commands for the settings page.

    Container switching runs on the host via update-cms.sh (guard verify +
    auto rollback); the script lives inside the deploy container and is copied
    out once. Paths are the Unraid defaults and may be edited by the user.
    """
    remote_version = str(remote_version or "").strip()
    if not remote_version:
        return ""
    return (
        f"# 1) 一次性：把升级脚本从 {container} 容器拷到宿主机（已拷过可跳过）\n"
        f"docker cp {container}:/app/scripts/cms-strm-guard/ /mnt/user/appdata/cms-tg-ingest/scripts/\n"
        "# 2) 升级 CMS 容器（含守卫验证 + 失败自动回滚），目录按实际调整\n"
        f"/mnt/user/appdata/cms-tg-ingest/scripts/cms-strm-guard/update-cms.sh "
        f"/boot/config/plugins/compose.manager/projects/CMS {remote_version}"
    )


def api_cms_version(checker: Any | None = None, background_jobs: Any | None = None) -> dict[str, Any]:
    if checker is None or not callable(getattr(checker, "status", None)):
        return {"enabled": False, "current_version": "", "update_ready": False, "background_job": None}
    payload = checker.status()
    payload = _safe_api_value(payload)
    payload.setdefault("upgrade_status", "")
    payload.setdefault("upgrade_error", "")
    if payload.get("update_available"):
        payload["upgrade_hint"] = build_cms_upgrade_hint(str(payload.get("remote_version") or ""))
    else:
        payload["upgrade_hint"] = ""
    payload["background_job"] = serialize_background_job(background_jobs, prefix="cms:upgrade")
    return payload


def emby_image_url(base_url: str, item_id: str, *, max_height: int, api_key: str) -> str:
    """Build a browser-loadable Emby poster URL.

    The api key is only ever interpolated here on the server side; the
    frontend receives a complete, directly loadable image URL.
    """
    base_url = str(base_url or "").rstrip("/")
    item_id = str(item_id or "").strip()
    if not base_url or not item_id:
        return ""
    return (
        f"{base_url}/emby/Items/{quote(item_id, safe='')}/Images/Primary"
        f"?maxHeight={int(max_height)}&apiKey={quote(str(api_key or ''), safe='')}"
    )


_emby_dashboard_cache: dict[str, Any] = {"at": 0.0, "value": None}
_EMBY_DASHBOARD_CACHE_SECONDS = 60.0


def _reset_emby_dashboard_cache() -> None:
    _emby_dashboard_cache.update({"at": 0.0, "value": None})


def api_emby_dashboard(emby: Any | None, *, refresh: bool = False) -> dict[str, Any]:
    """Aggregate Emby stats, library summaries and recent items for the board."""
    if emby is None or not getattr(emby, "enabled", False):
        return {"available": False, "reason": "emby_not_configured"}
    now = time.monotonic()
    cached = _emby_dashboard_cache.get("value")
    if not refresh and cached is not None and now - float(_emby_dashboard_cache.get("at") or 0) < _EMBY_DASHBOARD_CACHE_SECONDS:
        return cached
    try:
        payload = _build_emby_dashboard(emby)
    except Exception as exc:  # noqa: BLE001 - an Emby outage must not 500 the board
        _reset_emby_dashboard_cache()
        return {"available": False, "reason": "emby_unreachable", "error": _safe_error(str(exc))}
    _emby_dashboard_cache.update({"at": now, "value": payload})
    return payload


def _build_emby_dashboard(emby: Any) -> dict[str, Any]:
    base_url = str(getattr(emby, "base_url", "") or "").rstrip("/")
    api_key = str(getattr(emby, "api_key", "") or "")
    # Counts is the liveness signal: if it fails the Emby server is treated as
    # unreachable and the whole board degrades. Library/recent failures below
    # are per-item and swallowed individually.
    counts = emby._get("/Items/Counts")
    stats: dict[str, Any] = {"movie_count": 0, "series_count": 0, "episode_count": 0, "library_count": 0}
    if isinstance(counts, dict):
        stats = {
            "movie_count": int(counts.get("MovieCount") or 0),
            "series_count": int(counts.get("SeriesCount") or 0),
            "episode_count": int(counts.get("EpisodeCount") or 0),
            "library_count": 0,
        }

    libraries: list[dict[str, Any]] = []
    try:
        for info in emby.library_summary():
            item_id = str(info.get("item_id") or "")
            libraries.append(
                {
                    "name": str(info.get("name") or ""),
                    "count": int(info.get("count") or 0),
                    "poster_url": emby_image_url(base_url, item_id, max_height=280, api_key=api_key) if item_id else "",
                }
            )
        stats["library_count"] = len(libraries)
    except Exception:  # noqa: BLE001 - library listing is best-effort
        pass

    recent: list[dict[str, Any]] = []
    try:
        for item in emby.recent_items(20, include_item_types="Movie,Series"):
            item_id = str(item.get("Id") or item.get("ItemId") or "").strip()
            if not item_id:
                continue
            genres = [str(value) for value in (item.get("Genres") or []) if value]
            recent.append(
                {
                    "id": item_id,
                    "name": str(item.get("Name") or item.get("SeriesName") or ""),
                    "type": str(item.get("Type") or ""),
                    "year": int(item.get("ProductionYear") or 0) or None,
                    "rating": float(item.get("CommunityRating") or 0) or None,
                    "genres": genres[:3],
                    "poster_url": emby_image_url(base_url, item_id, max_height=420, api_key=api_key),
                }
            )
    except Exception:  # noqa: BLE001 - recent items are best-effort
        pass

    return {
        "available": True,
        "emby_base": base_url,
        "stats": stats,
        "libraries": libraries,
        "recent": recent,
    }
