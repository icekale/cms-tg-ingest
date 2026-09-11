# pyright: reportOptionalSubscript=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportArgumentType=false
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge
from app import assistant
from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore
from app.web import WebApp
from tests.legacy_submission_store import SubmissionStore
from tests.test_bridge_v02_integration import FakeCmsSubmit


def _completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _pi_stdout_events(reply: str = "结论：任务 #1 处于 needs_action。") -> str:
    lines = [
        json.dumps({"type": "message_update", "message": {"role": "assistant", "content": [{"type": "text", "text": reply[:4]}]}}),
        json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}}),
        json.dumps({"type": "agent_settled"}),
    ]
    return "\n".join(lines) + "\n"


class RunPiTests(unittest.TestCase):
    def test_missing_binary_raises_friendly_error(self):
        with patch.object(assistant, "resolve_pi_binary", return_value=""):
            with self.assertRaises(assistant.AssistantError) as ctx:
                assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))
            self.assertIn("pi", str(ctx.exception))

    def test_run_builds_isolated_command_and_parses_reply(self):
        recorded = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs
            return _completed_process(stdout=_pi_stdout_events())

        with patch.object(assistant, "resolve_pi_binary", return_value="/usr/local/bin/pi"), patch.object(
            assistant.subprocess, "run", side_effect=fake_run
        ):
            result = assistant.run_pi(
                "为什么任务失败？",
                session_id="11111111-2222-3333-4444-555555555555",
                session_dir=Path("/tmp/assistant-test"),
                model="glm/*",
                timeout=30.0,
            )

        self.assertEqual(result, {"reply": "结论：任务 #1 处于 needs_action。", "session_id": "11111111-2222-3333-4444-555555555555"})
        argv = recorded["argv"]
        self.assertEqual(argv[0], "/usr/local/bin/pi")
        for flag in ("--print", "--mode", "--no-extensions", "--no-skills", "--no-approve"):
            self.assertIn(flag, argv)
        # 默认开启只读工具：加载随镜像分发的扩展并放行内置只读工具
        self.assertNotIn("--no-tools", argv)
        self.assertIn("-e", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], assistant.ASSISTANT_TOOL_NAMES)
        self.assertEqual(argv[argv.index("--session-id") + 1], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(argv[argv.index("--model") + 1], "glm/*")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], assistant.ASSISTANT_SYSTEM_PROMPT)
        # 问题正文通过 stdin 传递，不占 argv。
        self.assertEqual(recorded["kwargs"]["input"], "为什么任务失败？")

    def test_run_pi_tools_disabled_falls_back_to_no_tools(self):
        recorded = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            return _completed_process(stdout=_pi_stdout_events("好的"))

        with patch.dict(os.environ, {"PI_ASSISTANT_TOOLS": "0"}), patch.object(
            assistant, "resolve_pi_binary", return_value="pi"
        ), patch.object(assistant.subprocess, "run", side_effect=fake_run):
            assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))
        self.assertIn("--no-tools", recorded["argv"])
        self.assertNotIn("-e", recorded["argv"])

    def test_subprocess_env_strips_business_secrets(self):
        recorded = {}

        def fake_run(argv, **kwargs):
            recorded["env"] = kwargs.get("env") or {}
            return _completed_process(stdout=_pi_stdout_events("好的"))

        base_env = {
            "PATH": "/usr/bin",
            "TG_BOT_TOKEN": "secret-tg",
            "CMS_PASSWORD": "secret-cms",
            "OPENAI_API_KEY": "secret-ai",
            "WEB_PASSWORD": "secret-web",
            "P115_COOKIE_PATH": "/tmp/cookie",
            "EMBY_API_KEY": "secret-emby",
        }
        with patch.dict(os.environ, base_env), patch.object(
            assistant, "resolve_pi_binary", return_value="pi"
        ), patch.object(assistant.subprocess, "run", side_effect=fake_run):
            assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))
        env = recorded["env"]
        for key in base_env:
            if key == "PATH":
                continue
            self.assertNotIn(key, env, f"{key} 不应传入助手子进程")
        self.assertIn("PATH", env)

    def test_sanitized_env_keeps_tool_runtime_vars(self):
        extra = {
            "DATABASE_PATH": "/data/cms-tg-ingest.db",
            "CMS_TOOLS_SCRIPT": "/app/scripts/assistant_read.py",
            "CMS_TOOLS_OPS_SCRIPT": "/app/scripts/assistant_ops.py",
            "CMS_PASSWORD": "secret-cms",
        }
        with patch.dict(os.environ, extra, clear=False):
            env = assistant.sanitized_env()
        self.assertEqual(env["DATABASE_PATH"], extra["DATABASE_PATH"])
        self.assertEqual(env["CMS_TOOLS_SCRIPT"], extra["CMS_TOOLS_SCRIPT"])
        self.assertEqual(env["CMS_TOOLS_OPS_SCRIPT"], extra["CMS_TOOLS_OPS_SCRIPT"])
        self.assertNotIn("CMS_PASSWORD", env)

    def test_timeout_wrapped_as_assistant_timeout(self):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaises(assistant.AssistantTimeout):
                assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))

    def test_nonzero_exit_surfaces_stderr_tail(self):
        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess,
            "run",
            return_value=_completed_process(stderr="no auth configured for provider", returncode=1),
        ):
            with self.assertRaises(assistant.AssistantError) as ctx:
                assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))
        self.assertIn("no auth configured", str(ctx.exception))

    def test_transient_failure_retries_once_then_succeeds(self):
        responses = [
            _completed_process(stderr="bootstrap noise", returncode=1),
            _completed_process(stdout=_pi_stdout_events("重试后的回复")),
        ]
        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess, "run", side_effect=responses
        ) as fake_run:
            result = assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))
        self.assertEqual(fake_run.call_count, 2)
        self.assertEqual(result["reply"], "重试后的回复")

    def test_persistent_failure_raises_after_two_attempts(self):
        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess,
            "run",
            return_value=_completed_process(stderr="still broken", returncode=1),
        ) as fake_run:
            with self.assertRaises(assistant.AssistantError) as ctx:
                assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))
        self.assertEqual(fake_run.call_count, 2)
        self.assertIn("still broken", str(ctx.exception))

    def test_empty_stdout_raises(self):
        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess, "run", return_value=_completed_process()
        ):
            with self.assertRaises(assistant.AssistantError):
                assistant.run_pi("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))


class ContextTests(unittest.TestCase):
    def test_task_brief_whitelists_fields_and_includes_recent_events(self):
        brief = assistant.build_task_brief(
            {
                "id": 1,
                "display_title": "哑舍 S01",
                "metadata": {"secret": "should-not-leak"},
                "safe_url": "https://115cdn.com/s/x",
                "status": "needs_action",
                "error": {"summary": "整理超时"},
                "events": [
                    {"stage": "organizing", "status": "failed", "message": "超时", "created_at": 1, "extra": "drop"},
                    {"stage": "needs_action", "status": "needs_action", "message": "等待人工", "created_at": 2},
                ],
            }
        )
        self.assertEqual(brief["display_title"], "哑舍 S01")
        self.assertNotIn("metadata", brief)
        self.assertNotIn("safe_url", brief)
        self.assertEqual(brief["recent_events"][0], {"stage": "organizing", "status": "failed", "message": "超时", "created_at": 1})

    def test_snapshot_health_is_compact_and_reports_assistant_health(self):
        from app.web_api import serialize_health

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("哑舍 S01", "", "https://115cdn.com/s/health")
            store.record_event(task.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "整理超时")
            assistant.record_assistant_run(
                ok=False,
                session_dir=assistant.assistant_session_dir(store),
                error="pi 退出码 1: boom",
            )
            snap = assistant.build_snapshot(store, task_id=task.id)
            full = serialize_health(store, enabled=True)
        health = snap["health"]
        # 快照只留诊断用得上的字段，serialize_health 的其余字段是噪音
        self.assertTrue(set(full) - set(health))
        self.assertEqual(health["assistant"]["consecutive_failures"], 1)
        self.assertIn("pi 退出码 1", health["assistant"]["last_error"])
        self.assertLessEqual(len(json.dumps(health["assistant"], ensure_ascii=False)), 400)

    def test_user_message_embeds_snapshot(self):
        context = assistant.build_context_payload(
            version="0.0.0-test",
            health={"problem_count": 1},
            open_tasks=[{"id": 1, "display_title": "哑舍 S01", "status": "needs_action"}],
            task_detail={"id": 1, "display_title": "哑舍 S01"},
        )
        message = assistant.build_user_message("为什么失败？", context)
        self.assertTrue(message.startswith("为什么失败？"))
        self.assertIn("系统快照", message)
        payload = json.loads(message.split("----\n", 1)[1])
        self.assertEqual(payload["version"], "0.0.0-test")
        self.assertEqual(payload["open_tasks"][0]["display_title"], "哑舍 S01")
        self.assertEqual(payload["focus_task"]["id"], 1)

    def test_snapshot_open_tasks_omit_event_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("哑舍 S01", "", "https://115cdn.com/s/snapshot")
            store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "整理中")
            store.record_event(task.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "整理超时")
            snap = assistant.build_snapshot(store, task_id=task.id)
        open_task = next(item for item in snap["open_tasks"] if item["id"] == task.id)
        self.assertNotIn("recent_events", open_task)
        self.assertEqual(open_task["last_event"], "整理超时")
        self.assertIn("recent_events", snap["focus_task"])
        problem = snap["health"].get("latest_problem")
        if isinstance(problem, dict):
            self.assertNotIn("metadata", problem)
            self.assertNotIn("safe_url", problem)


