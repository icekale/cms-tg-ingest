import tempfile
import unittest
from pathlib import Path

import bridge
from app.models import TaskStage, TaskStatus
from app.task_runner import StageOutcome
from app.task_store import TaskStore


class FakeCms:
    def __init__(self):
        self.plain_share_down_calls = []
        self.auto_organize_calls = 0
        self.share_sync_calls = []

    def add_share_down(self, share_code, receive_code, *args, **kwargs):
        self.plain_share_down_calls.append((share_code, receive_code, args, kwargs))

    def run_auto_organize(self):
        self.auto_organize_calls += 1

    def add_share115_sync_task(self, own_code, own_pwd, cid, local_path):
        self.share_sync_calls.append((own_code, own_pwd, cid, local_path))


class FakeP115:
    def __init__(self):
        self.received = []
        self.folder = None
        self.created_shares = []

    def receive_share_to_cid(self, share_code, receive_code, receive_cid):
        self.received.append((share_code, receive_code, receive_cid))
        return {"title": "received title", "file_ids": ["file-a", "file-b"]}

    def find_organized_folder(self, recognition, title, excluded_parent_ids=None, min_update_time=0):
        return self.folder

    def create_long_share(self, file_id):
        self.created_shares.append(file_id)
        return {
            "share_code": "owncode",
            "receive_code": "ownpwd",
            "share_url": "https://115.com/s/owncode?password=ownpwd",
        }


class FakeTelegram:
    pass


class FakeClassifier:
    enabled = True
    high_confidence = 0.75
    suggest_confidence = 0.45

    def __init__(self):
        self.calls = []

    def classify_media(self, recognition, share_name):
        self.calls.append((dict(recognition), share_name))
        return {
            "category": "外国电视",
            "confidence": 0.92,
            "media_type": "tv",
            "title": "Fallback Show",
            "tmdb_id": "654321",
            "reason": "fake high confidence",
        }


class FakeTmdbResolver:
    enabled = True

    def __init__(self):
        self.lookups = []
        self.searches = []

    def lookup(self, tmdb_id, media_type, share_name):
        self.lookups.append((tmdb_id, media_type, share_name))
        return {"ok": False}

    def search(self, query, media_type):
        self.searches.append((query, media_type))
        return {"ok": False}


class BridgeSelfShareTaskWorkflowTests(unittest.TestCase):
    def _workflow(self, root, receive_cid="pending-cid", openai_classifier=None, tmdb_resolver=None):
        self.cms = FakeCms()
        self.p115 = FakeP115()
        self.submissions = bridge.SubmissionStore(Path(root) / "submissions.db")
        self.tasks = TaskStore(Path(root) / "tasks.db")
        self.config = bridge.SelfShareConfig(
            enabled=True,
            cms_cid="0",
            cms_local_path="/media/share",
            parent_cid_category_map={"movie-parent": "华语电影"},
            auto_organize_retry_seconds=30,
        )
        return bridge.BridgeSelfShareTaskWorkflow(
            self.cms,
            FakeTelegram(),
            "chat-id",
            self.submissions,
            self.tasks,
            self.p115,
            self.config,
            bridge.MoveConfig(source_roots=[], library_roots={}),
            None,
            openai_classifier,
            tmdb_resolver,
            receive_cid=receive_cid,
        )

    def _claim_task(self, share_code, receive_code, stage, metadata=None, submission_id=None):
        task = self.tasks.upsert_task(share_code, receive_code, f"https://115cdn.com/s/{share_code}?password={receive_code}")
        if metadata or submission_id is not None:
            task = self.tasks.record_event(
                task.id,
                stage,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=submission_id,
                metadata_patch=metadata,
            )
        self.tasks.enqueue_task(task.id, stage, next_run_at=1.0)
        claimed = self.tasks.claim_next_runnable("worker", now=1.0)
        self.assertIsNotNone(claimed)
        return claimed

    def _row(self, share_code="abc", receive_code="1234"):
        return self.submissions.upsert_submission(
            bridge.ShareKey(share_code, receive_code),
            f"https://115cdn.com/s/{share_code}?password={receive_code}",
            "received",
            title="received title",
        )

    def test_received_stage_receives_share_and_creates_submission_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED)

            result = workflow.run_stage(task)
            row = self.submissions.find_by_key(bridge.ShareKey("abc", "1234"))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [("abc", "1234", "pending-cid")])
            self.assertEqual(self.cms.plain_share_down_calls, [])
            self.assertEqual(row["workflow_mode"], "self_share_sync")
            self.assertEqual(result.metadata["submission_id"], row["id"])

    def test_organizing_stage_defers_when_folder_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(self.cms.auto_organize_calls, 1)
            self.assertIn("等待 CMS 整理", result.message)

    def test_recognizing_stage_uses_cms_parent_category_before_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            organized_folder = {
                "file_id": "folder-id",
                "file_name": "S-双喜-2025-[tmdb=123456]",
                "parent_id": "movie-parent",
            }
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {"submission_id": row["id"], "organized_folder": organized_folder},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "华语电影")
            self.assertEqual(result.metadata["tmdb_id"], "123456")

    def test_recognizing_stage_uses_openai_tmdb_fallback_for_unmapped_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            classifier = FakeClassifier()
            tmdb = FakeTmdbResolver()
            workflow = self._workflow(tmp, openai_classifier=classifier, tmdb_resolver=tmdb)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="Fallback.Show.S01.2025") or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            organized_folder = {
                "file_id": "folder-id",
                "file_name": "Fallback.Show.S01.2025",
                "parent_id": "unmapped-parent",
            }
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {"submission_id": row["id"], "organized_folder": organized_folder},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "外国电视")
            self.assertEqual(result.metadata["tmdb_id"], "654321")
            self.assertEqual(len(classifier.calls), 1)
            self.assertNotEqual(tmdb.searches, [])

    def test_recognizing_stage_mapped_parent_category_skips_openai(self):
        with tempfile.TemporaryDirectory() as tmp:
            classifier = FakeClassifier()
            workflow = self._workflow(tmp, openai_classifier=classifier, tmdb_resolver=FakeTmdbResolver())
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            organized_folder = {
                "file_id": "folder-id",
                "file_name": "S-双喜-2025-[tmdb=123456]",
                "parent_id": "movie-parent",
            }
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {"submission_id": row["id"], "organized_folder": organized_folder},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "华语电影")
            self.assertEqual(classifier.calls, [])

    def test_recognizing_stage_uses_parent_id_from_recognition_metadata_when_folder_metadata_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            classifier = FakeClassifier()
            workflow = self._workflow(tmp, openai_classifier=classifier)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            self.submissions.update_recognition(
                int(row["id"]),
                {"organized_parent_id": "movie-parent", "share_name": "S-双喜-2025-[tmdb=123456]"},
                "organized_found",
            )
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "华语电影")
            self.assertEqual(classifier.calls, [])

    def test_own_share_stage_creates_share_and_share_sync_stage_submits_cms_share_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            own_share_task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"]},
                row["id"],
            )

            own_share_result = workflow.run_stage(own_share_task)
            share_sync_task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_SYNC_SUBMITTED,
                {"submission_id": row["id"], **own_share_result.metadata},
                row["id"],
            )
            share_sync_result = workflow.run_stage(share_sync_task)

            self.assertEqual(own_share_result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(share_sync_result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.created_shares, ["folder-id"])
            self.assertEqual(self.cms.share_sync_calls, [("owncode", "ownpwd", "0", "/media/share")])
            self.assertEqual(self.cms.plain_share_down_calls, [])


if __name__ == "__main__":
    unittest.main()
