import json
import tempfile
import unittest
from pathlib import Path

import bridge
from app.config import Config
from app.models import TaskStage, TaskStatus
from app.quality_automation import QualityAutomation, QualityRepairPlan, QualityRunSummary
from app.task_store import TaskStore
from app.telegram_ui import (
    format_quality_manual_report,
    format_quality_scan_summary,
    quality_manual_keyboard,
    quality_manual_rows,
)


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


class FailingEditTelegram(FakeTelegram):
    def __init__(self, error: Exception):
        super().__init__()
        self._error = error

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        raise self._error


class NotModifiedEditTelegram(FailingEditTelegram):
    """Telegram that rejects identical edits like the real API does."""

    def __init__(self):
        super().__init__(
            RuntimeError(
                "HTTP 400 from https://api.telegram.org/bot1: "
                '{"ok":false,"error_code":400,"description":"Bad Request: message is not modified"}'
            )
        )


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
        self.assertIn("质量任务", format_quality_manual_report(rows).to_plain())

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

            self.assertIn("质量巡检：发现", telegram.messages[-1][1])
            self.assertIn("Web 质量页", telegram.messages[-1][1])
            self.assertIsNone(telegram.messages[-1][2])

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

    def test_quality_callback_noop_press_does_not_duplicate_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, task = self.make_service(tmp)
            service.store.mark_quality_ignored(task.id, "test", rule_id="strm_mode_mismatch")
            telegram = NotModifiedEditTelegram()
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")

            bridge.handle_update(
                {
                    "callback_query": {
                        "id": "quality-noop",
                        "from": {"id": 464100862},
                        "message": {"chat": {"id": 464100862}, "message_id": 17},
                        "data": f"quality:ignore:{task.id}:strm_mode_mismatch:1",
                    }
                },
                object(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=service.store,
                task_engine_enabled=True,
                quality_automation=service,
            )

            # Ignored task only allows resume; the no-op press leaves the queue
            # unchanged, the identical edit is rejected, and NO duplicate is sent.
            self.assertEqual(telegram.messages, [])
            self.assertEqual(len(telegram.edits), 1)
            self.assertIn("规则或操作已过期", telegram.answers[-1][1])

    def test_quality_callback_other_edit_failure_still_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, task = self.make_service(tmp)
            telegram = FailingEditTelegram(RuntimeError("HTTP 500 from telegram"))
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")

            bridge.handle_update(
                {
                    "callback_query": {
                        "id": "quality-edit500",
                        "from": {"id": 464100862},
                        "message": {"chat": {"id": 464100862}, "message_id": 18},
                        "data": f"quality:ignore:{task.id}:strm_mode_mismatch:1",
                    }
                },
                object(),
                telegram,
                "464100862",
                submission_store,
                poll_status=False,
                task_store=service.store,
                task_engine_enabled=True,
                quality_automation=service,
            )

            # A genuine failure (not "not modified") still refreshes via a new message.
            self.assertEqual(len(telegram.messages), 1)

    def test_notify_quality_run_dedupes_persistent_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _task = self.make_service(tmp)
            telegram = FakeTelegram()
            summary = QualityRunSummary(
                run_id="run-f",
                status="failed",
                failed_count=1,
                plans=(
                    QualityRepairPlan(
                        task_id=7,
                        action="reprocess",
                        reason="probe boom",
                        rule_id="strm_mode_mismatch",
                        execution_status="failed",
                    ),
                ),
            )

            bridge.notify_quality_run(service, telegram, "464100862", summary)
            bridge.notify_quality_run(service, telegram, "464100862", summary)

            self.assertEqual(len(telegram.messages), 1)

    def test_notify_quality_run_dedupes_run_level_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _task = self.make_service(tmp)
            telegram = FakeTelegram()
            summary = QualityRunSummary(
                run_id="run-e",
                status="failed",
                failed_count=1,
                error="SomeError: boom",
            )

            bridge.notify_quality_run(service, telegram, "464100862", summary)
            bridge.notify_quality_run(service, telegram, "464100862", summary)

            self.assertEqual(len(telegram.messages), 1)

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

    def test_quality_scan_summary_is_count_only(self):
        empty = format_quality_scan_summary([])
        counted = format_quality_scan_summary(
            [{"rule_id": "unexpected_strm"}, {"rule_id": "no_issue"}, {"rule_id": "strm_mode_mismatch"}]
        )
        self.assertEqual(empty, "质量巡检：未发现需要关注的本地 STRM 问题。")
        self.assertEqual(counted, "质量巡检：发现 2 个问题，请到 Web 质量页查看。")

    def test_quality_startup_does_not_pause_invalid_share_probe_when_scan_enabled(self):
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        self.assertIn("on_enabled_changed=None", source)
        self.assertIn("set_invalid_probe_enabled(False)", source)
        self.assertNotIn(
            "set_invalid_probe_enabled(bool(quality_automation.config.quality_auto_enabled))",
            source,
        )


if __name__ == "__main__":
    unittest.main()
