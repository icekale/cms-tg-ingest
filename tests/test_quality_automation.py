import json
import os
import tempfile
import time
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from app.config import Config
from app.models import TaskStage, TaskStatus
from app.quality import QualityIssue, scan_task_quality
from app.quality_automation import (
    QualityAutomation,
    QualityRepairPlan,
    QualityRunSummary,
    load_quality_notify_state,
    quality_notify_signature,
    save_quality_notify_state,
    should_notify_quality_run,
)
from app.quality_rules import QualityRuleMatch
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

    def test_backup_defaults_to_daily_local_database_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.required_env(tmp), clear=True):
            config = Config.from_env()

        self.assertTrue(config.backup_enabled)
        self.assertEqual(config.backup_time, "03:30")
        self.assertEqual(config.backup_timezone, "Asia/Shanghai")
        self.assertEqual(config.backup_dir, "/data/backups")
        self.assertEqual(config.backup_retention_days, 14)

    def test_backup_settings_parse_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env.update(
                {
                    "BACKUP_ENABLED": "false",
                    "BACKUP_TIME": "04:05",
                    "BACKUP_TIMEZONE": "UTC",
                    "BACKUP_DIR": "/var/backups/cms",
                    "BACKUP_RETENTION_DAYS": "30",
                }
            )
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

        self.assertFalse(config.backup_enabled)
        self.assertEqual(config.backup_time, "04:05")
        self.assertEqual(config.backup_timezone, "UTC")
        self.assertEqual(config.backup_dir, "/var/backups/cms")
        self.assertEqual(config.backup_retention_days, 30)

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

    def test_quality_archive_after_seconds_parses_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["QUALITY_ARCHIVE_AFTER_SECONDS"] = "86400"
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

        self.assertEqual(config.quality_archive_after_seconds, 86400)

    def test_quality_unfixable_retention_days_parses_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.required_env(tmp)
            env["QUALITY_UNFIXABLE_RETENTION_DAYS"] = "14"
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()

        self.assertEqual(config.quality_unfixable_retention_days, 14)

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

    def test_corrupt_runtime_overrides_are_ignored_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state(
                "quality_auto_overrides",
                json.dumps(
                    {
                        "quality_auto_enabled": "false",
                        "quality_auto_time": "not-a-time",
                        "quality_auto_timezone": "Not/AZone",
                        "quality_auto_max_tasks": 0,
                        "quality_auto_115_check_limit": -1,
                    }
                ),
            )
            config = Config(
                tg_bot_token="token",
                tg_allowed_chat_id="chat",
                cms_base_url="http://cms",
                cms_username="user",
                cms_password="pass",
                task_db_path=str(Path(tmp) / "tasks.db"),
                quality_auto_enabled=True,
                quality_auto_time="02:50",
                quality_auto_timezone="Asia/Shanghai",
                quality_auto_max_tasks=50,
                quality_auto_115_check_limit=3,
            )

            QualityAutomation(store, config, allowed_roots=[])

            self.assertFalse(config.quality_auto_enabled)
            self.assertEqual(config.quality_auto_time, "02:50")
            self.assertEqual(config.quality_auto_timezone, "Asia/Shanghai")
            self.assertEqual(config.quality_auto_max_tasks, 50)
            self.assertEqual(config.quality_auto_115_check_limit, 3)


