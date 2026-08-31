import json
import sqlite3
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.models import StageCheckpoint, StageResult, TaskStage, TaskStatus
from app.strm_mode import effective_task_strm_mode
from app.task_store import TaskStore, WorkflowRowAdapter, operation_scope


class TaskStoreTests(unittest.TestCase):
    def test_prepare_operation_is_idempotent_and_preserves_original_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("operation-prepare", "", "https://115cdn.com/s/operation-prepare")

            prepared = store.prepare_operation(
                task.id,
                "g0:u0:submit",
                "cms_submit",
                {"title": "First title", "files": ["one"]},
            )
            repeated = store.prepare_operation(
                task.id,
                "g0:u0:submit",
                "cms_submit",
                {"files": ["one"], "title": "First title"},
            )

            self.assertEqual(repeated, prepared)
            self.assertEqual(prepared.status, "prepared")
            self.assertEqual(prepared.request, {"title": "First title", "files": ["one"]})
            with self.assertRaisesRegex(ValueError, "immutable"):
                store.prepare_operation(
                    task.id,
                    "g0:u0:submit",
                    "cms_submit",
                    {"title": "Changed title"},
                )
            self.assertEqual(
                store.find_operation(task.id, "g0:u0:submit").request,
                {"title": "First title", "files": ["one"]},
            )

    def test_operation_start_authorizes_only_prepared_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("operation-start", "", "https://115cdn.com/s/operation-start")

            store.prepare_operation(task.id, "started", "cms_submit", {"id": 1})
            started = store.start_operation(task.id, "started")
            self.assertEqual(started.status, "started")
            self.assertEqual(started.attempt_count, 1)
            self.assertIsNone(store.start_operation(task.id, "started"))
            self.assertEqual(store.find_operation(task.id, "started").attempt_count, 1)

            store.prepare_operation(task.id, "uncertain", "cms_submit", {"id": 2})
            store.start_operation(task.id, "uncertain")
            store.mark_operation_uncertain(task.id, "uncertain", "network timeout")
            self.assertIsNone(store.start_operation(task.id, "uncertain"))

            store.prepare_operation(task.id, "succeeded", "cms_submit", {"id": 3})
            store.start_operation(task.id, "succeeded")
            store.complete_operation(task.id, "succeeded", {"submission_id": 7})
            self.assertIsNone(store.start_operation(task.id, "succeeded"))

            store.prepare_operation(task.id, "failed", "cms_submit", {"id": 4})
            store.start_operation(task.id, "failed")
            failed = store.mark_operation_failed(task.id, "failed", "request rejected")
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.last_error, "request rejected")
            self.assertIsNone(store.start_operation(task.id, "failed"))

    def test_reprepare_operation_allows_started_and_uncertain_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("operation-reprepare", "", "https://115cdn.com/s/operation-reprepare")

            store.prepare_operation(task.id, "started", "create_share", {"file_id": "folder"})
            started = store.start_operation(task.id, "started")
            reprepared = store.reprepare_operation(task.id, "started")
            self.assertEqual(reprepared.status, "prepared")
            self.assertEqual(reprepared.attempt_count, started.attempt_count)
            self.assertEqual(reprepared.result, {})
            self.assertEqual(reprepared.last_error, "")
            restarted = store.start_operation(task.id, "started")
            self.assertEqual(restarted.status, "started")
            self.assertEqual(restarted.attempt_count, 2)

            store.prepare_operation(task.id, "uncertain", "create_share", {"file_id": "other"})
            store.start_operation(task.id, "uncertain")
            store.mark_operation_uncertain(task.id, "uncertain", "timeout")
            self.assertEqual(store.reprepare_operation(task.id, "uncertain").status, "prepared")

            store.prepare_operation(task.id, "succeeded", "create_share", {"file_id": "done"})
            store.start_operation(task.id, "succeeded")
            store.complete_operation(task.id, "succeeded", {"share_code": "abc"})
            self.assertIsNone(store.reprepare_operation(task.id, "succeeded"))

    def test_complete_operation_persists_json_result_after_reopening_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path)
            task = store.upsert_task("operation-result", "", "https://115cdn.com/s/operation-result")
            store.prepare_operation(task.id, "g0:u0:submit", "cms_submit", {"title": "Movie"})
            store.start_operation(task.id, "g0:u0:submit")

            completed = store.complete_operation(
                task.id,
                "g0:u0:submit",
                {"submission_id": 7, "accepted": True},
            )
            reopened = TaskStore(db_path).find_operation(task.id, "g0:u0:submit")

            self.assertEqual(completed.status, "succeeded")
            self.assertEqual(completed.result, {"submission_id": 7, "accepted": True})
            self.assertEqual(reopened, completed)

    def test_clear_finished_tasks_archives_without_removing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("operation-cleanup", "", "https://115cdn.com/s/operation-cleanup")
            store.prepare_operation(task.id, "g0:u0:submit", "cms_submit", {"title": "Movie"})
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

            self.assertEqual(store.clear_finished_tasks(), 1)
            archived = store.find_task(task.id)
            self.assertIsNotNone(archived)
            self.assertGreater(float(archived.archived_at or 0), 0)
            self.assertIsNotNone(store.find_operation(task.id, "g0:u0:submit"))
            self.assertEqual(store.list_recent_tasks(limit=10), [])

    def test_delete_finished_task_removes_task_events_and_operations_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("delete-finished", "", "https://115cdn.com/s/delete-finished")
            task = store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            store.prepare_operation(task.id, "delete-test", "cms_submit", {"id": 1})

            deleted = store.delete_finished_task(task.id, expected_updated_at=task.updated_at)

            self.assertTrue(deleted)
            self.assertIsNone(store.find_task(task.id))
            self.assertEqual(store.list_events(task.id), [])
            self.assertIsNone(store.find_operation(task.id, "delete-test"))

    def test_delete_finished_task_rejects_active_claim_and_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("delete-active", "", "https://115cdn.com/s/delete-active")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            active = store.claim_next_runnable("worker", now=10)

            self.assertFalse(store.delete_finished_task(active.id, expected_updated_at=active.updated_at))
            store.clear_worker_claims("worker", now=11)
            changed = store.find_task(active.id)
            store.record_event(changed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            self.assertFalse(store.delete_finished_task(changed.id, expected_updated_at=changed.updated_at))
            self.assertIsNotNone(store.find_task(active.id))

    def test_clear_finished_tasks_includes_cancelled_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("clear-cancelled", "", "https://115cdn.com/s/clear-cancelled")
            store.request_task_termination(task.id, "Web", now=10)

            self.assertEqual(store.clear_finished_tasks(), 1)
            archived = store.find_task(task.id)
            self.assertIsNotNone(archived)
            self.assertGreater(float(archived.archived_at or 0), 0)

    def test_clear_finished_tasks_keeps_quality_claimed_terminal_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            claimed = store.upsert_task("clear-claimed", "", "https://115cdn.com/s/clear-claimed")
            store.record_event(claimed.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            claimed = store.claim_quality_cleanup(claimed.id, "cleanup-run", now=10)
            succeeded = store.upsert_task("clear-succeeded", "", "https://115cdn.com/s/clear-succeeded")
            store.record_event(succeeded.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            failed = store.upsert_task("clear-failed", "", "https://115cdn.com/s/clear-failed")
            store.record_event(failed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            cancelled = store.upsert_task("clear-cancelled-2", "", "https://115cdn.com/s/clear-cancelled-2")
            store.request_task_termination(cancelled.id, "Web", now=10)

            self.assertEqual(store.clear_finished_tasks(), 3)
            self.assertEqual(store.find_task(claimed.id).claimed_by, "quality-cleanup:cleanup-run")
            self.assertEqual(float(store.find_task(claimed.id).archived_at or 0), 0)
            self.assertGreater(float(store.find_task(succeeded.id).archived_at or 0), 0)
            self.assertGreater(float(store.find_task(failed.id).archived_at or 0), 0)
            self.assertGreater(float(store.find_task(cancelled.id).archived_at or 0), 0)

    def test_unclaimed_task_termination_is_immediate_and_not_runnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("terminate-pending", "", "https://115cdn.com/s/terminate-pending")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)

            terminated = store.request_task_termination(task.id, "Web", now=10)

            self.assertEqual(terminated.status, TaskStatus.CANCELLED)
            self.assertEqual(terminated.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(terminated.next_run_at, -1)
            self.assertEqual(terminated.claimed_by, "")
            self.assertIsNone(store.claim_next_runnable("worker", now=10))
            self.assertEqual(store.list_events(task.id)[-1]["message"], "Web 已终止任务")

    def test_claimed_task_termination_preserves_claim_version_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("terminate-running", "", "https://115cdn.com/s/terminate-running")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker", now=10)

            requested = store.request_task_termination(task.id, "Web", now=11)
            repeated = store.request_task_termination(task.id, "Web", now=12)

            self.assertEqual(requested.status, TaskStatus.RUNNING)
            self.assertEqual(requested.claim_token, claimed.claim_token)
            self.assertEqual(requested.updated_at, claimed.updated_at)
            self.assertEqual(requested.metadata["termination_requested_at"], 11)
            self.assertEqual(repeated.metadata["termination_requested_at"], 11)
            messages = [event["message"] for event in store.list_events(task.id)]
            self.assertEqual(messages.count("Web 已请求终止，等待当前阶段结束"), 1)

    def test_settle_requested_termination_requires_current_claim_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("settle", "", "https://115cdn.com/s/settle")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=0)
            claimed = store.claim_next_runnable("worker", now=10)
            store.request_task_termination(task.id, "Web", now=11)

            stale = store.settle_requested_termination(task.id, "worker", "stale-token", now=12)
            settled = store.settle_requested_termination(
                task.id,
                "worker",
                claimed.claim_token,
                error_type="stage_exception",
                error_summary="boom",
                error_detail="RuntimeError('boom')",
                now=13,
            )

            self.assertIsNone(stale)
            self.assertEqual(settled.status, TaskStatus.CANCELLED)
            self.assertEqual(settled.current_stage, TaskStage.STRM_READY)
            self.assertEqual(settled.error_summary, "boom")
            self.assertEqual(settled.claimed_by, "")
            self.assertEqual(settled.next_run_at, -1)
            self.assertNotIn("termination_requested_at", settled.metadata)
            self.assertEqual(store.list_events(task.id)[-1]["status"], "cancelled")

    def test_operation_journal_migrates_and_reprocess_changes_operation_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path)
            task = store.upsert_task("operation-scope", "", "https://115cdn.com/s/operation-scope")
            self.assertEqual(operation_scope(task), "g0:u0")

            reprocessed = store.reprocess_task(task.id, next_run_at=0)
            updated_series = store.patch_metadata(reprocessed.id, {"update_requested_run": 1})

            self.assertEqual(operation_scope(reprocessed), "g1:u0")
            self.assertEqual(operation_scope(updated_series), "g1:u1")
    def test_self_share_review_mode_override_round_trip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            self.assertIsNone(store.get_self_share_review_mode_override())
            self.assertEqual(store.set_self_share_review_mode_override("ten_minutes"), "ten_minutes")
            self.assertEqual(store.get_self_share_review_mode_override(), "ten_minutes")
            self.assertEqual(store.set_self_share_review_mode_override("off"), "off")
            self.assertEqual(store.get_self_share_review_mode_override(), "off")

            with self.assertRaisesRegex(ValueError, "审核观察"):
                store.set_self_share_review_mode_override("invalid")

            store.clear_self_share_review_mode_override()
            self.assertIsNone(store.get_self_share_review_mode_override())

    def test_wake_self_share_review_tasks_only_reschedules_review_waiters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            waiting = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            unrelated = store.upsert_task("unrelated", "", "https://115cdn.com/s/unrelated")
            store.record_event(
                waiting.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "等待分享审核",
                metadata_patch={"share_review_status": "pending"},
                next_run_at=1000.0,
            )
            store.record_event(
                unrelated.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "等待其他清理条件",
                next_run_at=1000.0,
            )

            count = store.wake_self_share_review_tasks(now=100.0)

            self.assertEqual(count, 1)
            self.assertEqual(store.find_task(waiting.id).next_run_at, 100.0)
            self.assertEqual(store.find_task(unrelated.id).next_run_at, 1000.0)

    def test_self_share_receive_cid_override_round_trip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            self.assertIsNone(store.get_self_share_receive_cid_override())
            self.assertEqual(store.set_self_share_receive_cid_override("3481694068122059860"), "3481694068122059860")
            self.assertEqual(store.get_self_share_receive_cid_override(), "3481694068122059860")

            with self.assertRaisesRegex(ValueError, "目录 ID"):
                store.set_self_share_receive_cid_override("not-a-cid")

            store.clear_self_share_receive_cid_override()
            self.assertIsNone(store.get_self_share_receive_cid_override())

    def test_patch_claimed_metadata_preserves_claim_version_and_rejects_stale_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("claimed-metadata", "", "https://115cdn.com/s/claimed-metadata")
            store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=100)

            patched = store.patch_claimed_metadata(
                claimed.id,
                expected_claimed_by="worker-a",
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at,
                patch={"receive_target_cid": "111"},
            )
            stale = store.patch_claimed_metadata(
                claimed.id,
                expected_claimed_by="worker-a",
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at + 1,
                patch={"receive_target_cid": "222"},
            )

            self.assertIsNotNone(patched)
            self.assertEqual(patched.metadata["receive_target_cid"], "111")
            self.assertEqual(patched.updated_at, claimed.updated_at)
            self.assertEqual(patched.claimed_by, "worker-a")
            self.assertIsNone(stale)
            self.assertEqual(store.find_task(claimed.id).metadata["receive_target_cid"], "111")

    def test_unguarded_event_does_not_rewrite_claimed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("claimed-event", "", "https://115cdn.com/s/claimed-event")
            store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=100)

            before = store.find_task(claimed.id)
            recorded = store.record_event(
                claimed.id,
                TaskStage.CMS_SUBMITTED,
                TaskStatus.RUNNING,
                "外部同步事件",
                metadata_patch={"external_hint": "x"},
            )

            after = store.find_task(claimed.id)
            # The event is traced...
            self.assertIsNotNone(recorded)
            self.assertEqual(len(store.list_events(claimed.id)), 2)
            # ...but the claimed task fields a worker CAS depends on are intact.
            self.assertEqual(after.current_stage, before.current_stage)
            self.assertEqual(after.status, TaskStatus.RUNNING)
            self.assertEqual(after.claimed_by, before.claimed_by)
            self.assertEqual(after.claimed_at, before.claimed_at)
            self.assertEqual(after.claim_token, before.claim_token)
            self.assertEqual(after.updated_at, before.updated_at)
            # The worker can still commit its stage result.
            committed = store.complete_claimed_stage(
                claimed.id,
                expected_stage=before.current_stage,
                expected_claimed_by=before.claimed_by,
                expected_claimed_at=before.claimed_at,
                expected_claim_token=before.claim_token,
                expected_updated_at=before.updated_at,
                success_message="阶段完成",
                success_metadata={},
                next_stage=None,
                next_run_at=time.time(),
            )
            self.assertIsNotNone(committed)
            self.assertEqual(committed.status, TaskStatus.SUCCEEDED)

    def test_quality_state_has_non_persisting_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-defaults", "", "https://115cdn.com/s/quality-defaults")

            state = store.quality_state(task.id)

            self.assertEqual(state["quality_manual_status"], "open")
            self.assertEqual(state["quality_repair_attempts"], 0)
            self.assertEqual(state["quality_last_attempt_at"], 0)
            self.assertEqual(state["quality_next_eligible_at"], 0)
            self.assertEqual(state["quality_rule_id"], "")
            self.assertEqual(state["quality_issue_codes"], [])
            self.assertEqual(store.find_task(task.id).metadata, task.metadata)

    def test_quality_state_update_is_compare_and_set_and_traceable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-cas", "", "https://115cdn.com/s/quality-cas")

            updated = store.update_quality_state(
                task.id,
                expected_updated_at=task.updated_at,
                patch={
                    "quality_manual_status": "snoozed",
                    "quality_next_eligible_at": 123.0,
                    "quality_snoozed_until": time.time() + 123,
                },
                message="质量问题暂缓",
                actor="tester",
            )
            stale = store.update_quality_state(
                task.id,
                expected_updated_at=task.updated_at,
                patch={"quality_manual_status": "ignored"},
                message="过期更新",
                actor="stale",
            )

            self.assertIsNotNone(updated)
            self.assertIsNone(stale)
            self.assertEqual(store.quality_state(task.id)["quality_manual_status"], "snoozed")
            events = store.list_events(task.id)
            self.assertTrue(any("tester" in str(event) and "质量问题暂缓" in str(event) for event in events))

    def test_quality_state_reads_legacy_repair_fields_without_migrating_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-legacy", "", "https://115cdn.com/s/quality-legacy")
            store.patch_metadata(
                task.id,
                {
                    "quality_repair_queued": True,
                    "quality_repair_started_at": 12.5,
                    "quality_repair_deadline_at": 20.0,
                    "quality_attempts": 1,
                },
            )

            state = store.quality_state(task.id)

            self.assertTrue(state["quality_repair_queued"])
            self.assertEqual(state["quality_repair_started_at"], 12.5)
            self.assertEqual(state["quality_repair_deadline_at"], 20.0)
            self.assertEqual(state["quality_repair_attempts"], 1)

    def test_quality_state_uses_legacy_attempts_when_new_value_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-invalid-attempts", "", "https://115cdn.com/s/quality-invalid-attempts")
            store.patch_metadata(
                task.id,
                {
                    "quality_repair_attempts": "not-an-int",
                    "quality_attempts": "4",
                },
            )

            self.assertEqual(store.quality_state(task.id)["quality_repair_attempts"], 4)

    def test_quality_state_falls_back_to_task_retry_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-retry-fallback", "", "https://115cdn.com/s/quality-retry-fallback")
            retried = store.record_event(
                task.id,
                TaskStage.RECEIVED,
                TaskStatus.FAILED,
                "retry",
                increment_retry=True,
                metadata_patch={"quality_repair_attempts": "invalid", "quality_attempts": "also-invalid"},
            )

            self.assertEqual(store.quality_state(retried.id)["quality_repair_attempts"], 1)

    def test_quality_state_has_legacy_defaults_and_expires_snooze_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-state-schema", "", "https://115cdn.com/s/quality-state-schema")
            patched = store.patch_metadata(
                task.id,
                {
                    "quality_manual_status": "snoozed",
                    "quality_snoozed_until": time.time() - 1,
                    "quality_repair_queued": "false",
                },
            )

            state = store.quality_state(task.id)

            self.assertEqual(state["quality_manual_status"], "open")
            self.assertFalse(state["quality_repair_queued"])
            self.assertEqual(state["quality_repair_started_at"], 0)
            self.assertEqual(state["quality_repair_deadline_at"], 0)
            self.assertEqual(state["quality_snoozed_until"], patched.metadata["quality_snoozed_until"])

    def test_cleanup_completion_cas_checks_claim_timestamp_and_task_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cleanup-cas", "", "https://115cdn.com/s/cleanup-cas")
            reserved = store.claim_quality_cleanup(task.id, "cleanup-run", now=100.0)

            self.assertIsNotNone(reserved)
            self.assertFalse(
                store.record_quality_cleanup_event(
                    task.id,
                    "cleanup-run",
                    TaskStatus.SUCCEEDED,
                    "stale completion",
                    expected_claimed_at=reserved.claimed_at,
                    expected_claim_token=reserved.claim_token,
                    expected_updated_at=reserved.updated_at + 1,
                )
            )
            still_claimed = store.find_task(task.id)
            self.assertEqual(still_claimed.claimed_by, "quality-cleanup:cleanup-run")
            self.assertFalse(
                store.finalize_quality_cleanup(
                    task.id,
                    "cleanup-run",
                    expected_claimed_at=reserved.claimed_at,
                    expected_claim_token=reserved.claim_token,
                    expected_updated_at=reserved.updated_at + 1,
                )
            )
            self.assertTrue(
                store.finalize_quality_cleanup(
                    task.id,
                    "cleanup-run",
                    expected_claimed_at=reserved.claimed_at,
                    expected_claim_token=reserved.claim_token,
                    expected_updated_at=reserved.updated_at,
                )
            )

    def test_quality_manual_state_transitions_are_atomic_and_resume_clears_quality_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-manual", "", "https://115cdn.com/s/quality-manual")

            snoozed = store.mark_quality_snoozed(task.id, time.time() + 456, "alice")
            self.assertEqual(store.quality_state(snoozed.id)["quality_manual_status"], "snoozed")
            ignored = store.mark_quality_ignored(task.id, "bob")
            self.assertEqual(store.quality_state(ignored.id)["quality_manual_status"], "ignored")
            resumed = store.resume_quality(task.id, "carol")

            state = store.quality_state(resumed.id)
            self.assertEqual(state["quality_manual_status"], "open")
            self.assertEqual(state["quality_next_eligible_at"], 0)
            self.assertEqual(state["quality_repair_attempts"], 0)
            self.assertEqual(state["quality_rule_id"], "")
            self.assertGreaterEqual(len(store.list_events(task.id)), 3)

    def test_quality_manual_transition_rejects_stale_expected_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-cas-stale", "", "https://115cdn.com/s/quality-cas-stale")
            store.patch_metadata(task.id, {"changed": True})

            self.assertIsNone(
                store.mark_quality_snoozed(
                    task.id,
                    time.time() + 456,
                    "alice",
                    expected_updated_at=task.updated_at,
                )
            )
            self.assertEqual(store.quality_state(task.id)["quality_manual_status"], "open")

    def test_quality_manual_transition_persists_optional_rule_and_action_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-context", "", "https://115cdn.com/s/quality-context")

            snoozed = store.mark_quality_snoozed(task.id, time.time() + 456, "alice", rule_id="missing_destination")
            ignored = store.mark_quality_ignored(task.id, "bob", rule_id="missing_destination")
            resumed = store.resume_quality(task.id, "carol", rule_id="missing_destination")

            self.assertEqual(snoozed.metadata["quality_rule_id"], "missing_destination")
            self.assertEqual(ignored.metadata["quality_rule_id"], "missing_destination")
            self.assertEqual(resumed.metadata["quality_rule_id"], "missing_destination")
            messages = [event["message"] for event in store.list_events(task.id)]
            self.assertTrue(any("rule=missing_destination" in message and "action=snooze" in message for message in messages))
            self.assertTrue(any("rule=missing_destination" in message and "action=ignore" in message for message in messages))
            self.assertTrue(any("rule=missing_destination" in message and "action=resume" in message for message in messages))

    def test_resume_quality_clears_current_repair_metadata_but_keeps_event_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-resume-clean", "", "https://115cdn.com/s/quality-resume-clean")
            current = store.patch_metadata(
                task.id,
                {
                    "quality_manual_status": "manual_required",
                    "quality_repair_attempts": 2,
                    "quality_next_eligible_at": 999.0,
                    "quality_repair_queued": True,
                    "quality_repair_started_at": 123.0,
                    "quality_repair_deadline_at": 456.0,
                    "quality_repair_action": "reprocess",
                    "quality_repair_reason": "manual_required",
                    "quality_run_id": "run-current",
                    "quality_last_run_id": "run-current",
                    "quality_last_attempt_at": 123.0,
                },
            )

            resumed = store.resume_quality(task.id, "tester")
            metadata = resumed.metadata
            state = store.quality_state(task.id)

            for key in (
                "quality_repair_action",
                "quality_repair_reason",
                "quality_run_id",
                "quality_last_run_id",
                "quality_last_attempt_at",
            ):
                self.assertNotIn(key, metadata)
            self.assertEqual(state["quality_manual_status"], "open")
            self.assertEqual(state["quality_repair_attempts"], 0)
            self.assertEqual(state["quality_next_eligible_at"], 0)
            self.assertFalse(state["quality_repair_queued"])
            self.assertEqual(state["quality_repair_started_at"], 0)
            self.assertEqual(state["quality_repair_deadline_at"], 0)
            self.assertTrue(any("恢复自动评估" in event["message"] for event in store.list_events(task.id)))

    def test_constructor_default_strm_mode_is_used_until_runtime_state_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db", default_strm_mode="direct")
            first = store.upsert_task("constructor-direct", "", "https://115cdn.com/s/constructor-direct")

            store.set_runtime_state("strm_default_mode", "shared")
            second = store.upsert_task("runtime-shared", "", "https://115cdn.com/s/runtime-shared")

            self.assertEqual(first.metadata["strm_mode"], "direct")
            self.assertEqual(second.metadata["strm_mode"], "shared")

    def test_default_strm_mode_uses_shared_and_setter_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            self.assertEqual(store.get_default_strm_mode(), "shared")
            self.assertEqual(store.set_default_strm_mode("DIRECT"), "direct")
            self.assertEqual(store.get_default_strm_mode(), "direct")
            with self.assertRaises(ValueError):
                store.set_default_strm_mode("self_share_sync")

    def test_upsert_task_writes_mode_once_and_does_not_overwrite_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            first = store.upsert_task("abc", "", "https://115cdn.com/s/abc", strm_mode="direct")
            repeated = store.upsert_task("abc", "", "https://115cdn.com/s/abc", strm_mode="shared")

            self.assertEqual(first.metadata["strm_mode"], "direct")
            self.assertEqual(repeated.metadata["strm_mode"], "direct")

    def test_upsert_task_backfills_mode_only_for_legacy_task_when_explicitly_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("legacy", "", "https://115cdn.com/s/legacy")
            with store._connection() as conn:
                conn.execute("UPDATE tasks SET metadata_json = '{}' WHERE id = ?", (task.id,))

            legacy = store.find_task(task.id)
            self.assertEqual(legacy.metadata, {})
            untouched = store.upsert_task("legacy", "", "https://115cdn.com/s/legacy")
            self.assertEqual(untouched.metadata, {})
            backfilled = store.upsert_task("legacy", "", "https://115cdn.com/s/legacy", strm_mode="direct")
            self.assertEqual(backfilled.metadata["strm_mode"], "direct")

    def test_organized_scan_cursor_round_trips_in_legacy_metadata_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path)
            task = store.upsert_task("legacy-scan", "", "https://115cdn.com/s/legacy-scan")
            with store._connection() as conn:
                conn.execute("UPDATE tasks SET metadata_json = '{}' WHERE id = ?", (task.id,))

            cursor = {
                "version": 1,
                "root_parent_ids": ["exists-root"],
                "queue": [{"parent_id": "child-1", "parts": ["Movie"], "depth": 1, "offset": 0}],
                "seen": ["exists-root", "child-1"],
            }
            reopened = TaskStore(db_path)
            updated = reopened.patch_metadata(task.id, {"organized_scan_cursor": cursor})

            self.assertEqual(updated.metadata["organized_scan_cursor"], cursor)

    def test_reprocess_task_clears_stale_organized_scan_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("reprocess-scan", "", "https://115cdn.com/s/reprocess-scan")
            task = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "扫描中",
                metadata_patch={
                    "organized_scan_cursor": {"version": 1, "queue": [{"parent_id": "old"}]},
                    "organized_folder": {"file_id": "old-folder"},
                    "keep_for_retry": True,
                },
            )

            updated = store.reprocess_task(task.id, message="重新开始", next_run_at=0)

            self.assertNotIn("organized_scan_cursor", updated.metadata)
            self.assertNotIn("organized_folder", updated.metadata)
            self.assertTrue(updated.metadata["keep_for_retry"])

    def test_reprocess_task_clears_stale_receive_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("reprocess-receive", "", "https://115cdn.com/s/reprocess-receive")
            task = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "整理中",
                metadata_patch={
                    "received_title": "旧文件 {tmdb-952936}",
                    "received_file_ids": ["old-id"],
                    "received_items": [{"file_id": "old-id"}],
                    "received_items_complete": True,
                    "received_expected_item_count": 1,
                    "received_existing_file_ids": [],
                    "received_snapshot_complete": True,
                    "tmdb_hint_normalized": True,
                    "self_share_reprocess_reset": True,
                    "own_share_file_id": "old-folder",
                    "own_share_file_name": "旧目录-[tmdb=952936]",
                    "own_share_code": "old-share",
                    "own_share_receive_code": "1212",
                    "share_sync_status": "submitted",
                    "source_path": "/old/share",
                    "dest_path": "/old/library",
                    "emby_status": "confirmed",
                    "emby_item_id": "old-item",
                    "emby_path": "/old/library/movie.strm",
                    "cleanup_status": "deleted",
                    "quality_repair_queued": True,
                    "keep_for_retry": True,
                },
            )

            updated = store.reprocess_task(task.id, message="重新开始", next_run_at=0)

            for key in (
                "received_title",
                "received_file_ids",
                "received_items",
                "received_items_complete",
                "received_expected_item_count",
                "received_existing_file_ids",
                "received_snapshot_complete",
                "tmdb_hint_normalized",
                "self_share_reprocess_reset",
                "own_share_file_id",
                "own_share_file_name",
                "own_share_code",
                "own_share_receive_code",
                "share_sync_status",
                "source_path",
                "dest_path",
                "emby_status",
                "emby_item_id",
                "emby_path",
                "cleanup_status",
                "quality_repair_queued",
            ):
                self.assertNotIn(key, updated.metadata)
            self.assertGreater(updated.metadata["reprocess_started_at"], 0)
            self.assertTrue(updated.metadata["keep_for_retry"])

    def test_reprocess_task_preserves_received_snapshot_after_successful_receive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("reprocess-keeps-receive", "", "https://115cdn.com/s/reprocess-keeps-receive")
            task = store.record_event(
                task.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                "整理目录冲突",
                metadata_patch={
                    "intake_identity": {"root_ids": ["root"], "files": [{"id": "file"}]},
                    "received_items": [{"file_id": "root"}],
                },
            )
            operation = store.prepare_operation(task.id, "g0:u0:receive_share", "receive_share", {})
            store.start_operation(task.id, operation.operation_key)
            store.complete_operation(task.id, operation.operation_key, {"received": True})

            updated = store.reprocess_task(task.id, next_run_at=0)

            self.assertEqual(updated.metadata["intake_identity"]["root_ids"], ["root"])
            self.assertEqual(updated.metadata["received_items"][0]["file_id"], "root")

    def test_cloud_reprocess_returns_to_cloud_downloading_and_clears_attempt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db", default_strm_mode="direct")
            task = store.upsert_cloud_task(
                "btih:reprocess-cloud",
                "magnet:?xt=urn:btih:reprocess-cloud",
                chat_id="464100862",
                title="云下载电影",
            )
            task = store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.FAILED,
                "清理失败",
                metadata_patch={
                    "cloud_info_hash": "abc",
                    "cloud_task_id": "task-1",
                    "cloud_started_at": 100,
                    "cloud_target_cid": "old",
                    "cloud_status": "completed",
                    "cloud_output_file_id": "file-1",
                    "cloud_output_parent_id": "parent-1",
                    "cloud_output_name": "电影.mkv",
                    "cloud_output_items": [{"file_id": "file-1"}],
                    "auto_organize_pending": True,
                    "auto_organize_last_error": "旧错误",
                    "auto_organize_submitted_at": 200,
                    "custom": "keep",
                },
            )

            updated = store.reprocess_task(task.id, next_run_at=0)

            self.assertEqual(updated.source_type, "cloud_download")
            self.assertEqual(updated.source_key, "btih:reprocess-cloud")
            self.assertEqual(updated.url, "magnet:?xt=urn:btih:reprocess-cloud")
            self.assertEqual(updated.current_stage, TaskStage.CLOUD_DOWNLOADING)
            self.assertEqual(updated.metadata["strm_mode"], "shared")
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.CLOUD_DOWNLOADING.value)
            for key in (
                "cloud_info_hash",
                "cloud_task_id",
                "cloud_started_at",
                "cloud_target_cid",
                "cloud_status",
                "cloud_output_file_id",
                "cloud_output_parent_id",
                "cloud_output_name",
                "cloud_output_items",
                "auto_organize_pending",
                "auto_organize_last_error",
                "auto_organize_submitted_at",
            ):
                self.assertNotIn(key, updated.metadata)
            self.assertEqual(updated.metadata["custom"], "keep")

    def test_share_reprocess_still_returns_to_received(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("reprocess-share", "", "https://115cdn.com/s/reprocess-share")
            task = store.record_event(task.id, TaskStage.CLEANED, TaskStatus.FAILED, "失败")

            updated = store.reprocess_task(task.id, next_run_at=0)

            self.assertEqual(updated.source_type, "share")
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.RECEIVED.value)

    def test_explicit_mode_does_not_backfill_active_legacy_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("legacy-active", "", "https://115cdn.com/s/legacy-active")
            with store._connection() as conn:
                conn.execute("UPDATE tasks SET metadata_json = '{}' WHERE id = ?", (task.id,))
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=10)

            duplicate = store.upsert_task(
                "legacy-active",
                "",
                "https://115cdn.com/s/legacy-active",
                strm_mode="direct",
            )

            self.assertEqual(duplicate.metadata, claimed.metadata)
            self.assertEqual(duplicate.updated_at, claimed.updated_at)
            self.assertEqual(duplicate.claimed_by, claimed.claimed_by)
            self.assertEqual(duplicate.claimed_at, claimed.claimed_at)
            self.assertEqual(duplicate.current_stage, claimed.current_stage)
            self.assertEqual(duplicate.status, claimed.status)
            self.assertEqual(duplicate.retry_count, claimed.retry_count)
            self.assertEqual(duplicate.next_run_at, claimed.next_run_at)

    def test_upsert_cloud_task_is_idempotent_by_source_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            first = store.upsert_cloud_task("ed2k:hash:10", "ed2k://source", chat_id="464100862")
            second = store.upsert_cloud_task("ed2k:hash:10", "ed2k://source", chat_id="464100862")

            self.assertEqual(first.id, second.id)
            self.assertEqual(second.source_type, "cloud_download")
            self.assertEqual(second.source_key, "ed2k:hash:10")
            self.assertEqual(second.current_stage, TaskStage.CLOUD_DOWNLOADING)

    def test_legacy_cloud_task_always_resolves_to_shared_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db", default_strm_mode="direct")
            task = store.upsert_cloud_task("btih:legacy", "magnet:?xt=urn:btih:legacy")
            with store._connection() as conn:
                conn.execute(
                    "UPDATE tasks SET metadata_json = ? WHERE id = ?",
                    ('{"legacy_marker":"keep"}', task.id),
                )

            loaded = store.find_task(task.id)
            self.assertEqual(effective_task_strm_mode(loaded, default_mode="direct"), "shared")

            upserted = store.upsert_cloud_task("btih:legacy", "magnet:?xt=urn:btih:legacy")
            self.assertEqual(upserted.metadata["strm_mode"], "shared")
            self.assertEqual(upserted.metadata["legacy_marker"], "keep")

    def test_cloud_upsert_preserves_explicit_mode_but_runtime_mode_stays_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_cloud_task("btih:explicit", "magnet:?xt=urn:btih:explicit")
            with store._connection() as conn:
                conn.execute(
                    "UPDATE tasks SET metadata_json = ? WHERE id = ?",
                    ('{"strm_mode":"direct","custom":"keep"}', task.id),
                )

            upserted = store.upsert_cloud_task("btih:explicit", "magnet:?xt=urn:btih:explicit")

            self.assertEqual(upserted.metadata["strm_mode"], "direct")
            self.assertEqual(upserted.metadata["custom"], "keep")
            self.assertEqual(effective_task_strm_mode(upserted), "shared")

    def test_cloud_task_only_accepts_shared_strm_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_cloud_task("btih:mode", "magnet:?xt=urn:btih:mode")

            for mode in ("direct", "source_shared"):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    store.set_task_strm_mode(task.id, mode)

            updated = store.set_task_strm_mode(task.id, "shared")
            self.assertEqual(updated.metadata["strm_mode"], "shared")

    def test_upsert_cloud_task_does_not_mutate_active_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_cloud_task("btih:abc", "magnet:?xt=urn:btih:abc", chat_id="464100862")
            store.enqueue_task(task.id, TaskStage.CLOUD_DOWNLOADING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-1", now=100)

            duplicate = store.upsert_cloud_task("btih:abc", "magnet:?xt=urn:btih:abc", chat_id="464100862")

            self.assertEqual(duplicate.updated_at, claimed.updated_at)
            self.assertEqual(duplicate.claimed_by, "worker-1")
            self.assertEqual(duplicate.metadata["strm_mode"], "shared")

    def test_find_task_by_source_returns_cloud_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            expected = store.upsert_cloud_task("btih:abc", "magnet:?xt=urn:btih:abc")

            found = store.find_task_by_source("cloud_download", "btih:abc")

            self.assertEqual(found.id, expected.id)

    def test_initializes_tasks_and_events_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            store = TaskStore(db_path)

            conn = sqlite3.connect(db_path)
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()

            self.assertIn("tasks", tables)
            self.assertIn("task_events", tables)
            self.assertIs(store.db_path, db_path)

    def test_upsert_task_is_idempotent_by_share_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            first = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            second = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")

            self.assertEqual(first.id, second.id)
            self.assertEqual(second.current_stage, TaskStage.RECEIVED)
            self.assertEqual(second.status, TaskStatus.PENDING)

    def test_get_or_create_share_task_creates_once_and_preserves_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            created = store.get_or_create_share_task(
                "atomic-share",
                "1212",
                "https://115cdn.com/s/atomic-share?password=1212",
                chat_id="winner-chat",
            )
            frozen = store.record_event(
                created.id,
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "winner frozen checkpoint",
                metadata_patch={
                    "series_update_parent_task_id": 328,
                    "update_requested_run": 1,
                },
                next_run_at=-1,
                expected_stage=TaskStage.RECEIVED,
                expected_status=TaskStatus.PENDING,
                expected_updated_at=created.updated_at,
            )

            existing = store.get_or_create_share_task(
                "atomic-share",
                "1212",
                "https://115cdn.com/s/loser-url?password=9999",
                chat_id="loser-chat",
            )

            self.assertEqual(existing, frozen)
            self.assertEqual(existing.url, "https://115cdn.com/s/atomic-share?password=1212")
            self.assertEqual(existing.chat_id, "winner-chat")
            self.assertEqual(existing.metadata, frozen.metadata)
            self.assertEqual(existing.current_stage, TaskStage.RECEIVED)
            self.assertEqual(existing.status, TaskStatus.PENDING)
            self.assertEqual(existing.claimed_by, frozen.claimed_by)
            self.assertEqual(existing.claim_token, frozen.claim_token)
            self.assertEqual(existing.next_run_at, -1)
            self.assertEqual(existing.updated_at, frozen.updated_at)
            self.assertEqual(store.find_task(created.id), frozen)

    def test_duplicate_upsert_does_not_change_active_claim_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("same", "1212", "https://115cdn.com/s/same?password=1212")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=10)

            duplicate = store.upsert_task(
                "same",
                "1212",
                "https://115cdn.com/s/same?password=1212",
                chat_id="464100862",
            )

            self.assertEqual(duplicate.claimed_by, "worker-a")
            self.assertEqual(duplicate.claimed_at, claimed.claimed_at)
            self.assertEqual(duplicate.updated_at, claimed.updated_at)

    def test_find_task_by_share_key_returns_only_matching_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            expected = store.upsert_task("series", "pass", "https://115cdn.com/s/series?password=pass")
            store.upsert_task("series", "other", "https://115cdn.com/s/series?password=other")

            found = store.find_task_by_share_key("series", "pass")

            self.assertEqual(found.id, expected.id)
            self.assertIsNone(store.find_task_by_share_key("missing", "pass"))

    def test_list_tasks_by_own_share_file_id_returns_exact_other_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            first = store.upsert_task("first", "", "https://115cdn.com/s/first")
            second = store.upsert_task("second", "", "https://115cdn.com/s/second")
            third = store.upsert_task("third", "", "https://115cdn.com/s/third")

            store.record_event(
                first.id,
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "first owner",
                metadata_patch={"own_share_file_id": "folder-1"},
            )
            store.record_event(
                second.id,
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "second owner",
                metadata_patch={"own_share_file_id": "folder-1"},
            )
            store.record_event(
                third.id,
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "different owner",
                metadata_patch={"own_share_file_id": "folder-2"},
            )

            owners = store.list_tasks_by_own_share_file_id("folder-1")

            self.assertEqual([task.id for task in owners], [second.id, first.id])
            self.assertEqual(
                [task.id for task in store.list_tasks_by_own_share_file_id("folder-1", exclude_task_id=second.id)],
                [first.id],
            )
            self.assertEqual(store.list_tasks_by_own_share_file_id(""), [])

    def test_archived_tasks_do_not_own_live_share_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            archived = store.upsert_task("old", "1212", "https://115cdn.com/s/old")
            archived = store.record_event(
                archived.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                metadata_patch={"own_share_file_id": "folder-1", "own_share_code": "own-old"},
            )
            live = store.upsert_task("new", "1212", "https://115cdn.com/s/new")
            store.record_event(
                live.id,
                TaskStage.OWN_SHARE_CREATED,
                TaskStatus.PENDING,
                "live owner",
                metadata_patch={"own_share_file_id": "folder-1", "own_share_code": "own-new"},
            )
            self.assertTrue(
                store.archive_task(
                    archived.id,
                    actor="test",
                    reason="user_delete",
                    expected_updated_at=archived.updated_at,
                )
            )

            owners = store.list_tasks_by_own_share_file_id("folder-1")
            live_codes = store.list_live_share_codes()

            self.assertEqual([task.id for task in owners], [live.id])
            self.assertEqual(live_codes, {"own-new"})

    def test_share_identity_lookups_include_normalized_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("fact-owner", "1212", "https://115cdn.com/s/fact-owner")
            store.write_facts(
                task.id,
                share={"file_id": "folder-fact", "own_share_code": "own-fact"},
                move={"dest_path": "/library/Movie", "move_status": "moved"},
                emby={"library": "电影", "status": "confirmed"},
            )

            owners = store.list_tasks_by_own_share_file_id("folder-fact")
            found = store.find_task(task.id)
            recent = store.list_recent_tasks(limit=1)
            live_codes = store.list_live_share_codes()

            self.assertEqual([item.id for item in owners], [task.id])
            self.assertEqual(owners[0].metadata.get("own_share_code"), "own-fact")
            self.assertEqual(found.metadata.get("dest_path"), "/library/Movie")
            self.assertEqual(found.metadata.get("emby_parent"), "电影")
            self.assertEqual(recent[0].metadata.get("dest_path"), "/library/Movie")
            self.assertEqual(live_codes, {"own-fact"})

    def test_claim_next_runnable_overlays_normalized_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("claim-facts", "1212", "https://115cdn.com/s/claim-facts")
            store.enqueue_task(task.id, TaskStage.MOVED, next_run_at=0)
            store.write_facts(
                task.id,
                move={"dest_path": "/library/Claim", "move_status": "moved"},
            )

            claimed = store.claim_next_runnable("worker", now=1)

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.metadata.get("dest_path"), "/library/Claim")

    def test_claim_task_lock_keeps_overlaid_dest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("lock-facts", "1212", "https://115cdn.com/s/lock-facts")
            store.enqueue_task(task.id, TaskStage.MOVED, next_run_at=0)
            store.write_facts(
                task.id,
                move={"dest_path": "/library/Lock", "move_status": "moved"},
            )
            claimed = store.claim_next_runnable("worker", now=1)

            locked = store.claim_task_lock(
                claimed.id,
                {
                    "_lock_key": "dest:/library/Lock",
                    "_lock_reason": "媒体库目录阶段",
                    "_lock_waiting": False,
                    "_lock_owner_task_id": "",
                },
                lambda _holder: False,
                expected_stage=claimed.current_stage,
                expected_claimed_by="worker",
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at,
                wait_message="等待资源锁",
                next_run_at=2,
                now=1,
            )

            self.assertIsNotNone(locked.task)
            self.assertEqual(locked.task.metadata.get("dest_path"), "/library/Lock")
            self.assertEqual(locked.task.metadata.get("_lock_key"), "dest:/library/Lock")

    def test_patch_claimed_metadata_keeps_overlaid_dest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("patch-facts", "1212", "https://115cdn.com/s/patch-facts")
            store.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            store.write_facts(
                task.id,
                move={"dest_path": "/library/Patch", "move_status": "moved"},
            )
            claimed = store.claim_next_runnable("worker", now=1)

            patched = store.patch_claimed_metadata(
                claimed.id,
                expected_claimed_by="worker",
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at,
                patch={"receive_target_cid": "111"},
            )

            self.assertIsNotNone(patched)
            self.assertEqual(patched.metadata.get("receive_target_cid"), "111")
            self.assertEqual(patched.metadata.get("dest_path"), "/library/Patch")

    def test_record_stage_event_updates_current_task_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            updated = store.record_event(
                task.id,
                TaskStage.CMS_SUBMITTED,
                TaskStatus.SUCCEEDED,
                "CMS submitted",
                title="示例电影",
                tmdb_id="12345",
                category="欧美电影",
            )
            events = store.list_events(task.id)

            self.assertEqual(updated.current_stage, TaskStage.CMS_SUBMITTED)
            self.assertEqual(updated.status, TaskStatus.SUCCEEDED)
            self.assertEqual(updated.title, "示例电影")
            self.assertEqual(updated.tmdb_id, "12345")
            self.assertEqual(updated.category, "欧美电影")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["message"], "CMS submitted")

    def test_complete_claimed_stage_atomically_publishes_next_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("atomic", "1212", "https://115cdn.com/s/atomic")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=10)

            updated = store.complete_claimed_stage(
                claimed.id,
                expected_stage=claimed.current_stage,
                expected_claimed_by="worker-a",
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at,
                success_message="整理完成",
                success_metadata={"organized": True},
                next_stage=TaskStage.RECOGNIZING,
                next_run_at=10,
            )

            self.assertEqual(updated.current_stage, TaskStage.RECOGNIZING)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.claimed_by, "")
            events = store.list_events(task.id)
            self.assertEqual([(event["stage"], event["status"]) for event in events[-2:]], [
                (TaskStage.ORGANIZING.value, TaskStatus.SUCCEEDED.value),
                (TaskStage.RECOGNIZING.value, TaskStatus.PENDING.value),
            ])

    def test_complete_claimed_stage_rejects_stale_claim_without_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("atomic-stale", "1212", "https://115cdn.com/s/atomic-stale")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-a", now=10)
            events_before = store.list_events(task.id)

            updated = store.complete_claimed_stage(
                claimed.id,
                expected_stage=claimed.current_stage,
                expected_claimed_by="worker-a",
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at + 1,
                success_message="整理完成",
                success_metadata={"organized": True},
                next_stage=TaskStage.RECOGNIZING,
                next_run_at=10,
            )

            self.assertIsNone(updated)
            self.assertEqual(store.list_events(task.id), events_before)

    def test_repeated_running_event_updates_task_without_growing_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待 CMS 整理完成",
                metadata_patch={"first": "yes"},
                next_run_at=10.0,
                clear_claim=True,
            )
            updated = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待 CMS 整理完成",
                metadata_patch={"second": "yes"},
                next_run_at=25.0,
                clear_claim=True,
            )
            events = store.list_events(task.id)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["message"], "等待 CMS 整理完成")
            self.assertEqual(updated.next_run_at, 25.0)
            self.assertEqual(updated.metadata["first"], "yes")
            self.assertEqual(updated.metadata["second"], "yes")

    def test_compare_and_set_transition_records_initial_and_target_events_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            updated = store.compare_and_set_transition(
                task.id,
                TaskStage.RECEIVED,
                {TaskStatus.PENDING},
                require_unclaimed=True,
                target_stage=TaskStage.EMBY_CONFIRMED,
                target_status=TaskStatus.PENDING,
                initial_event_message="initial transition",
                target_event_message="queued transition",
                next_run_at=0,
                clear_errors=True,
                clear_claim=True,
            )
            events = store.list_events(task.id)

            self.assertIsNotNone(updated)
            self.assertEqual(
                [(event["stage"], event["status"], event["message"]) for event in events],
                [
                    (TaskStage.RECEIVED.value, TaskStatus.PENDING.value, "initial transition"),
                    (TaskStage.EMBY_CONFIRMED.value, TaskStatus.PENDING.value, "queued transition"),
                ],
            )
            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.next_run_at, 0)

    def test_compare_and_set_transition_can_override_initial_event_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

            store.compare_and_set_transition(
                task.id,
                TaskStage.CLEANED,
                {TaskStatus.SUCCEEDED},
                require_unclaimed=True,
                target_stage=TaskStage.EMBY_CONFIRMED,
                target_status=TaskStatus.PENDING,
                initial_event_message="restore requested",
                initial_event_stage=TaskStage.EMBY_CONFIRMED,
                target_event_message="restore queued",
                next_run_at=0,
            )

            self.assertEqual(
                [(event["stage"], event["status"], event["message"]) for event in store.list_events(task.id)[-2:]],
                [
                    (TaskStage.EMBY_CONFIRMED.value, TaskStatus.PENDING.value, "restore requested"),
                    (TaskStage.EMBY_CONFIRMED.value, TaskStatus.PENDING.value, "restore queued"),
                ],
            )

    def test_record_failure_stores_error_and_retry_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            failed = store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM not found",
                error_type="strm_missing",
                error_summary="未找到 STRM 文件夹",
                error_detail="checked /mnt/user/Unraid/strm/share",
                increment_retry=True,
            )

            self.assertEqual(failed.current_stage, TaskStage.STRM_READY)
            self.assertEqual(failed.status, TaskStatus.FAILED)
            self.assertEqual(failed.error_type, "strm_missing")
            self.assertEqual(failed.error_summary, "未找到 STRM 文件夹")
            self.assertEqual(failed.retry_count, 1)

    def test_list_recent_tasks_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            one = store.upsert_task("one", "", "https://115cdn.com/s/one")
            two = store.upsert_task("two", "", "https://115cdn.com/s/two")

            recent = store.list_recent_tasks(limit=2)

            self.assertEqual([task.id for task in recent], [two.id, one.id])

    def test_list_open_tasks_excludes_succeeded_and_returns_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.record_event(running.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "running")
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            succeeded = store.upsert_task("succeeded", "", "https://115cdn.com/s/succeeded")
            store.record_event(succeeded.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            manual = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "choose")

            open_tasks = store.list_open_tasks()

            self.assertEqual([task.id for task in open_tasks], [manual.id, failed.id, running.id, pending.id])
            self.assertEqual(
                [task.status for task in open_tasks],
                [TaskStatus.NEEDS_ACTION, TaskStatus.FAILED, TaskStatus.RUNNING, TaskStatus.PENDING],
            )

    def test_list_open_tasks_and_health_exclude_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            store.enqueue_task(pending.id, TaskStage.RECEIVED, next_run_at=0)
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            failed = store.record_event(failed.id, TaskStage.FAILED, TaskStatus.FAILED, "failed")
            self.assertTrue(
                store.archive_task(
                    failed.id,
                    actor="test",
                    reason="user_delete",
                    expected_updated_at=failed.updated_at,
                )
            )

            open_tasks = store.list_open_tasks()
            health = store.aggregate_open_task_health(limit=10)

            self.assertEqual([task.id for task in open_tasks], [pending.id])
            self.assertEqual(health.failed_count, 0)
            self.assertEqual(health.pending_count, 1)
            self.assertEqual(health.problem_count, 0)

    def test_upsert_after_archive_creates_a_new_live_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            done = store.upsert_task("same-share", "1212", "https://115cdn.com/s/same-share")
            done = store.record_event(done.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            self.assertTrue(
                store.archive_task(
                    done.id,
                    actor="test",
                    reason="user_delete",
                    expected_updated_at=done.updated_at,
                )
            )

            live = store.upsert_task("same-share", "1212", "https://115cdn.com/s/same-share")
            found = store.find_task_by_share_key("same-share", "1212")
            archived = store.find_task(done.id)

            self.assertNotEqual(live.id, done.id)
            self.assertEqual(found.id, live.id)
            self.assertEqual(float(live.archived_at or 0), 0)
            self.assertGreater(float(archived.archived_at or 0), 0)
            self.assertEqual(archived.share_code, "same-share")

    def test_list_open_tasks_searches_status_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            statements = []
            original_connect = store._connect

            def traced_connect():
                conn = original_connect()
                conn.set_trace_callback(statements.append)
                return conn

            with patch.object(store, "_connect", side_effect=traced_connect):
                store.list_open_tasks()

            select_sql = next(
                statement.strip()
                for statement in statements
                if statement.lstrip().startswith("SELECT * FROM tasks")
            )
            conn = sqlite3.connect(store.db_path)
            try:
                plan = [str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {select_sql}")]
            finally:
                conn.close()
            normalized_plan = "\n".join(plan).upper()

            self.assertIn("STATUS IN", select_sql.upper())
            self.assertIn("SEARCH TASKS", normalized_plan)
            self.assertIn("IDX_TASKS_NEXT_RUN", normalized_plan)
            self.assertIn("STATUS", normalized_plan)
            self.assertNotIn("SCAN TASKS USING INDEX IDX_TASKS_UPDATED_AT", normalized_plan)

    def test_find_pending_stage_excludes_requested_task_without_scanning_recent_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            current = store.upsert_task("current", "", "https://115cdn.com/s/current")
            store.record_event(current.id, TaskStage.SHARE_SYNC_SUBMITTED, TaskStatus.RUNNING, "current", next_run_at=1.0)
            waiting = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            store.record_event(waiting.id, TaskStage.STRM_READY, TaskStatus.PENDING, "waiting", next_run_at=1.0)

            with patch.object(store, "list_recent_tasks", side_effect=AssertionError("SQL stage lookup must not scan recent tasks")):
                found = store.find_pending_stage(TaskStage.STRM_READY, exclude_task_id=current.id)
                missing = store.find_pending_stage(TaskStage.SHARE_SYNC_SUBMITTED, exclude_task_id=current.id)

            self.assertEqual(found.id, waiting.id)
            self.assertIsNone(missing)

    def test_runtime_state_persists_value_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            store.set_runtime_state("task_runner", "running", updated_at=123.0)

            self.assertEqual(store.get_runtime_state("task_runner"), {"value": "running", "updated_at": 123.0})
            self.assertIsNone(store.get_runtime_state("missing"))

    def test_quality_run_history_upserts_by_run_id_and_lists_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.record_quality_run(
                "run-1",
                "2026-08-01",
                "succeeded",
                10.0,
                12.0,
                issue_count=3,
                scanned_count=100,
                rule_counts={"unsafe_path": 2},
            )
            store.record_quality_run("run-2", "2026-08-02", "failed", 20.0, 25.0, failed_count=1)
            store.record_quality_run(
                "run-1",
                "2026-08-01",
                "succeeded",
                10.0,
                13.0,
                issue_count=4,
                rule_counts={"unsafe_path": 2},
            )

            runs = store.list_quality_runs(limit=10)

            self.assertEqual([row["run_id"] for row in runs], ["run-2", "run-1"])
            first = next(row for row in runs if row["run_id"] == "run-1")
            self.assertEqual(first["issue_count"], 4)
            self.assertEqual(json.loads(first["rule_counts_json"]), {"unsafe_path": 2})

    def test_quality_run_trend_groups_by_date_and_respects_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            now = time.time()
            store.record_quality_run("a", "day1", "succeeded", now - 2 * 86400, now, scanned_count=10)
            store.record_quality_run("b", "day1", "succeeded", now - 2 * 86400, now, scanned_count=15)
            store.record_quality_run("c", "day2", "failed", now - 86400, now, failed_count=1)
            store.record_quality_run("old", "old-day", "succeeded", now - 31 * 86400, now, scanned_count=999)

            trend = store.quality_run_trend(days=30)

            self.assertEqual([row["run_date"] for row in trend], ["day1", "day2"])
            self.assertEqual(trend[0]["runs"], 2)
            self.assertEqual(trend[0]["scanned_count"], 25)
            self.assertEqual(trend[1]["failed_count"], 1)

    def test_cms_version_overrides_merge_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            self.assertEqual(store.get_cms_version_overrides(), {})
            merged = store.set_cms_version_overrides(
                {"enabled": True, "interval_seconds": 86400, "image": "cms:latest"}
            )
            self.assertTrue(merged["enabled"])
            self.assertEqual(merged["interval_seconds"], 86400)
            merged = store.set_cms_version_overrides({"auto_pull": True})
            self.assertTrue(merged["auto_pull"])
            self.assertTrue(merged["enabled"])
            store.clear_cms_version_overrides()
            self.assertEqual(store.get_cms_version_overrides(), {})

    def test_own_share_receive_code_override_can_be_set_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            self.assertIsNone(store.get_own_share_receive_code_override())
            self.assertEqual(store.set_own_share_receive_code_override("a1B2"), "a1B2")
            self.assertEqual(store.get_own_share_receive_code_override(), "a1B2")
            store.clear_own_share_receive_code_override()
            self.assertIsNone(store.get_own_share_receive_code_override())

    def test_own_share_receive_code_override_rejects_non_alphanumeric_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            for value in ("", "12 12", "12-12", "密码"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    store.set_own_share_receive_code_override(value)

    def test_claim_quality_run_only_claims_a_date_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            self.assertTrue(store.claim_quality_run("2026-07-19", now=100.0))
            self.assertFalse(store.claim_quality_run("2026-07-19", now=200.0))
            self.assertTrue(store.claim_quality_run("2026-07-20", now=300.0))

    def test_claim_quality_run_is_atomic_across_store_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)

            self.assertTrue(first_store.claim_quality_run("2026-07-19", now=100.0))
            self.assertFalse(second_store.claim_quality_run("2026-07-19", now=200.0))

    def test_claim_quality_run_allows_only_one_concurrent_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)
            start = threading.Barrier(2)

            def claim(store):
                start.wait()
                return store.claim_quality_run("2026-07-19", now=100.0)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = [
                    future.result()
                    for future in (
                        executor.submit(claim, first_store),
                        executor.submit(claim, second_store),
                    )
                ]

            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 1)

    def test_queue_summary_counts_statuses_and_lock_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            store.enqueue_task(pending.id, TaskStage.RECEIVED, next_run_at=0)
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.enqueue_task(running.id, TaskStage.ORGANIZING, next_run_at=0)
            store.claim_next_runnable("worker", now=0)
            waiting = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            store.record_event(
                waiting.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待资源锁",
                metadata_patch={"_lock_key": "115:global", "_lock_reason": "115/CMS 全局阶段", "_lock_waiting": True},
            )
            manual = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "请选择分类")
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing")

            summary = store.queue_summary(limit=10)

            self.assertEqual(summary.recent_count, 5)
            self.assertEqual(summary.pending_count, 1)
            self.assertEqual(summary.running_count, 2)
            self.assertEqual(summary.needs_action_count, 1)
            self.assertEqual(summary.failed_count, 1)
            self.assertEqual(summary.lock_wait_count, 1)
            self.assertEqual(summary.latest_lock_wait.id, waiting.id)

    def test_task_store_persists_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234", chat_id="464100862")

            updated = store.record_event(
                task.id,
                TaskStage.RECEIVED,
                TaskStatus.RUNNING,
                "已接收",
                submission_id=7,
                metadata_patch={"own_share_file_id": "fid-1", "emby_parent": "电影"},
            )

            self.assertEqual(updated.chat_id, "464100862")
            self.assertEqual(updated.submission_id, 7)
            self.assertEqual(updated.metadata["own_share_file_id"], "fid-1")
            self.assertEqual(updated.metadata["emby_parent"], "电影")

    def test_enqueue_and_claim_next_runnable_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, message="等待整理", next_run_at=1.0)

            early = store.claim_next_runnable("worker-1", now=0.5)
            claimed = store.claim_next_runnable("worker-1", now=1.0)
            second = store.claim_next_runnable("worker-2", now=1.0)

            self.assertIsNone(early)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(claimed.status, TaskStatus.RUNNING)
            self.assertEqual(claimed.claimed_by, "worker-1")
            self.assertIsNone(second)

    def test_failed_task_is_not_claimed_until_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "失败", error_summary="未找到 STRM")

            self.assertIsNone(store.claim_next_runnable("worker-1", now=10.0))

            store.enqueue_task(task.id, TaskStage.STRM_READY, message="手动重试", next_run_at=10.0)
            claimed = store.claim_next_runnable("worker-1", now=10.0)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.current_stage, TaskStage.STRM_READY)

    def test_pending_cleaned_task_is_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.CLEANED, message="等待清理", next_run_at=1.0)

            claimed = store.claim_next_runnable("worker-1", now=1.0)

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.current_stage, TaskStage.CLEANED)
            self.assertEqual(claimed.status, TaskStatus.RUNNING)

    def test_succeeded_cleaned_task_is_not_claimable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "清理完成",
                next_run_at=1.0,
            )

            self.assertIsNone(store.claim_next_runnable("worker-1", now=1.0))

    def test_cross_instance_claim_does_not_double_claim_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)
            task = first_store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            first_store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)

            first_claim = first_store.claim_next_runnable("worker-1", now=1.0)
            second_claim = second_store.claim_next_runnable("worker-2", now=1.0)

            self.assertIsNotNone(first_claim)
            self.assertIsNone(second_claim)

    def test_default_stale_claim_timeout_is_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)

            first_claim = store.claim_next_runnable("worker-1", now=1.0)
            second_claim = store.claim_next_runnable("worker-2", now=1000.0)

            self.assertIsNotNone(first_claim)
            self.assertIsNone(second_claim)

    def test_claims_by_same_worker_at_same_timestamp_receive_different_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("same-worker", "", "https://115cdn.com/s/same-worker")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)

            first = store.claim_next_runnable("worker-1", now=100.0)
            store.clear_worker_claims("worker-1", now=100.0)
            second = store.claim_next_runnable("worker-1", now=100.0)

            self.assertTrue(first.claim_token)
            self.assertTrue(second.claim_token)
            self.assertNotEqual(first.claim_token, second.claim_token)
            self.assertEqual(first.claimed_at, second.claimed_at)
            self.assertEqual(first.updated_at, second.updated_at)

    def test_stale_claim_token_cannot_commit_after_same_worker_reclaims(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("stale-token", "", "https://115cdn.com/s/stale-token")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            first = store.claim_next_runnable("worker-1", now=100.0)
            store.clear_worker_claims("worker-1", now=100.0)
            second = store.claim_next_runnable("worker-1", now=100.0)
            events_before = store.list_events(task.id)

            stale = store.complete_claimed_stage(
                task.id,
                expected_stage=first.current_stage,
                expected_claimed_by=first.claimed_by,
                expected_claimed_at=first.claimed_at,
                expected_claim_token=first.claim_token,
                expected_updated_at=first.updated_at,
                success_message="stale result",
                success_metadata={},
                next_stage=TaskStage.RECOGNIZING,
                next_run_at=100.0,
            )

            self.assertIsNone(stale)
            self.assertEqual(store.find_task(task.id).claim_token, second.claim_token)
            self.assertEqual(store.list_events(task.id), events_before)

    def test_renew_claim_only_changes_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("renew", "", "https://115cdn.com/s/renew")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-1", now=100.0)

            renewed = store.renew_claim(
                claimed.id,
                claimed.claimed_by,
                claimed.claim_token,
                now=150.0,
            )
            current = store.find_task(task.id)

            self.assertTrue(renewed)
            self.assertEqual(current.claim_heartbeat_at, 150.0)
            self.assertEqual(current.claimed_at, claimed.claimed_at)
            self.assertEqual(current.claim_token, claimed.claim_token)
            self.assertEqual(current.updated_at, claimed.updated_at)

    def test_claim_heartbeat_prevents_live_claim_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("heartbeat", "", "https://115cdn.com/s/heartbeat")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-1", now=100.0, stale_after_seconds=60)

            self.assertTrue(store.renew_claim(task.id, "worker-1", claimed.claim_token, now=150.0))
            self.assertIsNone(store.claim_next_runnable("worker-2", now=200.0, stale_after_seconds=60))

            recovered = store.claim_next_runnable("worker-2", now=211.0, stale_after_seconds=60)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.claimed_by, "worker-2")
            self.assertNotEqual(recovered.claim_token, claimed.claim_token)

    def test_active_lock_holder_liveness_uses_claim_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            holder = store.upsert_task("lock-holder", "", "https://115cdn.com/s/lock-holder")
            store.patch_metadata(holder.id, {"_lock_key": "115:global"})
            store.enqueue_task(holder.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("worker-1", now=100.0)
            self.assertTrue(store.renew_claim(holder.id, "worker-1", claimed.claim_token, now=150.0))

            active = store.find_active_lock_holder(
                "115:global",
                exclude_task_id=999,
                now=200.0,
                stale_after_seconds=60,
            )

            self.assertIsNotNone(active)
            self.assertEqual(active.id, holder.id)

    def test_claim_task_lock_rejects_stale_owner_before_metadata_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("stale-lock", "", "https://115cdn.com/s/stale-lock")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            stale = store.claim_next_runnable("worker-1", now=100.0)
            replacement = store.compare_and_set_transition(
                task.id,
                stale.current_stage,
                {TaskStatus.RUNNING},
                require_unclaimed=False,
                target_stage=stale.current_stage,
                target_status=TaskStatus.RUNNING,
                target_event_message="replacement claim",
                claim_by="worker-2",
            )
            events_before = store.list_events(task.id)

            result = store.claim_task_lock(
                task.id,
                {"_lock_key": "115:global"},
                lambda _holder: False,
                expected_stage=stale.current_stage,
                expected_claimed_by=stale.claimed_by,
                expected_claimed_at=stale.claimed_at,
                expected_claim_token=stale.claim_token,
                expected_updated_at=stale.updated_at,
                wait_message="waiting",
                next_run_at=200.0,
                now=101.0,
            )
            current = store.find_task(task.id)

            self.assertTrue(result.stale)
            self.assertIsNone(result.task)
            self.assertEqual(current.claimed_by, replacement.claimed_by)
            self.assertEqual(current.claim_token, replacement.claim_token)
            self.assertNotIn("_lock_key", current.metadata)
            self.assertEqual(store.list_events(task.id), events_before)

    def test_claim_task_lock_rejects_stale_owner_before_wait_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            holder = store.upsert_task("holder", "", "https://115cdn.com/s/holder")
            store.patch_metadata(holder.id, {"_lock_key": "115:global"})
            store.enqueue_task(holder.id, TaskStage.ORGANIZING, next_run_at=0)
            store.claim_next_runnable("holder-worker", now=100.0)
            task = store.upsert_task("stale-wait", "", "https://115cdn.com/s/stale-wait")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            stale = store.claim_next_runnable("worker-1", now=100.0)
            replacement = store.compare_and_set_transition(
                task.id,
                stale.current_stage,
                {TaskStatus.RUNNING},
                require_unclaimed=False,
                target_stage=stale.current_stage,
                target_status=TaskStatus.RUNNING,
                target_event_message="replacement claim",
                claim_by="worker-2",
            )
            events_before = store.list_events(task.id)

            result = store.claim_task_lock(
                task.id,
                {"_lock_key": "115:global"},
                lambda candidate: candidate.id == holder.id,
                expected_stage=stale.current_stage,
                expected_claimed_by=stale.claimed_by,
                expected_claimed_at=stale.claimed_at,
                expected_claim_token=stale.claim_token,
                expected_updated_at=stale.updated_at,
                wait_message="waiting",
                next_run_at=200.0,
                now=101.0,
            )
            current = store.find_task(task.id)

            self.assertTrue(result.stale)
            self.assertIsNone(result.holder)
            self.assertEqual(current.claimed_by, replacement.claimed_by)
            self.assertEqual(current.claim_token, replacement.claim_token)
            self.assertNotIn("_lock_waiting", current.metadata)
            self.assertEqual(store.list_events(task.id), events_before)

    def test_claim_task_lock_returns_released_waiter_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            holder = store.upsert_task("holder", "", "https://115cdn.com/s/holder")
            store.patch_metadata(holder.id, {"_lock_key": "115:global"})
            store.enqueue_task(holder.id, TaskStage.ORGANIZING, next_run_at=0)
            store.claim_next_runnable("holder-worker", now=100.0)
            waiter = store.upsert_task("waiter", "", "https://115cdn.com/s/waiter")
            store.enqueue_task(waiter.id, TaskStage.ORGANIZING, next_run_at=0)
            claimed = store.claim_next_runnable("waiter-worker", now=100.0)

            result = store.claim_task_lock(
                waiter.id,
                {"_lock_key": "115:global", "_lock_waiting": False},
                lambda candidate: candidate.id == holder.id,
                expected_stage=claimed.current_stage,
                expected_claimed_by=claimed.claimed_by,
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at,
                wait_message="waiting",
                next_run_at=200.0,
                now=101.0,
            )

            self.assertEqual(result.holder.id, holder.id)
            self.assertEqual(result.task.id, waiter.id)
            self.assertEqual(result.task.claimed_by, "")
            self.assertTrue(result.task.metadata["_lock_waiting"])

    def test_record_event_preserves_claim_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("worker-1", now=1.0)

            updated = store.record_event(claimed.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "处理中")

            self.assertEqual(updated.claimed_by, "worker-1")
            self.assertEqual(updated.claimed_at, 1.0)

    def test_record_event_clear_claim_false_preserves_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("worker-1", now=1.0)

            updated = store.record_event(
                claimed.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "处理中",
                clear_claim=False,
            )

            self.assertEqual(updated.claimed_by, "worker-1")
            self.assertEqual(updated.claimed_at, 1.0)

    def test_clear_worker_claims_releases_previous_process_running_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=1.0)
            claimed = store.claim_next_runnable("task-runner", now=1.0)
            self.assertEqual(claimed.claimed_by, "task-runner")

            released = store.clear_worker_claims("task-runner", now=10.0)
            updated = store.find_task(task.id)

            self.assertEqual(released, 1)
            self.assertEqual(updated.status, TaskStatus.RUNNING)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.claimed_at, 0)
            self.assertEqual(updated.next_run_at, 10.0)

    def test_metadata_merge_preserves_existing_keys_and_ignores_none_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "收到", metadata_patch={"keep": "yes"})

            updated = store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "整理",
                metadata_patch={"new": "value", "keep": None},
            )

            self.assertEqual(updated.metadata["keep"], "yes")
            self.assertEqual(updated.metadata["new"], "value")

    def test_cross_instance_metadata_patches_preserve_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            first_store = TaskStore(db_path)
            second_store = TaskStore(db_path)
            task = first_store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            first_store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "收到", metadata_patch={"first": "1"})
            updated = second_store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "整理", metadata_patch={"second": "2"})

            self.assertEqual(updated.metadata["first"], "1")
            self.assertEqual(updated.metadata["second"], "2")

    def test_legacy_schema_migrates_runtime_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        share_code TEXT NOT NULL,
                        receive_code TEXT NOT NULL DEFAULT '',
                        url TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        tmdb_id TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT '',
                        current_stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_type TEXT NOT NULL DEFAULT '',
                        error_summary TEXT NOT NULL DEFAULT '',
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(share_code, receive_code)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE task_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT NOT NULL DEFAULT '',
                        error_type TEXT NOT NULL DEFAULT '',
                        error_detail TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(id)
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO tasks (share_code, receive_code, url, current_stage, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("legacy", "", "https://115cdn.com/s/legacy", TaskStage.RECEIVED.value, TaskStatus.PENDING.value, 1.0, 1.0),
                )
                conn.commit()
            finally:
                conn.close()

            store = TaskStore(db_path)
            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                conn.close()
            legacy_claim = store.claim_next_runnable("worker-1", now=10.0)
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc", chat_id="464100862")
            updated = store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "收到", submission_id=7)

            self.assertTrue({"chat_id", "submission_id", "next_run_at", "claimed_by", "claimed_at", "claim_token", "claim_heartbeat_at", "metadata_json", "source_type", "source_key"} <= columns)
            self.assertIn("task_operations", tables)
            self.assertIsNone(legacy_claim)
            self.assertEqual(updated.chat_id, "464100862")
            self.assertEqual(updated.submission_id, 7)
            self.assertEqual(store.find_task_by_share_key("legacy", "").source_key, "share:legacy:")
            self.assertEqual(store.find_task_by_share_key("legacy", "").source_type, "share")

    def test_active_legacy_claim_migration_backfills_renewable_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        share_code TEXT NOT NULL,
                        receive_code TEXT NOT NULL DEFAULT '',
                        source_type TEXT NOT NULL DEFAULT 'share',
                        source_key TEXT NOT NULL DEFAULT '',
                        url TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        tmdb_id TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT '',
                        current_stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_type TEXT NOT NULL DEFAULT '',
                        error_summary TEXT NOT NULL DEFAULT '',
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        chat_id TEXT NOT NULL DEFAULT '',
                        submission_id INTEGER,
                        next_run_at REAL NOT NULL DEFAULT -1,
                        claimed_by TEXT NOT NULL DEFAULT '',
                        claimed_at REAL NOT NULL DEFAULT 0,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(share_code, receive_code)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO tasks (
                        share_code, source_key, url, current_stage, status, next_run_at,
                        claimed_by, claimed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-active",
                        "share:legacy-active:",
                        "https://115cdn.com/s/legacy-active",
                        TaskStage.ORGANIZING.value,
                        TaskStatus.RUNNING.value,
                        0.0,
                        "legacy-worker",
                        42.0,
                        1.0,
                        42.0,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            first_store = TaskStore(db_path)
            migrated = first_store.find_task_by_share_key("legacy-active", "")
            first_token = migrated.claim_token
            second_store = TaskStore(db_path)
            reopened = second_store.find_task_by_share_key("legacy-active", "")

            self.assertEqual(migrated.claim_heartbeat_at, 42.0)
            self.assertTrue(first_token)
            self.assertEqual(reopened.claim_token, first_token)
            self.assertEqual(reopened.claimed_by, "legacy-worker")

    def _claim_running(self, store: TaskStore, share_code: str):
        task = store.upsert_task(share_code, "", f"https://115cdn.com/s/{share_code}")
        store.enqueue_task(task.id, TaskStage.MOVED, next_run_at=1.0)
        claimed = store.claim_next_runnable("worker-1", now=1.0)
        self.assertIsNotNone(claimed)
        return claimed

    def test_commit_claimed_result_writes_facts_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            claimed = self._claim_running(store, "checkpoint-ok")
            result = StageResult.complete(
                "moved",
                {"dest_path": "/library/movie"},
                checkpoint=StageCheckpoint(move={"dest_path": "/library/movie", "move_status": "moved"}),
            )
            updated = store.commit_claimed_result(
                claimed,
                "worker-1",
                result,
                next_stage=TaskStage.EMBY_CONFIRMED,
                next_run_at=2.0,
            )
            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            with sqlite3.connect(store.db_path) as conn:
                row = conn.execute("SELECT dest_path, move_status FROM task_moves WHERE task_id = ?", (claimed.id,)).fetchone()
            self.assertEqual(row[0], "/library/movie")
            self.assertEqual(row[1], "moved")

    def test_commit_claimed_result_rolls_back_on_unknown_fact_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            claimed = self._claim_running(store, "checkpoint-bad")
            result = StageResult.complete(
                "moved",
                checkpoint=StageCheckpoint(move={"not_a_column": "nope"}),
            )
            with self.assertRaises(ValueError):
                store.commit_claimed_result(
                    claimed,
                    "worker-1",
                    result,
                    next_stage=TaskStage.EMBY_CONFIRMED,
                    next_run_at=2.0,
                )
            current = store.find_task(claimed.id)
            self.assertEqual(current.status, TaskStatus.RUNNING)
            self.assertEqual(current.current_stage, TaskStage.MOVED)
            with sqlite3.connect(store.db_path) as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM task_moves WHERE task_id = ?", (claimed.id,)).fetchone())

    def test_commit_claimed_result_discards_stale_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            claimed = self._claim_running(store, "checkpoint-stale")
            stale = claimed
            store.record_event(
                claimed.id,
                claimed.current_stage,
                TaskStatus.RUNNING,
                "heartbeat",
                expected_stage=claimed.current_stage,
                expected_status=TaskStatus.RUNNING,
                expected_claimed_by="worker-1",
                expected_claimed_at=claimed.claimed_at,
                expected_claim_token=claimed.claim_token,
                expected_updated_at=claimed.updated_at,
            )
            result = StageResult.complete("moved", checkpoint=StageCheckpoint(move={"move_status": "moved"}))
            self.assertIsNone(
                store.commit_claimed_result(
                    stale,
                    "worker-1",
                    result,
                    next_stage=TaskStage.EMBY_CONFIRMED,
                    next_run_at=2.0,
                )
            )
            with sqlite3.connect(store.db_path) as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM task_moves WHERE task_id = ?", (claimed.id,)).fetchone())

    def test_workflow_facts_projects_joined_rows_without_creating_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            missing = store.workflow_facts(99)
            self.assertEqual(missing, {})
            with sqlite3.connect(store.db_path) as conn:
                before = conn.execute("SELECT COUNT(*) FROM task_media").fetchone()[0]

            claimed = self._claim_running(store, "facts-ok")
            empty = store.workflow_facts(claimed.id)
            self.assertNotIn("submission_id", empty)
            self.assertEqual(empty.get("id"), claimed.id)
            self.assertEqual(empty.get("move_status"), "")

            store.commit_claimed_result(
                claimed,
                "worker-1",
                StageResult.complete(
                    "moved",
                    checkpoint=StageCheckpoint(
                        media={"title": "Movie", "category": "华语电影", "tmdb_id": "123"},
                        share={"canonical_name": "L-Movie-2016", "own_share_code": "own"},
                        move={"dest_path": "/library/movie", "move_status": "moved"},
                    ),
                ),
                next_stage=TaskStage.EMBY_CONFIRMED,
                next_run_at=2.0,
            )
            facts = store.workflow_facts(claimed.id)
            self.assertEqual(facts["id"], claimed.id)
            self.assertNotIn("submission_id", facts)
            self.assertEqual(facts["title"], "Movie")
            self.assertEqual(facts["category_final"], "华语电影")
            self.assertEqual(facts["own_share_file_name"], "L-Movie-2016")
            self.assertEqual(facts["own_share_code"], "own")
            self.assertEqual(facts["dest_path"], "/library/movie")
            self.assertEqual(facts["move_status"], "moved")
            with sqlite3.connect(store.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_media").fetchone()[0], before + 1)

    def test_reprocess_clears_share_and_move_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            claimed = self._claim_running(store, "reprocess-facts")
            store.commit_claimed_result(
                claimed,
                "worker-1",
                StageResult.complete(
                    "moved",
                    checkpoint=StageCheckpoint(
                        share={"canonical_name": "L-Movie", "own_share_code": "own"},
                        move={"move_status": "moved", "dest_path": "/library/movie"},
                    ),
                ),
                next_stage=TaskStage.EMBY_CONFIRMED,
                next_run_at=2.0,
            )
            store.prepare_operation(claimed.id, "g0:u0:receive_share:abc:cid", "receive_share", {"cid": "cid"})
            store.start_operation(claimed.id, "g0:u0:receive_share:abc:cid")
            store.complete_operation(claimed.id, "g0:u0:receive_share:abc:cid", {"received_items_complete": True})
            store.reprocess_task(claimed.id, next_run_at=0)
            facts = store.workflow_facts(claimed.id)
            self.assertFalse(facts.get("own_share_code"))
            self.assertEqual(facts.get("move_status"), "")

    def test_write_facts_refuses_live_foreign_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            claimed = self._claim_running(store, "claim-fence")
            store.write_facts(
                claimed.id,
                move={"move_status": "moved", "dest_path": "/stolen"},
                now=1.0,
            )
            facts = store.workflow_facts(claimed.id)
            self.assertNotEqual(facts.get("dest_path"), "/stolen")
            self.assertNotEqual(facts.get("move_status"), "moved")

    def test_write_facts_allows_matching_claim_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            claimed = self._claim_running(store, "claim-holder")
            store.write_facts(
                claimed.id,
                move={"move_status": "moved", "dest_path": "/library/movie"},
                claimed_by=claimed.claimed_by,
                claim_token=claimed.claim_token,
                now=1.0,
            )
            facts = store.workflow_facts(claimed.id)
            self.assertEqual(facts.get("dest_path"), "/library/movie")
            self.assertEqual(facts.get("move_status"), "moved")

    def test_write_facts_allows_unclaimed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("unclaimed-facts", "", "https://115cdn.com/s/unclaimed-facts")
            store.write_facts(task.id, probe={"last_probe_at": 9.0}, now=9.0)
            facts = store.workflow_facts(task.id)
            self.assertEqual(facts.get("share_probe_at"), 9.0)

    def test_adapter_enqueues_missing_library_restore_without_moving(self):
        from app.media.strm import enqueue_missing_self_share_restores, enqueue_stranded_self_share_repairs
        from app.config import MoveConfig

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "library" / "Movie"
            store = TaskStore(root / "tasks.db")
            adapter = WorkflowRowAdapter(store)
            task = store.upsert_task("missing-dest", "", "https://115cdn.com/s/missing-dest")
            store.write_facts(
                task.id,
                share={
                    "canonical_name": "Movie",
                    "own_share_code": "ownabc",
                    "file_id": "fid",
                },
                move={
                    "move_status": "moved",
                    "dest_path": str(dest),
                },
            )
            queued = enqueue_missing_self_share_restores(adapter, limit=10)
            stranded = enqueue_stranded_self_share_repairs(
                adapter,
                MoveConfig(source_roots=[root / "share"], library_roots={"电影": root / "library"}),
                limit=10,
            )
            self.assertEqual(queued, 1)
            self.assertEqual(stranded, 0)
            self.assertFalse(dest.exists())
            command = store.claim_next_command("inspector")
            self.assertEqual(command["command_type"], "restore")
            self.assertEqual(store.find_task(task.id).id, task.id)

    def test_adapter_reads_probe_identity_and_emby_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            adapter = WorkflowRowAdapter(store)
            dest = "/library/Movie"
            probed = store.upsert_task("probed", "", "https://115cdn.com/s/probed")
            fresh = store.upsert_task("fresh", "", "https://115cdn.com/s/fresh")
            other = store.upsert_task("other", "", "https://115cdn.com/s/other")
            for task, share_code, tmdb in ((probed, "own-probed", "111"), (fresh, "own-fresh", "111"), (other, "own-other", "222")):
                store.write_facts(
                    task.id,
                    media={"tmdb_id": tmdb, "category": "华语电影"},
                    share={
                        "canonical_name": "Movie",
                        "own_share_code": share_code,
                        "own_share_receive_code": "1212",
                        "file_id": f"fid-{share_code}",
                    },
                    move={"move_status": "moved", "dest_path": dest},
                    emby={"status": "confirmed", "path": f"{dest}/movie.strm"},
                    cleanup={"status": "pending", "target_id": f"fid-{share_code}"},
                )
            adapter.update_share_probe(probed.id)

            probes = adapter.self_share_probe_candidates(limit=10)
            self.assertGreaterEqual(len(probes), 2)
            self.assertNotEqual(probes[0]["id"], probed.id)
            self.assertEqual(
                set(adapter.live_self_share_identities(dest, "111")),
                {("own-fresh", "1212"), ("own-probed", "1212")},
            )
            self.assertEqual(adapter.live_self_share_identities(dest, "222"), (("own-other", "1212"),))
            self.assertIsNotNone(adapter.latest_self_share_identity(dest, "111"))
            self.assertEqual(len(adapter.all_confirmed_with_emby_path()), 3)
            self.assertEqual(len(adapter.pending_self_share_cleanup_candidates(limit=10)), 3)

    def test_adapter_recent_and_clear_finished_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            adapter = WorkflowRowAdapter(store)
            live = store.upsert_task("live", "", "https://115cdn.com/s/live")
            store.enqueue_task(live.id, TaskStage.RECEIVED, next_run_at=0)
            done = store.upsert_task("done", "", "https://115cdn.com/s/done")
            store.record_event(done.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "missing", error_summary="未找到 STRM")

            rows = adapter.recent(limit=10)
            ids = {row["id"] for row in rows}
            self.assertEqual(ids, {live.id, done.id, failed.id})
            self.assertTrue(any(row.get("last_error") == "未找到 STRM" for row in rows))

            self.assertEqual(adapter.clear_finished_history(), 2)
            remaining = {row["id"] for row in adapter.recent(limit=10)}
            self.assertEqual(remaining, {live.id})
            self.assertGreater(float(store.find_task(done.id).archived_at or 0), 0)
            self.assertGreater(float(store.find_task(failed.id).archived_at or 0), 0)

    def test_adapter_restore_sync_claim_is_fenced_for_retry_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            adapter = WorkflowRowAdapter(store)
            task = store.upsert_task("restore-sync", "", "https://115cdn.com/s/restore-sync")
            store.write_facts(task.id, share={"own_share_code": "own", "canonical_name": "Movie"})

            self.assertTrue(adapter.claim_self_share_restore_sync(task.id, retry_seconds=60, now=100.0))
            self.assertFalse(adapter.claim_self_share_restore_sync(task.id, retry_seconds=60, now=100.0))
            self.assertFalse(adapter.claim_self_share_restore_sync(task.id, retry_seconds=60, now=150.0))
            self.assertTrue(adapter.claim_self_share_restore_sync(task.id, retry_seconds=60, now=161.0))
            facts = adapter.find_by_id(task.id)
            self.assertEqual(facts["share_sync_status"], "restore_submitted")
            self.assertEqual(facts["workflow_phase"], "restore_share_sync_submitted")
