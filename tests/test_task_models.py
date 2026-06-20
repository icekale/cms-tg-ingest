import unittest

from app.models import RetryAction, TaskStage, TaskStatus, next_stage_after_success, terminal_stages


class TaskModelTests(unittest.TestCase):
    def test_stage_values_match_v02_design(self):
        self.assertEqual(TaskStage.RECEIVED.value, "received")
        self.assertEqual(TaskStage.CMS_SUBMITTED.value, "cms_submitted")
        self.assertEqual(TaskStage.ORGANIZED.value, "organized")
        self.assertEqual(TaskStage.OWN_SHARE_CREATED.value, "own_share_created")
        self.assertEqual(TaskStage.SHARE_SYNC_SUBMITTED.value, "share_sync_submitted")
        self.assertEqual(TaskStage.STRM_READY.value, "strm_ready")
        self.assertEqual(TaskStage.MOVED.value, "moved")
        self.assertEqual(TaskStage.EMBY_CONFIRMED.value, "emby_confirmed")
        self.assertEqual(TaskStage.CLEANED.value, "cleaned")
        self.assertEqual(TaskStage.NEEDS_ACTION.value, "needs_action")
        self.assertEqual(TaskStage.FAILED.value, "failed")

    def test_success_next_stage_flow(self):
        self.assertEqual(next_stage_after_success(TaskStage.RECEIVED), TaskStage.CMS_SUBMITTED)
        self.assertEqual(next_stage_after_success(TaskStage.CMS_SUBMITTED), TaskStage.ORGANIZED)
        self.assertEqual(next_stage_after_success(TaskStage.ORGANIZED), TaskStage.OWN_SHARE_CREATED)
        self.assertEqual(next_stage_after_success(TaskStage.OWN_SHARE_CREATED), TaskStage.SHARE_SYNC_SUBMITTED)
        self.assertEqual(next_stage_after_success(TaskStage.SHARE_SYNC_SUBMITTED), TaskStage.STRM_READY)
        self.assertEqual(next_stage_after_success(TaskStage.STRM_READY), TaskStage.MOVED)
        self.assertEqual(next_stage_after_success(TaskStage.MOVED), TaskStage.EMBY_CONFIRMED)
        self.assertEqual(next_stage_after_success(TaskStage.EMBY_CONFIRMED), TaskStage.CLEANED)
        self.assertIsNone(next_stage_after_success(TaskStage.CLEANED))

    def test_status_and_retry_action_values_are_stable(self):
        self.assertEqual(TaskStatus.PENDING.value, "pending")
        self.assertEqual(TaskStatus.RUNNING.value, "running")
        self.assertEqual(TaskStatus.SUCCEEDED.value, "succeeded")
        self.assertEqual(TaskStatus.FAILED.value, "failed")
        self.assertEqual(TaskStatus.NEEDS_ACTION.value, "needs_action")
        self.assertEqual(RetryAction.RETRY_CURRENT_STAGE.value, "retry_current_stage")
        self.assertEqual(RetryAction.MANUAL_ACTION_REQUIRED.value, "manual_action_required")

    def test_terminal_stages(self):
        self.assertIn(TaskStage.CLEANED, terminal_stages())
        self.assertIn(TaskStage.NEEDS_ACTION, terminal_stages())
        self.assertIn(TaskStage.FAILED, terminal_stages())
        self.assertNotIn(TaskStage.MOVED, terminal_stages())


if __name__ == "__main__":
    unittest.main()