class QualityNotifyTests(unittest.TestCase):
    def make_summary(self, plans=(), *, failed_count=0):
        return QualityRunSummary(
            run_id="run-1",
            status="succeeded" if not failed_count else "failed",
            failed_count=failed_count,
            plans=tuple(plans),
        )

    def make_plan(self, task_id, reason="terminal_task", execution_status="skipped", rule_id="no_issue"):
        return QualityRepairPlan(
            task_id=task_id,
            action="skip",
            reason=reason,
            rule_id=rule_id,
            execution_status=execution_status,
        )

    def test_notify_when_repair_execution_failed(self):
        summary = self.make_summary(failed_count=1)
        should_send, signature = should_notify_quality_run(summary)
        self.assertTrue(should_send)
        self.assertEqual(signature, "")

    def test_notify_when_actionable_plan_signature_changes(self):
        queued = self.make_plan(7, reason="reprocess", execution_status="queued", rule_id="strm_mode_mismatch")
        summary = self.make_summary([queued])
        should_send, signature = should_notify_quality_run(summary)
        self.assertTrue(should_send)

        should_send, _ = should_notify_quality_run(summary, previous_signature=signature)
        self.assertFalse(should_send)

        another = self.make_plan(8, reason="reprocess", execution_status="queued", rule_id="strm_mode_mismatch")
        should_send, _ = should_notify_quality_run(self.make_summary([another]), previous_signature=signature)
        self.assertTrue(should_send)

    def test_terminal_skips_do_not_notify(self):
        summary = self.make_summary([self.make_plan(1), self.make_plan(2)])
        should_send, signature = should_notify_quality_run(summary)
        self.assertFalse(should_send)
        self.assertEqual(quality_notify_signature(summary), "")

    def test_new_open_manual_issue_notifies_once(self):
        open_plan = self.make_plan(3, reason="missing_destination")
        summary = self.make_summary([open_plan])
        should_send, _ = should_notify_quality_run(summary, previous_open_ids=frozenset({1}))
        self.assertTrue(should_send)

        should_send, _ = should_notify_quality_run(summary, previous_open_ids=frozenset({1, 3}))
        self.assertFalse(should_send)

    def test_notify_state_round_trips_through_runtime_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            save_quality_notify_state(store, "sig-1", frozenset({3, 7}))
            signature, open_ids = load_quality_notify_state(store)
            self.assertEqual(signature, "sig-1")
            self.assertEqual(open_ids, frozenset({3, 7}))

    def test_corrupt_notify_state_falls_back_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            store.set_runtime_state("quality_auto_last_notify", "not-json")
            signature, open_ids = load_quality_notify_state(store)
            self.assertEqual(signature, "")
            self.assertEqual(open_ids, frozenset())


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

    def test_next_run_at_round_trips_spring_dst_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(
                tmp,
                quality_auto_timezone="America/New_York",
                quality_auto_time="02:50",
            )
            local_timezone = ZoneInfo("America/New_York")
            now = datetime(2026, 3, 8, 1, 49, tzinfo=local_timezone)

            next_run = service.next_run_at(now)

            self.assertEqual(next_run.replace(tzinfo=None), datetime(2026, 3, 8, 3, 50))
            self.assertEqual(next_run.astimezone(timezone.utc), datetime(2026, 3, 8, 7, 50, tzinfo=timezone.utc))
            self.assertIsNone(service.run_if_due(datetime(2026, 3, 8, 3, 49, tzinfo=local_timezone)))
            self.assertEqual(
                service.run_if_due(datetime(2026, 3, 8, 3, 50, tzinfo=local_timezone)).status,
                "succeeded",
            )

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

    def test_same_run_id_allows_only_one_run_once_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            scan_started = Event()
            release_scan = Event()
            scan_calls = 0
            scan_calls_lock = Lock()

            def blocked_scan(*args, **kwargs):
                nonlocal scan_calls
                with scan_calls_lock:
                    scan_calls += 1
                    call_number = scan_calls
                if call_number == 1:
                    scan_started.set()
                    release_scan.wait(timeout=5)
                return []

            fixed_now = datetime(2099, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
            with patch("app.quality_automation.scan_task_quality", side_effect=blocked_scan):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(service.run_once, "same-run", fixed_now)
                    self.assertTrue(scan_started.wait(timeout=5))
                    second = executor.submit(service.run_once, "same-run", fixed_now)
                    second_summary = second.result(timeout=5)
                    release_scan.set()
                    first_summary = first.result(timeout=5)

            self.assertEqual(sorted((first_summary.status, second_summary.status)), ["conflict", "succeeded"])

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
                    datetime(2099, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
                )

            self.assertEqual(summary.status, "failed")
            self.assertEqual(summary.finished_at, summary.started_at)
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

    def test_old_runner_cannot_overwrite_new_lease_when_it_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            timezone = ZoneInfo("Asia/Shanghai")
            started_at = datetime(2026, 7, 20, 2, 50, tzinfo=timezone)
            takeover_at = started_at.timestamp() + service.STALE_RUN_SECONDS + 1
            scan_started = Event()
            release_scan = Event()

            def blocked_scan(*args, **kwargs):
                scan_started.set()
                release_scan.wait(timeout=5)
                return []

            with patch("app.quality_automation.scan_task_quality", side_effect=blocked_scan):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    old_future = executor.submit(service.run_once, "old-run", started_at)
                    self.assertTrue(scan_started.wait(timeout=5))
                    self.assertTrue(
                        store.claim_quality_run_execution(
                            "new-run",
                            takeover_at,
                            stale_after_seconds=service.STALE_RUN_SECONDS,
                        )
                    )
                    service._persist_summary(
                        QualityRunSummary("new-run", "running", started_at=started_at.isoformat()),
                        takeover_at,
                    )
                    release_scan.set()
                    old_summary = old_future.result(timeout=5)

            self.assertEqual(old_summary.status, "superseded")
            self.assertEqual(store.get_runtime_state("quality_auto_status")["value"], "running")
            self.assertEqual(store.get_runtime_state("quality_auto_current_run_id")["value"], "new-run")
            persisted = json.loads(store.get_runtime_state("quality_auto_last_summary")["value"])
            self.assertEqual(persisted["run_id"], "new-run")

    def test_injected_now_controls_finished_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            fixed_now = datetime(2099, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai"))

            summary = service.run_once("injected-time", fixed_now)

            self.assertEqual(summary.finished_at, fixed_now.isoformat())
            self.assertGreaterEqual(
                datetime.fromisoformat(summary.finished_at),
                datetime.fromisoformat(summary.started_at),
            )

    def test_run_once_persists_quality_run_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            summary = service.run_once(
                "history-run",
                datetime(2099, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
            )

            runs = service.store.list_quality_runs()

            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_id"], "history-run")
            self.assertEqual(runs[0]["status"], summary.status)
            self.assertEqual(runs[0]["scanned_count"], summary.scanned_count)
            self.assertEqual(runs[0]["issue_count"], summary.issue_count)


class _ManualActionRuleEngine:
    def evaluate(self, task, issues, *, config=None):
        return QualityRuleMatch(
            rule_id="missing_destination",
            priority=60,
            risk_level="medium",
            reason="destination directory is missing",
            issue_codes=("missing_dest",),
            manual_actions=("view", "snooze", "ignore", "resume"),
            evidence=(str(task.metadata.get("dest_path") or ""),),
        )


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
    def add_task(store, share_code, dest_path=None, own_share_code="own", **extra_metadata):
        task = store.upsert_task(share_code, "", f"https://115cdn.com/s/{share_code}")
        metadata = {}
        if dest_path is not None:
            metadata.update({"dest_path": str(dest_path), "own_share_code": own_share_code})
        metadata.update(extra_metadata)
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
            direct = self.add_task(service.store, "direct", direct_dest, own_share_receive_code="1212")
            unexpected_dest = library / "unexpected"
            unexpected_dest.mkdir()
            (unexpected_dest / "movie.strm").write_text("https://cms/s/other_1212_file.mkv", encoding="utf-8")
            unexpected = self.add_task(service.store, "unexpected", unexpected_dest, own_share_receive_code="1212")

            summary = service.run_once("planning-run", datetime(2026, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai")))
            plans = {plan.task_id: plan for plan in summary.plans}

            self.assertEqual(plans[missing_dest.id].action, "skip")
            self.assertEqual(plans[missing_dest.id].reason, "missing_destination")
            self.assertEqual(plans[missing_strm.id].action, "skip")
            self.assertEqual(plans[missing_strm.id].reason, "missing_strm")
            self.assertEqual(plans[direct.id].action, "reprocess")
            self.assertEqual(plans[unexpected.id].action, "skip")
            self.assertEqual(plans[unexpected.id].reason, "unexpected_strm")
            self.assertEqual(summary.planned_count, 1)
            self.assertTrue(all(isinstance(plan, QualityRepairPlan) for plan in summary.plans))

    def test_missing_destination_is_manual_skip_and_never_calls_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp)
            service.repair_adapter = adapter
            task = self.add_task(service.store, "missing-dest-no-restore", library / "not-created")

            summary = service.run_once("no-restore")
            plan = next(plan for plan in summary.plans if plan.task_id == task.id)

            self.assertEqual(plan.action, "skip")
            self.assertIn(plan.reason, {"missing_destination", "manual_required"})
            self.assertEqual(adapter.calls, [])

    def test_manual_action_reprocesses_only_through_taskstore_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp)
            service.repair_adapter = adapter
            destination = library / "manual-direct"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            task = self.add_task(service.store, "manual-direct", destination, own_share_receive_code="1212")

            first = service.manual_action(
                task.id,
                "strm_mode_mismatch",
                "execute",
                "tester",
                rule_version="1",
            )
            second = service.manual_action(
                task.id,
                "strm_mode_mismatch",
                "execute",
                "tester",
                rule_version="1",
            )

            current = service.store.find_task(task.id)
            self.assertEqual(first["status"], "queued")
            self.assertEqual(first["action"], "execute")
            self.assertEqual(current.current_stage, TaskStage.RECEIVED)
            self.assertEqual(current.status, TaskStatus.PENDING)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(second["status"], "conflict")
            self.assertEqual(len(service.store.list_events(task.id)), 2)

    def test_manual_missing_destination_allows_explicit_reprocess_but_never_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            task = self.add_task(service.store, "manual-missing", library / "does-not-exist")

            restore = service.manual_action(task.id, "missing_destination", "restore", "tester", rule_version="1")
            reprocess = service.manual_action(
                task.id, "missing_destination", "reprocess", "tester", rule_version="1"
            )

            self.assertEqual(restore["status"], "rejected")
            self.assertEqual(restore["reason"], "action_not_allowed")
            self.assertEqual(reprocess["status"], "queued")
            self.assertEqual(service.store.find_task(task.id).current_stage, TaskStage.RECEIVED)

    def test_manual_cloud_reprocess_returns_to_cloud_downloading_and_clears_attempt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            task = service.store.upsert_cloud_task(
                "btih:quality-manual-cloud",
                "magnet:?xt=urn:btih:quality-manual-cloud",
            )
            task = service.store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.PENDING,
                "云下载失败",
                metadata_patch={
                    "dest_path": str(library / "cloud"),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "cloud_task_id": "old-task",
                    "cloud_output_items": [{"file_id": "old-file"}],
                    "auto_organize_pending": True,
                },
            )

            descriptor = {
                "rule_id": "strm_mode_mismatch",
                "available_actions": ["reprocess"],
            }
            with patch.object(service, "quality_descriptor", return_value=descriptor):
                result = service.manual_action(task.id, "strm_mode_mismatch", "reprocess", "tester")

            updated = service.store.find_task(task.id)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(updated.current_stage, TaskStage.CLOUD_DOWNLOADING)
            self.assertEqual(updated.metadata["strm_mode"], "shared")
            self.assertTrue(updated.metadata["quality_repair_queued"])
            self.assertNotIn("cloud_task_id", updated.metadata)
            self.assertNotIn("cloud_output_items", updated.metadata)
            self.assertNotIn("auto_organize_pending", updated.metadata)

    def test_real_rule_descriptors_expose_manual_and_gated_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            missing_dest = self.add_task(service.store, "descriptor-missing-dest", library / "missing")
            empty = library / "empty"
            empty.mkdir(parents=True)
            missing_strm = self.add_task(service.store, "descriptor-missing-strm", empty)
            mismatch_dir = library / "mismatch"
            mismatch_dir.mkdir()
            (mismatch_dir / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
            mismatch = self.add_task(
                service.store,
                "descriptor-mismatch",
                mismatch_dir,
                own_share_receive_code="1212",
            )
            unexpected_dir = library / "unexpected"
            unexpected_dir.mkdir()
            (unexpected_dir / "movie.strm").write_text("https://cms/s/other_1212_movie.mkv", encoding="utf-8")
            unexpected = self.add_task(
                service.store,
                "descriptor-unexpected",
                unexpected_dir,
                own_share_receive_code="1212",
                strm_mode="direct",
            )

            for task, expected_rule in (
                (missing_dest, "missing_destination"),
                (missing_strm, "missing_strm"),
                (mismatch, "strm_mode_mismatch"),
                (unexpected, "unexpected_strm"),
            ):
                with self.subTest(rule=expected_rule):
                    descriptor = service.quality_descriptor(task)
                    self.assertEqual(descriptor["rule_id"], expected_rule)
                    self.assertIn("snooze", descriptor["available_actions"])
                    self.assertIn("ignore", descriptor["available_actions"])

            self.assertNotIn("execute", service.quality_descriptor(missing_dest)["available_actions"])
            self.assertNotIn("execute", service.quality_descriptor(missing_strm)["available_actions"])
            self.assertIn("execute", service.quality_descriptor(mismatch)["available_actions"])
            # unexpected_strm is never auto-reprocessed: stale sibling strm from older
            # share generations cannot be fixed by re-running the pipeline.
            self.assertNotIn("reprocess", service.quality_descriptor(unexpected)["available_actions"])

            service.store.update_quality_state(
                missing_dest.id,
                service.store.find_task(missing_dest.id).updated_at,
                {"quality_manual_status": "manual_required"},
                "manual review",
                "tester",
            )
            manual_required = service.quality_descriptor(service.store.find_task(missing_dest.id))
            self.assertEqual(
                manual_required["available_actions"], ["view", "resume", "snooze", "ignore"]
            )

    def test_manual_required_repeated_failure_can_be_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            task = self.add_task(
                service.store,
                "repeated-manual",
                library / "stale",
                own_share_code="own",
                strm_mode="shared",
                quality_repair_attempts=2,
            )
            library.mkdir(exist_ok=True)
            (library / "stale").mkdir(exist_ok=True)
            (library / "stale" / "movie.strm").write_text(
                "https://cms/d/other.mkv", encoding="utf-8"
            )
            service.store.update_quality_state(
                task.id,
                service.store.find_task(task.id).updated_at,
                {"quality_manual_status": "manual_required"},
                "attempts exhausted",
                "quality-auto",
            )

            descriptor = service.quality_descriptor(service.store.find_task(task.id))
            self.assertEqual(descriptor["rule_id"], "repeated_failure")
            for action in ("view", "snooze", "ignore", "resume"):
                self.assertIn(action, descriptor["available_actions"])

            ignored = service.manual_action(
                task.id, "repeated_failure", "ignore", "tester", rule_version="1"
            )
            self.assertEqual(ignored["status"], "ignored")
            refreshed = service.store.find_task(task.id)
            self.assertEqual(
                str(refreshed.metadata.get("quality_manual_status") or "").strip().lower(),
                "ignored",
            )

    def test_manual_action_snooze_resume_and_rejects_unknown_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            service.rule_engine = _ManualActionRuleEngine()
            task = self.add_task(service.store, "manual-snooze", library / "does-not-exist")

            snoozed = service.manual_action(
                task.id, "missing_destination", "snooze", "tester", until=time.time() + 3600, rule_version="1"
            )
            resumed = service.manual_action(task.id, "missing_destination", "resume", "tester", rule_version="1")
            missing = service.manual_action(999999, "missing_destination", "view", "tester", rule_version="1")

            self.assertEqual(snoozed["status"], "snoozed")
            self.assertEqual(resumed["status"], "resumed")
            self.assertEqual(missing["status"], "not_found")

    def test_manual_action_passes_initial_updated_at_to_cas_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            service.rule_engine = _ManualActionRuleEngine()
            task = self.add_task(service.store, "manual-cas", library / "does-not-exist")
            initial_updated_at = task.updated_at
            original = service.store.mark_quality_snoozed

            with patch.object(service.store, "mark_quality_snoozed", wraps=original) as mark:
                result = service.manual_action(
                    task.id,
                    "missing_destination",
                    "snooze",
                    "tester",
                    until=time.time() + 3600,
                    rule_version="1",
                )

            self.assertEqual(result["status"], "snoozed")
            self.assertEqual(mark.call_args.kwargs["expected_updated_at"], initial_updated_at)

    def test_non_finite_risk_cooldown_is_not_auto_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "risk-nan"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "risk-nan",
                destination,
                own_share_receive_code="1212",
                p115_risk_cooldown_until="NaN",
            )

            descriptor = service.quality_descriptor(service.store.find_task(task.id))

            self.assertEqual(descriptor["rule_id"], "risk_controlled")
            self.assertEqual(descriptor["available_actions"], ["view"])

    def test_manual_snooze_rejects_non_finite_until(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            service.rule_engine = _ManualActionRuleEngine()
            task = self.add_task(service.store, "manual-nan", library / "does-not-exist")
            before = service.store.find_task(task.id)

            for until in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(until=until):
                    result = service.manual_action(
                        task.id,
                        "missing_destination",
                        "snooze",
                        "tester",
                        until=until,
                        rule_version="1",
                    )
                    self.assertEqual(result["status"], "rejected")
                    self.assertEqual(result["reason"], "invalid_until")
                    self.assertEqual(service.store.find_task(task.id), before)
            self.assertEqual(service.store.list_events(task.id), [
                event for event in service.store.list_events(task.id)
            ])

    def test_quality_descriptor_requires_source_evidence_and_blocks_unsafe_risk_terminal_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            no_evidence_dest = library / "no-evidence"
            no_evidence_dest.mkdir(parents=True)
            (no_evidence_dest / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
            no_evidence = self.add_task(service.store, "no-evidence", no_evidence_dest)
            no_evidence_descriptor = service.quality_descriptor(no_evidence)

            unsafe = self.add_task(service.store, "unsafe", Path(tmp) / "outside")
            unsafe_descriptor = service.quality_descriptor(unsafe)

            risk_dest = library / "risk"
            risk_dest.mkdir(parents=True)
            (risk_dest / "movie.strm").write_text("https://cms/d/movie.mkv", encoding="utf-8")
            risk = self.add_task(
                service.store,
                "risk",
                risk_dest,
                own_share_receive_code="1212",
                p115_risk_controlled=True,
            )
            risk_descriptor = service.quality_descriptor(risk)

            terminal = self.add_task(
                service.store,
                "terminal",
                library / "terminal-missing",
                invalid_share_cleaned=True,
            )
            terminal_descriptor = service.quality_descriptor(terminal)

            self.assertNotIn("execute", no_evidence_descriptor["available_actions"])
            self.assertNotIn("reprocess", no_evidence_descriptor["available_actions"])
            self.assertEqual(unsafe_descriptor["available_actions"], ["view"])
            self.assertNotIn("snooze", risk_descriptor["available_actions"])
            self.assertNotIn("ignore", risk_descriptor["available_actions"])
            self.assertNotIn("snooze", terminal_descriptor["available_actions"])
            self.assertNotIn("ignore", terminal_descriptor["available_actions"])

    def test_manual_quality_events_persist_rule_and_action_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            service.rule_engine = _ManualActionRuleEngine()
            task = self.add_task(service.store, "trace", library / "does-not-exist")

            service.manual_action(task.id, "missing_destination", "snooze", "tester", rule_version="1")
            snoozed = service.store.find_task(task.id)
            snooze_event = service.store.list_events(task.id)[-1]
            self.assertEqual(snoozed.metadata["quality_rule_id"], "missing_destination")
            self.assertEqual(snoozed.metadata["quality_repair_action"], "snooze")
            self.assertIn("rule=missing_destination", snooze_event["message"])
            self.assertIn("action=snooze", snooze_event["message"])

            service.manual_action(task.id, "missing_destination", "resume", "tester", rule_version="1")
            resumed = service.store.find_task(task.id)
            resume_event = service.store.list_events(task.id)[-1]
            self.assertEqual(resumed.metadata["quality_rule_id"], "missing_destination")
            self.assertEqual(resumed.metadata["quality_repair_action"], "resume")
            self.assertIn("rule=missing_destination", resume_event["message"])
            self.assertIn("action=resume", resume_event["message"])

    def test_expired_snooze_is_open_for_planning_and_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp)
            service.repair_adapter = adapter
            destination = library / "expired-snooze"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            task = self.add_task(service.store, "expired-snooze", destination, own_share_receive_code="1212")
            service.store.mark_quality_snoozed(task.id, time.time() - 1, "tester")
            current = service.store.find_task(task.id)
            issue = QualityIssue("direct_strm", "direct", str(destination / "movie.strm"), task.id)

            plan = service._plan([current], [issue])[0]

            self.assertEqual(plan.action, "reprocess")
            self.assertEqual(service.execute_plan(plan, "expired-snooze-run").execution_status, "queued")

    def test_invalid_cooldown_is_manual_and_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp)
            service.repair_adapter = adapter
            destination = library / "invalid-cooldown"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "invalid-cooldown",
                destination,
                own_share_receive_code="1212",
                quality_next_eligible_at="not-a-timestamp",
            )
            issue = QualityIssue("direct_strm", "direct", str(destination / "movie.strm"), task.id)
            current = service.store.find_task(task.id)

            plan = service._plan([current], [issue])[0]
            result = service.execute_plan(
                QualityRepairPlan(task.id, "reprocess", "strm_mode_mismatch", ("direct_strm",)),
                "invalid-cooldown-run",
            )
            state = service.store.quality_state(task.id)

            self.assertEqual(plan.action, "skip")
            self.assertEqual(plan.reason, "invalid_cooldown")
            self.assertEqual(state["quality_manual_status"], "manual_required")
            self.assertEqual(state["quality_rule_reason"], "invalid_cooldown")
            self.assertEqual(result.execution_status, "skipped")
            self.assertEqual(result.reason, "invalid_cooldown")
            self.assertEqual(adapter.calls, [])

    def test_reprocess_requires_complete_source_evidence_and_rule_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp)
            service.repair_adapter = adapter
            service.store.set_runtime_state(
                "quality_rule_config",
                json.dumps({"allow_auto_reprocess": True, "max_attempts": 2, "cooldown_seconds": 86400}),
            )
            dest = library / "direct"
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "evidence-required",
                dest,
            )

            summary = service.run_once("evidence-run")
            plan = next(plan for plan in summary.plans if plan.task_id == task.id)

            self.assertEqual(plan.action, "skip")
            self.assertEqual(plan.reason, "missing_source_evidence")
            self.assertEqual(adapter.calls, [])

    def test_terminal_invalid_share_never_enters_restore_or_reprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp)
            service.repair_adapter = adapter
            task = self.add_task(
                service.store,
                "terminal-invalid",
                library / "deleted-destination",
                own_share_receive_code="1212",
                invalid_share_cleaned=True,
            )

            summary = service.run_once("terminal-invalid-run")
            plan = next(plan for plan in summary.plans if plan.task_id == task.id)

            self.assertEqual(plan.action, "skip")
            self.assertEqual(plan.reason, "terminal_invalid_share")
            self.assertEqual(adapter.calls, [])

    def test_115_check_budget_limits_reprocess_adapter_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp)
            service.repair_adapter = adapter
            service.config.quality_auto_115_check_limit = 1
            tasks = []
            for name in ("budget-one", "budget-two"):
                dest = library / name
                dest.mkdir(parents=True)
                (dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
                tasks.append(
                    self.add_task(service.store, name, dest, own_share_receive_code="1212")
                )

            summary = service.run_once("budget-run")
            plans = {plan.task_id: plan for plan in summary.plans}

            self.assertEqual(len([call for call in adapter.calls if call[0] == "reprocess"]), 1)
            self.assertEqual(sum(plan.execution_status == "queued" for plan in summary.plans), 1)
            self.assertEqual(sum(plan.reason == "115_check_budget" for plan in summary.plans), 1)
            self.assertEqual(summary.budget_used["115_check_limit"], 1)
            self.assertEqual(summary.budget_used["used"]["115_checks"], 1)
            self.assertEqual(set(plans), {task.id for task in tasks})

    def test_max_tasks_budget_limits_actual_adapter_calls_across_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, max_tasks=1)
            service.repair_adapter = adapter
            tasks = []
            for name in ("max-one", "max-two"):
                dest = library / name
                dest.mkdir(parents=True)
                (dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
                tasks.append(self.add_task(service.store, name, dest, own_share_receive_code="1212"))

            summary = service.run_once("max-tasks-run")

            self.assertEqual(len([call for call in adapter.calls if call[0] == "reprocess"]), 1)
            self.assertEqual(sum(plan.execution_status == "queued" for plan in summary.plans), 1)
            self.assertEqual(sum(plan.reason == "max_tasks" for plan in summary.plans), 1)
            self.assertEqual(summary.budget_used["max_tasks"], 1)
            self.assertEqual(summary.budget_used["used"]["max_tasks"], 1)

    def test_reprocess_cooldown_and_attempt_limit_block_until_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            dest = library / "cooldown"
            dest.mkdir(parents=True)
            task = self.add_task(service.store, "cooldown", dest, own_share_receive_code="1212")
            issue = QualityIssue("direct_strm", "direct", str(dest / "movie.strm"), task.id)

            service.store.patch_metadata(
                task.id,
                {"quality_repair_attempts": 1, "quality_next_eligible_at": 200.0},
            )
            current = service.store.find_task(task.id)
            cooldown = service._plan([current], [issue], now=100.0)[0]
            self.assertEqual(cooldown.reason, "cooldown")

            service.store.patch_metadata(task.id, {"quality_next_eligible_at": 0})
            current = service.store.find_task(task.id)
            allowed = service._plan([current], [issue], now=100.0)[0]
            self.assertEqual(allowed.action, "reprocess")

            service.store.patch_metadata(task.id, {"quality_repair_attempts": 2})
            current = service.store.find_task(task.id)
            exhausted = service._plan([current], [issue], now=200000.0)[0]
            self.assertEqual(exhausted.reason, "manual_required")
            self.assertEqual(service.store.quality_state(task.id)["quality_manual_status"], "manual_required")

            service.store.resume_quality(task.id, "tester")
            current = service.store.find_task(task.id)
            resumed = service._plan([current], [issue], now=100.0)[0]
            self.assertEqual(resumed.action, "reprocess")

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

            self.assertFalse(any(plan.task_id == task.id for plan in summary.plans))

    def test_safe_task_without_issues_is_still_evaluated_without_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            task = self.add_task(service.store, "clean-evaluation", library / "movie")
            evaluator = Mock(wraps=service.rule_engine.evaluate)
            service.rule_engine.evaluate = evaluator

            plans = service._plan([task], [])

            evaluator.assert_called_once()
            self.assertEqual(evaluator.call_args.args[1], [])
            self.assertEqual(plans, [])

    def test_missing_or_outside_metadata_is_skipped_as_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            missing = self.add_task(service.store, "missing-metadata")
            outside_dest = Path(tmp) / "outside"
            outside_dest.mkdir()
            (outside_dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            outside = self.add_task(service.store, "outside", outside_dest)

            with patch.object(Path, "read_text", side_effect=AssertionError("unsafe STRM was read")):
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
                service.store, "enqueue_task"
            ) as enqueue_task, patch.object(
                service.store, "reprocess_task"
            ) as reprocess_task:
                summary = service.run_once("no-side-effects")

            scan.assert_called_once()
            enqueue_task.assert_not_called()
            reprocess_task.assert_not_called()
            self.assertEqual(summary.planned_count, 0)

    def test_run_once_uses_one_task_snapshot_for_scan_and_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            with patch.object(
                service.store,
                "list_recent_tasks",
                wraps=service.store.list_recent_tasks,
            ) as list_recent_tasks:
                service.run_once("one-snapshot", datetime(2099, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai")))

            self.assertEqual(list_recent_tasks.call_count, 1)

    def test_run_once_scans_beyond_repair_limit_without_increasing_repair_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            direct_dest = library / "direct"
            direct_dest.mkdir(parents=True)
            (direct_dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            service, _ = self.make_service(tmp, max_tasks=1)
            direct = self.add_task(service.store, "direct", direct_dest, own_share_receive_code="1212")
            clean = service.store.upsert_task("clean", "", "https://115cdn.com/s/clean")
            service.store.record_event(clean.id, TaskStage.MOVED, TaskStatus.SUCCEEDED, "moved")

            summary = service.run_once("scan-more-than-repair-limit")

            self.assertEqual(summary.scanned_count, 2)
            self.assertEqual(summary.planned_count, 1)


class FakeQualityRepairAdapter:
    def __init__(self):
        self.calls = []
        self.release_restore = None
        self.release_reprocess = None

    def restore(self, task, run_id):
        self.calls.append(("restore", task.id, run_id))
        if self.release_restore is not None:
            self.release_restore.wait(timeout=5)
        return True

    def reprocess(self, task, run_id):
        self.calls.append(("reprocess", task.id, run_id))
        if self.release_reprocess is not None:
            self.release_reprocess.wait(timeout=5)
        return True

    def rebuild_invalid_share(self, task, run_id):
        self.calls.append(("invalid_share", task.id, run_id))
        return True

    def cleanup(self, task, run_id):
        self.calls.append(("cleanup", task.id, run_id))
        return True


class ClaimTakingAdapter(FakeQualityRepairAdapter):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def reprocess(self, task, run_id):
        self.calls.append(("reprocess", task.id, run_id))
        takeover = self.store.compare_and_set_transition(
            task.id,
            task.current_stage,
            {TaskStatus.RUNNING},
            require_unclaimed=False,
            target_stage=task.current_stage,
            target_status=TaskStatus.RUNNING,
            target_event_message="claim taken over",
            claim_by="worker:takeover",
        )
        self.assert_taken_over = takeover is not None
        return True


class MetadataUpdatingAdapter(FakeQualityRepairAdapter):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def reprocess(self, task, run_id):
        self.calls.append(("reprocess", task.id, run_id))
        self.store.patch_metadata(task.id, {"adapter_metadata": "updated"})
        return True


class CleanupClaimTakingAdapter(FakeQualityRepairAdapter):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def cleanup(self, task, run_id):
        self.calls.append(("cleanup", task.id, run_id))
        takeover = self.store.compare_and_set_transition(
            task.id,
            task.current_stage,
            {TaskStatus.SUCCEEDED},
            require_unclaimed=False,
            target_stage=task.current_stage,
            target_status=TaskStatus.SUCCEEDED,
            target_event_message="cleanup claim taken over",
            claim_by="worker:cleanup-takeover",
        )
        self.assert_taken_over = takeover is not None
        return True


class CleanupMetadataUpdatingAdapter(FakeQualityRepairAdapter):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def cleanup(self, task, run_id):
        self.calls.append(("cleanup", task.id, run_id))
        self.store.patch_metadata(task.id, {"adapter_cleanup_metadata": "updated"})
        return True


class RenewingCleanupAdapter(FakeQualityRepairAdapter):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.renewed = False
        self.wrong_token_renewed = False
        self.renewed_claim = None

    def cleanup(self, task, run_id):
        self.calls.append(("cleanup", task.id, run_id))
        self.wrong_token_renewed = self.store.renew_claim(
            task.id,
            task.claimed_by,
            "wrong-token",
            now=task.claimed_at + 1,
        )
        self.renewed = self.store.renew_claim(
            task.id,
            task.claimed_by,
            task.claim_token,
            now=task.claimed_at + 2,
        )
        self.renewed_claim = self.store.find_task(task.id)
        return self.renewed


class QualityRepairExecutionTests(unittest.TestCase):
    def make_service(self, tmp, adapter=None):
        library = Path(tmp) / "library"
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
        )
        return QualityAutomation(
            TaskStore(Path(tmp) / "tasks.db"),
            config,
            allowed_roots=[library],
            repair_adapter=adapter,
        ), library

    @staticmethod
    def add_task(store, share_code, dest, **metadata):
        task = store.upsert_task(share_code, "", f"https://115cdn.com/s/{share_code}")
        return store.record_event(
            task.id,
            TaskStage.MOVED,
            TaskStatus.SUCCEEDED,
            "moved",
            metadata_patch={"dest_path": str(dest), "own_share_code": "own", **metadata},
        )

    def test_restore_and_reprocess_use_atomic_existing_task_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            missing = self.add_task(service.store, "missing", library / "missing")
            direct_dest = library / "direct"
            direct_dest.mkdir(parents=True)
            (direct_dest / "movie.strm").write_text("https://cms/d/direct.mkv", encoding="utf-8")
            direct = self.add_task(
                service.store,
                "direct",
                direct_dest,
                organized_scan_cursor={"queue": [{"parent_id": "old-parent"}]},
                organized_folder={"file_id": "old-folder"},
                received_title="旧文件 {tmdb-952936}",
                received_file_ids=["old-file"],
                received_items=[{"file_id": "old-file"}],
                received_items_complete=True,
                received_expected_item_count=1,
                received_existing_file_ids=[],
                received_snapshot_complete=True,
                tmdb_hint_normalized=True,
                own_share_receive_code="1212",
            )

            summary = service.run_once("repair-run", datetime(2099, 7, 20, 2, 50, tzinfo=ZoneInfo("Asia/Shanghai")))

            self.assertEqual(summary.status, "succeeded")
            self.assertEqual(summary.queued_count, 1)
            self.assertEqual(service.store.find_task(missing.id).current_stage, TaskStage.MOVED)
            self.assertEqual(service.store.find_task(direct.id).current_stage, TaskStage.RECEIVED)
            reprocessed = service.store.find_task(direct.id)
            self.assertEqual(reprocessed.metadata["force_reprocess"], True)
            self.assertGreater(reprocessed.metadata["reprocess_started_at"], 0)
            for key in (
                "organized_scan_cursor",
                "organized_folder",
                "received_title",
                "received_file_ids",
                "received_items",
                "received_items_complete",
                "received_expected_item_count",
                "received_existing_file_ids",
                "received_snapshot_complete",
                "tmdb_hint_normalized",
            ):
                self.assertNotIn(key, reprocessed.metadata)
            self.assertEqual(
                sorted(call[0] for call in adapter.calls),
                ["reprocess"],
            )

    def test_automatic_cloud_reprocess_returns_to_cloud_downloading_and_clears_attempt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            task = service.store.upsert_cloud_task(
                "btih:quality-auto-cloud",
                "magnet:?xt=urn:btih:quality-auto-cloud",
            )
            task = service.store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.PENDING,
                "云下载失败",
                metadata_patch={
                    "dest_path": str(library / "cloud"),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "cloud_task_id": "old-task",
                    "cloud_output_items": [{"file_id": "old-file"}],
                    "auto_organize_pending": True,
                },
            )
            plan = QualityRepairPlan(
                task.id,
                "reprocess",
                "strm_mode_mismatch",
                ("direct_strm",),
                planned_updated_at=task.updated_at,
            )

            result = service.execute_plan(plan, "quality-auto-cloud-run")

            updated = service.store.find_task(task.id)
            self.assertEqual(result.execution_status, "queued")
            self.assertEqual(updated.current_stage, TaskStage.CLOUD_DOWNLOADING)
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.metadata["strm_mode"], "shared")
            self.assertTrue(updated.metadata["quality_repair_queued"])
            self.assertNotIn("cloud_task_id", updated.metadata)
            self.assertNotIn("cloud_output_items", updated.metadata)
            self.assertNotIn("auto_organize_pending", updated.metadata)

    def test_invalid_share_requires_explicit_invalid_status_and_risk_is_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            invalid = self.add_task(service.store, "invalid", library / "invalid", invalid_share_status="invalid")
            unknown = self.add_task(service.store, "unknown", library / "unknown", invalid_share_status="unknown")
            risk = self.add_task(
                service.store,
                "risk",
                library / "risk",
                p115_risk_controlled=True,
            )
            with patch(
                "app.quality_automation.scan_task_quality",
                return_value=[
                    QualityIssue("invalid_share", "share unavailable", task_id=invalid.id),
                    QualityIssue("invalid_share", "share unavailable", task_id=unknown.id),
                    QualityIssue("direct_strm", "direct", task_id=risk.id),
                ],
            ):
                summary = service.run_once("invalid-run")

            plans = {plan.task_id: plan for plan in summary.plans}
            self.assertEqual(plans[invalid.id].execution_status, "skipped")
            self.assertEqual(plans[invalid.id].reason, "terminal_invalid_share")
            self.assertEqual(plans[unknown.id].execution_status, "skipped")
            self.assertEqual(plans[unknown.id].reason, "manual_required")
            self.assertEqual(plans[risk.id].execution_status, "skipped")
            self.assertEqual(plans[risk.id].reason, "risk_controlled")
            self.assertEqual([call[0] for call in adapter.calls], [])

    def test_execute_plan_rejects_explicit_invalid_share_before_cas(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            task = self.add_task(
                service.store,
                "execute-invalid",
                library / "movie",
                own_share_receive_code="1212",
                invalid_share_status="invalid",
            )
            plan = QualityRepairPlan(
                task.id,
                "reprocess",
                "strm_mode_mismatch",
                ("direct_strm",),
                planned_updated_at=task.updated_at,
            )

            result = service.execute_plan(plan, "execute-invalid-run")

            self.assertEqual(result.execution_status, "skipped")
            self.assertEqual(result.reason, "terminal_invalid_share")
            self.assertEqual(adapter.calls, [])
            current = service.store.find_task(task.id)
            self.assertEqual(current.current_stage, TaskStage.MOVED)
            self.assertEqual(current.status, TaskStatus.SUCCEEDED)

    def test_cleanup_requires_all_positive_gates_and_preserves_files_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            destination = library / "movie"
            destination.mkdir(parents=True)
            strm = destination / "movie.strm"
            strm.write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "cleanup",
                destination,
                own_share_available=True,
                own_share_receive_code="1212",
                emby_status="confirmed",
                emby_match_count=1,
                share_review_status="passed",
            )

            blocked = service.cleanup_if_safe(task, "cleanup-blocked")
            self.assertEqual(blocked.status, "blocked_cleanup")
            self.assertEqual(adapter.calls, [])
            self.assertTrue(strm.exists())

            service.store.patch_metadata(task.id, {"quality_success_event": True})
            service.store.record_event(
                task.id,
                TaskStage.EMBY_CONFIRMED,
                TaskStatus.SUCCEEDED,
                "Emby confirmed",
            )
            current = service.store.find_task(task.id)
            self.assertEqual(service.cleanup_if_safe(current, "cleanup-ok").status, "cleaned")
            self.assertEqual([call[0] for call in adapter.calls], ["cleanup"])

    def test_cleanup_does_not_run_when_emby_confirmation_is_not_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "emby-failure",
                destination,
                own_share_available=True,
                emby_status="failed",
                emby_match_count=0,
            )

            result = service.cleanup_if_safe(task, "emby-failure")

            self.assertEqual(result.status, "blocked_cleanup")
            self.assertEqual(result.reason, "emby_not_confirmed_unique")
            self.assertEqual(adapter.calls, [])

    def test_cleanup_requires_async_share_review_to_have_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            destination = library / "review-pending"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "review-pending",
                destination,
                own_share_available=True,
                own_share_receive_code="1212",
                emby_status="confirmed",
                emby_match_count=1,
                share_review_status="pending",
            )
            service.store.patch_metadata(task.id, {"quality_success_event": True})
            service.store.record_event(task.id, TaskStage.EMBY_CONFIRMED, TaskStatus.SUCCEEDED, "Emby confirmed")
            current = service.store.find_task(task.id)

            result = service.cleanup_if_safe(current, "review-pending")

            self.assertEqual(result.status, "blocked_cleanup")
            self.assertEqual(result.reason, "share_review_not_passed")
            self.assertEqual(adapter.calls, [])

    def test_two_quality_owners_cannot_execute_same_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_adapter = FakeQualityRepairAdapter()
            second_adapter = FakeQualityRepairAdapter()
            first, library = self.make_service(tmp, first_adapter)
            second, _ = self.make_service(tmp, second_adapter)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_task(first.store, "duplicate-owner", destination, own_share_receive_code="1212")
            plan = QualityRepairPlan(task.id, "reprocess", "direct_strm", ("direct_strm",))
            first_adapter.release_reprocess = Event()

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(first.execute_plan, plan, "owner-one")
                for _ in range(50):
                    current = first.store.find_task(task.id)
                    if current and current.status == TaskStatus.RUNNING:
                        break
                    Event().wait(0.01)
                second_result = second.execute_plan(plan, "owner-two")
                first_adapter.release_reprocess.set()
                first_result = first_future.result(timeout=5)

            self.assertEqual(first_result.execution_status, "queued")
            self.assertEqual(second_result.execution_status, "skipped")
            self.assertEqual(second_result.reason, "task_busy")

    def test_completion_does_not_clear_a_claim_taken_over_by_another_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            adapter = ClaimTakingAdapter(store)
            service, library = self.make_service(tmp, adapter)
            destination = library / "claim-taken-over"
            destination.mkdir(parents=True)
            task = self.add_task(service.store, "claim-taken-over", destination, own_share_receive_code="1212")
            plan = QualityRepairPlan(task.id, "reprocess", "strm_mode_mismatch", ("direct_strm",))

            result = service.execute_plan(plan, "claim-owner")
            current = service.store.find_task(task.id)

            self.assertEqual(result.execution_status, "skipped")
            self.assertEqual(result.reason, "claim_lost")
            self.assertTrue(adapter.assert_taken_over)
            self.assertEqual(current.claimed_by, "worker:takeover")
            self.assertEqual(current.status, TaskStatus.RUNNING)

    def test_completion_rejects_metadata_update_during_adapter_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            adapter = MetadataUpdatingAdapter(store)
            service, library = self.make_service(tmp, adapter)
            destination = library / "metadata-updated"
            destination.mkdir(parents=True)
            task = self.add_task(service.store, "metadata-updated", destination, own_share_receive_code="1212")
            plan = QualityRepairPlan(task.id, "reprocess", "strm_mode_mismatch", ("direct_strm",))

            result = service.execute_plan(plan, "metadata-owner")
            current = service.store.find_task(task.id)

            self.assertEqual(result.execution_status, "skipped")
            self.assertEqual(result.reason, "claim_lost")
            self.assertEqual(current.claimed_by, "quality:metadata-owner")
            self.assertEqual(current.status, TaskStatus.RUNNING)
            self.assertEqual(current.metadata["adapter_metadata"], "updated")
            self.assertFalse(current.metadata.get("quality_repair_queued", False))

    def test_cleanup_completion_rejects_a_claim_taken_over_by_another_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CleanupClaimTakingAdapter(TaskStore(Path(tmp) / "tasks.db"))
            service, library = self.make_service(tmp, adapter)
            destination = library / "cleanup-claim-taken-over"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "cleanup-claim-taken-over",
                destination,
                own_share_available=True,
                own_share_receive_code="1212",
                emby_status="confirmed",
                emby_match_count=1,
                share_review_status="passed",
            )
            service.store.patch_metadata(task.id, {"quality_success_event": True})
            service.store.record_event(task.id, TaskStage.EMBY_CONFIRMED, TaskStatus.SUCCEEDED, "Emby confirmed")
            current = service.store.find_task(task.id)

            result = service.cleanup_if_safe(current, "cleanup-owner")
            final = service.store.find_task(task.id)

            self.assertEqual(result.status, "blocked_cleanup")
            self.assertEqual(result.reason, "cleanup_completion_persist_failed")
            self.assertTrue(adapter.assert_taken_over)
            self.assertEqual(final.claimed_by, "worker:cleanup-takeover")
            self.assertFalse(final.metadata.get("quality_cleanup_completed", False))

    def test_quality_cleanup_terminal_claim_can_be_renewed_by_token_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            adapter = RenewingCleanupAdapter(service.store)
            service.repair_adapter = adapter
            destination = library / "cleanup-renewal"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "cleanup-renewal",
                destination,
                own_share_available=True,
                own_share_receive_code="1212",
                emby_status="confirmed",
                emby_match_count=1,
                share_review_status="passed",
            )
            service.store.patch_metadata(task.id, {"quality_success_event": True})
            service.store.record_event(task.id, TaskStage.EMBY_CONFIRMED, TaskStatus.SUCCEEDED, "Emby confirmed")
            current = service.store.find_task(task.id)

            result = service.cleanup_if_safe(current, "cleanup-renewal")

            self.assertEqual(result.status, "cleaned")
            self.assertFalse(adapter.wrong_token_renewed)
            self.assertTrue(adapter.renewed)
            self.assertEqual(adapter.renewed_claim.status, TaskStatus.SUCCEEDED)
            self.assertEqual(adapter.renewed_claim.claim_heartbeat_at, adapter.renewed_claim.claimed_at + 2)

    def test_cleanup_completion_rejects_metadata_update_during_adapter_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            adapter = CleanupMetadataUpdatingAdapter(service.store)
            service.repair_adapter = adapter
            destination = library / "cleanup-metadata-updated"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_task(
                service.store,
                "cleanup-metadata-updated",
                destination,
                own_share_available=True,
                own_share_receive_code="1212",
                emby_status="confirmed",
                emby_match_count=1,
                share_review_status="passed",
            )
            service.store.patch_metadata(task.id, {"quality_success_event": True})
            service.store.record_event(task.id, TaskStage.EMBY_CONFIRMED, TaskStatus.SUCCEEDED, "Emby confirmed")
            current = service.store.find_task(task.id)

            result = service.cleanup_if_safe(current, "cleanup-metadata-owner")
            final = service.store.find_task(task.id)

            self.assertEqual(result.status, "blocked_cleanup")
            self.assertEqual(result.reason, "cleanup_completion_persist_failed")
            self.assertEqual(final.claimed_by, "quality-cleanup:cleanup-metadata-owner")
            self.assertEqual(final.metadata["adapter_cleanup_metadata"], "updated")
            self.assertFalse(final.metadata.get("quality_cleanup_completed", False))

    def test_two_executions_reach_attempt_limit_and_third_is_not_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeQualityRepairAdapter()
            service, library = self.make_service(tmp, adapter)
            destination = library / "attempts"
            destination.mkdir(parents=True)
            task = self.add_task(service.store, "attempts", destination, own_share_receive_code="1212")
            issue = QualityIssue("direct_strm", "direct", str(destination / "movie.strm"), task.id)

            first = service._plan([task], [issue])[0]
            self.assertEqual(service.execute_plan(first, "attempt-one").execution_status, "queued")

            service.store.patch_metadata(
                task.id,
                {
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "quality_next_eligible_at": 0,
                },
            )
            current = service.store.find_task(task.id)
            second = service._plan([current], [issue])[0]
            self.assertEqual(service.execute_plan(second, "attempt-two").execution_status, "queued")

            service.store.patch_metadata(
                task.id,
                {
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "quality_next_eligible_at": 0,
                },
            )
            current = service.store.find_task(task.id)
            third = service._plan([current], [issue])[0]

            self.assertEqual(third.reason, "manual_required")


