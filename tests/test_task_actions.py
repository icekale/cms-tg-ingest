import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import TaskStage, TaskStatus
from app.task_actions import apply_task_action, available_lifecycle_actions, available_task_actions
from app.task_runner import TaskRunner
from app.task_store import TaskStore, WorkflowRowAdapter
from tests.legacy_submission_store import SubmissionStore


class TaskActionsTest(unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return TaskStore(Path(tmp.name) / "tasks.db")

    def drain_commands(self, store: TaskStore) -> None:
        class IdleWorkflow:
            def run_stage(self, task):
                raise AssertionError("stage should not run while draining commands")

        runner = TaskRunner(store, IdleWorkflow(), worker_id="test-runner")
        while True:
            command = store.claim_next_command(runner.worker_id)
            if not command:
                return
            runner._run_command(command)

    def test_retry_enqueues_command_instead_of_cas(self):
        store = self.make_store()
        task = store.upsert_task("retry-queue", "", "https://115cdn.com/s/retry-queue")
        task = store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed")
        with patch.object(store, "compare_and_set_transition", side_effect=AssertionError("cas")):
            result = apply_task_action(store, task.id, "retry", max_retries=3, actor="Web")
        self.assertTrue(result.applied)
        self.assertEqual(store.find_task(task.id).status, TaskStatus.FAILED)
        command = store.claim_next_command("inspector")
        self.assertEqual(command["command_type"], "retry")
        self.assertEqual(command["actor"], "Web")

    def test_resume_organizing_requeues_without_new_receive_generation(self):
        store = self.make_store()
        task = store.upsert_task("resume", "", "https://115cdn.com/s/resume")
        task = store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "等待 CMS 整理完成",
            metadata_patch={
                "_defer_stage": TaskStage.ORGANIZING.value,
                "intake_identity": {"root_ids": ["root"], "files": [{"id": "file"}]},
            },
        )
        operation = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {"share_code": "resume"})
        self.assertIsNotNone(operation)
        store.start_operation(task.id, operation.operation_key)
        store.complete_operation(task.id, operation.operation_key, {"received": True})

        result = apply_task_action(store, task.id, "resume_organizing", max_retries=3, actor="Web")
        self.assertTrue(result.applied)
        self.drain_commands(store)
        resumed = store.find_task(task.id)
        self.assertEqual(resumed.current_stage, TaskStage.ORGANIZING)
        self.assertEqual(resumed.status, TaskStatus.PENDING)
        self.assertFalse(resumed.metadata.get("_defer_stage"))
        self.assertEqual(len([op for op in store.list_operations(task.id) if op.operation_type == "receive_share"]), 1)
        self.assertNotIn("own_share_code", resumed.metadata)

    def test_resume_organizing_accepts_legacy_retry_stage_marker(self):
        store = self.make_store()
        task = store.upsert_task("resume-legacy", "", "https://115cdn.com/s/resume-legacy")
        task = store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "CMS 整理等待超时",
            metadata_patch={
                "retry_from_stage": TaskStage.ORGANIZING.value,
                "retry_stage": TaskStage.ORGANIZING.value,
                "intake_identity": {"root_ids": ["root"], "files": [{"id": "file"}]},
            },
        )
        operation = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {})
        store.start_operation(task.id, operation.operation_key)
        store.complete_operation(task.id, operation.operation_key, {"received": True})

        self.assertIn("resume_organizing", available_task_actions(task, 3, store=store))

    def test_resume_organizing_accepts_legacy_direct_organizing_needs_action(self):
        store = self.make_store()
        task = store.upsert_task("resume-direct", "", "https://115cdn.com/s/resume-direct")
        task = store.record_event(
            task.id,
            TaskStage.ORGANIZING,
            TaskStatus.NEEDS_ACTION,
            "接收文件归属存在歧义",
            error_type="needs_action",
            metadata_patch={
                "intake_identity": {"root_ids": ["root"], "files": [{"id": "file"}]},
            },
        )
        operation = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {})
        store.start_operation(task.id, operation.operation_key)
        store.complete_operation(task.id, operation.operation_key, {"received": True})

        self.assertIn("resume_organizing", available_task_actions(task, 3, store=store))
        result = apply_task_action(store, task.id, "resume_organizing", max_retries=3, actor="Web")

        self.assertTrue(result.applied)
        self.drain_commands(store)
        resumed = store.find_task(task.id)
        self.assertEqual(resumed.current_stage, TaskStage.ORGANIZING)
        self.assertEqual(resumed.status, TaskStatus.PENDING)

    def test_resume_organizing_rejects_existing_create_share_operation(self):
        store = self.make_store()
        task = store.upsert_task("resume-share", "", "https://115cdn.com/s/resume-share")
        task = store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "等待 CMS 整理完成",
            metadata_patch={
                "_defer_stage": TaskStage.ORGANIZING.value,
                "intake_identity": {"root_ids": ["root"], "files": [{"id": "file"}]},
            },
        )
        receive = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {})
        store.start_operation(task.id, receive.operation_key)
        store.complete_operation(task.id, receive.operation_key, {"received": True})
        store.prepare_operation(task.id, "g0:u0:create_share:dest-a", "create_share", {})

        self.assertNotIn("resume_organizing", available_task_actions(task, 3, store=store))
        result = apply_task_action(store, task.id, "resume_organizing", max_retries=3, actor="Web")
        self.assertFalse(result.applied)

    def test_resume_organizing_requires_existing_receive_and_snapshot(self):
        store = self.make_store()
        task = store.upsert_task("resume-invalid", "", "https://115cdn.com/s/resume-invalid")
        task = store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "等待 CMS 整理完成",
            metadata_patch={"_defer_stage": TaskStage.ORGANIZING.value},
        )
        self.assertNotIn("resume_organizing", available_task_actions(task, 3, store=store))
        result = apply_task_action(store, task.id, "resume_organizing", max_retries=3, actor="Web")
        self.assertFalse(result.applied)

    def test_resume_organizing_rejects_malformed_legacy_intake_identity(self):
        store = self.make_store()
        task = store.upsert_task("resume-malformed", "", "https://115cdn.com/s/resume-malformed")
        task = store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "CMS 整理等待超时",
            metadata_patch={
                "retry_from_stage": TaskStage.ORGANIZING.value,
                "retry_stage": TaskStage.ORGANIZING.value,
                "intake_identity": {"root_ids": [""], "files": [{}]},
            },
        )
        operation = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {})
        store.start_operation(task.id, operation.operation_key)
        store.complete_operation(task.id, operation.operation_key, {"received": True})

        self.assertNotIn("resume_organizing", available_task_actions(task, 3, store=store))

    def test_reprocess_preserves_received_snapshot_after_successful_receive(self):
        store = self.make_store()
        task = store.upsert_task("reprocess-received", "", "https://115cdn.com/s/reprocess-received")
        task = store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "整理目录冲突",
            metadata_patch={
                "intake_identity": {
                    "root_ids": ["received-root"],
                    "files": [{"id": "file-a", "name": "file-a.mkv"}],
                },
                "received_items": [{"file_id": "received-root", "file_name": "Root", "is_folder": True}],
                "received_file_ids": ["file-a"],
                "received_snapshot_complete": True,
                "_lock_key": "115:global",
            },
        )
        operation = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {})
        store.start_operation(task.id, operation.operation_key)
        store.complete_operation(task.id, operation.operation_key, {"received": True})

        result = apply_task_action(store, task.id, "reprocess", max_retries=3, actor="Web")

        self.assertTrue(result.applied)
        self.drain_commands(store)
        updated = store.find_task(task.id)
        self.assertEqual(updated.metadata["intake_identity"]["root_ids"], ["received-root"])
        self.assertEqual(updated.metadata["received_items"][0]["file_id"], "received-root")
        self.assertEqual(updated.metadata["received_file_ids"], ["file-a"])
        self.assertNotIn("_lock_key", updated.metadata)

    def test_claimed_running_task_allows_only_terminate(self):
        store = self.make_store()
        task = store.upsert_task("claimed", "", "https://115cdn.com/s/claimed")
        store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=0)
        store.claim_next_runnable("worker", now=0)
        task = store.find_task(task.id)

        self.assertEqual(available_task_actions(task, 3), frozenset({"terminate"}))
        result = apply_task_action(store, task.id, "terminate", max_retries=3, actor="Web")

        self.assertTrue(result.applied)
        self.assertEqual(result.task.status, TaskStatus.RUNNING)
        self.assertTrue(result.task.metadata["termination_requested_at"] > 0)

    def test_cancelled_task_is_delete_only_and_repeat_terminate_is_idempotent(self):
        store = self.make_store()
        task = store.upsert_task("cancelled", "", "https://115cdn.com/s/cancelled")

        first = apply_task_action(store, task.id, "terminate", max_retries=3, actor="Web")
        repeated = apply_task_action(store, task.id, "terminate", max_retries=3, actor="Web")

        self.assertTrue(first.applied)
        self.assertTrue(repeated.applied)
        self.assertEqual(available_lifecycle_actions(repeated.task), frozenset({"delete"}))

    def test_failed_retryable_task_allows_retry_and_reprocess(self):
        store = self.make_store()
        task = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
        task = store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed")

        self.assertEqual(available_task_actions(task, 3), frozenset({"retry", "reprocess"}))
        result = apply_task_action(store, task.id, "retry", max_retries=3, actor="test")
        self.assertTrue(result.applied)
        self.drain_commands(store)
        updated = store.find_task(task.id)
        self.assertEqual(updated.current_stage, TaskStage.STRM_READY)
        self.assertEqual(updated.status, TaskStatus.PENDING)

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
        self.drain_commands(store)
        updated = store.find_task(task.id)
        self.assertEqual(updated.current_stage, TaskStage.CLOUD_DOWNLOADING)
        self.assertEqual(updated.metadata.get("strm_mode"), "shared")

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

    def test_queued_retry_does_not_clear_new_claim(self):
        store = self.make_store()
        task = store.upsert_task("race", "", "https://115cdn.com/s/race")
        task = store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed")
        result = apply_task_action(store, task.id, "retry", max_retries=3, actor="test")
        self.assertTrue(result.applied)
        store.record_event(
            task.id,
            TaskStage.STRM_READY,
            TaskStatus.PENDING,
            "requeued by another actor",
            clear_claim=True,
            next_run_at=0,
        )
        store.claim_next_runnable("new-worker", now=0)
        self.drain_commands(store)
        self.assertEqual(store.find_task(task.id).claimed_by, "new-worker")

    def test_delete_task_record_and_submission_removes_task_and_row(self):
        from bridge import ShareKey
        from app.task_actions import delete_task_record_and_submission

        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            submission_store = SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                ShareKey("abc", "1234"),
                "https://115cdn.com/s/abc?password=1234",
                "completed",
                title="任务",
            )
            task = task_store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            task = task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                submission_id=int(row["id"]),
            )

            result = delete_task_record_and_submission(task_store, submission_store, task.id)

            self.assertTrue(result.applied)
            self.assertEqual(result.reason, "任务已归档")
            self.assertIsNotNone(task_store.find_task(task.id))
            self.assertIsNone(submission_store.find_by_id(int(row["id"])))

    def test_delete_task_record_and_submission_uses_metadata_submission_id(self):
        from bridge import ShareKey
        from app.task_actions import delete_task_record_and_submission

        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            submission_store = SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                ShareKey("meta", "1234"),
                "https://115cdn.com/s/meta?password=1234",
                "completed",
                title="任务",
            )
            task = task_store.upsert_task("meta", "1234", "https://115cdn.com/s/meta?password=1234")
            task = task_store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                metadata_patch={"submission_id": int(row["id"])},
            )

            result = delete_task_record_and_submission(task_store, submission_store, task.id)

            self.assertTrue(result.applied)
            self.assertEqual(result.reason, "任务已归档")
            self.assertIsNotNone(task_store.find_task(task.id))
            self.assertIsNone(submission_store.find_by_id(int(row["id"])))

    def test_delete_with_unified_adapter_does_not_report_missing_submission(self):
        from app.task_actions import delete_task_record_and_submission

        store = self.make_store()
        adapter = WorkflowRowAdapter(store)
        task = store.upsert_task("unified-del", "", "https://115cdn.com/s/unified-del")
        task = store.record_event(
            task.id,
            TaskStage.CLEANED,
            TaskStatus.SUCCEEDED,
            "done",
            metadata_patch={"submission_id": task.id},
        )
        result = delete_task_record_and_submission(store, adapter, task.id)
        self.assertTrue(result.applied)
        self.assertEqual(result.reason, "任务已归档")
        self.assertGreater(float(store.find_task(task.id).archived_at or 0), 0)


if __name__ == "__main__":
    unittest.main()
