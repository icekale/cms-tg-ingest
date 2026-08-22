import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bridge
from app.config import MoveConfig, SelfShareConfig
from app.models import TaskStage, TaskStatus
from app.strm_mode import effective_task_strm_mode
from app.task_runner import TaskRunner
from app.task_store import TaskStore
from app.workflows.direct import ModeRoutingWorkflow
from app.workflows.self_share import BridgeSelfShareTaskWorkflow


ED2K = "ed2k://|file|Example.mkv|10|" + "ABCDEF0123456789" + "ABCDEF0123456789|/"
TARGET_CID = "3298928530653445613"


class FakeCloudP115:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.add_calls = []
        self.status_calls = []
        self.receive_calls = []
        self.discover_calls = []
        self.ensure_calls = []

    def cloud_download_add(self, url, target_cid):
        self.add_calls.append((url, target_cid))
        return {"info_hash": "hash", "task_id": "task-1", "status": "running"}

    def cloud_download_status(self, identity):
        self.status_calls.append(dict(identity))
        return self.statuses.pop(0)

    def discover_cloud_download_outputs(self, status):
        self.discover_calls.append(dict(status))
        if status.get("output_items"):
            return [dict(item) for item in status["output_items"]]
        return [
            {
                "file_id": status["file_id"],
                "file_name": status.get("file_name") or "Example",
                "parent_id": status.get("parent_id") or TARGET_CID,
                "is_folder": bool(status.get("is_folder")),
            }
        ]

    def ensure_cloud_outputs_in_target(self, items, target_cid):
        self.ensure_calls.append((list(items), target_cid))
        return [{**item, "parent_id": target_cid} for item in items]

    def receive_share_to_cid(self, *args):
        self.receive_calls.append(args)
        raise AssertionError("cloud input must not use share receive")


class FakeCms:
    def __init__(self, fail_auto_organize=False):
        self.auto_organize_calls = 0
        self.fail_auto_organize = fail_auto_organize

    def run_auto_organize(self):
        self.auto_organize_calls += 1
        if self.fail_auto_organize:
            raise RuntimeError("CMS auto organize temporarily unavailable")
        return {"code": 200}


class FakeTelegram:
    def send_message(self, *args, **kwargs):
        return None

    def send_rich_message(self, chat_id, document, reply_markup=None):
        self.messages = getattr(self, "messages", [])
        self.messages.append((chat_id, document.to_plain(), reply_markup))


class FakeSubmissionStore:
    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def upsert_submission(self, key, url, status, title=None, **kwargs):
        row = self.rows.get((key.share_code, key.receive_code))
        if row is None:
            row = {
                "id": self.next_id,
                "share_code": key.share_code,
                "receive_code": key.receive_code,
                "url": url,
                "status": status,
                "title": title or "",
            }
            self.next_id += 1
        else:
            row.update({"status": status, "title": title or row.get("title", "")})
        self.rows[(key.share_code, key.receive_code)] = row
        return dict(row)

    def update_self_share(self, row_id, **changes):
        for key, row in self.rows.items():
            if row["id"] == row_id:
                row.update(changes)
                return dict(row)
        return None

    def find_by_id(self, row_id):
        for row in self.rows.values():
            if row["id"] == row_id:
                return dict(row)
        return None

    def find_by_key(self, key):
        row = self.rows.get((key.share_code, key.receive_code))
        return dict(row) if row else None


class PipelineP115(FakeCloudP115):
    def __init__(self):
        super().__init__([])
        self.folder = {
            "file_id": "organized-folder",
            "file_name": "Example Movie (2020) [tmdb=123]",
            "parent_id": "movie-parent",
            "category": "华语电影",
        }
        self.created_shares = []
        self.own_share = None
        self.deleted = []
        self.renamed = []

    def cloud_download_status(self, identity):
        self.status_calls.append(dict(identity))
        return {
            "status": 11,
            "file_id": "cloud-folder",
            "parent_id": TARGET_CID,
            "file_name": "Example.mkv",
        }

    def find_organized_folder(self, recognition, title, **kwargs):
        return dict(self.folder)

    def rename_file(self, file_id, file_name):
        self.renamed.append((str(file_id), str(file_name)))
        return {"state": True}

    def create_share(self, file_id):
        self.created_shares.append(str(file_id))
        self.own_share = {
            "share_code": "owncode",
            "receive_code": "ownpwd",
            "share_url": "https://115.com/s/owncode?password=ownpwd",
            "share_title": self.folder["file_name"],
        }
        return dict(self.own_share)

    def ensure_share_settings(self, share_code, receive_code):
        if not self.own_share or share_code != self.own_share["share_code"]:
            raise AssertionError("share settings require the created share")
        return {
            "share_code": share_code,
            "receive_code": self.own_share["receive_code"],
        }

    def find_own_share_by_title(self, title, min_create_time=0):
        if not self.own_share or title != self.own_share["share_title"]:
            return None
        return dict(self.own_share)

    def create_long_share(self, file_id, preferred_receive_code=""):
        created = self.create_share(file_id)
        settings = self.ensure_share_settings(
            created["share_code"],
            preferred_receive_code or created["receive_code"],
        )
        return {**created, **settings}

    def inspect_share(self, share_code, receive_code):
        return {"available": True, "share_state": "0", "have_vio_file": False}

    def delete_file(self, file_id):
        self.deleted.append(str(file_id))
        return {"state": True}