class CmsToolsExtensionTests(unittest.TestCase):
    def setUp(self):
        self.src = (Path(__file__).resolve().parent.parent / "pi-extensions" / "cms-tools.ts").read_text()

    def test_failures_throw_instead_of_text_result(self):
        self.assertNotIn("task_detail failed:", self.src)
        self.assertNotIn("catch (err)", self.src)
        self.assertIn("reject(", self.src)

    def test_terminate_is_gated_on_tool_call(self):
        self.assertIn('pi.on("tool_call"', self.src)
        self.assertIn("block: true", self.src)
        self.assertIn("terminate", self.src)

    def test_automation_sweep_and_sensitive_paths_are_guarded(self):
        # 自动巡检会话一律拦 terminate；读工具拒读密钥/凭据/会话文件
        self.assertIn("CMS_TOOLS_AUTOMATION", self.src)
        self.assertIn("AUTOMATED", self.src)
        self.assertIn("SENSITIVE_PATH_RE", self.src)
        self.assertIn("PATH_TOOLS", self.src)
        self.assertIn("CONFIRM_MAX_CHARS", self.src)

    def test_uses_pi_truncation_signal_and_enums(self):
        self.assertIn("truncateHead", self.src)
        self.assertIn("StringEnum", self.src)
        self.assertIn("Type.Integer", self.src)
        self.assertIn("signal", self.src)
        self.assertIn("promptGuidelines", self.src)
        self.assertIn('pi.on("context"', self.src)


