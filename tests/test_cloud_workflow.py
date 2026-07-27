import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def create_long_share(self, file_id, preferred_receive_code=""):
        self.created_shares.append(str(file_id))
        return {
            "share_code": "owncode",
            "receive_code": "ownpwd",
            "share_url": "https://115.com/s/owncode?password=ownpwd",
        }

    def inspect_share(self, share_code, receive_code):
        return {"available": True, "share_state": "0", "have_vio_file": False}

    def delete_file(self, file_id):
        self.deleted.append(str(file_id))
        return {"state": True}


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


def make_workflow(p115, store, task_store=None, cms=None):
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
    )


class CloudWorkflowTests(unittest.TestCase):
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