class FaultInjectingPipelineP115(PipelineP115):
    def __init__(self):
        super().__init__()
        self.output_items = [
            {
                "file_id": "cloud-folder",
                "file_name": "Example Movie",
                "parent_id": TARGET_CID,
                "is_folder": True,
            },
            {
                "file_id": "bonus-folder",
                "file_name": "Example Movie Extras",
                "parent_id": TARGET_CID,
                "is_folder": True,
            },
        ]
        self.target_ids = set()
        self.movement_calls = 0
        self.fail_after_first_move = True

    def discover_cloud_download_outputs(self, status):
        return [dict(item) for item in self.output_items]

    def ensure_cloud_outputs_in_target(self, items, target_cid):
        self.movement_calls += 1
        normalized = []
        for item in items:
            file_id = str(item["file_id"])
            if file_id not in self.target_ids:
                self.target_ids.add(file_id)
                if self.fail_after_first_move and len(self.target_ids) == 1:
                    self.fail_after_first_move = False
                    raise RuntimeError("simulated process interruption after first cloud move")
            normalized.append({**item, "parent_id": target_cid})
        return normalized


class PipelineCms(FakeCms):
    def __init__(self, source_root):
        super().__init__()
        self.source_root = Path(source_root)
        self.alias_name = ""
        self.share_sync_calls = []
        self.plain_share_down_calls = []

    def add_share115_sync_task(self, own_code, own_pwd, cid, local_path):
        self.share_sync_calls.append((own_code, own_pwd, cid, local_path))
        self.assert_source_folder = self.source_root / self.alias_name
        self.assert_source_folder.mkdir(parents=True, exist_ok=True)
        (self.assert_source_folder / "Example Movie.strm").write_text(
            f"https://115.com/s/{own_code}_{own_pwd}_Example.mkv",
            encoding="utf-8",
        )


class PipelineEmby:
    enabled = True

    def __init__(self):
        self.item = None
        self.refreshed = []

    def refresh_library_for_path(self, path):
        self.refreshed.append(str(path))
        return "电影库"

    def find_item_by_tmdb(self, tmdb_id):
        return self.item

    def recent_items(self, limit=30):
        return [self.item] if self.item else []

    def library_name_for_item(self, item):
        return item.get("LibraryName")


def make_workflow(p115, store, task_store=None, cms=None, cms_cloud_index=None):
    config = SelfShareConfig(
        enabled=True,
        strm_root=Path(tempfile.gettempdir()) / "cms-tg-ingest-cloud-test",
        cms_local_path="/media/share",
        cms_cid="0",
        auto_organize_retry_seconds=30,
    )
    config.cloud_poll_seconds = 30
    config.cloud_timeout_seconds = 3600
    return BridgeSelfShareTaskWorkflow(
        cms=cms or FakeCms(),
        telegram=FakeTelegram(),
        chat_id="464100862",
        store=store,
        task_store=task_store,
        p115=p115,
        self_share_config=config,
        move_config=MoveConfig(source_roots=[], library_roots={}),
        emby=None,
        openai_classifier=None,
        tmdb_resolver=None,
        receive_cid=TARGET_CID,
        cms_cloud_index=cms_cloud_index,
    )