class AssistantChatEndpointTests(unittest.TestCase):

    def setUp(self):
        # 记忆提取是后台线程，单测里统一关掉，避免二次调用测试桩
        p = patch.object(assistant, "extract_memory_async", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _make_app(self, tmp: str, task_title: str = "哑舍 S01"):
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task(task_title, "", "https://115cdn.com/s/assistant")
        store.record_event(task.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "整理超时，等待人工处理")
        return WebApp(store), task

    def test_chat_returns_reply_and_echoes_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, task = self._make_app(tmp)
            captured = {}

            def fake_run_pi(question, *, session_id, session_dir, model="", timeout=0, **_kwargs):
                captured["question"] = question
                captured["session_id"] = session_id
                captured["session_dir"] = session_dir
                return {"reply": "任务 #1 进入 needs_action，因为整理超时。", "session_id": session_id}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=fake_run_pi
            ):
                status, _headers, body = app.handle_request(
                    "POST",
                    "/api/v1/assistant/chat",
                    {"Content-Type": "application/json"},
                    json.dumps({"question": "#1 任务为什么 needs_action？"}).encode(),
                )

            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.assertIn("needs_action", payload["reply"])
            self.assertTrue(payload["session_id"])
            self.assertIn("#1 任务为什么 needs_action？", captured["question"])
            self.assertIn("哑舍 S01", captured["question"], "快照必须包含失败任务标题")
            self.assertIn("整理超时，等待人工处理", captured["question"], "快照必须包含失败原因")
            self.assertEqual(captured["session_dir"].name, "assistant-sessions")

    def test_memory_endpoints_list_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _task = self._make_app(tmp)
            memory_dir = assistant.assistant_memory_dir(assistant.assistant_session_dir(app.store))
            memory_dir.mkdir(parents=True, exist_ok=True)
            (memory_dir / "MEMORY.md").write_text("- 偏好：中文回复\n- 教训：先查事件\n", encoding="utf-8")
            status, _headers, body = app.handle_request("GET", "/api/v1/assistant/memory", {}, b"")
            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["entries"][0], {"index": 1, "text": "- 偏好：中文回复"})
            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/assistant/memory/delete",
                {"Content-Type": "application/json"},
                json.dumps({"indexes": [1]}).encode(),
            )
            payload = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(payload["deleted"], ["- 偏好：中文回复"])
            self.assertEqual([item["text"] for item in payload["entries"]], ["- 教训：先查事件"])
            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/assistant/memory/delete",
                {"Content-Type": "application/json"},
                json.dumps({"indexes": []}).encode(),
            )
            self.assertEqual(status, 400)

    def test_chat_rejects_empty_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _task = self._make_app(tmp)
            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/assistant/chat",
                {"Content-Type": "application/json"},
                json.dumps({"question": "   "}).encode(),
            )
            self.assertEqual(status, 400)

    def test_chat_without_pi_returns_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _task = self._make_app(tmp)
            with patch.object(assistant, "resolve_pi_binary", return_value=""):
                status, _headers, body = app.handle_request(
                    "POST",
                    "/api/v1/assistant/chat",
                    {"Content-Type": "application/json"},
                    json.dumps({"question": "在吗？"}).encode(),
                )
            self.assertEqual(status, 503)
            self.assertIn("pi", json.loads(body)["error"])

    def test_chat_invalid_session_id_gets_fresh_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _task = self._make_app(tmp)
            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant,
                "run_pi",
                side_effect=lambda question, *, session_id, **_kw: {"reply": "ok", "session_id": session_id},
            ):
                status, _headers, body = app.handle_request(
                    "POST",
                    "/api/v1/assistant/chat",
                    {"Content-Type": "application/json"},
                    json.dumps({"question": "在吗？", "session_id": "../../etc/passwd"}).encode(),
                )
            self.assertEqual(status, 200)
            issued = json.loads(body)["session_id"]
            self.assertNotIn("/", issued)
            self.assertNotIn(".", issued)

    def test_chat_focus_task_and_session_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, task = self._make_app(tmp)
            captured = {}

            def fake_run_pi(question, *, session_id, **_kwargs):
                captured["question"] = question
                captured["session_id"] = session_id
                return {"reply": "好", "session_id": session_id}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=fake_run_pi
            ):
                status, _headers, body = app.handle_request(
                    "POST",
                    "/api/v1/assistant/chat",
                    {"Content-Type": "application/json"},
                    json.dumps({"question": "这个任务怎么了？", "task_id": task.id, "session_id": "ABCDEF01-2345-6789-ABCD-EF0123456789"}).encode(),
                )
            self.assertEqual(status, 200)
            self.assertEqual(captured["session_id"], "abcdef01-2345-6789-abcd-ef0123456789")
            self.assertIn('"focus_task"', captured["question"])


class _FakeTelegram:
    def __init__(self):
        self._lock = threading.Lock()
        self.sent: list[str] = []
        self._next_id = 0

    def send_message(self, chat_id, text, reply_markup=None):
        with self._lock:
            self.sent.append(str(text))
            self._next_id += 1
        return {"result": {"message_id": self._next_id}}

    def send_rich_message(self, chat_id, document, reply_markup=None):
        with self._lock:
            self.sent.append("rich-message")

    def send_chat_action(self, chat_id, action="typing"):
        with self._lock:
            self.sent.append("typing")
        return {"ok": True}

    def edit_message_text(self, chat_id, message_id, text):
        with self._lock:
            self.sent.append(str(text))
        return {"ok": True}

    def answer_callback_query(self, callback_id, text=None, show_alert=False):
        return {"ok": True}

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if predicate(self.sent):
                    return True
            time.sleep(0.05)
        return False


def _guards_config():
    # workflow_mode="direct" 让 CMS 守卫检查走 not_applicable 快路径（不碰 docker）。
    return (
        SimpleNamespace(workflow_mode="direct", cms_update_docker_socket=""),
        SimpleNamespace(workflow_mode=""),
    )


def _needs_action_task(store: TaskStore, title: str = "哑舍 第一季"):
    task = store.upsert_task(title, "", "https://115cdn.com/s/tg-assistant")
    store.record_event(
        task.id,
        TaskStage.NEEDS_ACTION,
        TaskStatus.NEEDS_ACTION,
        "CMS 整理超时（等待 15 分钟无进展），已停止自动重试，需要人工确认",
    )
    return task


