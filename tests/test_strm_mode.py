import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Config
from app.models import TaskStage, TaskStatus, next_stage_after_success
from app.strm_mode import (
    STRM_MODE_LABELS,
    effective_task_strm_mode,
    is_strm_mode_locked,
    next_stage_for_mode,
    normalize_strm_mode,
)
from app.task_bridge import ensure_task_for_link, record_submission_event
from app.task_store import TaskStore


class StrmModeTests(unittest.TestCase):
    def test_normalize_strm_mode_accepts_case_and_rejects_unknown_values(self):
        self.assertEqual(normalize_strm_mode(" DIRECT "), "direct")
        self.assertEqual(normalize_strm_mode(""), "shared")
        self.assertEqual(STRM_MODE_LABELS, {"shared": "共享 STRM", "direct": "直链 STRM"})
        with self.assertRaises(ValueError):
            normalize_strm_mode("self_share_sync")

    def test_effective_mode_prefers_task_metadata_then_legacy_workflow_then_default(self):
        task = SimpleNamespace(metadata={"strm_mode": "DIRECT"})
        self.assertEqual(effective_task_strm_mode(task, legacy_workflow_mode="self_share_sync"), "direct")
        self.assertEqual(
            effective_task_strm_mode(SimpleNamespace(metadata={}), legacy_workflow_mode="self_share_sync"),
            "shared",
        )
        self.assertEqual(
            effective_task_strm_mode(SimpleNamespace(metadata={}), legacy_workflow_mode="direct"),
            "direct",
        )
        self.assertEqual(
            effective_task_strm_mode({"metadata": {}}, default_mode="direct", legacy_workflow_mode="unknown"),
            "direct",
        )

    def test_next_stage_for_mode_keeps_shared_flow_and_uses_direct_flow(self):
        self.assertEqual(next_stage_for_mode(TaskStage.RECOGNIZING, "shared"), TaskStage.SHARE_ALIAS_PREPARED)
        self.assertEqual(next_stage_after_success(TaskStage.RECOGNIZING), TaskStage.SHARE_ALIAS_PREPARED)
        self.assertEqual(next_stage_after_success(TaskStage.RECOGNIZING, "direct"), TaskStage.STRM_READY)
        self.assertEqual(next_stage_for_mode(TaskStage.RECEIVED, "direct"), TaskStage.ORGANIZING)
        self.assertEqual(next_stage_for_mode(TaskStage.CLOUD_DOWNLOADING, "direct"), TaskStage.ORGANIZING)
        self.assertEqual(next_stage_for_mode(TaskStage.ORGANIZING, "direct"), TaskStage.RECOGNIZING)
        self.assertEqual(next_stage_for_mode(TaskStage.RECOGNIZING, "direct"), TaskStage.STRM_READY)
        self.assertEqual(next_stage_for_mode(TaskStage.STRM_READY, "direct"), TaskStage.MOVED)
        self.assertEqual(next_stage_for_mode(TaskStage.MOVED, "direct"), TaskStage.EMBY_CONFIRMED)
        self.assertIsNone(next_stage_for_mode(TaskStage.EMBY_CONFIRMED, "direct"))

    def test_mode_is_locked_from_share_alias_prepared_onward(self):
        locked = (
            TaskStage.SHARE_ALIAS_PREPARED,
            TaskStage.OWN_SHARE_CREATED,
            TaskStage.SHARE_VALIDATED,
            TaskStage.SHARE_SYNC_SUBMITTED,
            TaskStage.STRM_READY,
            TaskStage.CMS_DELETE_SETTLED,
            TaskStage.MOVED,
            TaskStage.EMBY_CONFIRMED,
            TaskStage.CLEANED,
        )
        for stage in locked:
            with self.subTest(stage=stage):
                self.assertTrue(is_strm_mode_locked(stage))
        self.assertFalse(is_strm_mode_locked(TaskStage.RECOGNIZING))


class ConfigStrmModeTests(unittest.TestCase):
    REQUIRED = {
        "TG_BOT_TOKEN": "token",
        "TG_ALLOWED_CHAT_ID": "464100862",
        "CMS_BASE_URL": "http://cms.test",
        "CMS_USERNAME": "user",
        "CMS_PASSWORD": "password",
    }

    def test_strm_default_mode_precedes_legacy_workflow_mode(self):
        env = {**self.REQUIRED, "STRM_DEFAULT_MODE": "DIRECT", "WORKFLOW_MODE": "self_share_sync"}
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.strm_default_mode, "direct")
        self.assertEqual(config.frontend_dist_path, "/app/frontend/dist")

    def test_legacy_workflow_mode_maps_when_strm_default_is_absent(self):
        with patch.dict(os.environ, {**self.REQUIRED, "WORKFLOW_MODE": "self_share_sync"}, clear=True):
            shared = Config.from_env()
        self.assertEqual(shared.strm_default_mode, "shared")

        with patch.dict(os.environ, {**self.REQUIRED, "WORKFLOW_MODE": "direct"}, clear=True):
            direct = Config.from_env()
        self.assertEqual(direct.strm_default_mode, "direct")

    def test_strm_default_mode_is_shared_when_both_environment_variables_are_absent(self):
        with patch.dict(os.environ, self.REQUIRED, clear=True):
            config = Config.from_env()
        self.assertEqual(config.strm_default_mode, "shared")
        self.assertEqual(config.workflow_mode, "direct")


class TaskBridgeStrmModeTests(unittest.TestCase):
    def test_bridge_preserves_explicit_strm_mode_without_guessing_from_url_or_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            direct = ensure_task_for_link(
                store,
                "direct-share",
                "",
                "https://115cdn.com/s/direct-share",
                strm_mode="direct",
            )
            self.assertEqual(direct.metadata["strm_mode"], "direct")

            shared = record_submission_event(
                store,
                {
                    "share_code": "shared-share",
                    "receive_code": "",
                    "url": "https://115cdn.com/s/shared-share",
                    "title": "direct link title",
                },
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "received",
            )
            self.assertEqual(shared.metadata["strm_mode"], "shared")

    def test_record_submission_event_preserves_explicit_strm_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = record_submission_event(
                store,
                {
                    "share_code": "explicit-direct",
                    "receive_code": "",
                    "url": "https://115cdn.com/s/explicit-direct",
                },
                TaskStage.RECEIVED,
                TaskStatus.PENDING,
                "received",
                strm_mode="direct",
            )

            self.assertEqual(task.metadata["strm_mode"], "direct")
            self.assertEqual(store.find_task(task.id).metadata["strm_mode"], "direct")


if __name__ == "__main__":
    unittest.main()