class QualityAutoRecheckTests(unittest.TestCase):
    def make_service(self, tmp):
        library = Path(tmp) / "library"
        library.mkdir(parents=True)
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
        )
        return QualityAutomation(TaskStore(Path(tmp) / "tasks.db"), config, allowed_roots=[library]), library

    def add_waiting_task(self, store, dest_path=None, **metadata_patch):
        task = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
        metadata = {
            "retry_stage": "emby_confirmed",
            "quality_issue_codes": ["missing_strm"],
        }
        if dest_path is not None:
            metadata.update({"dest_path": str(dest_path), "own_share_code": "own", "own_share_receive_code": "1212"})
        metadata.update(metadata_patch)
        return store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "目标 STRM 等待超时，请人工检查后重试",
            error_type="stage_wait_timeout",
            error_summary="目标 STRM 等待超时，请人工检查后重试",
            metadata_patch=metadata,
        )

    def test_recovered_waiting_task_is_requeued_to_retry_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_waiting_task(service.store, destination)

            plan = service._plan([task], [], now=100.0)[0]

            self.assertEqual(plan.action, "requeue")
            self.assertEqual(plan.rule_id, "auto_recheck")
            self.assertEqual(plan.target_stage, "emby_confirmed")

            executed = service.execute_plan(plan, "run-1")
            current = service.store.find_task(task.id)
            self.assertEqual(executed.execution_status, "queued")
            self.assertEqual(current.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(current.status, TaskStatus.PENDING)
            self.assertEqual(current.next_run_at, 0)
            self.assertEqual(current.error_type, "")
            self.assertEqual(current.metadata["quality_auto_recheck_count"], 1)
            self.assertGreater(current.metadata["quality_auto_recheck_next_at"], 100.0)
            self.assertIn("自动复检", service.store.list_events(task.id)[-1]["message"])

    def test_still_missing_waiting_task_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            task = self.add_waiting_task(service.store, library / "missing")
            issue = QualityIssue(
                "missing_strm",
                "destination has no STRM file",
                str(library / "missing"),
                task.id,
            )

            plans = service._plan([task], [issue], now=100.0)

            self.assertTrue(plans)
            self.assertEqual(plans[0].action, "skip")
            self.assertFalse(any(plan.action == "requeue" for plan in plans))

    def test_unsafe_retry_stage_is_never_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_waiting_task(service.store, destination, retry_stage="received")

            plans = service._plan([task], [], now=100.0)

            self.assertFalse(any(plan.action == "requeue" for plan in plans))

    def test_requeue_respects_cooldown_and_attempt_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            in_cooldown = self.add_waiting_task(
                service.store,
                destination,
                quality_auto_recheck_next_at=200.0,
            )
            capped = self.add_waiting_task(
                service.store,
                library / "other",
                quality_auto_recheck_count=3,
            )
            (library / "other").mkdir(parents=True)
            (library / "other" / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")

            plans = {plan.task_id: plan for plan in service._plan([in_cooldown, capped], [], now=100.0)}

            self.assertNotIn(in_cooldown.id, plans)
            self.assertNotIn(capped.id, plans)

    def test_requeue_skips_claimed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.add_waiting_task(service.store, destination)
            service.store.compare_and_set_transition(
                task.id,
                TaskStage.NEEDS_ACTION,
                {TaskStatus.NEEDS_ACTION},
                require_unclaimed=True,
                target_stage=TaskStage.NEEDS_ACTION,
                target_status=TaskStatus.NEEDS_ACTION,
                target_event_message="claim",
                claim_by="worker-1",
                expected_updated_at=task.updated_at,
            )
            current = service.store.find_task(task.id)

            plans = service._plan([current], [], now=100.0)

            self.assertFalse(any(plan.action == "requeue" for plan in plans))

    def test_exhausted_or_cooldown_recheck_candidate_is_skipped_from_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            exhausted = self.add_waiting_task(service.store, destination, quality_auto_recheck_count=3)
            cooled = self.add_waiting_task(service.store, destination, quality_auto_recheck_next_at=200.0)

            self.assertEqual(service._scan_skip_reason(exhausted, 100.0), "terminal")
            self.assertEqual(service._scan_skip_reason(cooled, 100.0), "terminal")
            self.assertFalse(service._recheck_candidate(exhausted, 100.0))
            self.assertFalse(service._recheck_candidate(cooled, 100.0))
            self.assertIsNone(service._auto_recheck_plan(exhausted, 100.0))
            self.assertIsNone(service._auto_recheck_plan(cooled, 100.0))


class QualityArchiveTests(unittest.TestCase):
    def make_service(self, tmp, archive_after_seconds=7 * 24 * 60 * 60):
        library = Path(tmp) / "library"
        library.mkdir(parents=True)
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
            quality_archive_after_seconds=archive_after_seconds,
        )
        return QualityAutomation(TaskStore(Path(tmp) / "tasks.db"), config, allowed_roots=[library]), library

    def terminal_task(self, store, *, rule_id="unsafe_path", issue_codes=("unsafe_metadata",), age_seconds=8 * 86400):
        task = store.upsert_task("legacy", "", "https://115cdn.com/s/legacy")
        task = store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "legacy terminal",
            error_type="",
            error_summary="",
            metadata_patch={"quality_rule_id": rule_id, "quality_issue_codes": list(issue_codes)},
        )
        with store._connection() as conn:
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (time.time() - age_seconds, task.id),
            )
        return store.find_task(task.id)

    def test_old_terminal_unsafe_task_is_archived_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            task = self.terminal_task(service.store)

            archived = service._archive_terminal_task(task, time.time(), "run-1")

            self.assertTrue(archived)
            current = service.store.find_task(task.id)
            self.assertGreater(float(current.metadata.get("quality_archived_at") or 0), 0)
            self.assertTrue(service._is_archived(current))
            self.assertEqual(service._scan_skip_reason(current, time.time()), "archived")
            plans = service._plan([current], [], now=time.time())
            self.assertTrue(plans)
            self.assertEqual(plans[0].reason, "archived")

    def test_resume_clears_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            task = self.terminal_task(service.store)
            service._archive_terminal_task(task, time.time(), "run-1")

            result = service.manual_action(task.id, "unsafe_path", "resume", "tester", rule_version="1")

            self.assertEqual(result["status"], "resumed")
            current = service.store.find_task(task.id)
            self.assertFalse(service._is_archived(current))

    def test_recent_or_non_archive_rule_terminal_task_is_not_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            recent = self.terminal_task(service.store, age_seconds=3600)
            other_rule = self.terminal_task(service.store, rule_id="missing_strm", issue_codes=("missing_strm",))

            self.assertFalse(service._archive_terminal_task(recent, time.time(), "run-1"))
            self.assertFalse(service._archive_terminal_task(other_rule, time.time(), "run-1"))

    def test_terminal_marker_is_recorded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            task = self.terminal_task(service.store)

            first = service._ensure_terminal_marker(task, 100.0)
            current = service.store.find_task(task.id)
            second = service._ensure_terminal_marker(current, 200.0)

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertAlmostEqual(float(current.metadata["quality_terminal_since"]), 100.0)

    def test_archive_uses_terminal_marker_not_recent_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp, archive_after_seconds=86400)
            task = self.terminal_task(service.store)
            service.store.patch_metadata(
                task.id,
                {
                    "quality_terminal_since": time.time() - 2 * 86400,
                    "quality_rule_id": "unsafe_path",
                    "quality_issue_codes": ["unsafe_metadata"],
                },
            )
            current = service.store.find_task(task.id)

            self.assertTrue(service._archive_terminal_task(current, time.time(), "run-1"))