class CloudWorkflowTests(unittest.TestCase):
    def test_cloud_submit_recovers_remote_task_after_local_result_write_failure(self):
        class RecoveringCloudP115(FakeCloudP115):
            def __init__(self):
                super().__init__([])
                self.recovery_calls = 0

            def find_cloud_download_by_source(self, url):
                self.recovery_calls += 1
                if not self.add_calls:
                    return {}
                return {"info_hash": "hash", "task_id": "task-1", "status": "running"}

        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            p115 = RecoveringCloudP115()
            workflow = make_workflow(p115, FakeSubmissionStore(), task_store=task_store)
            complete_operation = task_store.complete_operation
            failed = False

            def fail_first_result_write(*args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("simulated cloud result persistence failure")
                return complete_operation(*args, **kwargs)

            with patch.object(task_store, "complete_operation", side_effect=fail_first_result_write):
                with self.assertRaisesRegex(RuntimeError, "persistence failure"):
                    workflow.run_stage(task)
                recovered = workflow.run_stage(task)

            self.assertEqual(recovered.outcome.value, "defer")
            self.assertEqual(p115.add_calls, [(ED2K, TARGET_CID)])
            self.assertEqual(p115.recovery_calls, 1)
            self.assertEqual(recovered.metadata["cloud_task_id"], "task-1")

    def test_auto_organize_uncertain_result_requires_action_without_retrigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            submissions = FakeSubmissionStore()
            row = submissions.upsert_submission(
                bridge.ShareKey(task.share_code, task.receive_code),
                task.url,
                "received",
                title="Example.mkv",
            )
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                "等待 CMS 整理",
                metadata_patch={
                    "submission_id": row["id"],
                    "cloud_started_at": time.time(),
                    "cloud_output_file_id": "folder-1",
                    "cloud_output_items": [
                        {
                            "file_id": "folder-1",
                            "file_name": "Example.mkv",
                            "parent_id": TARGET_CID,
                            "is_folder": True,
                        }
                    ],
                    "auto_organize_pending": True,
                },
            )
            cms = FakeCms()
            workflow = make_workflow(
                FakeCloudP115([]),
                submissions,
                task_store=task_store,
                cms=cms,
            )
            complete_operation = task_store.complete_operation
            failed = False

            def fail_first_result_write(*args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("simulated organize result persistence failure")
                return complete_operation(*args, **kwargs)

            with patch.object(task_store, "complete_operation", side_effect=fail_first_result_write):
                with self.assertRaisesRegex(RuntimeError, "persistence failure"):
                    workflow.run_stage(task)
                recovered = workflow.run_stage(task)

            self.assertEqual(recovered.outcome.value, "needs_action")
            self.assertEqual(recovered.error_type, "cloud_auto_organize_uncertain")
            self.assertEqual(cms.auto_organize_calls, 1)
            self.assertTrue(recovered.metadata["auto_organize_pending"])

    def test_auto_organize_retry_recovers_when_attempt_metadata_was_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            submissions = FakeSubmissionStore()
            row = submissions.upsert_submission(
                bridge.ShareKey(task.share_code, task.receive_code),
                task.url,
                "received",
                title="Example.mkv",
            )
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                "等待 CMS 整理",
                metadata_patch={
                    "submission_id": row["id"],
                    "cloud_started_at": time.time(),
                    "cloud_output_file_id": "folder-1",
                    "cloud_output_items": [
                        {
                            "file_id": "folder-1",
                            "file_name": "Example.mkv",
                            "parent_id": TARGET_CID,
                            "is_folder": True,
                        }
                    ],
                    "auto_organize_pending": True,
                },
            )
            cms = FakeCms(fail_auto_organize=True)
            workflow = make_workflow(
                FakeCloudP115([]),
                submissions,
                task_store=task_store,
                cms=cms,
            )

            failed = workflow.run_stage(task)
            cms.fail_auto_organize = False
            recovered = workflow.run_stage(task)

            self.assertEqual(failed.outcome.value, "defer")
            self.assertEqual(recovered.outcome.value, "complete")
            self.assertEqual(cms.auto_organize_calls, 2)

    def test_cloud_output_items_persist_before_multi_item_movement(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            p115 = FakeCloudP115(
                [
                    {
                        "status": 11,
                        "file_id": "cloud-folder",
                        "parent_id": TARGET_CID,
                        "file_name": "Example",
                        "output_items": [
                            {
                                "file_id": "video",
                                "file_name": "Example.mkv",
                                "parent_id": "cloud-folder",
                                "is_folder": False,
                            },
                            {
                                "file_id": "subtitle",
                                "file_name": "Example.zh.srt",
                                "parent_id": "cloud-folder",
                                "is_folder": False,
                            },
                        ],
                    }
                ]
            )
            workflow = make_workflow(p115, FakeSubmissionStore(), task_store=task_store)
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                "等待云下载",
                metadata_patch={
                    "cloud_info_hash": "hash",
                    "cloud_task_id": "task-1",
                    "cloud_started_at": time.time(),
                },
            )

            discovered = workflow.run_stage(task)

            self.assertEqual(discovered.outcome.value, "defer")
            self.assertEqual(len(discovered.metadata["cloud_output_items"]), 2)
            self.assertEqual(p115.ensure_calls, [])

            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                discovered.message,
                metadata_patch=discovered.metadata,
            )
            moved = workflow.run_stage(task)

            self.assertEqual(moved.outcome.value, "complete")
            self.assertEqual(len(p115.ensure_calls), 1)
            self.assertEqual(moved.metadata["received_file_ids"], ["video", "subtitle"])
            self.assertEqual(moved.metadata["received_expected_item_count"], 2)
            self.assertEqual(
                [item["is_folder"] for item in moved.metadata["received_items"]],
                [False, False],
            )

    def test_cloud_tasks_route_to_shared_workflow_even_with_direct_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("btih:abc", "magnet:?xt=urn:btih:abc")
            calls = []
            shared = SimpleNamespace(run_stage=lambda received: calls.append(received))
            workflow = ModeRoutingWorkflow(direct=object(), shared=shared, default_mode="direct")

            self.assertEqual(effective_task_strm_mode(task), "shared")
            workflow.run_stage(task)

            self.assertEqual([received.current_stage for received in calls], [TaskStage.CLOUD_DOWNLOADING])

    def test_existing_cloud_job_keeps_original_target_after_receive_cid_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task_store.set_self_share_receive_cid_override("3481694068122059860")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                "等待云下载",
                metadata_patch={
                    "cloud_info_hash": "hash",
                    "cloud_task_id": "task-1",
                    "cloud_started_at": time.time(),
                    "cloud_target_cid": TARGET_CID,
                },
            )
            p115 = FakeCloudP115(
                [
                    {
                        "status": 11,
                        "file_id": "folder-1",
                        "parent_id": TARGET_CID,
                        "file_name": "Example",
                    },
                ]
            )
            submissions = FakeSubmissionStore()
            workflow = make_workflow(p115, submissions, task_store=task_store)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome.value, "defer")
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                result.message,
                metadata_patch=result.metadata,
            )
            result = workflow.run_stage(task)

            self.assertEqual(result.outcome.value, "complete")
            self.assertEqual(p115.status_calls, [{"info_hash": "hash", "task_id": "task-1"}])
            self.assertEqual(result.metadata["cloud_output_parent_id"], TARGET_CID)

    def test_cloud_input_is_submitted_once_then_creates_submission_without_receiving(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            p115 = FakeCloudP115(
                [
                    {
                        "status": 11,
                        "file_id": "folder-1",
                        "parent_id": TARGET_CID,
                        "file_name": "Example",
                    },
                ]
            )
            submissions = FakeSubmissionStore()
            cms = FakeCms()
            workflow = make_workflow(p115, submissions, cms=cms)

            first = workflow.run_stage(task)
            self.assertEqual(first.outcome.value, "defer")
            self.assertEqual(len(p115.add_calls), 1)
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                first.message,
                metadata_patch=first.metadata,
            )

            second = workflow.run_stage(task)

            self.assertEqual(second.outcome.value, "defer")
            self.assertEqual(len(p115.add_calls), 1)
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                second.message,
                metadata_patch=second.metadata,
            )
            third = workflow.run_stage(task)

            self.assertEqual(third.outcome.value, "complete")
            self.assertEqual(len(p115.add_calls), 1)
            self.assertEqual(p115.receive_calls, [])
            self.assertEqual(len(submissions.rows), 1)
            self.assertEqual(third.metadata["submission_id"], 1)
            self.assertEqual(third.metadata["cloud_output_file_id"], "folder-1")
            self.assertEqual(cms.auto_organize_calls, 1)
            self.assertEqual(next(iter(submissions.rows.values()))["workflow_phase"], "auto_organize_submitted")

    def test_cloud_stage_defers_cms_failure_without_resubmitting_cloud_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            p115 = FakeCloudP115(
                [
                    {
                        "status": 11,
                        "file_id": "folder-1",
                        "parent_id": TARGET_CID,
                        "file_name": "Example",
                    },
                ]
            )
            submissions = FakeSubmissionStore()
            cms = FakeCms(fail_auto_organize=True)
            workflow = make_workflow(p115, submissions, cms=cms)

            first = workflow.run_stage(task)
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                first.message,
                metadata_patch=first.metadata,
            )

            second = workflow.run_stage(task)
            self.assertEqual(second.outcome.value, "defer")
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                second.message,
                metadata_patch=second.metadata,
            )

            try:
                second = workflow.run_stage(task)
            except Exception as exc:
                self.fail(f"CMS failure should defer instead of raising: {exc}")

            self.assertEqual(second.outcome.value, "defer")
            self.assertIn("CMS", second.message)
            self.assertEqual(len(p115.add_calls), 1)
            self.assertEqual(cms.auto_organize_calls, 1)
            self.assertEqual(second.metadata["cloud_output_file_id"], "folder-1")

            cms.fail_auto_organize = False
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                second.message,
                metadata_patch=second.metadata,
            )
            third = workflow.run_stage(task)

            self.assertEqual(third.outcome.value, "complete")
            self.assertEqual(len(p115.add_calls), 1)
            self.assertEqual(len(p115.status_calls), 1)
            self.assertEqual(cms.auto_organize_calls, 2)
            self.assertFalse(third.metadata["auto_organize_pending"])

    def test_cloud_auto_organize_timeout_needs_action_without_new_cloud_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                "等待 CMS 整理",
                metadata_patch={
                    "cloud_info_hash": "hash",
                    "cloud_task_id": "task-1",
                    "cloud_started_at": 100,
                    "cloud_timeout_seconds": 300,
                    "auto_organize_pending": True,
                    "cloud_output_items": [
                        {
                            "file_id": "folder-1",
                            "file_name": "Example",
                            "parent_id": TARGET_CID,
                            "is_folder": True,
                        }
                    ],
                    "cloud_output_file_id": "folder-1",
                    "cloud_output_parent_id": TARGET_CID,
                },
            )
            p115 = FakeCloudP115([])
            submissions = FakeSubmissionStore()
            cms = FakeCms(fail_auto_organize=True)
            workflow = make_workflow(p115, submissions, task_store=task_store, cms=cms)
            workflow._now = lambda: 401

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome.value, "needs_action")
            self.assertEqual(result.error_type, "cloud_auto_organize_timeout")
            self.assertEqual(p115.add_calls, [])
            self.assertEqual(p115.status_calls, [])
            self.assertEqual(cms.auto_organize_calls, 0)
            self.assertEqual(result.metadata["cloud_output_file_id"], "folder-1")
            self.assertEqual(result.metadata["cloud_output_items"][0]["parent_id"], TARGET_CID)

    def test_cloud_timeout_fails_before_any_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            p115 = FakeCloudP115([])
            submissions = FakeSubmissionStore()
            workflow = make_workflow(p115, submissions)
            task = task_store.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                "等待云下载",
                metadata_patch={
                    "cloud_info_hash": "hash",
                    "cloud_task_id": "task-1",
                    "cloud_started_at": 1,
                },
            )
            workflow._now = lambda: 4000
            result = workflow.run_stage(task)

            self.assertEqual(result.outcome.value, "failed")
            self.assertEqual(result.error_type, "cloud_download_timeout")
            self.assertEqual(submissions.rows, {})

    def test_cloud_source_completes_authoritative_self_share_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            library_root = root / "library"
            share_root.mkdir()
            task_store = TaskStore(root / "tasks.db")
            submissions = bridge.SubmissionStore(root / "submissions.db")
            p115 = PipelineP115()
            cms = PipelineCms(share_root)
            emby = PipelineEmby()
            cleanup = p115
            config = SelfShareConfig(
                enabled=True,
                strm_root=share_root,
                cms_local_path="/media/share",
                cms_cid="0",
                excluded_parent_ids=set(),
                cleanup_after_emby=True,
                parent_cid_category_map={"movie-parent": "华语电影"},
                cloud_poll_seconds=30,
                cloud_timeout_seconds=3600,
                auto_organize_retry_seconds=30,
                review_grace_seconds=1,
                review_checkpoints_seconds=(1,),
            )
            move_config = MoveConfig(
                source_roots=[share_root],
                library_roots={"华语电影": library_root},
                conflict_policy="merge",
                stable_seconds=0,
            )
            workflow = BridgeSelfShareTaskWorkflow(
                cms=cms,
                telegram=FakeTelegram(),
                chat_id="464100862",
                store=submissions,
                task_store=task_store,
                p115=p115,
                self_share_config=config,
                move_config=move_config,
                emby=emby,
                openai_classifier=None,
                tmdb_resolver=None,
                cleanup_client=cleanup,
                receive_cid=TARGET_CID,
            )
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            task_store.enqueue_task(task.id, TaskStage.CLOUD_DOWNLOADING, next_run_at=0)
            clock = [time.time()]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(
                task_store,
                workflow,
                worker_id="cloud-pipeline-test",
                interval_seconds=1,
                now=lambda: clock[0],
            )

            for _ in range(30):
                current = task_store.find_task(task.id)
                if current.current_stage == TaskStage.SHARE_SYNC_SUBMITTED and not cms.alias_name:
                    row = submissions.find_by_id(current.metadata["submission_id"])
                    cms.alias_name = row["own_share_file_name"]
                runner.run_once()
                current = task_store.find_task(task.id)
                self.assertIsNotNone(current)
                if current.current_stage == TaskStage.EMBY_CONFIRMED and current.status == TaskStatus.PENDING:
                    row = submissions.find_by_id(current.metadata["submission_id"])
                    emby.item = {
                        "Id": "emby-123",
                        "Name": "Example Movie",
                        "Path": row["dest_path"],
                        "ProviderIds": {"Tmdb": "123"},
                        "LibraryName": "电影库",
                    }
                if current.status == TaskStatus.SUCCEEDED and current.current_stage == TaskStage.CLEANED:
                    break
                clock[0] = max(clock[0] + 0.1, float(current.next_run_at or clock[0]) + 0.1)

            final = task_store.find_task(task.id)
            row = submissions.find_by_id(final.metadata["submission_id"])
            self.assertEqual(final.current_stage, TaskStage.CLEANED)
            self.assertEqual(final.status, TaskStatus.SUCCEEDED)
            self.assertEqual(cms.plain_share_down_calls, [])
            self.assertEqual(len(cms.share_sync_calls), 1)
            self.assertEqual(p115.renamed, [])
            self.assertEqual(p115.created_shares, ["organized-folder"])
            self.assertEqual(p115.deleted, ["organized-folder"])
            self.assertEqual(row["cleanup_status"], "deleted")
            self.assertEqual(row["emby_parent"], "电影库")
            self.assertTrue(Path(row["dest_path"]).is_dir())
            self.assertIn("/s/owncode_ownpwd_", next(Path(row["dest_path"]).glob("*.strm")).read_text(encoding="utf-8"))
            self.assertEqual(len(emby.refreshed), 1)

    def test_cloud_fault_injection_recovers_without_duplicate_submission_or_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            library_root = root / "library"
            share_root.mkdir()
            task_store = TaskStore(root / "tasks.db")
            submissions = bridge.SubmissionStore(root / "submissions.db")
            p115 = FaultInjectingPipelineP115()
            cms = PipelineCms(share_root)
            emby = PipelineEmby()
            config = SelfShareConfig(
                enabled=True,
                strm_root=share_root,
                cms_local_path="/media/share",
                cms_cid="0",
                cleanup_after_emby=True,
                parent_cid_category_map={"movie-parent": "华语电影"},
                cloud_poll_seconds=30,
                cloud_timeout_seconds=3600,
                auto_organize_retry_seconds=30,
                review_grace_seconds=1,
                review_checkpoints_seconds=(1,),
            )
            move_config = MoveConfig(
                source_roots=[share_root],
                library_roots={"华语电影": library_root},
                conflict_policy="merge",
                stable_seconds=0,
            )
            workflow = BridgeSelfShareTaskWorkflow(
                cms=cms,
                telegram=FakeTelegram(),
                chat_id="464100862",
                store=submissions,
                task_store=task_store,
                p115=p115,
                self_share_config=config,
                move_config=move_config,
                emby=emby,
                openai_classifier=None,
                tmdb_resolver=None,
                cleanup_client=p115,
                receive_cid=TARGET_CID,
            )
            task = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="Example.mkv")
            task_store.enqueue_task(task.id, TaskStage.CLOUD_DOWNLOADING, next_run_at=0)

            claimed = task_store.claim_next_runnable("claim-worker", now=0)
            duplicate = task_store.upsert_cloud_task("ed2k:hash:10", ED2K, title="renamed.mkv")
            self.assertEqual(duplicate.id, claimed.id)
            self.assertEqual(duplicate.title, "Example.mkv")
            task_store.enqueue_task(claimed.id, TaskStage.CLOUD_DOWNLOADING, next_run_at=0)

            clock = [time.time()]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(
                task_store,
                workflow,
                worker_id="cloud-fault-test",
                interval_seconds=1,
                now=lambda: clock[0],
            )
            interruption_recovered = False
            for _ in range(60):
                runner.run_once()
                current = task_store.find_task(task.id)
                self.assertIsNotNone(current)
                if current.status == TaskStatus.FAILED and not interruption_recovered:
                    self.assertEqual(p115.target_ids, {"cloud-folder"})
                    task_store.record_event(
                        task.id,
                        TaskStage.CLOUD_DOWNLOADING,
                        TaskStatus.RUNNING,
                        "模拟进程中断后恢复",
                        metadata_patch=current.metadata,
                    )
                    task_store.enqueue_task(task.id, TaskStage.CLOUD_DOWNLOADING, next_run_at=0)
                    interruption_recovered = True
                if current.current_stage == TaskStage.SHARE_SYNC_SUBMITTED and not cms.alias_name:
                    row = submissions.find_by_id(current.metadata["submission_id"])
                    cms.alias_name = row["own_share_file_name"]
                if current.current_stage == TaskStage.EMBY_CONFIRMED and current.status == TaskStatus.PENDING:
                    row = submissions.find_by_id(current.metadata["submission_id"])
                    emby.item = {
                        "Id": "emby-123",
                        "Name": "Example Movie",
                        "Path": row["dest_path"],
                        "ProviderIds": {"Tmdb": "123"},
                        "LibraryName": "电影库",
                    }
                if current.status == TaskStatus.SUCCEEDED and current.current_stage == TaskStage.CLEANED:
                    break
                clock[0] = max(clock[0] + 0.1, float(current.next_run_at or clock[0]) + 0.1)

            final = task_store.find_task(task.id)
            row = submissions.find_by_id(final.metadata["submission_id"])
            self.assertTrue(interruption_recovered)
            self.assertEqual(
                final.current_stage,
                TaskStage.CLEANED,
                msg=f"events={task_store.list_events(task.id)} metadata={final.metadata}",
            )
            self.assertEqual(final.status, TaskStatus.SUCCEEDED)
            self.assertEqual(len(p115.add_calls), 1)
            self.assertEqual(p115.target_ids, {"cloud-folder", "bonus-folder"})
            self.assertEqual(p115.movement_calls, 2)
            self.assertEqual(p115.created_shares, ["organized-folder"])
            self.assertEqual(p115.deleted, ["organized-folder"])
            self.assertEqual(len(submissions.recent(limit=10)), 1)
            self.assertEqual(row["cleanup_status"], "deleted")
            self.assertEqual(row["emby_parent"], "电影库")


