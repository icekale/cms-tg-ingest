import sqlite3
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
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
                    expected_updated_at=reserved.updated_at + 1,
                )
            )
            self.assertTrue(
                store.finalize_quality_cleanup(
                    task.id,
                    "cleanup-run",
                    expected_claimed_at=reserved.claimed_at,
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
            finally:
                conn.close()
            legacy_claim = store.claim_next_runnable("worker-1", now=10.0)
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc", chat_id="464100862")
            updated = store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "收到", submission_id=7)

            self.assertTrue({"chat_id", "submission_id", "next_run_at", "claimed_by", "claimed_at", "metadata_json", "source_type", "source_key"} <= columns)
            self.assertIsNone(legacy_claim)
            self.assertEqual(updated.chat_id, "464100862")
            self.assertEqual(updated.submission_id, 7)
            self.assertEqual(store.find_task_by_share_key("legacy", "").source_key, "share:legacy:")
            self.assertEqual(store.find_task_by_share_key("legacy", "").source_type, "share")