class TelegramAssistantTests(unittest.TestCase):

    def setUp(self):
        # 记忆提取是后台线程，单测里统一关掉，避免二次调用测试桩
        p = patch.object(assistant, "extract_memory_async", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def test_chunk_assistant_text(self):
        self.assertEqual(bridge._chunk_assistant_text(""), [""])
        self.assertEqual(bridge._chunk_assistant_text("短回复"), ["短回复"])
        long_line = "x" * 9000
        chunks = bridge._chunk_assistant_text(long_line, limit=3800)
        self.assertEqual("".join(chunks), long_line)
        self.assertTrue(all(len(chunk) <= 3800 for chunk in chunks))
        many_lines = "\n".join(f"第{i}行" for i in range(2000))
        chunks = bridge._chunk_assistant_text(many_lines, limit=500)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertIn("第1999行", chunks[-1])

    def test_assistant_command_without_pi_reports_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            telegram = _FakeTelegram()
            with patch.object(assistant, "resolve_pi_binary", return_value=""):
                bridge.handle_assistant_command("/助手", "/助手", 42, telegram, store)
            self.assertEqual(len(telegram.sent), 1)
            self.assertIn("pi", telegram.sent[0])

    def test_assistant_command_sends_placeholder_then_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            _needs_action_task(store)
            telegram = _FakeTelegram()
            captured = []

            def fake_run_pi(question, *, session_id, **kwargs):
                captured.append(session_id)
                return {"reply": "结论：整理超时，建议 reprocess。", "session_id": session_id}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi_stream", side_effect=fake_run_pi
            ):
                bridge.handle_assistant_command("/助手 系统现在有什么问题？", "/助手", 42, telegram, store)
                self.assertTrue(telegram.wait_for(lambda sent: any("结论" in m for m in sent)))
                bridge.handle_assistant_command("/助手 还有什么要处理的？", "/助手", 42, telegram, store)
                self.assertTrue(telegram.wait_for(lambda sent: len([m for m in sent if "结论" in m]) >= 2))

            self.assertTrue(any("正在分析" in m for m in telegram.sent))
            # 同一 TG 会话复用同一个 pi session（多轮记忆）
            self.assertEqual(len(captured), 2)
            self.assertEqual(captured[0], captured[1])

    def test_assistant_command_includes_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            _needs_action_task(store)
            telegram = _FakeTelegram()
            captured = {}

            def fake_run_pi(question, *, session_id, **kwargs):
                captured["question"] = question
                return {"reply": "结论：整理超时。", "session_id": session_id}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi_stream", side_effect=fake_run_pi
            ):
                bridge.handle_assistant_command("/助手 系统现在有什么问题？", "/助手", 42, telegram, store)
                self.assertTrue(telegram.wait_for(lambda sent: any("结论" in m for m in sent)))
            self.assertIn("哑舍 第一季", captured["question"], "TG 提问也必须带系统快照")

    def test_assistant_command_focus_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            telegram = _FakeTelegram()
            captured = {}

            def fake_run_pi(question, **kwargs):
                captured["question"] = question
                return {"reply": "诊断内容", "session_id": kwargs["session_id"]}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi_stream", side_effect=fake_run_pi
            ):
                bridge.handle_assistant_command(f"/助手 {task.id} 为什么失败", "/助手", 42, telegram, store)
                self.assertTrue(telegram.wait_for(lambda sent: any("诊断内容" in m for m in sent)))
            self.assertIn('"focus_task"', captured["question"])
            self.assertTrue(any("关于任务 #" in m for m in telegram.sent))

    def test_diagnosis_sweep_diagnoses_once_per_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            telegram = _FakeTelegram()
            config, self_share_config = _guards_config()
            calls = {"count": 0}

            def fake_run_pi(question, **kwargs):
                calls["count"] += 1
                calls["tools"] = kwargs.get("tools")
                return {"reply": f"自动诊断 {calls['count']}", "session_id": kwargs["session_id"]}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=fake_run_pi
            ):
                diagnosed = bridge.run_assistant_diagnosis_sweep(
                    store, telegram, "42", config, self_share_config
                )
                self.assertEqual(diagnosed, 1)
                self.assertTrue(calls["tools"])
                snapshot = store.find_task(task.id)
                diagnosis = snapshot.metadata.get(assistant.DIAGNOSIS_META_KEY)
                self.assertEqual(diagnosis["reply"], "自动诊断 1")
                self.assertTrue(telegram.wait_for(lambda sent: any("已诊断" in m for m in sent)))
                self.assertFalse(any("自动诊断 1" in m for m in telegram.sent))

                # 同一事件不重复诊断
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
                self.assertEqual(calls["count"], 1)

                # 原因变化（新事件）后重新诊断；测试里关掉诊断冷却
                store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.NEEDS_ACTION, "再次整理超时")
                with patch.object(assistant, "diagnosis_cooldown_seconds", return_value=0):
                    bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
                self.assertEqual(calls["count"], 2)
                self.assertEqual(store.find_task(task.id).metadata[assistant.DIAGNOSIS_META_KEY]["reply"], "自动诊断 2")

    def test_diagnosis_sweep_error_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            telegram = _FakeTelegram()
            config, self_share_config = _guards_config()
            calls = {"count": 0}

            def fake_run_pi(question, **kwargs):
                calls["count"] += 1
                raise assistant.AssistantError("pi 退出码 1: no auth")

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=fake_run_pi
            ):
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
                self.assertEqual(calls["count"], 1)
                diagnosis = store.find_task(task.id).metadata[assistant.DIAGNOSIS_META_KEY]
                self.assertIn("no auth", diagnosis["error"])
                # 失败后 6 小时内不重试
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
                self.assertEqual(calls["count"], 1)

    def test_auto_repair_reprocess_once_for_needs_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            telegram = _FakeTelegram()
            config, self_share_config = _guards_config()
            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", return_value={"reply": "整理超时，建议 reprocess", "session_id": "x"}
            ):
                diagnosed = bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
            self.assertEqual(diagnosed, 1)
            diagnosis = store.find_task(task.id).metadata[assistant.DIAGNOSIS_META_KEY]
            self.assertEqual(diagnosis["auto_repair_action"], "reprocess")
            self.assertTrue(diagnosis["auto_repair_applied"])
            self.assertTrue(any("自动执行 reprocess" in m for m in telegram.sent))
            # 同一事件不再二次 reprocess
            telegram.sent.clear()
            with patch.object(assistant, "run_pi", side_effect=AssertionError("should not diagnose again")):
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
            self.assertFalse(any("自动执行" in m for m in telegram.sent))

    def test_auto_repair_skips_share_risk_reprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("违规分享", "", "https://115cdn.com/s/vio")
            store.record_event(
                task.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                "115 标记 have_vio_file，分享不可用",
            )
            telegram = _FakeTelegram()
            with patch.object(assistant, "auto_repair_enabled", return_value=True):
                applied = bridge.maybe_auto_repair_task(store, task.id, telegram, "42")
            self.assertFalse(applied)
            self.assertEqual(telegram.sent, [])

    def test_auto_repair_skips_emby_when_share_became_unavailable(self):
        task = SimpleNamespace(
            status=TaskStatus.NEEDS_ACTION,
            current_stage=TaskStage.CLEANED,
            claimed_by="",
            metadata={},
            retry_count=0,
            error_summary="",
            id=430,
        )
        store = SimpleNamespace(
            list_events=lambda _tid: [
                {"message": "自有分享在异步审核中已变为不可用，源文件已保留，停止自动改名和重建"}
            ]
        )
        with patch("app.task_actions.available_task_actions", return_value=frozenset({"emby", "reprocess", "restore"})):
            self.assertEqual(assistant.choose_auto_repair_action(task, store), "")

    def test_diagnosis_failure_keeps_previous_auto_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            store.patch_metadata(
                task.id,
                {
                    assistant.DIAGNOSIS_META_KEY: {
                        "event_id": 0,
                        "reply": "旧诊断",
                        "auto_repair_action": "reprocess",
                        "auto_repair_applied": True,
                    }
                },
            )
            store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.NEEDS_ACTION, "再次整理超时")
            telegram = _FakeTelegram()
            config, self_share_config = _guards_config()
            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=assistant.AssistantError("timeout")
            ), patch.object(assistant, "diagnosis_cooldown_seconds", return_value=0):
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
            diagnosis = store.find_task(task.id).metadata[assistant.DIAGNOSIS_META_KEY]
            self.assertEqual(diagnosis["auto_repair_action"], "reprocess")
            self.assertTrue(diagnosis["auto_repair_applied"])
            self.assertIn("timeout", diagnosis["error"])

    def test_auto_repair_does_not_repeat_same_action(self):
        task = SimpleNamespace(
            status=TaskStatus.NEEDS_ACTION,
            current_stage=TaskStage.ORGANIZING,
            claimed_by="",
            metadata={
                assistant.DIAGNOSIS_META_KEY: {
                    "auto_repair_action": "resume_organizing",
                    "auto_repair_applied": True,
                    "auto_repair_at": time.time() - 7 * 3600,
                    "auto_repair_tried": ["resume_organizing"],
                }
            },
            retry_count=0,
            error_summary="整理超时",
            id=445,
        )
        with patch(
            "app.task_actions.available_task_actions", return_value=frozenset({"resume_organizing", "reprocess"})
        ):
            self.assertEqual(assistant.choose_auto_repair_action(task, store=None), "reprocess")
        with patch(
            "app.task_actions.available_task_actions", return_value=frozenset({"resume_organizing"})
        ):
            self.assertEqual(assistant.choose_auto_repair_action(task, store=None), "")

    def test_auto_repair_ambiguous_ownership_resumes_once(self):
        task = SimpleNamespace(
            status=TaskStatus.NEEDS_ACTION,
            current_stage=TaskStage.ORGANIZING,
            claimed_by="",
            metadata={},
            retry_count=0,
            error_summary="接收文件归属存在歧义，已停止自动绑定",
            id=432,
        )
        with patch(
            "app.task_actions.available_task_actions", return_value=frozenset({"resume_organizing", "reprocess"})
        ):
            self.assertEqual(assistant.choose_auto_repair_action(task, store=None), "resume_organizing")
        task.metadata = {
            assistant.DIAGNOSIS_META_KEY: {"auto_repair_tried": ["resume_organizing"]}
        }
        with patch(
            "app.task_actions.available_task_actions", return_value=frozenset({"resume_organizing", "reprocess"})
        ):
            self.assertEqual(assistant.choose_auto_repair_action(task, store=None), "")

    def test_diagnosis_quality_event_does_not_rediagnose(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            telegram = _FakeTelegram()
            config, self_share_config = _guards_config()
            calls = {"count": 0}

            def fake_run_pi(question, **kwargs):
                calls["count"] += 1
                self.assertTrue(kwargs.get("tools"))
                return {"reply": "诊断", "session_id": "x"}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=fake_run_pi
            ), patch.object(assistant, "diagnosis_cooldown_seconds", return_value=0), patch.object(
                assistant, "auto_repair_enabled", return_value=False
            ):
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
                store.record_event(
                    task.id,
                    TaskStage.NEEDS_ACTION,
                    TaskStatus.NEEDS_ACTION,
                    "质量巡检记录终态时间（actor=quality-auto）",
                )
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
            self.assertEqual(calls["count"], 1)
            self.assertTrue(any("结论写在任务详情" in m for m in telegram.sent))
            self.assertFalse(any("AI 诊断" in m for m in telegram.sent))

    def test_auto_repair_exhausted_notifies_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            store.patch_metadata(
                task.id,
                {
                    assistant.DIAGNOSIS_META_KEY: {
                        "reply": "旧诊断",
                        "reason": "CMS 整理超时（等待 15 分钟无进展），已停止自动重试，需要人工确认",
                        "auto_repair_action": "reprocess",
                        "auto_repair_applied": True,
                        "auto_repair_tried": ["reprocess"],
                    }
                },
            )
            telegram = _FakeTelegram()
            with patch.object(assistant, "auto_repair_enabled", return_value=True), patch(
                "app.task_actions.available_task_actions", return_value=frozenset({"reprocess"})
            ):
                self.assertFalse(bridge.maybe_auto_repair_task(store, task.id, telegram, "42"))
                self.assertEqual(sum(1 for m in telegram.sent if "仍需人工" in m), 1)
                self.assertFalse(bridge.maybe_auto_repair_task(store, task.id, telegram, "42"))
            self.assertEqual(sum(1 for m in telegram.sent if "仍需人工" in m), 1)

    def test_diagnosis_cooldown_skips_new_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store)
            telegram = _FakeTelegram()
            config, self_share_config = _guards_config()
            calls = {"count": 0}

            def fake_run_pi(question, **kwargs):
                calls["count"] += 1
                return {"reply": "诊断", "session_id": kwargs["session_id"]}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=fake_run_pi
            ), patch.object(assistant, "diagnosis_cooldown_seconds", return_value=6 * 3600):
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
                store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.NEEDS_ACTION, "再次整理超时")
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
            self.assertEqual(calls["count"], 1)

    def test_choose_auto_repair_prefers_retry_over_reprocess(self):
        task = SimpleNamespace(
            status=TaskStatus.FAILED,
            current_stage=TaskStage.ORGANIZING,
            claimed_by="",
            metadata={},
            retry_count=0,
            error_summary="超时",
        )
        with patch("app.task_actions.available_task_actions", return_value=frozenset({"retry", "reprocess"})):
            self.assertEqual(assistant.choose_auto_repair_action(task, store=None), "retry")


