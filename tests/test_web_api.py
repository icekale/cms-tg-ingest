import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.models import TaskStage, TaskStatus
from app.quality import QualityIssue
from app.hdhive_subscription_store import HdhiveSubscriptionStore
from app.series_rules import parse_episode_filter
from app.task_store import TaskStore
from app.config import Config
from app.background_jobs import BackgroundJobCoordinator, BackgroundJobSnapshot
from app.quality_automation import QualityAutomation
from app.web import WebApp
from app.web_api import (
    _safe_error,
    _safe_url,
    api_quality,
    api_response,
    api_tasks,
    serialize_event,
    serialize_hdhive,
    serialize_health,
    serialize_task,
)


class WebApiTests(unittest.TestCase):
    def test_task_purge_api_dry_run_then_deletes_task_and_submission(self):
        import bridge

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                bridge.ShareKey("purge", "1234"),
                "https://115cdn.com/s/purge?password=1234",
                "completed",
                title="任务",
            )
            task = store.upsert_task("purge", "1234", "https://115cdn.com/s/purge?password=1234")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                submission_id=int(row["id"]),
            )
            app = WebApp(store, submission_store=submission_store)

            dry_status, _dry_headers, dry_body = app.handle_request(
                "POST",
                "/api/v1/tasks/purge",
                {"Content-Type": "application/json"},
                json.dumps({"ids": [task.id], "dry_run": True}).encode(),
            )
            dry = json.loads(dry_body)

            self.assertEqual(dry_status, 200)
            self.assertTrue(dry["dry_run"])
            self.assertEqual(dry["deleted"][0]["id"], task.id)
            self.assertIsNotNone(store.find_task(task.id))
            self.assertIsNotNone(submission_store.find_by_id(int(row["id"])))

            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/tasks/purge",
                {"Content-Type": "application/json"},
                json.dumps({"ids": [task.id]}).encode(),
            )
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertFalse(payload["dry_run"])
            self.assertEqual(payload["deleted"][0]["id"], task.id)
            self.assertIsNone(store.find_task(task.id))
            self.assertIsNone(submission_store.find_by_id(int(row["id"])))

    def test_completed_task_exposes_missing_media_directory_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("drift", "", "https://115cdn.com/s/drift", strm_mode="shared")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                metadata_patch={"dest_path": str(Path(tmp) / "missing")},
            )

            payload = serialize_task(store.find_task(task.id), include_completion_drift=True)

            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["completion_drift"]["code"], "missing_dest")
            self.assertEqual(payload["completion_drift"]["message"], "已入库但当前媒体目录缺失")

    def test_completed_shared_task_exposes_wrong_strm_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "movie"
            destination.mkdir()
            (destination / "movie.strm").write_text("http://115.example/d/direct-file", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("wrong", "", "https://115cdn.com/s/wrong", strm_mode="shared")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                metadata_patch={
                    "dest_path": str(destination),
                    "own_share_code": "own-share",
                    "own_share_receive_code": "1212",
                },
            )

            payload = serialize_task(store.find_task(task.id), include_completion_drift=True)

            self.assertEqual(payload["completion_drift"]["code"], "unexpected_strm")

    def test_task_list_does_not_scan_completion_drift_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.upsert_task("list", "", "https://115cdn.com/s/list")
            with patch("app.web_api.completion_drift_for_task") as drift:
                api_tasks(store)

            drift.assert_not_called()

    def test_task_api_exposes_backend_lifecycle_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("actions", "", "https://115cdn.com/s/actions")
            app = WebApp(store)

            status, _headers, body = app.handle_request("GET", f"/api/v1/tasks/{task.id}", {}, b"")
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertEqual(payload["available_actions"], ["terminate"])
            self.assertFalse(payload["termination_requested"])

    def test_legacy_engine_hides_and_rejects_task_lifecycle_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            active = store.upsert_task("legacy-active", "", "https://115cdn.com/s/legacy-active")
            finished = store.upsert_task("legacy-finished", "", "https://115cdn.com/s/legacy-finished")
            store.record_event(finished.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            app = WebApp(store, task_engine_enabled=False)

            list_status, _headers, list_body = app.handle_request("GET", "/api/v1/tasks", {}, b"")
            terminate_status, _headers, terminate_body = app.handle_request(
                "POST", f"/api/v1/tasks/{active.id}/actions/terminate", {}, b""
            )
            legacy_terminate_status, _headers, _body = app.handle_request(
                "POST", f"/task/{active.id}/terminate", {}, b""
            )
            delete_status, _headers, delete_body = app.handle_request(
                "DELETE", f"/api/v1/tasks/{finished.id}", {}, b""
            )

            tasks = {item["id"]: item for item in json.loads(list_body)["items"]}
            self.assertEqual(list_status, 200)
            self.assertEqual(tasks[active.id]["available_actions"], [])
            self.assertEqual(tasks[finished.id]["available_actions"], [])
            self.assertEqual(terminate_status, 409)
            self.assertIn("旧版任务引擎", json.loads(terminate_body)["reason"])
            self.assertEqual(legacy_terminate_status, 409)
            self.assertEqual(delete_status, 409)
            self.assertIn("旧版任务引擎", json.loads(delete_body)["reason"])
            self.assertEqual(store.find_task(active.id).status, TaskStatus.PENDING)
            self.assertIsNotNone(store.find_task(finished.id))

    def test_legacy_engine_delete_does_not_recheck_or_mutate_after_missing_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("legacy-race", "", "https://115cdn.com/s/legacy-race")
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            persisted = store.find_task(task.id)
            original_find_task = store.find_task
            store.find_task = Mock(side_effect=[None, persisted])
            app = WebApp(store, task_engine_enabled=False)

            status, _headers, body = app.handle_request(
                "DELETE", f"/api/v1/tasks/{task.id}", {}, b""
            )

            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["message"], "任务不存在或已过期")
            self.assertEqual(store.find_task.call_count, 1)
            self.assertIsNotNone(original_find_task(task.id))

    def test_terminate_api_is_idempotent_for_claimed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("api-terminate", "", "https://115cdn.com/s/api-terminate")
            store.enqueue_task(task.id, TaskStage.ORGANIZING, next_run_at=0)
            store.claim_next_runnable("worker", now=1)
            app = WebApp(store)

            first_status, _headers, first_body = app.handle_request(
                "POST", f"/api/v1/tasks/{task.id}/actions/terminate", {}, b""
            )
            second_status, _headers, second_body = app.handle_request(
                "POST", f"/api/v1/tasks/{task.id}/actions/terminate", {}, b""
            )

            self.assertEqual(first_status, 200)
            self.assertEqual(second_status, 200)
            self.assertTrue(json.loads(first_body)["termination_requested"])
            self.assertTrue(json.loads(second_body)["termination_requested"])

    def test_delete_task_api_rejects_active_and_deletes_terminal_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("api-delete", "", "https://115cdn.com/s/api-delete")
            app = WebApp(store)

            conflict_status, _headers, conflict_body = app.handle_request(
                "DELETE", f"/api/v1/tasks/{task.id}", {}, b""
            )
            store.request_task_termination(task.id, "Web", now=1)
            deleted_status, _headers, deleted_body = app.handle_request(
                "DELETE", f"/api/v1/tasks/{task.id}", {}, b""
            )
            missing_status, _headers, missing_body = app.handle_request(
                "DELETE", f"/api/v1/tasks/{task.id}", {}, b""
            )

            self.assertEqual(conflict_status, 409)
            self.assertEqual(json.loads(conflict_body)["error"], "delete_not_allowed")
            self.assertEqual(json.loads(conflict_body)["reason"], "任务尚未结束，无法删除")
            self.assertEqual(deleted_status, 200)
            self.assertEqual(json.loads(deleted_body)["deleted"], task.id)
            self.assertEqual(missing_status, 404)
            self.assertEqual(json.loads(missing_body)["error"], "task_not_found")
            self.assertEqual(json.loads(missing_body)["message"], "任务不存在或已过期")

    def test_terminate_api_rejects_finished_task_and_reports_missing_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("terminate-finished", "", "https://115cdn.com/s/terminate-finished")
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            app = WebApp(store)

            conflict_status, _headers, conflict_body = app.handle_request(
                "POST", f"/api/v1/tasks/{task.id}/actions/terminate", {}, b""
            )
            missing_status, _headers, missing_body = app.handle_request(
                "POST", "/api/v1/tasks/999/actions/terminate", {}, b""
            )

            self.assertEqual(conflict_status, 409)
            self.assertEqual(json.loads(conflict_body)["error"], "action_not_allowed")
            self.assertEqual(json.loads(conflict_body)["reason"], "任务已经结束，无需终止")
            self.assertEqual(missing_status, 404)
            self.assertEqual(json.loads(missing_body)["error"], "task_not_found")
            self.assertEqual(json.loads(missing_body)["message"], "任务不存在或已过期")

    def test_body_limit_rejects_oversized_direct_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"), web_token="")

            accepted_status, _headers, _body = app.handle_request("POST", "/history/clear", {}, b"x" * 65536)
            rejected_status, _headers, _body = app.handle_request("POST", "/history/clear", {}, b"x" * 65537)

        self.assertEqual(accepted_status, 303)
        self.assertEqual(rejected_status, 413)

    def test_background_job_status_api_exposes_only_latest_safe_fields(self):
        snapshots = (
            BackgroundJobSnapshot("quality:run", "old quality", "succeeded", 1, 2, 3, ""),
            BackgroundJobSnapshot("quality:run", "latest quality", "failed", 4, 5, 6, "Authorization: Bearer api-secret"),
            BackgroundJobSnapshot("hdhive:run", "old HDHive", "succeeded", 7, 8, 9, ""),
            BackgroundJobSnapshot("hdhive:item:7", "latest HDHive", "failed", 10, 11, 12, "X-API-Key: hdhive-secret"),
        )

        class SnapshotSource:
            def list_snapshots(self):
                return snapshots

        with tempfile.TemporaryDirectory() as tmp:
            quality = api_quality(TaskStore(Path(tmp) / "tasks.db"), background_jobs=SnapshotSource())
        hdhive = serialize_hdhive(SimpleNamespace(list=lambda: []), background_jobs=SnapshotSource())

        for payload, description in ((quality["background_job"], "latest quality"), (hdhive["background_job"], "latest HDHive")):
            self.assertEqual(payload["description"], description)
            self.assertEqual(set(payload), {"description", "state", "started_at", "finished_at", "error"})
            self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("background_jobs", hdhive)

    def test_background_job_status_prefers_newest_submission_over_old_completion(self):
        snapshots = (
            BackgroundJobSnapshot("hdhive:run", "old completed", "succeeded", 10, 11, 1000, ""),
            BackgroundJobSnapshot("hdhive:item:7", "newly queued", "queued", 20, None, None, ""),
        )

        class SnapshotSource:
            def list_snapshots(self):
                return snapshots

        payload = serialize_hdhive(SimpleNamespace(list=lambda: []), background_jobs=SnapshotSource())

        self.assertEqual(payload["background_job"]["description"], "newly queued")
        self.assertEqual(payload["background_job"]["state"], "queued")

    def _quality_service(self, tmp, store):
        root = Path(tmp) / "library"
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=False,
        )
        return QualityAutomation(store, config, allowed_roots=[root]), root

    def test_quality_api_exposes_rule_state_and_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            destination = root / "direct"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
            task = store.upsert_task("quality-api", "", "https://115cdn.com/s/quality-api")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                title="质量 API 任务",
                metadata_patch={
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "organized_folder": {"file_name": "Q-质量 API 任务-2026-[tmdb=123]"},
                },
            )
            app = WebApp(store, quality_automation=quality)

            status, _headers, body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            payload = json.loads(body)
            item = payload["items"][0]

            self.assertEqual(status, 200)
            self.assertEqual(item["code"], "direct_strm")
            self.assertEqual(item["task_id"], task.id)
            self.assertEqual(item["title"], "质量 API 任务")
            self.assertEqual(item["display_title"], "Q-质量 API 任务-2026-[tmdb=123]")
            self.assertEqual(item["rule_id"], "strm_mode_mismatch")
            self.assertIn("execute", item["available_actions"])
            self.assertIn("snooze", item["available_actions"])
            self.assertIn("strm_mode_mismatch", payload["rule_counts"])
            self.assertIn("automation", payload)
            self.assertNotIn(str(destination), body.decode())
            self.assertIn("movie.strm", item["detail"])

    def test_quality_runs_api_exposes_history_and_trend(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            now = time.time()
            store.record_quality_run(
                "run-1",
                "day1",
                "succeeded",
                now - 86400,
                now,
                scanned_count=10,
                issue_count=2,
                rule_counts={"unsafe_path": 1},
            )
            app = WebApp(store)

            status, _headers, body = app.handle_request("GET", "/api/v1/quality/runs", {}, b"")
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(payload["items"][0]["run_id"], "run-1")
            self.assertEqual(payload["items"][0]["rule_counts"], {"unsafe_path": 1})
            self.assertEqual(payload["trend"][0]["run_date"], "day1")
            self.assertEqual(payload["trend"][0]["scanned_count"], 10)

    def test_cms_version_api_reports_and_triggers_check(self):
        class FakeChecker:
            def __init__(self):
                self.calls = 0

            def status(self):
                return {
                    "current_version": "1.0.0",
                    "update_ready": False,
                    "image": "",
                    "container": "cms",
                    "pull_result": "",
                    "message": "",
                }

            def check(self):
                self.calls += 1
                return {
                    **self.status(),
                    "current_version": "1.1.0",
                    "update_ready": True,
                }

        with tempfile.TemporaryDirectory() as tmp:
            checker = FakeChecker()
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store, cms_version_checker=checker)

            status, _headers, body = app.handle_request("GET", "/api/v1/cms/version", {}, b"")
            payload = json.loads(body)
            check_status, _check_headers, check_body = app.handle_request(
                "POST",
                "/api/v1/cms/version/check",
                {},
                b"",
            )
            check_payload = json.loads(check_body)

            self.assertEqual(status, 200)
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["current_version"], "1.0.0")
            self.assertEqual(check_status, 200)
            self.assertTrue(check_payload["update_ready"])
            self.assertEqual(checker.calls, 1)

    def test_quality_run_api_deduplicates_duplicate_clicks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            coordinator = BackgroundJobCoordinator()
            started = threading.Event()
            release = threading.Event()

            class BlockingQuality:
                def run_now(self):
                    started.set()
                    release.wait(1)

            app = WebApp(store, quality_automation=BlockingQuality(), background_jobs=coordinator)
            try:
                first_status, _headers, first_body = app.handle_request("POST", "/api/v1/quality/run", {}, b"")
                self.assertTrue(started.wait(1))
                duplicates = [app.handle_request("POST", "/api/v1/quality/run", {}, b"") for _ in range(19)]

                self.assertEqual(first_status, 202)
                self.assertEqual(json.loads(first_body)["job"]["outcome"], "accepted")
                self.assertTrue(all(status == 409 for status, _headers, _body in duplicates))
                self.assertTrue(all(json.loads(body)["job"]["outcome"] == "already_running" for _status, _headers, body in duplicates))
            finally:
                release.set()
                coordinator.shutdown(wait=True)

    def test_quality_action_api_validates_rule_and_returns_conflict_without_external_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            destination = root / "direct"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
            task = store.upsert_task("quality-action", "", "https://115cdn.com/s/quality-action")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                },
            )
            app = WebApp(store, quality_automation=quality)
            _, _, quality_body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            item = json.loads(quality_body)["items"][0]
            request = json.dumps(
                {
                    "task_id": task.id,
                    "rule_id": item["rule_id"],
                    "rule_version": item["rule_version"],
                    "action": "execute",
                    "actor": "tester",
                }
            ).encode()

            first_status, _, first_body = app.handle_request(
                "POST", "/api/v1/quality/action/execute", {"Content-Type": "application/json"}, request
            )
            second_status, _, second_body = app.handle_request(
                "POST", "/api/v1/quality/action/execute", {"Content-Type": "application/json"}, request
            )
            bad_status, _, bad_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/execute",
                {"Content-Type": "application/json"},
                json.dumps({**json.loads(request), "rule_id": "wrong"}).encode(),
            )

            self.assertEqual(first_status, 200)
            self.assertEqual(json.loads(first_body)["action"], "execute")
            self.assertEqual(second_status, 409)
            self.assertEqual(json.loads(second_body)["error"], "quality_action_conflict")
            self.assertEqual(bad_status, 409)
            self.assertEqual(json.loads(bad_body)["error"], "quality_rule_mismatch")

    def test_missing_quality_destination_exposes_reprocess_not_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            task = store.upsert_task("quality-missing", "", "https://115cdn.com/s/quality-missing")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={"dest_path": str(root / "missing"), "own_share_code": "own"},
            )
            app = WebApp(store, quality_automation=quality)
            _, _, body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            item = json.loads(body)["items"][0]

            self.assertIn("reprocess", item["available_actions"])
            self.assertNotIn("restore", item["available_actions"])

    def test_quality_api_orphan_issue_is_view_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            with patch(
                "app.web_api.scan_task_quality",
                return_value=[QualityIssue("missing_dest", "missing", "/private/path", 999, "孤立问题")],
            ):
                payload = __import__("app.web_api", fromlist=["api_quality"]).api_quality(store)

            item = payload["items"][0]
            self.assertEqual(item["manual_status"], "manual_required")
            self.assertEqual(item["available_actions"], ["view"])
            self.assertEqual(payload["manual_count"], 1)
            self.assertNotIn("/private/path", json.dumps(payload, ensure_ascii=False))
            self.assertIn("本地路径已隐藏", item["detail"])

    def test_quality_action_api_supports_ignore_and_resume_and_rejects_invalid_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            quality, root = self._quality_service(tmp, store)
            task = store.upsert_task("quality-ignore", "", "https://115cdn.com/s/quality-ignore")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={"dest_path": str(root / "missing"), "own_share_code": "own"},
            )
            app = WebApp(store, quality_automation=quality)
            _, _, body = app.handle_request("GET", "/api/v1/quality", {}, b"")
            item = json.loads(body)["items"][0]
            base = {
                "task_id": task.id,
                "rule_id": item["rule_id"],
                "rule_version": item["rule_version"],
            }
            snoozed_status, _, snoozed_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/snooze",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "snooze", "until": time.time() + 3600}).encode(),
            )
            resumed_status, _, resumed_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/resume",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "resume"}).encode(),
            )
            ignored_status, _, ignored_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/ignore",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "ignore"}).encode(),
            )
            resumed_again_status, _, resumed_again_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/resume",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "resume"}).encode(),
            )
            invalid_status, _, invalid_body = app.handle_request(
                "POST",
                "/api/v1/quality/action/ignore",
                {"Content-Type": "application/json"},
                json.dumps({**base, "action": "execute"}).encode(),
            )

            self.assertEqual(snoozed_status, 200)
            self.assertEqual(json.loads(snoozed_body)["status"], "snoozed")
            self.assertEqual(resumed_status, 200)
            self.assertEqual(json.loads(resumed_body)["status"], "resumed")
            self.assertEqual(ignored_status, 200)
            self.assertEqual(json.loads(ignored_body)["status"], "ignored")
            self.assertEqual(resumed_again_status, 200)
            self.assertEqual(json.loads(resumed_again_body)["status"], "resumed")
            self.assertEqual(invalid_status, 409)
            self.assertEqual(json.loads(invalid_body)["error"], "quality_action_not_allowed")
    def test_health_api_exposes_runner_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state("task_runner", "error", updated_at=100.0)

            payload = serialize_health(store, enabled=True, now=100.0)

        self.assertEqual(payload["runner_state"], "error")

    def test_hdhive_sensitive_url_and_error_variants_are_redacted(self):
        for key in (
            "share_password",
            "share_pwd",
            "refresh_token",
            "hdhive_token",
            "api_key",
            "auth_token",
            "bearer_token",
            "session_token",
            "csrf_token",
            "p115_cookie",
        ):
            with self.subTest(key=key):
                self.assertNotIn("variant-secret", _safe_url(f"https://hdhive.test/tv/x?{key}=variant-secret"))
                self.assertNotIn("variant-secret", _safe_error(f"{key}=variant-secret"))
        self.assertNotIn("user-secret", _safe_url("https://user:user-secret@hdhive.test/tv/x"))

    def test_api_recursively_redacts_sensitive_url_query_suffixes(self):
        _status, _headers, body = api_response(
            {
                "nested": {
                    "callback_url": (
                        "https://example.test/callback?"
                        "web_token=web-secret&emby_api_key=emby-secret&"
                        "own_share_receive_code=share-secret"
                    )
                }
            }
        )

        encoded = body.decode("utf-8")
        self.assertNotIn("web-secret", encoded)
        self.assertNotIn("emby-secret", encoded)
        self.assertNotIn("share-secret", encoded)

    def test_health_api_reports_last_database_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state(
                "backup_last_result",
                json.dumps({"status": "partial", "files": ["/data/backups/tasks.db"], "error": "missing source"}),
            )

            payload = serialize_health(store, enabled=True, now=100.0)

        self.assertEqual(payload["backup"]["status"], "partial")
        self.assertEqual(payload["backup"]["error"], "missing source")

    def test_hdhive_filter_api_returns_safe_subscription_summary_and_skip_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            subscription_store = HdhiveSubscriptionStore(Path(tmp) / "hdhive.db")
            subscription = subscription_store.create_subscription(
                "464100862",
                "tmdb_tv",
                "1416",
                "剧集",
                "1416",
                source_url="https://hdhive.com/tv/secret-page",
            )
            item = subscription_store.upsert_item(subscription.id, "S01E01", "resource-1", "valid", 1080, 8)
            subscription_store.mark_item_skipped(item.id, "filtered", "不在过滤范围")
            subscription_store.record_check(subscription.id, "password=subscription-secret token=subscription-token")
            error_item = subscription_store.upsert_item(subscription.id, "S01E02", "resource-2", "valid", 1080, 8)
            subscription_store.mark_item_failed(error_item.id, "password=item-secret token=item-token")
            unlocked_item = subscription_store.upsert_item(subscription.id, "S01E03", "resource-3", "valid", 1080, 8)
            subscription_store.mark_item_unlocked(
                unlocked_item.id,
                "https://115cdn.com/s/api-secret?password=api-password",
                8,
                "actual",
                1700000000,
            )

            class Service:
                store = subscription_store

                def list(self):
                    return self.store.list_subscriptions()

                def set_episode_filter(self, subscription_id, value):
                    parse_episode_filter(value)
                    return self.store.update_episode_filter(subscription_id, value)

            app = WebApp(task_store, hdhive_service=Service())
            status, _headers, body = app.handle_request(
                "POST",
                f"/api/v1/hdhive/subscriptions/{subscription.id}/episode-filter",
                {"Content-Type": "application/json"},
                b'{"episode_filter":"S02"}',
            )
            payload = json.loads(body)

        self.assertEqual(status, 200)
        row = payload["subscription"]
        self.assertEqual(row["episode_filter"], "S02")
        self.assertIn("last_summary_json", row)
        self.assertFalse(row["completed"])
        self.assertEqual(row["items"][0]["skip_reason"], "不在过滤范围")
        self.assertNotIn("source_url", row)
        self.assertNotIn("subscription-secret", json.dumps(row, ensure_ascii=False))
        self.assertNotIn("item-secret", json.dumps(row, ensure_ascii=False))
        self.assertNotIn("api-secret", json.dumps(row, ensure_ascii=False))
        self.assertTrue(all("unlocked_url" not in item for item in row["items"]))
        self.assertIn("password=***", row["last_error"])

    def test_hdhive_filter_api_rejects_invalid_filter_without_changing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            subscription_store = HdhiveSubscriptionStore(Path(tmp) / "hdhive.db")
            subscription = subscription_store.create_subscription("464100862", "tmdb_tv", "1416", "剧集", "1416")

            class Service:
                store = subscription_store

                def list(self):
                    return self.store.list_subscriptions()

                def set_episode_filter(self, subscription_id, value):
                    parse_episode_filter(value)
                    return self.store.update_episode_filter(subscription_id, value)

            app = WebApp(task_store, hdhive_service=Service())
            status, _headers, body = app.handle_request(
                "POST",
                f"/api/v1/hdhive/subscriptions/{subscription.id}/episode-filter",
                {"Content-Type": "application/json"},
                b'{"episode_filter":"S01E"}',
            )
            current = subscription_store.get_subscription(subscription.id)

        self.assertEqual(status, 400)
        self.assertEqual(current.episode_filter, "")
        self.assertIn("episode", json.loads(body)["error"])
    def test_task_api_redacts_share_password_and_returns_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task(
                "abc",
                "secret",
                "https://115cdn.com/s/abc?password=secret&foo=bar",
                strm_mode="direct",
            )
            store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.RUNNING, "已接收")
            app = WebApp(store)

            status, headers, body = app.handle_request("GET", f"/api/v1/tasks/{task.id}", {}, b"")
            payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(payload["strm_mode"], "direct")
        self.assertIn("password=***", payload["safe_url"])
        self.assertNotIn("secret", payload["safe_url"])
        self.assertEqual(payload["events"][0]["message"], "已接收")

    def test_task_mode_can_change_before_strm_stage_but_not_after_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc", strm_mode="shared")
            app = WebApp(store)

            status, _headers, _body = app.handle_request(
                "POST", f"/api/v1/tasks/{task.id}/strm-mode", {"Content-Type": "application/json"}, b'{"mode":"direct"}'
            )
            changed = store.find_task(task.id)
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.RUNNING, "locked")
            locked_status, _headers, locked_body = app.handle_request(
                "POST", f"/api/v1/tasks/{task.id}/strm-mode", {}, b"mode=shared"
            )

        self.assertEqual(status, 200)
        self.assertEqual(changed.metadata["strm_mode"], "direct")
        self.assertEqual(locked_status, 409)
        self.assertEqual(json.loads(locked_body)["code"], "strm_mode_locked")

    def test_default_mode_api_and_missing_frontend_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store, frontend_dist_path=Path(tmp) / "missing")
            status, _headers, body = app.handle_request(
                "POST", "/api/v1/settings/strm-mode", {"Content-Type": "application/json"}, b'{"mode":"direct"}'
            )
            frontend_status, _headers, frontend_body = app.handle_request("GET", "/app/", {}, b"")
            self.assertEqual(store.get_default_strm_mode(), "direct")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["strm_default_mode"], "direct")
        self.assertEqual(frontend_status, 404)
        self.assertIn(b"Frontend asset not found", frontend_body)

    def test_settings_api_exposes_three_strm_modes_and_program_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store)

            status, _headers, body = app.handle_request("GET", "/api/v1/settings", {}, b"")
            payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["app_name"], "cms-tg-ingest")
        self.assertRegex(payload["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(
            [item["value"] for item in payload["strm_modes"]],
            ["shared", "direct", "source_shared"],
        )

    def test_self_share_review_api_supports_ten_minutes_off_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(
                store,
                self_share_config=SimpleNamespace(
                    review_grace_seconds=86400,
                    review_checkpoints_seconds=(600, 3600, 21600, 86400),
                ),
            )
            waiting = store.upsert_task("review-wait", "", "https://115cdn.com/s/review-wait")
            store.record_event(
                waiting.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "等待分享审核",
                metadata_patch={"share_review_status": "pending"},
                next_run_at=9999999999.0,
            )

            initial_status, _headers, initial_body = app.handle_request("GET", "/api/v1/settings", {}, b"")
            ten_status, _headers, ten_body = app.handle_request(
                "POST",
                "/api/v1/settings/self-share-review",
                {"Content-Type": "application/json"},
                b'{"mode":"ten_minutes"}',
            )
            woken_next_run_at = store.find_task(waiting.id).next_run_at
            off_status, _headers, off_body = app.handle_request(
                "POST",
                "/api/v1/settings/self-share-review",
                {"Content-Type": "application/json"},
                b'{"mode":"off"}',
            )
            env_status, _headers, env_body = app.handle_request(
                "POST",
                "/api/v1/settings/self-share-review",
                {"Content-Type": "application/json"},
                b'{"mode":"env"}',
            )

        initial = json.loads(initial_body)["self_share_review"]
        ten_minutes = json.loads(ten_body)["self_share_review"]
        off = json.loads(off_body)["self_share_review"]
        env = json.loads(env_body)["self_share_review"]
        self.assertEqual(initial_status, 200)
        self.assertEqual(initial["mode"], "env")
        self.assertEqual(initial["seconds"], 86400)
        self.assertEqual(ten_status, 200)
        self.assertEqual(ten_minutes, {"mode": "ten_minutes", "seconds": 600, "source": "web"})
        self.assertLess(woken_next_run_at, 9999999999.0)
        self.assertEqual(off_status, 200)
        self.assertEqual(off, {"mode": "off", "seconds": 0, "source": "web"})
        self.assertEqual(env_status, 200)
        self.assertEqual(env, {"mode": "env", "seconds": 86400, "source": "env"})

    def test_self_share_review_api_rejects_invalid_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"))

            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/settings/self-share-review",
                {"Content-Type": "application/json"},
                b'{"mode":"invalid"}',
            )

        self.assertEqual(status, 400)
        self.assertIn("审核观察", json.loads(body)["error"])

    def test_own_share_receive_code_api_masks_reads_and_supports_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            config = SimpleNamespace(
                cms_state_db_path=Path(tmp) / "missing-cms.db",
                own_share_receive_code="env7",
            )
            app = WebApp(store, self_share_config=config)

            initial_status, _headers, initial_body = app.handle_request("GET", "/api/v1/overview", {}, b"")
            set_status, _headers, set_body = app.handle_request(
                "POST",
                "/api/v1/settings/own-share-receive-code",
                {"Content-Type": "application/json"},
                b'{"receive_code":"web9"}',
            )
            clear_status, _headers, clear_body = app.handle_request(
                "POST",
                "/api/v1/settings/own-share-receive-code",
                {"Content-Type": "application/json"},
                b'{"clear":true}',
            )

        initial = json.loads(initial_body)["own_share_receive_code"]
        updated = json.loads(set_body)["own_share_receive_code"]
        cleared = json.loads(clear_body)["own_share_receive_code"]
        self.assertEqual(initial_status, 200)
        self.assertEqual(initial, {"configured": True, "masked": "****", "source": "env"})
        self.assertEqual(set_status, 200)
        self.assertEqual(updated, {"configured": True, "masked": "****", "source": "web"})
        self.assertEqual(clear_status, 200)
        self.assertEqual(cleared, initial)
        for body in (initial_body, set_body, clear_body):
            self.assertNotIn("env7", body.decode("utf-8"))
            self.assertNotIn("web9", body.decode("utf-8"))

    def test_own_share_receive_code_api_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"))

            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/settings/own-share-receive-code",
                {"Content-Type": "application/json"},
                b'{"receive_code":"12-12"}',
            )

        self.assertEqual(status, 400)
        self.assertIn("字母和数字", json.loads(body)["error"])

    def test_self_share_receive_cid_api_reads_env_and_updates_runtime_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(
                store,
                self_share_config=SimpleNamespace(self_share_receive_cid="3298928530653445613"),
            )

            initial_status, _headers, initial_body = app.handle_request("GET", "/api/v1/settings", {}, b"")
            set_status, _headers, set_body = app.handle_request(
                "POST",
                "/api/v1/settings/self-share-receive-cid",
                {"Content-Type": "application/json"},
                b'{"receive_cid":"3481694068122059860"}',
            )
            updated_status, _headers, updated_body = app.handle_request("GET", "/api/v1/settings", {}, b"")

        initial = json.loads(initial_body)["self_share_receive_cid"]
        updated = json.loads(updated_body)["self_share_receive_cid"]
        self.assertEqual(initial_status, 200)
        self.assertEqual(initial, {"value": "3298928530653445613", "source": "env"})
        self.assertEqual(set_status, 200)
        self.assertEqual(json.loads(set_body)["self_share_receive_cid"]["value"], "3481694068122059860")
        self.assertEqual(updated_status, 200)
        self.assertEqual(updated, {"value": "3481694068122059860", "source": "web"})

    def test_self_share_receive_cid_api_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"))

            status, _headers, body = app.handle_request(
                "POST",
                "/api/v1/settings/self-share-receive-cid",
                {"Content-Type": "application/json"},
                b'{"receive_cid":"3481x"}',
            )

        self.assertEqual(status, 400)
        self.assertIn("目录 ID", json.loads(body)["error"])

    def test_frontend_history_route_falls_back_to_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<div id='app'>ok</div>", encoding="utf-8")
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"), frontend_dist_path=dist)
            status, _headers, body = app.handle_request("GET", "/app/tasks", {}, b"")

        self.assertEqual(status, 200)
        self.assertIn(b"id='app'", body)

    def test_root_redirects_to_vue_frontend_and_legacy_overview_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = WebApp(TaskStore(Path(tmp) / "tasks.db"))

            root_status, root_headers, root_body = app.handle_request("GET", "/", {}, b"")
            legacy_status, _legacy_headers, legacy_body = app.handle_request("GET", "/legacy", {}, b"")

        self.assertEqual(root_status, 302)
        self.assertEqual(root_headers["Location"], "/app/")
        self.assertEqual(root_body, b"")
        self.assertEqual(legacy_status, 200)
        self.assertIn("运行概览".encode("utf-8"), legacy_body)

    def test_task_action_api_reuses_existing_transition_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "失败", error_summary="重试我")
            app = WebApp(store)

            status, _headers, body = app.handle_request("POST", f"/api/v1/tasks/{task.id}/actions/retry", {}, b"")
            missing_status, _headers, missing_body = app.handle_request("POST", "/api/v1/tasks/999/actions/retry", {}, b"")

            succeeded = store.find_task(task.id)

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "pending")
        self.assertEqual(succeeded.status, TaskStatus.PENDING)
        self.assertEqual(missing_status, 404)
        self.assertEqual(json.loads(missing_body)["error"], "task_not_found")

    def test_task_action_api_uses_injected_retry_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("limited", "", "https://115cdn.com/s/limited")
            for _ in range(3):
                task = store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed", increment_retry=True)
            app = WebApp(store, max_retries=3)

            status, _headers, body = app.handle_request("POST", f"/api/v1/tasks/{task.id}/actions/retry", {}, b"")

        self.assertEqual(status, 409)
        self.assertIn("重试次数超过限制", json.loads(body)["reason"])

    def test_task_action_api_allows_retry_when_configured_limit_is_higher(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("allowed", "", "https://115cdn.com/s/allowed")
            for _ in range(3):
                task = store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed", increment_retry=True)
            app = WebApp(store, max_retries=5)

            status, _headers, body = app.handle_request("POST", f"/api/v1/tasks/{task.id}/actions/retry", {}, b"")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "pending")

    def test_history_and_quality_action_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            completed = store.upsert_task("completed", "", "https://115cdn.com/s/completed")
            store.record_event(completed.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "完成")
            quality = Mock()
            quality.status_snapshot.return_value = {"enabled": True, "time": "02:50"}
            quality.update_settings.return_value = {"enabled": True, "time": "03:10"}
            coordinator = BackgroundJobCoordinator()
            app = WebApp(store, quality_automation=quality, background_jobs=coordinator)

            clear_status, _headers, clear_body = app.handle_request("POST", "/api/v1/history/clear", {}, b"")
            settings_status, _headers, settings_body = app.handle_request(
                "POST",
                "/api/v1/quality/settings",
                {"Content-Type": "application/json"},
                b'{"enabled":true,"time":"03:10","timezone":"Asia/Shanghai","max_tasks":10,"check_limit":2}',
            )
            run_status, _headers, run_body = app.handle_request("POST", "/api/v1/quality/run", {}, b"")
            coordinator.shutdown(wait=True)

        self.assertEqual(clear_status, 200)
        self.assertEqual(json.loads(clear_body)["cleared"], 1)
        self.assertEqual(settings_status, 200)
        quality.update_settings.assert_called_once_with(
            enabled=True,
            run_time="03:10",
            timezone_name="Asia/Shanghai",
            max_tasks=10,
            check_limit=2,
        )
        self.assertEqual(run_status, 202)
        self.assertTrue(json.loads(run_body)["started"])
        quality.run_now.assert_called_once()

    def test_hdhive_action_api_delegates_to_existing_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            service = Mock()
            scheduler = Mock()
            scheduler.settings.return_value = {"enabled": True, "time": "01:30", "timezone": "Asia/Shanghai"}
            scheduler.update_settings.return_value = {"enabled": False, "time": "02:00", "timezone": "Asia/Shanghai"}
            coordinator = BackgroundJobCoordinator()
            app = WebApp(store, hdhive_service=service, hdhive_scheduler=scheduler, background_jobs=coordinator)

            pause_status, _headers, _body = app.handle_request("POST", "/api/v1/hdhive/subscriptions/7/pause", {}, b"")
            confirm_status, _headers, _body = app.handle_request("POST", "/api/v1/hdhive/items/8/confirm", {}, b"")
            settings_status, _headers, settings_body = app.handle_request(
                "POST",
                "/api/v1/hdhive/settings",
                {"Content-Type": "application/json"},
                b'{"enabled":false,"time":"02:00","timezone":"Asia/Shanghai"}',
            )
            run_status, _headers, run_body = app.handle_request("POST", "/api/v1/hdhive/run", {}, b"")
            coordinator.shutdown(wait=True)

        self.assertEqual(pause_status, 200)
        service.pause.assert_called_once_with(7)
        self.assertEqual(confirm_status, 202)
        service.confirm_item.assert_called_once_with(8)
        self.assertEqual(settings_status, 200)
        scheduler.update_settings.assert_called_once_with(enabled=False, run_time="02:00", timezone_name="Asia/Shanghai")
        self.assertEqual(json.loads(settings_body)["settings"]["time"], "02:00")
        self.assertEqual(run_status, 202)
        self.assertTrue(json.loads(run_body)["started"])
        scheduler.run_now.assert_called_once()

    def test_serialize_task_does_not_expose_secret_metadata(self):
        task = type(
            "Task",
            (),
            {
                "id": 1,
                "title": "x",
                "share_code": "x",
                "source_type": "share",
                "current_stage": TaskStage.RECEIVED,
                "status": TaskStatus.PENDING,
                "strm_mode": "shared",
                "category": "",
                "tmdb_id": "",
                "url": "https://115cdn.com/s/x?password=secret",
                "error_type": "",
                "error_summary": "",
                "retry_count": 0,
                "next_run_at": 0,
                "claimed_by": "",
                "metadata": {"own_share_url": "https://115cdn.com/s/x?password=secret", "source_path": "/safe"},
                "created_at": 0,
                "updated_at": 0,
            },
        )()
        payload = serialize_task(task)
        self.assertNotIn("secret", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["metadata"]["source_path"], "/safe")

    def test_serialize_task_exposes_organized_folder_as_display_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task(
                "folder-display",
                "",
                "https://115cdn.com/s/folder-display?password=secret",
            )
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                title="https://115cdn.com/s/folder-display?password=secret",
                metadata_patch={
                    "organized_folder": {
                        "file_name": "H-黑金-2011-[tmdb=77221]",
                    },
                },
            )

            payload = serialize_task(store.find_task(task.id))

        self.assertEqual(payload["title"], "https://115cdn.com/s/folder-display?password=***")
        self.assertEqual(payload["display_title"], "H-黑金-2011-[tmdb=77221]")

    def test_serialize_task_display_title_falls_back_without_folder_metadata(self):
        task = type(
            "Task",
            (),
            {
                "id": 2,
                "title": "原始电影标题",
                "share_code": "fallback-share",
                "source_type": "share",
                "current_stage": TaskStage.RECEIVED,
                "status": TaskStatus.PENDING,
                "strm_mode": "shared",
                "category": "",
                "tmdb_id": "",
                "url": "https://115cdn.com/s/fallback-share",
                "error_type": "",
                "error_summary": "",
                "retry_count": 0,
                "next_run_at": 0,
                "claimed_by": "",
                "metadata": {},
                "created_at": 0,
                "updated_at": 0,
            },
        )()

        payload = serialize_task(task)

        self.assertEqual(payload["display_title"], "原始电影标题")

    def test_api_recursively_redacts_task_event_and_hdhive_credentials(self):
        task = type(
            "Task",
            (),
            {
                "id": 1,
                "title": "safe title",
                "share_code": "safe",
                "source_type": "share",
                "current_stage": TaskStage.RECEIVED,
                "status": TaskStatus.FAILED,
                "strm_mode": "shared",
                "category": "",
                "tmdb_id": "",
                "url": "https://115cdn.com/s/safe",
                "error_type": "remote_error",
                "error_summary": 'Authorization: "Bearer summary-credential"',
                "retry_count": 0,
                "next_run_at": 0,
                "claimed_by": "",
                "metadata": {
                    "nested": [
                        {"own_share_receive_code": "metadata-credential"},
                        ('Cookie: "session=list-credential; csrf=list-csrf"',),
                    ]
                },
                "created_at": 0,
                "updated_at": 0,
            },
        )()
        event = serialize_event(
            {
                "id": 1,
                "stage": "received",
                "status": "failed",
                "message": 'Cookie: "session=event-credential"',
                "error_type": "remote_error",
                "created_at": 0,
            }
        )

        class Proxy:
            def account(self):
                raise RuntimeError('Authorization: "Bearer hdhive-credential"')

        hdhive = serialize_hdhive(SimpleNamespace(list=lambda: [], proxy=Proxy()))
        _status, _headers, body = api_response(
            {
                "task": serialize_task(task),
                "event": event,
                "hdhive": hdhive,
                "nested": [
                    {
                        "message": "Bearer boundary-credential",
                        "web_token": "web-token-credential",
                        "emby_api_key": "emby-key-credential",
                    }
                ],
                "own_share_receive_code": {
                    "configured": True,
                    "masked": "****",
                    "source": "env",
                    "value": "receive-code-credential",
                },
            }
        )
        encoded = body.decode("utf-8")
        decoded = json.loads(encoded)

        for secret in (
            "summary-credential",
            "metadata-credential",
            "list-credential",
            "list-csrf",
            "event-credential",
            "hdhive-credential",
            "boundary-credential",
            "web-token-credential",
            "emby-key-credential",
            "receive-code-credential",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(
            decoded["own_share_receive_code"],
            {"configured": True, "masked": "****", "source": "env"},
        )

    def test_serialize_task_rejects_non_normalizable_ed2k_url_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task(
                "ed2k-safe-url",
                "",
                "ed2k://|file|攻壳机动队：崛起4.mkv|16804289284|ED2K-TEST-HASH|/",
            )

            payload = serialize_task(task)

        self.assertEqual(payload["safe_url"], "")

    def test_serialize_task_reports_effective_shared_mode_for_cloud_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db", default_strm_mode="direct")
            task = store.upsert_cloud_task("btih:web", "magnet:?xt=urn:btih:web")
            with store._connection() as conn:
                conn.execute(
                    "UPDATE tasks SET metadata_json = ? WHERE id = ?",
                    ('{"strm_mode":"direct"}', task.id),
                )

            payload = serialize_task(store.find_task(task.id))

        self.assertEqual(payload["strm_mode"], "shared")


if __name__ == "__main__":
    unittest.main()