class QualityAutoRestoreTests(unittest.TestCase):
    def make_service(self, tmp, submission_store=None):
        library = Path(tmp) / "library"
        library.mkdir(parents=True)
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
        )
        return QualityAutomation(
            TaskStore(Path(tmp) / "tasks.db"),
            config,
            allowed_roots=[library],
            submission_store=submission_store,
        ), library

    def completed_missing_task(self, store, dest_path, **extra_metadata):
        task = store.upsert_task("restore", "", "https://115cdn.com/s/restore?password=1212")
        metadata = {
            "dest_path": str(dest_path),
            "own_share_code": "own",
            "own_share_receive_code": "1212",
            "own_share_file_id": "fid-own",
            "move_status": "moved",
            "emby_status": "confirmed",
        }
        metadata.update(extra_metadata)
        return store.record_event(
            task.id,
            TaskStage.CLEANED,
            TaskStatus.SUCCEEDED,
            "done",
            metadata_patch=metadata,
        )

    def test_completed_missing_destination_auto_restores_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            task = self.completed_missing_task(service.store, destination)
            issue = QualityIssue("missing_dest", "destination directory is missing", str(destination), task.id)

            plan = service._plan([task], [issue], now=100.0)[0]

            self.assertEqual(plan.action, "restore")
            self.assertEqual(plan.target_stage, "moved")

            executed = service.execute_plan(plan, "run-1")
            current = service.store.find_task(task.id)
            self.assertEqual(executed.execution_status, "queued")
            self.assertEqual(current.current_stage, TaskStage.MOVED)
            self.assertEqual(current.status, TaskStatus.PENDING)
            self.assertEqual(current.next_run_at, 0)
            self.assertEqual(current.metadata["retry_stage"], "moved")
            self.assertEqual(current.metadata["retry_from_stage"], "cleaned")
            self.assertTrue(current.metadata.get("quality_repair_queued"))

    def test_missing_destination_without_share_evidence_is_not_auto_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            task = self.completed_missing_task(
                service.store,
                destination,
                own_share_file_id="",
                own_share_code="",
            )
            issue = QualityIssue("missing_dest", "destination directory is missing", str(destination), task.id)

            plan = service._plan([task], [issue], now=100.0)[0]

            self.assertEqual(plan.action, "skip")
            self.assertEqual(plan.reason, "missing_destination")

    def test_invalid_share_and_risk_control_block_auto_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            invalid = self.completed_missing_task(
                service.store,
                library / "invalid",
                share_validation_status="invalid",
            )
            risky = self.completed_missing_task(
                service.store,
                library / "risky",
                p115_risk_controlled=True,
            )
            issues = [
                QualityIssue("missing_dest", "missing", str(destination), invalid.id),
                QualityIssue("missing_dest", "missing", str(destination), risky.id),
            ]

            plans = {plan.task_id: plan for plan in service._plan([invalid, risky], issues, now=100.0)}

            self.assertEqual(plans[invalid.id].action, "skip")
            self.assertEqual(plans[risky.id].action, "skip")

    def test_restore_respects_cooldown_and_attempt_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            cooled = self.completed_missing_task(service.store, destination, quality_next_eligible_at=200.0)
            capped = self.completed_missing_task(service.store, library / "capped", quality_repair_attempts=2)
            issues = [
                QualityIssue("missing_dest", "missing", str(destination), cooled.id),
                QualityIssue("missing_dest", "missing", str(library / "capped"), capped.id),
            ]

            plans = {plan.task_id: plan for plan in service._plan([cooled, capped], issues, now=100.0)}

            self.assertEqual(plans[cooled.id].action, "skip")
            self.assertEqual(plans[cooled.id].reason, "cooldown")
            self.assertEqual(plans[capped.id].action, "skip")

    def test_restore_falls_back_to_submission_move_status(self):
        from bridge import ShareKey, SubmissionStore

        with tempfile.TemporaryDirectory() as tmp:
            submission_store = SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                ShareKey("restore2", "1212"),
                "https://115cdn.com/s/restore2?password=1212",
                "completed",
                title="任务",
            )
            service, library = self.make_service(tmp, submission_store=submission_store)
            destination = library / "movie"
            submission_store.update_move(
                int(row["id"]),
                "moved",
                source_path=str(library / "src"),
                dest_path=str(destination),
                category_final="华语电影",
            )
            task = service.store.upsert_task("restore2", "", "https://115cdn.com/s/restore2?password=1212")
            task = service.store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                submission_id=int(row["id"]),
                metadata_patch={
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "own_share_file_id": "fid-own",
                    "emby_status": "confirmed",
                },
            )
            issue = QualityIssue("missing_dest", "destination directory is missing", str(destination), task.id)

            plan = service._plan([task], [issue], now=100.0)[0]

            self.assertEqual(plan.action, "restore")