class PlainTextAssistantRoutingTests(unittest.TestCase):
    def setUp(self):
        # 记忆提取是后台线程，单测里统一关掉，避免二次调用测试桩
        p = patch.object(assistant, "extract_memory_async", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)


    """免前缀：普通文本直接进 AI 助手，且不劫持链接/追更等既有语义。"""

    def _update(self, text):
        return {
            "message": {
                "chat": {"id": 464100862},
                "from": {"id": 464100862},
                "text": text,
            }
        }

    def _run_handle_update(self, text, telegram, task_store, submission_store, run_pi_side_effect, reply_marker):
        captured = {}

        def fake_run_pi_stream(question, *, session_id, on_update=None, **kwargs):
            captured["question"] = question
            captured["session_id"] = session_id
            return run_pi_side_effect(question, session_id=session_id, **kwargs)

        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant, "run_pi_stream", side_effect=fake_run_pi_stream
        ):
            bridge.handle_update(
                self._update(text),
                FakeCmsSubmit(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=task_store,
            )
            # 助手回复跑在后台线程里，必须在 patch 仍生效时等到最终回复出现
            # （typing/占位提示都会进 sent，不能作为完成信号）。
            telegram.wait_for(
                lambda sent: any(reply_marker in m for m in sent) or any("调用失败" in m for m in sent),
                timeout=10.0,
            )
        return captured

    def test_plain_question_routes_to_assistant_without_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            telegram = _FakeTelegram()
            captured = self._run_handle_update(
                "系统现在有什么问题？",
                telegram,
                store,
                SubmissionStore(Path(tmp) / "submissions.db"),
                lambda question, session_id, **kw: {"reply": "整体健康。", "session_id": session_id},
                reply_marker="整体健康",
            )
            self.assertTrue(any("整体健康" in m for m in telegram.sent))
            # 整条消息必须是问题本身，而不是 /助手 的默认健康问句
            self.assertIn("系统现在有什么问题？", captured["question"])
            self.assertIn("系统快照", captured["question"])

    def test_hash_prefixed_plain_text_focuses_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = _needs_action_task(store, "哑舍 第一季")
            telegram = _FakeTelegram()
            captured = self._run_handle_update(
                f"#{task.id} 这个任务怎么了",
                telegram,
                store,
                SubmissionStore(Path(tmp) / "submissions.db"),
                lambda question, session_id, **kw: {"reply": "整理超时。", "session_id": session_id},
                reply_marker="整理超时",
            )
            self.assertTrue(any("整理超时" in m for m in telegram.sent))
            self.assertIn('"focus_task"', captured["question"])
            self.assertIn("这个任务怎么了", captured["question"])

    def test_plain_text_silent_when_pi_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            telegram = _FakeTelegram()
            with patch.object(assistant, "resolve_pi_binary", return_value=""), patch.object(
                assistant, "run_pi_stream", side_effect=AssertionError("should not be called")
            ):
                bridge.handle_update(
                    self._update("随便聊聊"),
                    FakeCmsSubmit(),
                    telegram,
                    "464100862",
                    SubmissionStore(Path(tmp) / "submissions.db"),
                    poll_status=False,
                    task_store=store,
                )
            self.assertEqual(telegram.sent, [], "pi 未配置时应保持原静默行为")

    def test_link_intake_not_hijacked_by_assistant(self):
        with tempfile.TemporaryDirectory() as tmp:
            submission_store = SubmissionStore(Path(tmp) / "submissions.db")
            store = TaskStore(Path(tmp) / "tasks.db")
            telegram = _FakeTelegram()

            def boom(*args, **kwargs):
                raise AssertionError("115 链接不应进助手")

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi_stream", side_effect=boom
            ):
                bridge.handle_update(
                    self._update("https://115cdn.com/s/abc?password=1234"),
                    FakeCmsSubmit(),
                    telegram,
                    "464100862",
                    submission_store,
                    poll_status=False,
                    task_store=store,
                )
            tasks = store.list_recent_tasks(limit=5)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].share_code, "abc")
            self.assertFalse(any("正在分析" in m for m in telegram.sent), "链接入库不应触发助手占位提示")

    def test_series_update_with_non_link_payload_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            telegram = _FakeTelegram()
            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi_stream", side_effect=AssertionError("追更文本不应进助手")
            ):
                bridge.handle_update(
                    self._update("追更 你好"),
                    FakeCmsSubmit(),
                    telegram,
                    "464100862",
                    SubmissionStore(Path(tmp) / "submissions.db"),
                    poll_status=False,
                    task_store=store,
                )
            # 走追更分支（未启用自分享工作流的提示），绝不进助手
            self.assertTrue(any("追更" in m for m in telegram.sent), telegram.sent)
            self.assertFalse(any("正在分析" in m for m in telegram.sent), telegram.sent)


