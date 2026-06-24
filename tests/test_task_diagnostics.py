import unittest

from app.models import TaskSnapshot, TaskStage, TaskStatus
from app.task_diagnostics import classify_stuck_task, describe_task_wait


def make_task(**overrides):
    values = {
        "id": 1,
        "share_code": "abc",
        "receive_code": "",
        "url": "https://example.invalid/s/abc",
        "title": "",
        "tmdb_id": "",
        "category": "",
        "current_stage": TaskStage.RECEIVED,
        "status": TaskStatus.PENDING,
        "error_type": "",
        "error_summary": "",
        "retry_count": 0,
        "metadata": {},
        "created_at": 0.0,
        "updated_at": 0.0,
        "next_run_at": 0.0,
    }
    values.update(overrides)
    return TaskSnapshot(**values)


class TaskDiagnosticsTests(unittest.TestCase):
    def test_describe_task_wait_includes_reason_elapsed_next_check_and_count(self):
        task = make_task(
            metadata={"_defer_message": "等待自有分享 STRM", "_defer_count": 3},
            updated_at=100.0,
            next_run_at=145.0,
        )

        description = describe_task_wait(task, now=130.0)

        self.assertIn("等待自有分享 STRM", description)
        self.assertIn("已等待 30 秒", description)
        self.assertIn("下次检查 15 秒后", description)
        self.assertIn("第 3 次", description)

    def test_describe_task_wait_ignores_non_numeric_defer_count(self):
        task = make_task(
            metadata={"_defer_message": "等待自有分享 STRM", "_defer_count": "not-a-number"},
            updated_at=100.0,
            next_run_at=145.0,
        )

        description = describe_task_wait(task, now=130.0)

        self.assertIn("等待自有分享 STRM", description)
        self.assertNotIn("第 ", description)

    def test_classify_stuck_task_reports_old_running_deferred_stage(self):
        task = make_task(
            current_stage=TaskStage.ORGANIZING,
            status=TaskStatus.RUNNING,
            updated_at=0.0,
            metadata={"_defer_message": "等待 CMS 整理完成", "_defer_count": 31},
        )

        issue = classify_stuck_task(task, now=3600.0)

        self.assertEqual(issue.code, "stuck_stage")
        self.assertEqual(issue.stage, TaskStage.ORGANIZING)
        self.assertIn("等待 CMS 整理完成", issue.message)

    def test_classify_stuck_task_ignores_recent_task(self):
        task = make_task(updated_at=100.0)

        issue = classify_stuck_task(task, now=120.0)

        self.assertEqual(issue.code, "")


if __name__ == "__main__":
    unittest.main()