class CloudIntakeTests(unittest.TestCase):
    def test_handle_update_enqueues_cloud_source_without_cms_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            submissions = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            tasks = TaskStore(Path(tmp) / "tasks.db")

            bridge.handle_update(
                {
                    "message": {
                        "chat": {"id": 464100862},
                        "from": {"id": 464100862},
                        "text": ED2K,
                    }
                },
                FakeCms(),
                FakeTelegram(),
                "464100862",
                submissions,
                poll_status=False,
                task_store=tasks,
                task_engine_enabled=True,
                self_share_workflow=object(),
                self_share_receive_cid=TARGET_CID,
            )

            found = tasks.list_recent_tasks(limit=1)[0]
            self.assertEqual(found.source_type, "cloud_download")
            self.assertEqual(found.current_stage, TaskStage.CLOUD_DOWNLOADING)
            self.assertEqual(found.status, TaskStatus.PENDING)
            self.assertEqual(submissions.recent(limit=1), [])


if __name__ == "__main__":
    unittest.main()


class PostAutoOrganizeGuardTests(unittest.TestCase):
    def setUp(self):
        from app.workflows import self_share as ss
        self.ss_module = ss
        ss._post_organize_guard_last_scheduled_at = 0.0

    def test_schedule_post_organize_restore_guard_zero_delay_invokes_restore(self):
        from app.workflows.self_share import schedule_post_organize_restore_guard

        called = {}

        def fake_restore(store, cms, self_share_config, move_config, emby=None, limit=50):
            called["store"] = store
            called["cms"] = cms
            called["config"] = self_share_config
            called["move_config"] = move_config
            called["emby"] = emby
            called["limit"] = limit
            return 2

        store = object()
        cms = object()
        config = SelfShareConfig(enabled=True)
        move_config = MoveConfig(source_roots=[], library_roots={})
        emby = object()
        with patch("app.workflows.self_share.restore_missing_self_share_library_folders", side_effect=fake_restore) as restore_mock:
            thread = schedule_post_organize_restore_guard(
                store,
                cms,
                config,
                move_config,
                emby=emby,
                delay_seconds=0,
                limit=10,
            )
            self.assertIsNotNone(thread)
            thread.join(timeout=5)

        restore_mock.assert_called_once()
        self.assertEqual(called["store"], store)
        self.assertEqual(called["cms"], cms)
        self.assertEqual(called["config"], config)
        self.assertEqual(called["move_config"], move_config)
        self.assertEqual(called["emby"], emby)
        self.assertEqual(called["limit"], 10)

    def test_schedule_post_organize_restore_guard_dedupes_within_window(self):
        from app.workflows.self_share import schedule_post_organize_restore_guard

        store = object()
        cms = object()
        config = SelfShareConfig(enabled=True)
        move_config = MoveConfig(source_roots=[], library_roots={})
        with patch("app.workflows.self_share.restore_missing_self_share_library_folders", return_value=0) as restore_mock:
            first = schedule_post_organize_restore_guard(
                store, cms, config, move_config, delay_seconds=3600, limit=10
            )
            second = schedule_post_organize_restore_guard(
                store, cms, config, move_config, delay_seconds=3600, limit=10
            )
            self.assertIsNotNone(first)
            self.assertIsNone(second)
        restore_mock.assert_not_called()
        self.ss_module._post_organize_guard_last_scheduled_at = 0.0

    def test_trigger_cloud_auto_organize_success_schedules_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task = task_store.upsert_cloud_task("ed2k:hash:11", ED2K, title="Example.mkv")
            submissions = FakeSubmissionStore()
            p115 = FakeCloudP115(
                [
                    {
                        "status": 11,
                        "file_id": "folder-1",
                        "parent_id": TARGET_CID,
                        "file_name": "Example",
                    },
                ]
            )
            cms = FakeCms()
            workflow = make_workflow(p115, submissions, cms=cms)

            with patch(
                "app.workflows.self_share.schedule_post_organize_restore_guard",
                return_value=None,
            ) as schedule_mock:
                first = workflow.run_stage(task)
                self.assertEqual(first.outcome.value, "defer")
                task = task_store.record_event(
                    task.id,
                    TaskStage.CLOUD_DOWNLOADING,
                    TaskStatus.RUNNING,
                    first.message,
                    metadata_patch=first.metadata,
                )
                second = workflow.run_stage(task)
                self.assertEqual(second.outcome.value, "defer")
                task = task_store.record_event(
                    task.id,
                    TaskStage.CLOUD_DOWNLOADING,
                    TaskStatus.RUNNING,
                    second.message,
                    metadata_patch=second.metadata,
                )
                third = workflow.run_stage(task)

            self.assertEqual(third.outcome.value, "complete")
            self.assertEqual(cms.auto_organize_calls, 1)
            schedule_mock.assert_called_once()
            kwargs = schedule_mock.call_args.kwargs
            self.assertEqual(kwargs["store"], submissions)
            self.assertEqual(kwargs["cms"], cms)
            self.assertEqual(kwargs["self_share_config"], workflow.self_share_config)
            self.assertEqual(kwargs["move_config"], workflow.move_config)
            self.assertIsNone(kwargs["emby"])
            self.assertGreaterEqual(kwargs["delay_seconds"], 15)
            self.assertLessEqual(kwargs["delay_seconds"], 60)

    def test_stage_organizing_schedules_guard_after_auto_organize(self):
        from types import SimpleNamespace
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow

        submissions = FakeSubmissionStore()
        row = submissions.upsert_submission(
            bridge.ShareKey("swhou1y3nr6", "u148"),
            "https://115cdn.com/s/swhou1y3nr6?password=u148",
            "received",
            title="Example.mkv",
        )
        task = SimpleNamespace(
            id=1,
            share_code="swhou1y3nr6",
            receive_code="u148",
            url="https://115cdn.com/s/swhou1y3nr6?password=u148",
            title="Example.mkv",
            metadata={"submission_id": row["id"], "operation_generation": 0, "update_requested_run": 0},
        )
        p115 = FakeCloudP115([])
        cms = FakeCms()
        workflow = make_workflow(p115, submissions, cms=cms)

        with patch("app.workflows.self_share.schedule_post_organize_restore_guard", return_value=None) as schedule_mock:
            with patch.object(
                workflow,
                "_find_organized_folder",
                return_value=(None, {}, True, 0),
            ):
                result = workflow._stage_organizing(task)

        self.assertEqual(cms.auto_organize_calls, 1)
        self.assertEqual(result.outcome.value, "defer")
        schedule_mock.assert_called_once()
        kwargs = schedule_mock.call_args.kwargs
        self.assertEqual(kwargs["store"], submissions)
        self.assertEqual(kwargs["cms"], cms)
        self.assertEqual(kwargs["self_share_config"], workflow.self_share_config)
        self.assertEqual(kwargs["move_config"], workflow.move_config)
        self.assertIsNone(kwargs["emby"])
        self.assertGreaterEqual(kwargs["delay_seconds"], 15)
        self.assertLessEqual(kwargs["delay_seconds"], 60)

    def _organizing_task(self, row):
        return SimpleNamespace(
            id=1,
            share_code="swhou1y3nr6",
            receive_code="u148",
            url="https://115cdn.com/s/swhou1y3nr6?password=u148",
            title="Example.mkv",
            metadata={
                "submission_id": row["id"],
                "cloud_output_file_id": "cloud-file-1",
                "cloud_output_items": [
                    {
                        "file_id": "cloud-file-1",
                        "file_name": "Example.mkv",
                        "is_folder": False,
                        "parent_id": TARGET_CID,
                    }
                ],
                "received_file_ids": ["cloud-file-1"],
                "operation_generation": 0,
                "update_requested_run": 0,
            },
        )

    def _organizing_submission(self):
        submissions = FakeSubmissionStore()
        return submissions, submissions.upsert_submission(
            bridge.ShareKey("swhou1y3nr6", "u148"),
            "https://115cdn.com/s/swhou1y3nr6?password=u148",
            "auto_organize_submitted",
            title="Example.mkv",
        )

    def test_stage_organizing_waits_when_cloud_index_resolves_another_tasks_folder(self):
        """A folder that does not contain this task's cloud output must not be
        adopted; the stage defers instead of surfacing a false conflict."""
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow

        class RejectingIndex:
            def folder_contains_cloud_output(self, folder, cloud_output_file_ids):
                return False

        submissions, row = self._organizing_submission()
        p115 = FakeCloudP115([])
        workflow = make_workflow(p115, submissions, cms_cloud_index=RejectingIndex())
        folder = {
            "file_id": "other-folder",
            "file_name": "L-雷霆特攻队-2025-[tmdb=986056]",
            "parent_id": "movie-parent",
            "direct_file_id": "other-file",
        }

        with patch.object(
            workflow,
            "_find_organized_folder",
            return_value=(folder, {}, True, 0),
        ):
            result = workflow._stage_organizing(self._organizing_task(row))

        self.assertEqual(result.outcome.value, "defer")
        self.assertIn("等待 CMS 整理完成", result.message)
        self.assertEqual(row.get("own_share_file_id"), None)

    def test_stage_organizing_adopts_folder_containing_own_cloud_output(self):
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow

        class AcceptingIndex:
            def folder_contains_cloud_output(self, folder, cloud_output_file_ids):
                return True

        submissions, row = self._organizing_submission()
        p115 = FakeCloudP115([])
        workflow = make_workflow(p115, submissions, cms_cloud_index=AcceptingIndex())
        folder = {
            "file_id": "my-folder",
            "file_name": "Y-宇宙巨人：希曼崛起-2026-[tmdb=454639]",
            "parent_id": "movie-parent",
            "direct_file_id": "cloud-file-1",
        }

        with patch.object(workflow, "_conflicting_folder_owner", return_value=None):
            with patch.object(
                workflow,
                "_find_organized_folder",
                return_value=(folder, {}, True, 0),
            ):
                result = workflow._stage_organizing(self._organizing_task(row))

        self.assertEqual(result.outcome.value, "complete")
        self.assertEqual(submissions.find_by_id(row["id"])["own_share_file_id"], "my-folder")

    def test_stage_organizing_rejects_searched_folder_without_own_cloud_output(self):
        """A p115-search hit (no direct_file_id) must still be verified against
        the task's own cloud output file ids."""
        from app.workflows.self_share import BridgeSelfShareTaskWorkflow

        class RejectingIndex:
            def folder_contains_cloud_output(self, folder, cloud_output_file_ids):
                return False

        submissions, row = self._organizing_submission()
        p115 = FakeCloudP115([])
        workflow = make_workflow(p115, submissions, cms_cloud_index=RejectingIndex())
        folder = {
            "file_id": "unrelated-folder",
            "file_name": "G-攻壳机动队：崛起4-2014-[tmdb=279254]",
            "parent_id": "movie-parent",
        }

        with patch.object(
            workflow,
            "_find_organized_folder",
            return_value=(folder, {}, True, 0),
        ):
            result = workflow._stage_organizing(self._organizing_task(row))

        self.assertEqual(result.outcome.value, "defer")
        self.assertEqual(row.get("own_share_file_id"), None)
