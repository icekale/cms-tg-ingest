import json
import tempfile
import unittest
from pathlib import Path

import bridge
from app.config import Config
from app.models import TaskStage, TaskStatus
from app.quality_automation import QualityAutomation, QualityRepairPlan, QualityRunSummary
from app.task_store import TaskStore
from app.telegram_ui import format_quality_manual_report, quality_manual_keyboard, quality_manual_rows


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))

    def answer_callback_query(self, callback_id, text="", show_alert=False):
        self.answers.append((callback_id, text, show_alert))


class QualityTelegramTests(unittest.TestCase):
    @staticmethod
    def make_service(tmp):
        root = Path(tmp) / "library"
        destination = root / "direct"
        destination.mkdir(parents=True)
        (destination / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="464100862",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=False,
        )
        store = TaskStore(Path(tmp) / "tasks.db")
        task = store.upsert_task("quality-telegram", "", "https://115cdn.com/s/quality-telegram")
        task = store.record_event(
            task.id,
            TaskStage.MOVED,
            TaskStatus.SUCCEEDED,
            "moved",
            title="质量 Telegram 任务",
            metadata_patch={
                "dest_path": str(destination),
                "own_share_code": "own",
                "own_share_receive_code": "1212",
            },
        )
        return QualityAutomation(store, config, allowed_roots=[root]), task

    def test_quality_manual_rows_and_keyboard_are_compact_and_safe(self):
        rows = [
            {
                "task_id": 12,
                "title": "质量任务",
                "rule_id": "missing_destination",
                "rule_version": "1",
                "manual_status": "manual_required",
                "risk_level": "medium",
                "rule_reason": "需要人工确认",
                "available_actions": ["view", "reprocess", "snooze", "ignore"],
                "evidence": ["/private/path/movie.strm"],
            }
        ]

        selected = quality_manual_rows(rows)
        keyboard = quality_manual_keyboard(selected)
        data = [button["callback_data"] for line in keyboard["inline_keyboard"] for button in line]

        self.assertEqual(len(selected), 1)
        self.assertTrue(data)
        self.assertTrue(all(len(value) <= 64 for value in data))
        self.assertTrue(all("/private/path" not in value for value in data))
        self.assertIn("质量任务", format_quality_manual_report(rows))

    def test_quality_command_shows_rule_queue_and_callbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, task = self.make_service(tmp)
            telegram = FakeTelegram()
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")

            bridge.handle_update(
                {"message": {"chat": {"id": 464100862}, "from": {"id": 464100862}, "text": "/quality"}},
                object(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=service.store,
                task_engine_enabled=True,
                quality_automation=service,
            )

            self.assertIn("质量 Telegram 任务", telegram.messages[-1][1])
            buttons = [button for line in (telegram.messages[-1][2] or {}).get("inline_keyboard", []) for button in line]
            self.assertTrue(buttons)
            self.assertTrue(all(button["callback_data"].startswith("quality:") for button in buttons))
            self.assertEqual(json.loads(json.dumps(telegram.messages[-1][2], ensure_ascii=False))["inline_keyboard"][0][0]["callback_data"].split(":")[2], str(task.id))

    def test_quality_callback_uses_automation_cas_and_refreshes_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, task = self.make_service(tmp)
            telegram = FakeTelegram()
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            update = {
                "callback_query": {
                    "id": "quality-ignore",
                    "from": {"id": 464100862},
                    "message": {"chat": {"id": 464100862}, "message_id": 17},
                    "data": f"quality:ignore:{task.id}:strm_mode_mismatch:1",
                }
            }

            bridge.handle_update(
                update,
                object(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=service.store,
                task_engine_enabled=True,
                quality_automation=service,
            )

            self.assertEqual(service.store.quality_state(task.id)["quality_manual_status"], "ignored")
            self.assertEqual(telegram.answers[-1][1], "已忽略")
            self.assertEqual(telegram.edits[-1][1], 17)

    def test_quality_callback_rejects_stale_rule_version_without_changing_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, task = self.make_service(tmp)
            telegram = FakeTelegram()
            bridge.handle_update(
                {
                    "callback_query": {
                        "id": "quality-stale",
                        "from": {"id": 464100862},
                        "message": {"chat": {"id": 464100862}, "message_id": 18},
                        "data": f"quality:ignore:{task.id}:strm_mode_mismatch:999",
                    }
                },
                object(),
                telegram,
                "464100862",
                bridge.SubmissionStore(Path(tmp) / "submissions.db"),
                poll_status=False,
                task_store=service.store,
                task_engine_enabled=True,
                quality_automation=service,
            )

            self.assertEqual(service.store.quality_state(task.id)["quality_manual_status"], "open")
            self.assertIn("规则或操作已过期", telegram.answers[-1][1])

    def test_notify_quality_run_ignores_terminal_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _task = self.make_service(tmp)
            telegram = FakeTelegram()
            summary = QualityRunSummary(
                run_id="run-x",
                status="succeeded",
                plans=(
                    QualityRepairPlan(
                        task_id=1,
                        action="skip",
                        reason="terminal_task",
                        execution_status="skipped",
                    ),
                ),
            )

            bridge.notify_quality_run(service, telegram, "464100862", summary)
            bridge.notify_quality_run(service, telegram, "464100862", summary)

            self.assertEqual(telegram.messages, [])

    def test_notify_quality_run_sends_new_actionable_work_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _task = self.make_service(tmp)
            telegram = FakeTelegram()
            summary = QualityRunSummary(
                run_id="run-y",
                status="succeeded",
                plans=(
                    QualityRepairPlan(
                        task_id=7,
                        action="reprocess",
                        reason="strm_mode_mismatch",
                        rule_id="strm_mode_mismatch",
                        execution_status="queued",
                    ),
                ),
            )

            bridge.notify_quality_run(service, telegram, "464100862", summary)
            bridge.notify_quality_run(service, telegram, "464100862", summary)

            self.assertEqual(len(telegram.messages), 1)
            self.assertIn("质量", telegram.messages[0][1])


if __name__ == "__main__":
    unittest.main()
