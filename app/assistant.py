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
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .web_api import api_task_detail, api_tasks, serialize_event, serialize_health

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_QUESTION_CHARS = 8000
LOG = logging.getLogger(__name__)

# ---- hermes 式长期记忆：跨会话记住用户偏好/纠正/事实/教训 ----
# 文件格式与 pi-hermes-memory 兼容（MEMORY.md，一行一条带日期前缀），
# 存在数据卷上，注入系统提示 + 每轮对话后异步提取新条目。
MEMORY_MAX_CHARS = 6000
MEMORY_TRIM_CHARS = 4000

MEMORY_INJECTION_HEADER = (
    "\n\n---- 长期记忆（你与这位用户长期共事积累的认识，供参考；以最新对话为准）----\n"
)

MEMORY_EXTRACT_SYSTEM_PROMPT = (
    "你是记忆管理器。从对话中提取值得长期记住的信息：用户的身份与偏好、对助手回答的纠正、"
    "关于这套系统的重要事实、踩过的坑和失败教训。已有记忆不要重复收录；一次性的任务状态"
    "（如某任务当前卡在哪个阶段）不属于长期记忆。只输出新增条目，每行一条，"
    "格式为「- YYYY-MM-DD 内容」；没有任何新增就只输出 NOTHING。"
    "绝不输出 API key、token、密码、cookie 等敏感信息。"
)

_SECRET_LINE_RE = re.compile(r"sk-[A-Za-z0-9]{10,}|(?:api[_-]?key|token|password|passwd|cookie|密码|密钥)\s*[:=]", re.I)

# ---- 工具（只读）：助手可实时查任务库，而不是只看静态快照 ----
# 内置只读文件工具 + 自定义只读查询工具（见 pi-extensions/cms-tools.ts）。
# 不给 bash/edit/write：助手不能改代码、改库、执行任意命令。
ASSISTANT_TOOL_NAMES = "read,grep,find,ls,task_detail,query_tasks,task_events,system_stats,task_action"
TOOL_ENV_PATTERN = re.compile(
    r"^(TG_|CMS_|EMBY_|P115_|OPENAI_|WEB_|HDHIVE_|SELF_SHARE|BACKUP_|DATABASE_PATH|STRM_|HF_|GH_|GITHUB)"
)


