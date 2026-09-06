import json
import subprocess
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
        for flag in ("--print", "--mode", "--no-extensions", "--no-skills", "--no-tools", "--no-approve"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(argv[argv.index("--model") + 1], "glm/*")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], assistant.ASSISTANT_SYSTEM_PROMPT)
        # 问题正文通过 stdin 传递，不占 argv。
        self.assertEqual(recorded["kwargs"]["input"], "为什么任务失败？")

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


class AssistantChatEndpointTests(unittest.TestCase):
    def _make_app(self, tmp: str, task_title: str = "哑舍 S01"):
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task(task_title, "", "https://115cdn.com/s/assistant")
        store.record_event(task.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "整理超时，等待人工处理")
        return WebApp(store), task

    def test_chat_returns_reply_and_echoes_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, task = self._make_app(tmp)
            captured = {}

            def fake_run_pi(question, *, session_id, session_dir, model="", timeout=0):
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

    def send_message(self, chat_id, text, reply_markup=None):
        with self._lock:
            self.sent.append(str(text))

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
                assistant, "run_pi", side_effect=fake_run_pi
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
                assistant, "run_pi", side_effect=fake_run_pi
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
                assistant, "run_pi", side_effect=fake_run_pi
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
                return {"reply": f"自动诊断 {calls['count']}", "session_id": kwargs["session_id"]}

            with patch.object(assistant, "resolve_pi_binary", return_value="pi"), patch.object(
                assistant, "run_pi", side_effect=fake_run_pi
            ):
                diagnosed = bridge.run_assistant_diagnosis_sweep(
                    store, telegram, "42", config, self_share_config
                )
                self.assertEqual(diagnosed, 1)
                snapshot = store.find_task(task.id)
                diagnosis = snapshot.metadata.get(assistant.DIAGNOSIS_META_KEY)
                self.assertEqual(diagnosis["reply"], "自动诊断 1")
                self.assertTrue(telegram.wait_for(lambda sent: any("AI 诊断" in m for m in sent)))

                # 同一事件不重复诊断
                bridge.run_assistant_diagnosis_sweep(store, telegram, "42", config, self_share_config)
                self.assertEqual(calls["count"], 1)

                # 原因变化（新事件）后重新诊断
                store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.NEEDS_ACTION, "再次整理超时")
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


if __name__ == "__main__":
    unittest.main()