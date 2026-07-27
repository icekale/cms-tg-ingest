import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import TaskStage, TaskStatus
from app.task_actions import apply_task_action, available_task_actions
from app.task_store import TaskStore


class TaskActionsTest(unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return TaskStore(Path(tmp.name) / "tasks.db")

    def test_claimed_running_task_has_no_mutating_actions(self):
        store = self.make_store()
        task = store.upsert_task("claimed", "", "https://115cdn.com/s/claimed")
        store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=0)
        store.claim_next_runnable("worker", now=0)
        task = store.find_task(task.id)

        self.assertEqual(available_task_actions(task, 3), frozenset())
        result = apply_task_action(store, task.id, "retry", max_retries=3, actor="test")
        self.assertFalse(result.applied)
        self.assertEqual(result.task, task)

    def test_failed_retryable_task_allows_retry_and_reprocess(self):
        store = self.make_store()
        task = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
        task = store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed")

        self.assertEqual(available_task_actions(task, 3), frozenset({"retry", "reprocess"}))
        result = apply_task_action(store, task.id, "retry", max_retries=3, actor="test")
        self.assertTrue(result.applied)
        self.assertEqual(result.task.current_stage, TaskStage.STRM_READY)
        self.assertEqual(result.task.status, TaskStatus.PENDING)

    def test_needs_action_allows_only_reprocess(self):
        store = self.make_store()
        task = store.upsert_task("needs", "", "https://115cdn.com/s/needs")
        task = store.record_event(task.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "choose")

        self.assertEqual(available_task_actions(task, 3), frozenset({"reprocess"}))
        result = apply_task_action(store, task.id, "retry", max_retries=3, actor="test")
        self.assertFalse(result.applied)
        self.assertIn("人工", result.reason)

    def test_cleaned_share_allows_downstream_recovery_and_reprocess(self):
        store = self.make_store()
        task = store.upsert_task("cleaned", "", "https://115cdn.com/s/cleaned")
        task = store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

        self.assertEqual(
            available_task_actions(task, 3),
            frozenset({"emby", "restore", "reprocess"}),
        )

    def test_failed_cloud_task_retries_and_reprocesses_from_cloud_download(self):
        store = self.make_store()
        task = store.upsert_cloud_task("magnet:abc", "magnet:?xt=urn:btih:abc")
        task = store.record_event(task.id, TaskStage.CLOUD_DOWNLOADING, TaskStatus.FAILED, "cloud failed")

        self.assertEqual(available_task_actions(task, 3), frozenset({"retry", "reprocess"}))
        result = apply_task_action(store, task.id, "reprocess", max_retries=3, actor="test")
        self.assertTrue(result.applied)
        self.assertEqual(result.task.current_stage, TaskStage.CLOUD_DOWNLOADING)
        self.assertEqual(result.task.metadata.get("strm_mode"), "shared")

    def test_retry_count_at_limit_rejects_retry(self):
        store = self.make_store()
        task = store.upsert_task("limited", "", "https://115cdn.com/s/limited")
        task = store.record_event(
            task.id,
            TaskStage.STRM_READY,
            TaskStatus.FAILED,
            "failed",
            increment_retry=True,
        )
        task = store.record_event(
            task.id,
            TaskStage.STRM_READY,
            TaskStatus.FAILED,
            "failed again",
            increment_retry=True,
        )
        task = store.record_event(
            task.id,
            TaskStage.STRM_READY,
            TaskStatus.FAILED,
            "failed third",
            increment_retry=True,
        )

        self.assertNotIn("retry", available_task_actions(task, 3))

    def test_stale_snapshot_cas_does_not_clear_new_claim(self):
        store = self.make_store()
        task = store.upsert_task("race", "", "https://115cdn.com/s/race")
        task = store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed")
        original = store.compare_and_set_transition

        def claim_then_compare(*args, **kwargs):
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.PENDING,
                "requeued by another actor",
                clear_claim=True,
                next_run_at=0,
            )
            store.claim_next_runnable("new-worker", now=0)
            return original(*args, **kwargs)

        with patch.object(store, "compare_and_set_transition", side_effect=claim_then_compare):
            result = apply_task_action(store, task.id, "retry", max_retries=3, actor="test")

        self.assertFalse(result.applied)
        self.assertEqual(store.find_task(task.id).claimed_by, "new-worker")


if __name__ == "__main__":
    unittest.main()