def tools_enabled() -> bool:
    return str(os.environ.get("PI_ASSISTANT_TOOLS") or "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def resolve_extensions_path() -> str:
    override = str(os.environ.get("PI_ASSISTANT_EXTENSIONS") or "").strip()
    if override:
        return override
    candidates = [
        Path("/app/pi-extensions/cms-tools.ts"),
        Path(__file__).resolve().parent.parent / "pi-extensions" / "cms-tools.ts",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def sanitized_env() -> dict[str, str]:
    """子进程环境：剥掉业务密钥（TG/CMS/Emby/115/AI key 等），助手工具与
    pi 自身用不到它们；即使模型被诱导读环境也拿不到敏感值。"""
    keep = {}
    for key, value in os.environ.items():
        if TOOL_ENV_PATTERN.match(key):
            continue
        keep[key] = value
    return keep


def memory_enabled() -> bool:
    return str(os.environ.get("PI_ASSISTANT_MEMORY") or "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def assistant_memory_dir(session_dir: Path | None = None) -> Path:
    override = str(os.environ.get("PI_ASSISTANT_MEMORY_DIR") or "").strip()
    if override:
        return Path(override)
    base = Path(session_dir).parent if session_dir else assistant_session_dir(None).parent
    return base / "assistant-memory"


def read_memory_context(session_dir: Path | None = None, *, max_chars: int = 4000) -> str:
    """读取长期记忆文本（MEMORY.md 尾部优先，超长从头裁剪）。"""
    if not memory_enabled():
        return ""
    path = assistant_memory_dir(session_dir) / "MEMORY.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > max_chars:
        # 保留较新的条目（文件按时间追加，新的在尾部）。
        text = text[-max_chars:]
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
    return text.strip()


def append_memory_entries(output: str, session_dir: Path | None = None) -> list[str]:
    """把记忆提取的输出写进 MEMORY.md；返回实际新增的条目。"""
    if not memory_enabled():
        return []
    entries: list[str] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        line = line.lstrip("-").strip()
        # 模型可能自带日期前缀，剥掉后统一补当前日期。
        line = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", line)
        if not line or line.upper() == "NOTHING":
            continue
        if _SECRET_LINE_RE.search(line):
            LOG.info("Assistant memory entry dropped by secret filter")
            continue
        if line not in entries:
            entries.append(line)
    if not entries:
        return []
    memory_dir = assistant_memory_dir(session_dir)
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        path = memory_dir / "MEMORY.md"
        existing = ""
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            pass
        today = time.strftime("%Y-%m-%d")
        lines = [f"- {today} {entry}" for entry in entries if entry not in existing]
        if not lines:
            return []
        content = existing.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
        if len(content) > MEMORY_MAX_CHARS:
            content = content[-MEMORY_TRIM_CHARS:]
            newline = content.find("\n")
            if newline != -1:
                content = content[newline + 1 :]
        path.write_text(content.lstrip("\n"), encoding="utf-8")
        return lines
    except OSError as exc:
        LOG.warning("Assistant memory write failed: %s", exc)
        return []


def extract_memory_async(question: str, reply: str, session_dir: Path | None = None) -> None:
    """对话结束后在后台提取长期记忆（fire-and-forget，失败静默）。"""
    if not memory_enabled():
        return

    def work() -> None:
        try:
            existing = read_memory_context(session_dir, max_chars=MEMORY_MAX_CHARS)
            conversation = (
                f"已有记忆：\n{existing or '（空）'}\n\n"
                f"对话：\n用户：{str(question)[:2000]}\n助手：{str(reply)[:1500]}"
            )
            result = run_pi(
                conversation,
                session_id=None,
                session_dir=session_dir or assistant_session_dir(None),
                model=assistant_model(),
                timeout=45.0,
                system_prompt=MEMORY_EXTRACT_SYSTEM_PROMPT,
                tools=False,
            )
            added = append_memory_entries(result.get("reply", ""), session_dir)
            if added:
                LOG.info("Assistant memory learned %d new entries", len(added))
        except Exception:
            LOG.debug("Assistant memory extraction failed", exc_info=True)

    threading.Thread(target=work, name="assistant-memory", daemon=True).start()

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
    "你是 cms-tg-ingest 的内置智能助手，也是用户长期共事的运维搭档。cms-tg-ingest 是 Cloud Media Sync（CMS）"
    "的 Telegram 自动入库外挂：把 115 分享/磁力/ED2K/HDHive 资源交给 Bot，经 CMS 整理分类 → 生成 STRM → "
    "Emby 入库确认 → 清理转存源。任务状态机大致为 pending → receiving → organizing → sharing → syncing → "
    "moving → checking → succeeded；失败会自动重试，超过重试次数或需要人工判断时进入 needs_action。\n"
    "用户消息末尾可能附有「系统快照」JSON（健康状态、任务列表、指定任务详情）。回答规则：\n"
    "1. 优先基于快照和工具查证回答；涉及具体任务/数值时先用工具核实，不要编造快照里没有的状态、ID 或数值。\n"
    "2. 你有只读查询工具（task_detail / query_tasks / task_events / system_stats）和文件读取工具，"
    "可以实时查任务库与文件——主动用它们核实后再下结论，查不到就如实说。\n"
    "3. 诊断问题时给出：结论 → 依据 → 具体处理建议（可结合任务的 available_actions，说明在 Web 管理台或 "
    "Telegram 里如何操作）。\n"
    "4. 你可以执行任务操作工具（task_action：retry/reprocess/resume_organizing/emby/restore/terminate），"
    "它与 Web 管理台按钮同源、自带资格校验；但必须先向用户说明并获得明确同意（最新消息出现「确认/好的/执行」）才能调用，"
    "terminate 这类中止任务的动作尤其要确认；执行后如实报告结果。除该工具外不能改库、改配置、改文件内容。\n"
    "5. 绝不读取或输出密钥、密码、token、cookie（包括环境变量和 .env）。\n"
    "6. 你既能处理运维诊断，也可以正常陪聊、回答通用问题；不确定是不是系统问题时，按普通问题自然回答。\n"
    "7. 用简体中文回答，简洁分点，先结论后依据；信息不足时直接说明还缺什么。\n"
    "8. 你在一次对话中会收到多份快照，以最新一份为准。\n"
    "9. 你与用户是长期共事的同事：直接、简洁、口语一点，可以引用记忆里的偏好和历史；"
    "用户纠正你的地方要接受并调整，不要重复犯错。"
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


def _build_pi_argv(
    question: str,
    *,
    session_id: str | None,
    session_dir: Path,
    binary: str,
    system_prompt: str | None,
    inject_memory: bool,
    model: str,
    tools: bool | None = None,
) -> tuple[list[str], dict[str, str]]:
    use_tools = tools_enabled() if tools is None else tools
    argv = [
        binary,
        "--print",
        "--mode",
        "json",
        "--system-prompt",
        system_prompt or ASSISTANT_SYSTEM_PROMPT,
        # 禁用发现类来源（用户编码配置/技能/上下文文件），扩展只加载我们
        # 随镜像分发的只读工具（-e 显式路径不受 --no-extensions 影响）。
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
        "--session-dir",
        str(session_dir),
    ]
    extension_path = resolve_extensions_path() if use_tools else ""
    if extension_path:
        argv += ["-e", extension_path, "--tools", ASSISTANT_TOOL_NAMES]
    else:
        argv.append("--no-tools")
    if session_id:
        argv += ["--session-id", session_id]
    else:
        argv.append("--no-session")
    if inject_memory:
        memory = read_memory_context(session_dir)
        if memory:
            argv[argv.index("--system-prompt") + 1] += MEMORY_INJECTION_HEADER + memory
    if str(model or "").strip():
        argv += ["--model", str(model).strip()]
    env = sanitized_env()
    config_dir = str(os.environ.get("PI_ASSISTANT_CONFIG_DIR") or "").strip()
    if config_dir:
        env["PI_CODING_AGENT_DIR"] = config_dir
    return argv, env


def run_pi(
    question: str,
    *,
    session_id: str | None,
    session_dir: Path,
    binary: str = "",
    model: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    system_prompt: str | None = None,
    inject_memory: bool = False,
    tools: bool | None = None,
) -> dict[str, str]:
    """非交互调用 pi，返回 {reply, session_id}。失败抛 AssistantError/TimeoutExpired。

    session_id=None 时用 --no-session（一次性调用，如记忆提取）；
    inject_memory=True 时把长期记忆注入系统提示。
    """
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
    argv, env = _build_pi_argv(
        question,
        session_id=session_id,
        session_dir=session_dir,
        binary=binary,
        system_prompt=system_prompt,
        inject_memory=inject_memory,
        model=model,
        tools=tools,
    )
    # 容器冷启动后的首次调用可能撞上 pi 自身 bootstrap 的瞬态失败：
    # 进程级失败重试一次再放弃，避免任务背上数小时的诊断退避。
    proc = None
    stderr_tail = ""
    for attempt in (1, 2):
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
        if proc.returncode == 0:
            return {"reply": _parse_reply(proc.stdout or ""), "session_id": session_id}
        stderr_tail = (proc.stderr or proc.stdout or "")[-600:].strip()
        if attempt == 1:
            LOG.warning("pi run failed (attempt 1/2), retrying: %s", stderr_tail[:200])
    raise AssistantError(f"pi 退出码 {proc.returncode}: {stderr_tail or '无输出'}")


def run_pi_stream(
    question: str,
    *,
    session_id: str | None,
    session_dir: Path,
    binary: str = "",
    model: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    system_prompt: str | None = None,
    inject_memory: bool = False,
    tools: bool | None = None,
    on_update=None,
) -> dict[str, str]:
    """流式版 run_pi：解析 stdout 的 NDJSON 事件流，把助手的阶段性文本通过
    ``on_update(latest_text)`` 回调出去（调用方自行节流）。返回值与 run_pi 一致。"""
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
    argv, env = _build_pi_argv(
        question,
        session_id=session_id,
        session_dir=session_dir,
        binary=binary,
        system_prompt=system_prompt,
        inject_memory=inject_memory,
        model=model,
        tools=tools,
    )
    proc = subprocess.Popen(  # noqa: S603 - 固定 argv，无 shell
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(session_dir),
    )
    latest = ""
    deadline = time.time() + timeout
    returncode = -1
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(question)
        proc.stdin.close()
        for line in proc.stdout:
            if time.time() > deadline:
                raise AssistantTimeout(f"助手响应超时（超过 {timeout:.0f} 秒），可稍后重试或调大 PI_ASSISTANT_TIMEOUT")
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") not in {"message_update", "message_end"}:
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            text = "".join(
                str(part.get("text") or "")
                for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            if text:
                latest = text
                if on_update is not None and event.get("type") == "message_update":
                    try:
                        on_update(text)
                    except Exception:
                        LOG.debug("assistant on_update callback failed", exc_info=True)
        returncode = proc.wait(timeout=max(5.0, deadline - time.time()))
    except AssistantTimeout:
        proc.kill()
        raise
    finally:
        if returncode == -1 and proc.poll() is None:
            proc.kill()
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
    if returncode != 0:
        stderr_tail = ""
        try:
            stderr_tail = (proc.stderr.read() if proc.stderr else "")[-600:].strip()
        except Exception:
            pass
        raise AssistantError(f"pi 退出码 {returncode}: {stderr_tail or '无输出'}")
    if not latest:
        raise AssistantError("pi 未返回可解析的回复")
    return {"reply": latest, "session_id": session_id}