class MemoryTests(unittest.TestCase):
    """hermes 式长期记忆：注入、提取写回、密钥拦截、容量裁剪。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sessions = Path(self._tmp.name) / "assistant-sessions"
        p = patch.dict(os.environ, {"PI_ASSISTANT_MEMORY_DIR": str(self.sessions.parent / "assistant-memory")})
        p.start()
        self.addCleanup(p.stop)

    def test_memory_disabled_by_env(self):
        with patch.dict(os.environ, {"PI_ASSISTANT_MEMORY": "0"}):
            self.assertEqual(assistant.read_memory_context(self.sessions), "")
            self.assertEqual(assistant.append_memory_entries("- 2026-01-01 测试"), [])

    def test_injection_adds_memory_to_system_prompt(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["sp"] = argv[argv.index("--system-prompt") + 1]
            return _completed_process(stdout=_pi_stdout_events("好的"))

        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess, "run", side_effect=fake_run
        ):
            assistant.append_memory_entries("- 2026-09-06 用户偏好简体中文回复")
            assistant.run_pi("问题", session_id="a" * 32, session_dir=self.sessions, inject_memory=True)
        self.assertIn("长期记忆", captured["sp"])
        self.assertIn("偏好简体中文", captured["sp"])

    def test_no_injection_without_memory(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["sp"] = argv[argv.index("--system-prompt") + 1]
            return _completed_process(stdout=_pi_stdout_events("好的"))

        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess, "run", side_effect=fake_run
        ):
            assistant.run_pi("问题", session_id="a" * 32, session_dir=self.sessions, inject_memory=True)
        self.assertNotIn("长期记忆", captured["sp"])

    def test_extract_appends_new_entries_and_dedupes(self):
        added = assistant.append_memory_entries("- 2026-09-06 Emby 端口是 9096\n- 2026-09-06 Emby 端口是 9096")
        self.assertEqual(len(added), 1)
        memory = (assistant.assistant_memory_dir(self.sessions) / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("Emby 端口是 9096", memory)
        # 完全相同的条目不再重复
        self.assertEqual(assistant.append_memory_entries("- 2026-09-06 Emby 端口是 9096"), [])

    def test_extract_skips_secrets_and_noise(self):
        added = assistant.append_memory_entries(
            "NOTHING\n- 2026-09-06 我的 key 是 sk-abcdefghij123456\n普通文本不是条目"
        )
        self.assertEqual(added, [])
        self.assertFalse((assistant.assistant_memory_dir(self.sessions) / "MEMORY.md").exists())

    def test_memory_trim_keeps_recent_entries(self):
        for i in range(200):
            assistant.append_memory_entries(f"- 2026-01-01 第{i}条测试记忆内容，内容足够长以便撑大文件体积，这里再补充一段文字确保超过阈值")
        content = (assistant.assistant_memory_dir(self.sessions) / "MEMORY.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(content), assistant.MEMORY_MAX_CHARS + 200)
        self.assertIn("第199条", content, "裁剪后应保留较新的条目")
        self.assertNotIn("第0条测试", content)

    def test_extract_memory_async_learns_from_conversation(self):
        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess,
            "run",
            return_value=_completed_process(stdout=_pi_stdout_events("- 2026-09-06 用户在 Unraid 上运行本系统")),
        ):
            assistant.extract_memory_async(
                "我在哪里跑的这个系统？", "你在 Unraid 上运行它。", self.sessions
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                memory_path = assistant.assistant_memory_dir(self.sessions) / "MEMORY.md"
                if memory_path.exists() and "Unraid" in memory_path.read_text(encoding="utf-8"):
                    break
                time.sleep(0.1)
        memory = (assistant.assistant_memory_dir(self.sessions) / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("Unraid", memory)


    def test_memory_entries_are_listed_and_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "assistant-sessions"
            memory_dir = assistant.assistant_memory_dir(session_dir)
            memory_dir.mkdir(parents=True, exist_ok=True)
            (memory_dir / "MEMORY.md").write_text("- 偏好：中文回复\n- 教训：先查事件\n", encoding="utf-8")
            self.assertEqual(assistant.read_memory_entries(session_dir), ["- 偏好：中文回复", "- 教训：先查事件"])
            self.assertEqual(assistant.parse_memory_indexes("1, 3 5"), [1, 3, 5])
            self.assertEqual(assistant.delete_memory_entries([1], session_dir), ["- 偏好：中文回复"])
            self.assertEqual(assistant.read_memory_entries(session_dir), ["- 教训：先查事件"])
            # 越界/非法序号不报错也不删东西
            self.assertEqual(assistant.delete_memory_entries([7], session_dir), [])
            self.assertEqual(assistant.delete_memory_entries([0], session_dir), [])
            self.assertEqual(assistant.read_memory_entries(session_dir), ["- 教训：先查事件"])

    def test_read_memory_entries_without_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(assistant.read_memory_entries(Path(tmp) / "assistant-sessions"), [])

    def test_run_pi_stream_emits_updates_and_final(self):
        import io

        lines = [
            json.dumps({"type": "message_update", "message": {"role": "assistant", "content": [{"type": "text", "text": "第一段"}]}}),
            json.dumps({"type": "tool_call", "tool_name": "task_detail"}),
            json.dumps({"type": "message_update", "message": {"role": "assistant", "content": [{"type": "text", "text": "第一段，第二段"}]}}),
            json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "第一段，第二段"}]}}),
            json.dumps({"type": "agent_settled"}),
        ]
        fake_proc = SimpleNamespace(stdout=iter(lines), stderr=io.StringIO(), stdin=io.StringIO())
        fake_proc.wait = lambda timeout=None: 0
        updates = []
        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess, "Popen", return_value=fake_proc
        ) as fake_popen:
            result = assistant.run_pi_stream(
                "问题",
                session_id="a" * 32,
                session_dir=Path("/tmp/assistant-test"),
                on_update=updates.append,
            )
        self.assertEqual(updates, ["第一段", "第一段，第二段"])
        self.assertEqual(result["reply"], "第一段，第二段")
        argv = fake_popen.call_args.args[0]
        self.assertIn("--tools", argv)
        self.assertNotIn("--no-tools", argv)
        self.assertEqual(fake_popen.call_args.kwargs["input"] if "input" in fake_popen.call_args.kwargs else None, None)

    def test_run_pi_stream_raises_on_failure(self):
        import io

        fake_proc = SimpleNamespace(stdout=iter([]), stderr=io.StringIO("boom"), stdin=io.StringIO())
        fake_proc.wait = lambda timeout=None: 1
        with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
            assistant.subprocess, "Popen", return_value=fake_proc
        ):
            with self.assertRaises(assistant.AssistantError):
                assistant.run_pi_stream("问题", session_id="a" * 32, session_dir=Path("/tmp/assistant-test"))


class AssistantStatsTests(unittest.TestCase):
    """助手自身健康：调用/token/动作统计与会话清理。"""

    def _session_dir(self, tmp: str) -> Path:
        return Path(tmp) / "assistant-sessions"

    def test_run_stats_accumulate_usage_and_reset_failure_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = self._session_dir(tmp)
            assistant.record_assistant_run(ok=False, session_dir=session_dir, error="pi 退出码 1")
            failed = assistant.read_assistant_stats(session_dir)
            self.assertEqual((failed["runs"], failed["failures"]), (1, 1))
            self.assertEqual(failed["consecutive_failures"], 1)
            self.assertEqual(failed["last_error"], "pi 退出码 1")
            assistant.record_assistant_run(
                ok=True,
                session_dir=session_dir,
                usage={"input": 10, "output": 5, "totalTokens": 15, "cost": {"total": 0.25}},
            )
            stats = assistant.read_assistant_stats(session_dir)
            self.assertEqual((stats["runs"], stats["failures"]), (2, 1))
            self.assertEqual(stats["consecutive_failures"], 0)
            self.assertEqual((stats["tokens_input"], stats["tokens_output"], stats["tokens_total"]), (10, 5, 15))
            self.assertAlmostEqual(stats["cost_total"], 0.25)
            status = assistant.assistant_status(session_dir)
            self.assertEqual((status["runs"], status["tokens_total"]), (2, 15))
            self.assertIn("model", status)

    def test_corrupt_stats_file_falls_back_to_zeroes(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = self._session_dir(tmp)
            path = assistant.assistant_stats_path(session_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ 不是 json", encoding="utf-8")
            self.assertEqual(assistant.read_assistant_stats(session_dir)["runs"], 0)

    def test_run_pi_records_usage_without_leaking_it_to_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = self._session_dir(tmp)
            stdout = (
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "结论：可以重试。"}],
                            "usage": {"input": 30, "output": 7, "totalTokens": 37, "cost": {"total": 0.02}},
                        },
                    }
                )
                + "\n"
            )
            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant.subprocess, "run", return_value=_completed_process(stdout=stdout)
            ):
                result = assistant.run_pi("任务 #1 怎么办？", session_id="s-usage", session_dir=session_dir)
            self.assertEqual(result["reply"], "结论：可以重试。")
            self.assertNotIn("_usage", result)
            stats = assistant.read_assistant_stats(session_dir)
            self.assertEqual(stats["tokens_total"], 37)
            self.assertEqual(stats["runs"], 1)

    def test_failed_run_is_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = self._session_dir(tmp)
            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant.subprocess, "run", return_value=_completed_process(stderr="boom", returncode=1)
            ):
                with self.assertRaises(assistant.AssistantError):
                    assistant.run_pi("问题", session_id="s-fail", session_dir=session_dir)
            stats = assistant.read_assistant_stats(session_dir)
            self.assertEqual((stats["runs"], stats["failures"], stats["consecutive_failures"]), (1, 1, 1))

    def test_prune_drops_stale_sessions_and_caps_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = self._session_dir(tmp)
            session_dir.mkdir(parents=True, exist_ok=True)
            fresh = session_dir / "fresh.jsonl"
            fresh.write_text("{}", encoding="utf-8")
            stale = session_dir / "old.jsonl"
            stale.write_text("{}", encoding="utf-8")
            long_ago = time.time() - 30 * 86400
            os.utime(stale, (long_ago, long_ago))
            self.assertEqual(assistant.prune_assistant_sessions(session_dir, keep_days=7.0, max_files=300), 1)
            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())
            # 数量上限：最多保留 max_files 个（新的优先）
            for index in range(3):
                path = session_dir / f"s{index}.jsonl"
                path.write_text("{}", encoding="utf-8")
                stamp = time.time() - index * 60
                os.utime(path, (stamp, stamp))
            self.assertEqual(assistant.prune_assistant_sessions(session_dir, keep_days=7.0, max_files=2), 2)
            self.assertEqual(sorted(path.name for path in session_dir.glob("*.jsonl")), ["fresh.jsonl", "s0.jsonl"])

    def test_prune_on_missing_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(assistant.prune_assistant_sessions(Path(tmp) / "nope"), 0)


class AssistantConcurrencyTests(unittest.TestCase):
    """pi 并发闸门：全局并发位 + 同会话串行。"""

    def test_same_session_second_call_is_rejected(self):
        with assistant._assistant_run_slot("session-a"):
            with self.assertRaises(assistant.AssistantError):
                with assistant._assistant_run_slot("session-a"):
                    self.fail("同一会话不应并发进入")

    def test_different_sessions_run_concurrently(self):
        with assistant._assistant_run_slot("session-b"), assistant._assistant_run_slot("session-c"):
            pass

    def test_queue_timeout_when_all_slots_taken(self):
        holders = []
        try:
            with patch.dict(os.environ, {"PI_ASSISTANT_QUEUE_TIMEOUT": "0"}):
                while True:
                    slot = assistant._assistant_run_slot(f"holder-{len(holders)}")
                    try:
                        slot.__enter__()
                    except assistant.AssistantError:
                        break
                    holders.append(slot)
                self.assertTrue(holders, "至少应有一个并发位")
                with self.assertRaises(assistant.AssistantError):
                    with assistant._assistant_run_slot("late"):
                        pass
        finally:
            for slot in holders:
                slot.__exit__(None, None, None)


class AssistantOpsScriptTests(unittest.TestCase):
    """assistant_ops.py：白名单动作走 Web 同源入口，delete 被拒。"""

    def _run(self, *args):
        env = dict(os.environ, DATABASE_PATH=str(self.db_path))
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def setUp(self):
        import io as _io  # noqa: F401

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "tasks.db"
        self.script = Path(__file__).resolve().parent.parent / "scripts" / "assistant_ops.py"
        store = TaskStore(self.db_path)
        task = store.upsert_task("待终止任务", "", "https://115cdn.com/s/ops")
        store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.PENDING, "等待执行")
        self.task_id = task.id

    def test_terminate_pending_task_applies_and_is_idempotent(self):
        out = self._run("act", str(self.task_id), "terminate")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        payload = json.loads(out.stdout)
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["task"]["status"], "cancelled")
        # 再次终止：幂等（任务已终止）
        out2 = self._run("act", str(self.task_id), "terminate")
        self.assertEqual(out2.returncode, 0)
        self.assertTrue(json.loads(out2.stdout)["applied"])

    def test_delete_action_rejected_by_whitelist(self):
        out = self._run("act", str(self.task_id), "delete")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("invalid choice", out.stderr)

    def test_unsupported_action_for_state_reports_reason(self):
        # running 之外的任务不能 resume_organizing：应给出原因而不是崩溃
        out = self._run("act", str(self.task_id), "resume_organizing")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        payload = json.loads(out.stdout)
        self.assertFalse(payload["applied"])
        self.assertTrue(payload["reason"])

    def test_pi_action_is_written_back_to_diagnosis_metadata(self):
        out = self._run("act", str(self.task_id), "terminate")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        store = TaskStore(self.db_path)
        metadata = store.find_task(self.task_id).metadata or {}
        diagnosis = metadata.get(assistant.DIAGNOSIS_META_KEY) or {}
        self.assertEqual(diagnosis.get("auto_repair_source"), "pi")
        self.assertEqual(diagnosis.get("auto_repair_action"), "terminate")
        self.assertTrue(diagnosis.get("auto_repair_applied"))
        self.assertIn("terminate", diagnosis.get("auto_repair_tried") or [])
        stats = assistant.read_assistant_stats(assistant.assistant_session_dir(store))
        self.assertEqual(stats["actions_applied"], 1)
        self.assertEqual(stats["last_action"], "terminate")

    def test_automation_session_cannot_terminate(self):
        env = dict(os.environ, DATABASE_PATH=str(self.db_path), CMS_TOOLS_AUTOMATION="1")
        out = subprocess.run(
            [sys.executable, str(self.script), "act", str(self.task_id), "terminate"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        payload = json.loads(out.stdout)
        self.assertFalse(payload["applied"])
        self.assertIn("自动巡检", payload["reason"])
        store = TaskStore(self.db_path)
        self.assertEqual(str(store.find_task(self.task_id).status.value), "pending")


if __name__ == "__main__":
    unittest.main()