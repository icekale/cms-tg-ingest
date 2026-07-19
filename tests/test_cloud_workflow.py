import tempfile
import unittest
from pathlib import Path

import bridge
from app.config import MoveConfig, SelfShareConfig
from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore
from app.workflows.self_share import BridgeSelfShareTaskWorkflow


ED2K = "ed2k://|file|Example.mkv|10|ABCDEF0123456789ABCDEF0123456789|/"
TARGET_CID = "3298928530653445613"


class FakeCloudP115:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.add_calls = []
        self.status_calls = []
        self.receive_calls = []

    def cloud_download_add(self, url, target_cid):
        self.add_calls.append((url, target_cid))
        return {"info_hash": "hash", "task_id": "task-1", "status": "running"}

    def cloud_download_status(self, identity):
        self.status_calls.append(dict(identity))
        return self.statuses.pop(0)

    def receive_share_to_cid(self, *args):
        self.receive_calls.append(args)
        raise AssertionError("cloud input must not use share receive")


class FakeCms:
    def run_auto_organize(self):
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


def make_workflow(p115, store):
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
        cms=FakeCms(),
        telegram=FakeTelegram(),
        chat_id="464100862",
        store=store,
        task_store=None,
        p115=p115,
        self_share_config=config,
        move_config=MoveConfig(source_roots=[], library_roots={}),
        emby=None,
        openai_classifier=None,
        tmdb_resolver=None,
        receive_cid=TARGET_CID,
    )


class CloudWorkflowTests(unittest.TestCase):
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
            workflow = make_workflow(p115, submissions)

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

            self.assertEqual(second.outcome.value, "complete")
            self.assertEqual(len(p115.add_calls), 1)
            self.assertEqual(p115.receive_calls, [])
            self.assertEqual(len(submissions.rows), 1)
            self.assertEqual(second.metadata["submission_id"], 1)
            self.assertEqual(second.metadata["cloud_output_file_id"], "folder-1")

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
