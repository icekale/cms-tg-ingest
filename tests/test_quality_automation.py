import json
import os
import tempfile
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.config import Config
from app.models import TaskStage, TaskStatus
from app.quality import scan_task_quality
from app.quality_automation import QualityAutomation, QualityRepairPlan, QualityRunSummary
from app.task_store import TaskStore


class QualityAutomationConfigTests(unittest.TestCase):
    def required_env(self, tmp):
        return {
            "TG_BOT_TOKEN": "123456:test",
            "TG_ALLOWED_CHAT_ID": "464100862",
            "CMS_BASE_URL": "http://cms:9527",
            "CMS_USERNAME": "user",
            "CMS_PASSWORD": "pass",
            "TASK_DB_PATH": str(Path(tmp) / "tasks.db"),
        }

    def test_quality_automation_defaults_are_disabled_and_conservative(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            config = Config.from_env()

        self.assertFalse(config.quality_auto_enabled)
        self.assertEqual(config.quality_auto_time, "02:50")
        self.assertEqual(config.quality_auto_timezone, "Asia/Shanghai")
        self.assertEqual(config.quality_auto_max_tasks, 50)
        self.assertEqual(config.quality_auto_115_check_limit, 3)

    def test_quality_automation_settings_parse_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update(
                {
                    "QUALITY_AUTO_ENABLED": "true",
                    "QUALITY_AUTO_TIME": "23:05",
                    "QUALITY_AUTO_TIMEZONE": "UTC",
                    "QUALITY_AUTO_MAX_TASKS": "12",
                    "QUALITY_AUTO_115_CHECK_LIMIT": "7",
                }
            )
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

        self.assertTrue(config.quality_auto_enabled)
        self.assertEqual(config.quality_auto_time, "23:05")
        self.assertEqual(config.quality_auto_timezone, "UTC")
        self.assertEqual(config.quality_auto_max_tasks, 12)
        self.assertEqual(config.quality_auto_115_check_limit, 7)

    def test_quality_automation_rejects_invalid_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("2:50", "24:00", "02:60", "02:50:00"):
                env = self.required_env(tmp)
                env["QUALITY_AUTO_TIME"] = value
                with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(ValueError):
                        Config.from_env()

    def test_quality_automation_rejects_invalid_timezone(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {**self.required_env(tmp), "QUALITY_AUTO_TIMEZONE": "Not/AZone"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_quality_automation_rejects_non_positive_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("QUALITY_AUTO_MAX_TASKS", "QUALITY_AUTO_115_CHECK_LIMIT"):
                for value in ("0", "-1"):
                    env = self.required_env(tmp)
                    env[name] = value
                    with self.subTest(name=name, value=value), patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(ValueError):
                            Config.from_env()

    def test_quality_automation_rejects_non_integer_limits_with_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("QUALITY_AUTO_MAX_TASKS", "QUALITY_AUTO_115_CHECK_LIMIT"):
                env = self.required_env(tmp)
                env[name] = "not-a-number"
                with self.subTest(name=name), patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, rf"{name} must be a positive integer"):
                        Config.from_env()


class QualityScheduleTests(unittest.TestCase):
    def make_service(self, tmp, **config_overrides):
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
            **config_overrides,
        )
        store = TaskStore(Path(tmp) / "tasks.db")
        return QualityAutomation(store, config, allowed_roots=[Path(tmp) / "library"]), store

    def test_next_run_at_is_0250_same_local_date_at_0249(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            timezone = ZoneInfo("Asia/Shanghai")
            now = datetime(2026, 7, 20, 2, 49, tzinfo=timezone)

            self.assertEqual(service.next_run_at(now), datetime(2026, 7, 20, 2, 50, tzinfo=timezone))

    def test_run_if_due_claims_one_local_date_across_calls_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            timezone = ZoneInfo("Asia/Shanghai")
            due = datetime(2026, 7, 20, 2, 50, tzinfo=timezone)

            first = service.run_if_due(due)
            second = service.run_if_due(datetime(2026, 7, 20, 3, 0, tzinfo=timezone))
            restarted = QualityAutomation(
                store,
                service.config,
                allowed_roots=[Path(tmp) / "library"],
            ).run_if_due(datetime(2026, 7, 20, 4, 0, tzinfo=timezone))
            next_date = service.run_if_due(datetime(2026, 7, 21, 2, 50, tzinfo=timezone))

            self.assertIsInstance(first, QualityRunSummary)
            self.assertEqual(first.status, "succeeded")
            self.assertEqual(store.get_runtime_state("quality_auto_status")["value"], "succeeded")
            persisted = json.loads(store.get_runtime_state("quality_auto_last_summary")["value"])
            self.assertEqual(persisted["run_id"], next_date.run_id if next_date else first.run_id)
            self.assertIsNone(second)
            self.assertIsNone(restarted)
            self.assertIsInstance(next_date, QualityRunSummary)
            self.assertNotEqual(first.run_id, next_date.run_id)

    def test_run_now_refuses_when_runtime_status_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            store.set_runtime_state("quality_auto_status", "running")

            self.assertFalse(service.run_now())

    def test_run_summary_and_repair_plan_are_immutable(self):
        summary = QualityRunSummary("run", "succeeded")
        plan = QualityRepairPlan(1, "restore", "missing_dest")

        with self.assertRaises(FrozenInstanceError):
            summary.status = "failed"
        with self.assertRaises(FrozenInstanceError):
            plan.action = "reprocess"

    def test_runtime_state_contains_failed_summary_when_scan_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            with patch("app.quality_automation.scan_task_quality", side_effect=RuntimeError("scan failed")):
                summary = service.run_once(
                    "manual-run",
                    datetime(2026, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
                )

            self.assertEqual(summary.status, "failed")
            self.assertEqual(store.get_runtime_state("quality_auto_status")["value"], "failed")
            persisted = json.loads(store.get_runtime_state("quality_auto_last_summary")["value"])
            self.assertEqual(persisted["run_id"], "manual-run")
            self.assertEqual(persisted["status"], "failed")

    def test_stale_running_state_can_recover_but_fresh_running_state_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            timezone = ZoneInfo("Asia/Shanghai")
            due = datetime(2026, 7, 20, 2, 50, tzinfo=timezone)
            stale_at = due.timestamp() - service.STALE_RUN_SECONDS - 1

            store.set_runtime_state("quality_auto_status", "running", updated_at=stale_at)
            store.set_runtime_state("quality_auto_current_run_id", "quality-2026-07-20-crashed", updated_at=stale_at)
            self.assertTrue(store.claim_quality_run("2026-07-20", stale_at))

            recovered = service.run_if_due(due)

            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, "succeeded")

            fresh_due = datetime(2026, 7, 21, 2, 50, tzinfo=timezone)
            fresh_at = fresh_due.timestamp()
            store.set_runtime_state("quality_auto_status", "running", updated_at=fresh_at)
            store.set_runtime_state("quality_auto_current_run_id", "active-run", updated_at=fresh_at)
            store.set_runtime_state("quality_auto_current_run_date", "2026-07-21", updated_at=fresh_at)
            self.assertTrue(store.claim_quality_run("2026-07-21", fresh_at))

            self.assertIsNone(service.run_if_due(fresh_due))

    def test_concurrent_run_now_allows_exactly_one_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            second_service = QualityAutomation(
                TaskStore(Path(tmp) / "tasks.db"),
                service.config,
                allowed_roots=[Path(tmp) / "library"],
            )
            status_reads = Barrier(2)
            scan_started = Event()
            release_scan = Event()
            scan_calls = 0
            scan_calls_lock = Lock()
            original_get_runtime_state = TaskStore.get_runtime_state

            def synchronized_status_read(store, key):
                state = original_get_runtime_state(store, key)
                if key == "quality_auto_status":
                    status_reads.wait(timeout=5)
                return state

            def blocked_scan(*args, **kwargs):
                nonlocal scan_calls
                with scan_calls_lock:
                    scan_calls += 1
                    call_number = scan_calls
                if call_number == 1:
                    scan_started.set()
                    release_scan.wait(timeout=5)
                return []

            with patch.object(TaskStore, "get_runtime_state", synchronized_status_read), patch(
                "app.quality_automation.scan_task_quality", side_effect=blocked_scan
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(item.run_now) for item in (service, second_service)]
                    self.assertTrue(scan_started.wait(timeout=5))
                    done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                    self.assertEqual(len(done), 1)
                    release_scan.set()
                    results = [future.result() for future in futures]

            self.assertEqual(sorted(results), [False, True])


class QualityPlanningTests(unittest.TestCase):
    def make_service(self, tmp, max_tasks=50):
        library = Path(tmp) / "library"
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
            quality_auto_max_tasks=max_tasks,
        )
        return QualityAutomation(TaskStore(Path(tmp) / "tasks.db"), config, allowed_roots=[library]), library

    @staticmethod
    def add_task(store, share_code, dest_path=None, own_share_code="own"):
        task = store.upsert_task(share_code, "", f"https://115cdn.com/s/{share_code}")
        metadata = {}
        if dest_path is not None:
            metadata.update({"dest_path": str(dest_path), "own_share_code": own_share_code})
        return store.record_event(
            task.id,
            TaskStage.MOVED,
            TaskStatus.SUCCEEDED,
            "moved",
            metadata_patch=metadata,
        )

    def test_issue_planning_maps_local_issues_to_one_safe_plan_per_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            missing_dest = self.add_task(service.store, "missing-dest", library / "not-created")
            empty_dest = library / "empty"
            empty_dest.mkdir(parents=True)
            missing_strm = self.add_task(service.store, "missing-strm", empty_dest)
            direct_dest = library / "direct"
            direct_dest.mkdir()
            (direct_dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            direct = self.add_task(service.store, "direct", direct_dest)
            unexpected_dest = library / "unexpected"
            unexpected_dest.mkdir()
            (unexpected_dest / "movie.strm").write_text("https://cms/s/other_1212_file.mkv", encoding="utf-8")
            unexpected = self.add_task(service.store, "unexpected", unexpected_dest)

            summary = service.run_once("planning-run", datetime(2026, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai")))
            plans = {plan.task_id: plan for plan in summary.plans}

            self.assertEqual(plans[missing_dest.id].action, "restore")
            self.assertEqual(plans[missing_strm.id].action, "restore")
            self.assertEqual(plans[direct.id].action, "reprocess")
            self.assertEqual(plans[unexpected.id].action, "reprocess")
            self.assertEqual(summary.planned_count, 4)
            self.assertTrue(all(isinstance(plan, QualityRepairPlan) for plan in summary.plans))

    def test_active_claimed_task_is_skipped_as_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            dest = library / "busy"
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            task = self.add_task(service.store, "busy", dest)
            service.store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.PENDING,
                "queued",
                metadata_patch={"dest_path": str(dest), "own_share_code": "own"},
                next_run_at=0,
            )
            self.assertIsNotNone(service.store.claim_next_runnable("worker", now=1000))

            summary = service.run_once("busy-run")
            plan = next(plan for plan in summary.plans if plan.task_id == task.id)

            self.assertEqual(plan.action, "skip")
            self.assertEqual(plan.reason, "task_busy")

    def test_missing_or_outside_metadata_is_skipped_as_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            missing = self.add_task(service.store, "missing-metadata")
            outside_dest = Path(tmp) / "outside"
            outside_dest.mkdir()
            (outside_dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            outside = self.add_task(service.store, "outside", outside_dest)

            summary = service.run_once("unsafe-run")
            plans = {plan.task_id: plan for plan in summary.plans}

            self.assertEqual(plans[missing.id].reason, "unsafe_metadata")
            self.assertEqual(plans[outside.id].reason, "unsafe_metadata")
            self.assertEqual(plans[missing.id].action, "skip")
            self.assertEqual(plans[outside.id].action, "skip")

    def test_planning_does_not_invoke_external_or_delete_functions(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            dest = library / "movie"
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            self.add_task(service.store, "safe-local", dest)
            with patch("app.quality_automation.scan_task_quality", wraps=scan_task_quality) as scan, patch.object(
                service.store, "record_event"
            ) as record_event, patch.object(service.store, "enqueue_task") as enqueue_task, patch.object(
                service.store, "reprocess_task"
            ) as reprocess_task:
                summary = service.run_once("no-side-effects")

            scan.assert_called_once()
            record_event.assert_not_called()
            enqueue_task.assert_not_called()
            reprocess_task.assert_not_called()
            self.assertEqual(summary.planned_count, 1)


if __name__ == "__main__":
    unittest.main()