class QualityScanPerformanceTests(unittest.TestCase):
    def make_service(self, tmp):
        library = Path(tmp) / "library"
        library.mkdir(parents=True)
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
        )
        return QualityAutomation(TaskStore(Path(tmp) / "tasks.db"), config, allowed_roots=[library]), library

    def healthy_task(self, store, dest, *, share_code="own"):
        task = store.upsert_task(share_code, "", f"https://115cdn.com/s/{share_code}")
        return store.record_event(
            task.id,
            TaskStage.CLEANED,
            TaskStatus.SUCCEEDED,
            "done",
            metadata_patch={
                "dest_path": str(dest),
                "own_share_code": share_code,
                "own_share_receive_code": "1212",
                "own_share_file_id": "fid",
            },
        )

    def test_scan_skip_reason_keeps_recheck_and_skips_terminal_and_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            recheck_task = service.store.upsert_task("recheck", "", "https://115cdn.com/s/recheck")
            recheck_task = service.store.record_event(
                recheck_task.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                "wait",
                error_type="stage_wait_timeout",
                error_summary="等待超时",
                metadata_patch={
                    "retry_stage": "emby_confirmed",
                    "quality_issue_codes": ["missing_strm"],
                    "dest_path": str(destination),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                },
            )
            plain_terminal = service.store.upsert_task("plain", "", "https://115cdn.com/s/plain")
            plain_terminal = service.store.record_event(
                plain_terminal.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                "wait",
            )
            cooled = self.healthy_task(service.store, library / "movie", share_code="cooled")
            service.store.patch_metadata(cooled.id, {"quality_next_eligible_at": 200.0})
            cooled = service.store.find_task(cooled.id)

            now = 100.0
            self.assertIsNone(service._scan_skip_reason(recheck_task, now))
            self.assertEqual(service._scan_skip_reason(plain_terminal, now), "terminal")
            self.assertEqual(service._scan_skip_reason(cooled, now), "cooldown")

    def test_directory_cache_reuses_previous_scan_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            (destination / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.healthy_task(service.store, destination)
            calls: list[list] = []

            def fake_scan(*args, **kwargs):
                calls.append(kwargs.get("tasks") or [])
                return []

            with patch("app.quality_automation.scan_task_quality", side_effect=fake_scan):
                service.run_once("cache-run-1")
                service.run_once("cache-run-2")

            self.assertEqual(len(calls), 2)
            self.assertEqual([task.id for task in calls[0]], [task.id])
            self.assertEqual(calls[1], [])

    def test_directory_cache_rescans_after_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, library = self.make_service(tmp)
            destination = library / "movie"
            destination.mkdir(parents=True)
            strm_file = destination / "movie.strm"
            strm_file.write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            task = self.healthy_task(service.store, destination)
            calls: list[list] = []

            def fake_scan(*args, **kwargs):
                calls.append(kwargs.get("tasks") or [])
                return []

            with patch("app.quality_automation.scan_task_quality", side_effect=fake_scan):
                service.run_once("cache-run-1")
                strm_file.write_text("https://cms/d/direct.mkv", encoding="utf-8")
                service.run_once("cache-run-2")

            self.assertEqual([task.id for task in calls[1]], [task.id])


class QualityShareRevalidateTests(unittest.TestCase):
    def make_service(self, tmp, share_inspector=None):
        library = Path(tmp) / "library"
        library.mkdir(parents=True)
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
        )
        return QualityAutomation(
            TaskStore(Path(tmp) / "tasks.db"),
            config,
            allowed_roots=[library],
            share_inspector=share_inspector,
        ), library

    def invalid_share_task(self, store, **extra_metadata):
        task = store.upsert_task("share", "", "https://115cdn.com/s/share")
        metadata = {
            "own_share_code": "own",
            "own_share_receive_code": "1212",
            "share_validation_status": "invalid",
            "invalid_share_status": "invalid",
        }
        metadata.update(extra_metadata)
        return store.record_event(
            task.id,
            TaskStage.SHARE_VALIDATED,
            TaskStatus.NEEDS_ACTION,
            "自有分享已被 115 判定为不可用",
            error_type="needs_action",
            error_summary="自有分享已被 115 判定为不可用",
            metadata_patch=metadata,
        )

    def test_revalidates_eligible_invalid_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            task = self.invalid_share_task(service.store)

            revalidated = service._auto_revalidate_share(task, 100.0)

            self.assertTrue(revalidated)
            current = service.store.find_task(task.id)
            self.assertEqual(current.current_stage, TaskStage.SHARE_VALIDATED)
            self.assertEqual(current.status, TaskStatus.PENDING)
            self.assertEqual(current.next_run_at, 0)
            self.assertEqual(current.metadata.get("share_validation_status"), "")
            self.assertEqual(current.metadata["quality_share_recheck_count"], 1)
            self.assertGreater(current.metadata["quality_share_recheck_next_at"], 100.0)
            self.assertIn("自动复验", service.store.list_events(task.id)[-1]["message"])

    def test_share_revalidate_respects_cooldown_and_attempt_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            capped = self.invalid_share_task(service.store, quality_share_recheck_count=3)
            cooled = self.invalid_share_task(service.store, quality_share_recheck_next_at=200.0)

            self.assertFalse(service._auto_revalidate_share(capped, 100.0))
            self.assertFalse(service._auto_revalidate_share(cooled, 100.0))

    def test_share_revalidate_ignores_cleaned_source_and_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = self.make_service(tmp)
            cleaned = self.invalid_share_task(service.store, invalid_share_status="invalid_share_cleaned")
            claimed = self.invalid_share_task(service.store)
            service.store.compare_and_set_transition(
                claimed.id,
                TaskStage.SHARE_VALIDATED,
                {TaskStatus.NEEDS_ACTION},
                require_unclaimed=True,
                target_stage=TaskStage.SHARE_VALIDATED,
                target_status=TaskStatus.NEEDS_ACTION,
                target_event_message="claim",
                claim_by="worker-1",
                expected_updated_at=claimed.updated_at,
            )

            self.assertFalse(service._auto_revalidate_share(cleaned, 100.0))
            self.assertFalse(service._auto_revalidate_share(service.store.find_task(claimed.id), 100.0))

    def test_completed_task_stale_invalid_marker_cleared_when_share_valid(self):
        from bridge import ShareKey, SubmissionStore

        with tempfile.TemporaryDirectory() as tmp:
            submission_store = SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                ShareKey("done", "1212"),
                "https://115cdn.com/s/done?password=1212",
                "completed",
                title="任务",
            )
            submission_store.update_self_share(int(row["id"]), share_validation_status="invalid")
            service, library = self.make_service(
                tmp,
                share_inspector=lambda code, pwd: {"share_state": "1", "have_vio_file": False},
            )
            service.submission_store = submission_store
            task = service.store.upsert_task("done", "", "https://115cdn.com/s/done?password=1212")
            task = service.store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                submission_id=int(row["id"]),
                metadata_patch={
                    "dest_path": str(library / "movie"),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "share_validation_status": "valid",
                },
            )

            revalidated = service._auto_revalidate_share(task, 100.0)

            self.assertTrue(revalidated)
            self.assertEqual(
                submission_store.find_by_id(int(row["id"]))["share_validation_status"],
                "valid",
            )
            self.assertEqual(
                service.store.find_task(task.id).metadata["quality_share_recheck_count"],
                1,
            )

    def test_completed_task_marker_kept_when_share_still_invalid(self):
        from bridge import ShareKey, SubmissionStore

        with tempfile.TemporaryDirectory() as tmp:
            submission_store = SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                ShareKey("done2", "1212"),
                "https://115cdn.com/s/done2?password=1212",
                "completed",
                title="任务",
            )
            submission_store.update_self_share(int(row["id"]), share_validation_status="invalid")
            service, library = self.make_service(
                tmp,
                share_inspector=lambda code, pwd: {"share_state": "6", "have_vio_file": True},
            )
            service.submission_store = submission_store
            task = service.store.upsert_task("done2", "", "https://115cdn.com/s/done2?password=1212")
            task = service.store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                submission_id=int(row["id"]),
                metadata_patch={
                    "dest_path": str(library / "movie"),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "share_validation_status": "valid",
                },
            )

            self.assertFalse(service._auto_revalidate_share(task, 100.0))
            self.assertEqual(
                submission_store.find_by_id(int(row["id"]))["share_validation_status"],
                "invalid",
            )


