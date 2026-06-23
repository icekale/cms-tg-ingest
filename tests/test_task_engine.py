import unittest

from app.models import RetryAction, TaskStage, TaskStatus, TaskSnapshot
from app.task_engine import decide_retry, stage_display_name


def snapshot(stage, status=TaskStatus.FAILED, error_type="", retry_count=0):
    return TaskSnapshot(
        id=1,
        share_code="abc",
        receive_code="",
        url="https://115cdn.com/s/abc",
        title="示例",
        tmdb_id="123",
        category="欧美电影",
        current_stage=stage,
        status=status,
        error_type=error_type,
        error_summary="失败",
        retry_count=retry_count,
        created_at=1,
        updated_at=2,
    )


class TaskEngineTests(unittest.TestCase):
    def test_failed_strm_stage_retries_current_stage(self):
        decision = decide_retry(snapshot(TaskStage.STRM_READY, error_type="strm_missing"), max_retries=3)

        self.assertEqual(decision.action, RetryAction.RETRY_CURRENT_STAGE)
        self.assertEqual(decision.stage, TaskStage.STRM_READY)
        self.assertIn("STRM", decision.reason)

    def test_failed_emby_stage_retries_emby_confirmation(self):
        decision = decide_retry(snapshot(TaskStage.EMBY_CONFIRMED, error_type="emby_timeout"), max_retries=3)

        self.assertEqual(decision.action, RetryAction.RETRY_CURRENT_STAGE)
        self.assertEqual(decision.stage, TaskStage.EMBY_CONFIRMED)

    def test_needs_action_requires_manual_action(self):
        decision = decide_retry(snapshot(TaskStage.NEEDS_ACTION, status=TaskStatus.NEEDS_ACTION), max_retries=3)

        self.assertEqual(decision.action, RetryAction.MANUAL_ACTION_REQUIRED)
        self.assertIsNone(decision.stage)

    def test_cleaned_task_has_no_retry(self):
        decision = decide_retry(snapshot(TaskStage.CLEANED, status=TaskStatus.SUCCEEDED), max_retries=3)

        self.assertEqual(decision.action, RetryAction.NO_RETRY)
        self.assertIsNone(decision.stage)

    def test_retry_limit_blocks_retry(self):
        decision = decide_retry(snapshot(TaskStage.MOVED, retry_count=3), max_retries=3)

        self.assertEqual(decision.action, RetryAction.MANUAL_ACTION_REQUIRED)
        self.assertIn("超过", decision.reason)

    def test_stage_display_names_are_chinese(self):
        self.assertEqual(stage_display_name(TaskStage.CMS_SUBMITTED), "提交 CMS")
        self.assertEqual(stage_display_name(TaskStage.ORGANIZING), "CMS 整理")
        self.assertEqual(stage_display_name(TaskStage.RECOGNIZING), "识别分类")
        self.assertEqual(stage_display_name(TaskStage.EMBY_CONFIRMED), "Emby 确认")
