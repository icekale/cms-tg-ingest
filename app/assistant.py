"""内置 AI 运维助手：以 pi coding agent（@earendil-works/pi-coding-agent）为基座。

pi 负责模型接入、凭据与会话记忆；本模块只做三件事：
1. 把当前系统快照（健康状态、任务队列、任务详情）压成紧凑 JSON 附到用户消息里；
2. 以非交互模式（``pi -p --mode json``）调用 pi 子进程并解析回复；
3. 通过 ``--session-id`` 让同一对话跨请求延续（多轮记忆由 pi 会话文件承载）。

助手不启用任何工具（``--no-tools``），只基于快照做诊断与建议——快照来自
任务库，把模型输出重新交回给人决定是否执行。 ponytail: 若未来要让它直接
执行修复动作，应通过扩展注册只读/白名单工具，而不是放开 bash。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .web_api import api_task_detail, api_tasks, serialize_event, serialize_health

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_QUESTION_CHARS = 8000

# needs_action 自动诊断：结果写进任务 metadata（Web 任务详情可见），
# event_id 记录诊断时最新事件——只有原因变化（新事件）才重新诊断。
DIAGNOSIS_META_KEY = "assistant_diagnosis"
AUTO_DIAGNOSIS_QUESTION = (
    "该任务已进入 needs_action（需要人工处理）。请诊断根本原因，评估影响，"
    "并给出具体的处理建议（结合 available_actions 说明在 Web 管理台或 Telegram 里怎么操作）。"
)

# 传给模型的任务字段白名单：serialize_task 的完整 dict 含 metadata/URL 等
# 大而敏感的内容，快照只需要诊断要用的部分。
_TASK_FIELDS = (
    "id",
    "display_title",
    "source_type",
    "stage",
    "status",
    "strm_mode",
    "category",
    "tmdb_id",
    "error",
    "retry_count",
    "next_run_at",
    "available_actions",
    "why_slow",
    "stage_elapsed",
    "updated_at",
)

ASSISTANT_SYSTEM_PROMPT = (
    "你是 cms-tg-ingest 的内置运维助手。cms-tg-ingest 是 Cloud Media Sync（CMS）的 Telegram "
    "自动入库外挂：把 115 分享/磁力/ED2K/HDHive 资源交给 Bot，经 CMS 整理分类 → 生成 STRM → "
    "Emby 入库确认 → 清理转存源。任务状态机大致为 pending → receiving → organizing → sharing "
    "→ syncing → moving → checking → succeeded；失败会自动重试，超过重试次数或需要人工判断时"
    "进入 needs_action。\n"
    "用户消息末尾可能附有「系统快照」JSON（健康状态、任务列表、指定任务详情）。回答规则：\n"
    "1. 优先基于快照回答；不要编造快照里没有的状态、ID 或数值。\n"
    "2. 诊断问题时给出：结论 → 依据 → 具体处理建议（可结合任务的 available_actions，"
    "说明在 Web 管理台或 Telegram 里如何操作）。\n"
    "3. 你不能直接执行操作；只做诊断和给出步骤。\n"
    "4. 用简体中文回答，简洁分点，先结论后依据。\n"
    "5. 信息不足时直接说明还缺什么（如任务 ID、日志关键字）。\n"
    "6. 你在一次对话中会收到多份快照，以最新一份为准。"
)


class AssistantError(RuntimeError):
    """pi 调用失败（非零退出、输出不可解析等）。"""


class AssistantTimeout(AssistantError):
    """pi 调用超时。"""


def resolve_pi_binary() -> str:
    return str(os.environ.get("PI_ASSISTANT_BIN") or "").strip() or shutil.which("pi") or ""


def assistant_session_dir(store: Any) -> Path:
    override = str(os.environ.get("PI_ASSISTANT_SESSION_DIR") or "").strip()
    if override:
        return Path(override)
    db_path = str(getattr(store, "db_path", "") or "")
    base = Path(db_path).parent if db_path else Path("/data")
    return base / "assistant-sessions"


def assistant_timeout() -> float:
    try:
        return max(10.0, float(os.environ.get("PI_ASSISTANT_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def assistant_model() -> str:
    return str(os.environ.get("PI_ASSISTANT_MODEL") or "").strip()


def auto_diagnosis_enabled() -> bool:
    return str(os.environ.get("PI_ASSISTANT_AUTO_DIAGNOSIS") or "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def build_task_brief(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    brief = {key: task.get(key) for key in _TASK_FIELDS if task.get(key) not in (None, "", [], {})}
    events = task.get("events")
    if isinstance(events, list) and events:
        brief["recent_events"] = [
            {k: ev.get(k) for k in ("stage", "status", "message", "created_at")}
            for ev in events[-8:]
            if isinstance(ev, dict)
        ]
    return brief


def build_context_payload(
    *,
    version: str,
    health: dict[str, Any] | None,
    open_tasks: list[dict[str, Any]],
    task_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version,
        "health": health or {},
        "open_tasks": [build_task_brief(task) for task in open_tasks[:15]],
    }
    if task_detail is not None:
        payload["focus_task"] = build_task_brief(task_detail)
    return payload


def build_snapshot(
    store: Any,
    *,
    engine_enabled: bool = True,
    max_retries: int = 3,
    task_id: int = 0,
    guards: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """采集诊断快照：健康状态 + 开放任务（含最近事件）+ 可选聚焦任务。

    Web 助手与 Telegram /助手 共用，保证两边看到同样的上下文。guards 是
    可选的 CMS 守卫状态 dict（cms_strm_guard / cms_direct_strm_guard /
    cms_os_strm_guard），由调用方按需传入（Web 有缓存过的守卫结果）。
    """
    guards = guards or {}
    health = serialize_health(
        store,
        enabled=engine_enabled,
        cms_guard=guards.get("cms_strm_guard"),
        cms_direct_guard=guards.get("cms_direct_strm_guard"),
        cms_os_guard=guards.get("cms_os_strm_guard"),
    )
    open_items = api_tasks(
        store,
        limit=15,
        open_only=True,
        lifecycle_actions_enabled=engine_enabled,
        max_retries=max_retries,
    )["items"]
    # 列表序列化不带事件，而失败原因往往只在事件流里（task.error_summary
    # 可能为空）：给每个开放任务附最近 3 条事件，助手才有诊断依据。
    for item in open_items:
        item["events"] = [serialize_event(event) for event in store.list_events(int(item["id"]))[-3:]]
    detail = None
    if int(task_id) > 0:
        detail = api_task_detail(
            store,
            int(task_id),
            lifecycle_actions_enabled=engine_enabled,
            max_retries=max_retries,
        )
    return build_context_payload(
        version=__version__,
        health=health,
        open_tasks=open_items,
        task_detail=detail,
    )


def diagnosis_event_id(store: Any, task_id: int) -> int:
    """任务最新事件的 id：用于判断 needs_action 原因是否变化（变化才重新诊断）。"""
    events = store.list_events(int(task_id))
    return int(events[-1].get("id") or 0) if events else 0


def build_user_message(question: str, context: dict[str, Any]) -> str:
    return (
        f"{question}\n\n"
        "---- 系统快照（自动采集，供诊断参考）----\n"
        f"{json.dumps(context, ensure_ascii=False, default=str)}"
    )


def _parse_reply(stdout: str) -> str:
    reply = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if isinstance(message, dict) and message.get("role") == "assistant":
            parts = [
                str(part.get("text") or "")
                for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = "".join(parts).strip()
            if text and event.get("type") in {"message_end", "message_update"}:
                reply = text
    if not reply:
        raise AssistantError("pi 未返回可解析的回复")
    return reply


def new_session_id() -> str:
    return str(uuid.uuid4())


def run_pi(
    question: str,
    *,
    session_id: str,
    session_dir: Path,
    binary: str = "",
    model: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """非交互调用 pi，返回 {reply, session_id}。失败抛 AssistantError/TimeoutExpired。"""
    binary = binary or resolve_pi_binary()
    if not binary:
        raise AssistantError(
            "未找到 pi（@earendil-works/pi-coding-agent）。请安装 pi 并确保其在 PATH 中，"
            "或通过 PI_ASSISTANT_BIN 指定路径。"
        )
    session_dir = Path(session_dir)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AssistantError(f"无法创建会话目录 {session_dir}: {exc}") from exc
    argv = [
        binary,
        "--print",
        "--mode",
        "json",
        "--system-prompt",
        ASSISTANT_SYSTEM_PROMPT,
        # 助手只做诊断：禁用扩展/技能/上下文文件/工具，避免用户编码配置和
        # 项目本地文件影响线上助手，也显著加快启动。
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-tools",
        "--no-approve",
        "--session-dir",
        str(session_dir),
        "--session-id",
        session_id,
    ]
    if str(model or "").strip():
        argv += ["--model", str(model).strip()]
    env = dict(os.environ)
    config_dir = str(os.environ.get("PI_ASSISTANT_CONFIG_DIR") or "").strip()
    if config_dir:
        env["PI_CODING_AGENT_DIR"] = config_dir
    try:
        proc = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
            argv,
            input=question,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(session_dir),
        )
    except subprocess.TimeoutExpired as exc:
        raise AssistantTimeout(f"助手响应超时（超过 {timeout:.0f} 秒），可稍后重试或调大 PI_ASSISTANT_TIMEOUT") from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or proc.stdout or "")[-600:].strip()
        raise AssistantError(f"pi 退出码 {proc.returncode}: {stderr_tail or '无输出'}")
    return {"reply": _parse_reply(proc.stdout or ""), "session_id": session_id}