class QualityRetentionTests(unittest.TestCase):
    def make_service(self, tmp, retention_days=1, submission_store=None):
        library = Path(tmp) / "library"
        library.mkdir(parents=True)
        config = Config(
            tg_bot_token="token",
            tg_allowed_chat_id="chat",
            cms_base_url="http://cms",
            cms_username="user",
            cms_password="pass",
            task_db_path=str(Path(tmp) / "tasks.db"),
            quality_auto_enabled=True,
            quality_unfixable_retention_days=retention_days,
        )
        return QualityAutomation(
            TaskStore(Path(tmp) / "tasks.db"),
            config,
            allowed_roots=[library],
            submission_store=submission_store,
        )

    def unfixable_terminal_task(self, store, *, rule_id="terminal_invalid_share", age_seconds=2 * 86400, submission_id=None):
        task = store.upsert_task("unfixable", "", "https://115cdn.com/s/unfixable")
        metadata = {
            "quality_rule_id": rule_id,
            "quality_issue_codes": ["invalid_share"],
            "quality_terminal_since": time.time() - age_seconds,
        }
        return store.record_event(
            task.id,
            TaskStage.NEEDS_ACTION,
            TaskStatus.NEEDS_ACTION,
            "terminal",
            submission_id=submission_id,
            metadata_patch=metadata,
        )

    def test_retires_old_unfixable_terminal_task_with_submission(self):
        from bridge import ShareKey, SubmissionStore

        with tempfile.TemporaryDirectory() as tmp:
            submission_store = SubmissionStore(Path(tmp) / "submissions.db")
            row = submission_store.upsert_submission(
                ShareKey("unfixable", "1212"),
                "https://115cdn.com/s/unfixable?password=1212",
                "completed",
                title="任务",
            )
            service = self.make_service(tmp, submission_store=submission_store)
            task = self.unfixable_terminal_task(service.store, submission_id=int(row["id"]))

            retired = service._retire_unfixable_task(task, time.time(), "run-1")

            self.assertTrue(retired)
            self.assertIsNone(service.store.find_task(task.id))
            self.assertIsNone(submission_store.find_by_id(int(row["id"])))

    def test_keeps_recent_or_recoverable_terminal_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            library = Path(tmp) / "library"
            recent = self.unfixable_terminal_task(service.store, age_seconds=3600)
            recoverable = self.unfixable_terminal_task(service.store, rule_id="missing_strm")
            service.store.patch_metadata(
                recoverable.id,
                {
                    "retry_stage": "emby_confirmed",
                    "dest_path": str(library / "movie"),
                    "own_share_code": "own",
                    "own_share_receive_code": "1212",
                    "quality_issue_codes": ["missing_strm"],
                },
            )
            (library / "movie").mkdir(parents=True)
            (library / "movie" / "movie.strm").write_text("https://cms/s/own_1212_movie.mkv", encoding="utf-8")
            recoverable = service.store.find_task(recoverable.id)
            service.store.patch_metadata(recoverable.id, {"error_type": "stage_wait_timeout", "error_summary": "等待超时"})
            recoverable = service.store.find_task(recoverable.id)

            self.assertFalse(service._retire_unfixable_task(recent, time.time(), "run-1"))
            self.assertFalse(service._retire_unfixable_task(recoverable, time.time(), "run-1"))

    def test_retention_disabled_when_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(tmp, retention_days=0)
            task = self.unfixable_terminal_task(service.store)

            self.assertFalse(service._retire_unfixable_task(task, time.time(), "run-1"))
            self.assertIsNotNone(service.store.find_task(task.id))



if __name__ == "__main__":
    unittest.main()
