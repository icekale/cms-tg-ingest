import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import MoveConfig, SelfShareConfig
from app.models import TaskStage
from app.task_runner import StageOutcome
from app.task_store import TaskStore, operation_scope
from app.workflows.direct import SourceShareTaskWorkflow
from bridge import SubmissionStore


class FakeCms:
    def __init__(self):
        self.share_sync_calls = []

    def add_share115_sync_task(self, share_code, receive_code, cid, local_path):
        self.share_sync_calls.append((share_code, receive_code, cid, local_path))
        return {"code": 200}


class FakeTmdbResolver:
    enabled = True

    def lookup(self, tmdb_id, media_type, share_name):
        self.last_lookup = (tmdb_id, media_type, share_name)
        return {
            "ok": True,
            "title": "红龙",
            "type": "movie",
            "tmdb_id": str(tmdb_id),
            "category": "欧美电影",
            "source": "tmdb_api",
        }


class SourceShareTaskWorkflowTests(unittest.TestCase):
    def test_original_share_is_submitted_without_receive_or_own_share_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share_root = root / "share"
            source = share_root / "H-红龙-2002-[tmdb=9533]"
            source.mkdir(parents=True)
            (source / "红龙.strm").write_text(
                "http://cms.test/s/sourcecode_7788_red-dragon.mkv",
                encoding="utf-8",
            )
            task_store = TaskStore(root / "tasks.db")
            submissions = SubmissionStore(root / "submissions.db")
            cms = FakeCms()
            workflow = SourceShareTaskWorkflow(
                cms=cms,
                store=submissions,
                move_config=MoveConfig(source_roots=[], library_roots={"欧美电影": root / "library"}),
                self_share_config=SelfShareConfig(strm_root=share_root, cms_cid="0", cms_local_path="/media/share"),
                task_store=task_store,
                tmdb_resolver=FakeTmdbResolver(),
            )
            task = task_store.upsert_task(
                "sourcecode",
                "7788",
                "https://115cdn.com/s/sourcecode?password=7788",
                strm_mode="source_shared",
            )

            received = workflow.run_stage(task)
            sync_task = replace(
                task,
                current_stage=TaskStage.SHARE_SYNC_SUBMITTED,
                metadata={**task.metadata, **received.metadata},
            )
            submitted = workflow.run_stage(sync_task)
            recognizing_task = replace(
                sync_task,
                current_stage=TaskStage.RECOGNIZING,
                metadata={**sync_task.metadata, **submitted.metadata},
            )
            recognized = workflow.run_stage(recognizing_task)
            strm_task = replace(
                recognizing_task,
                current_stage=TaskStage.STRM_READY,
                metadata={**recognizing_task.metadata, **recognized.metadata},
            )
            strm_ready = workflow.run_stage(strm_task)
            moved_task = replace(
                strm_task,
                current_stage=TaskStage.MOVED,
                metadata={**strm_task.metadata, **strm_ready.metadata},
            )
            moved = workflow.run_stage(moved_task)

            self.assertEqual(received.outcome, StageOutcome.COMPLETE)
            self.assertEqual(submitted.outcome, StageOutcome.COMPLETE)
            self.assertEqual(recognized.outcome, StageOutcome.COMPLETE)
            self.assertEqual(strm_ready.outcome, StageOutcome.COMPLETE)
            self.assertEqual(moved.outcome, StageOutcome.COMPLETE)
            self.assertEqual(recognized.metadata["category"], "欧美电影")
            self.assertEqual(cms.share_sync_calls, [("sourcecode", "7788", "0", "/media/share")])
            row = submissions.find_by_id(received.metadata["submission_id"])
            self.assertEqual(row["own_share_code"], "sourcecode")
            self.assertEqual(row["own_share_receive_code"], "7788")
            self.assertEqual(row["own_share_file_name"], source.name)
            self.assertTrue((root / "library" / source.name / "红龙.strm").is_file())

    def test_share_sync_resumes_journaled_result_after_submission_persistence_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "tasks.db")
            submissions = SubmissionStore(root / "submissions.db")
            cms = FakeCms()
            workflow = SourceShareTaskWorkflow(
                cms=cms,
                store=submissions,
                move_config=MoveConfig(source_roots=[], library_roots={}),
                self_share_config=SelfShareConfig(
                    strm_root=root / "share",
                    cms_cid="0",
                    cms_local_path="/media/share",
                ),
                task_store=task_store,
                tmdb_resolver=FakeTmdbResolver(),
            )
            workflow.task_store = task_store
            task = task_store.upsert_task(
                "sourcecode",
                "7788",
                "https://115cdn.com/s/sourcecode?password=7788",
                strm_mode="source_shared",
            )
            received = workflow.run_stage(task)
            sync_task = replace(
                task,
                current_stage=TaskStage.SHARE_SYNC_SUBMITTED,
                metadata={**task.metadata, **received.metadata},
            )
            operation_key = f"{operation_scope(sync_task)}:cms_source_share_sync:source_shared:{task.share_code}"
            update_self_share = submissions.update_self_share
            save_attempts = 0

            def fail_first_submission_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving source-share submission")
                return update_self_share(*args, **kwargs)

            with patch.object(submissions, "update_self_share", side_effect=fail_first_submission_save):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    workflow.run_stage(sync_task)
                result = workflow.run_stage(sync_task)

            operation = task_store.find_operation(task.id, operation_key)

        self.assertEqual(result.outcome, StageOutcome.COMPLETE)
        self.assertEqual(cms.share_sync_calls, [("sourcecode", "7788", "0", "/media/share")])
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(operation.request["strm_mode"], "source_shared")
        self.assertEqual(operation.result, {"code": 200})

    def test_share_sync_started_operation_requires_action_without_second_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "tasks.db")
            submissions = SubmissionStore(root / "submissions.db")
            cms = FakeCms()
            config = SelfShareConfig(
                strm_root=root / "share",
                cms_cid="0",
                cms_local_path="/media/share",
            )
            workflow = SourceShareTaskWorkflow(
                cms=cms,
                store=submissions,
                move_config=MoveConfig(source_roots=[], library_roots={}),
                self_share_config=config,
                task_store=task_store,
                tmdb_resolver=FakeTmdbResolver(),
            )
            workflow.task_store = task_store
            task = task_store.upsert_task(
                "sourcecode",
                "7788",
                "https://115cdn.com/s/sourcecode?password=7788",
                strm_mode="source_shared",
            )
            received = workflow.run_stage(task)
            sync_task = replace(
                task,
                current_stage=TaskStage.SHARE_SYNC_SUBMITTED,
                metadata={**task.metadata, **received.metadata},
            )
            operation_key = f"{operation_scope(sync_task)}:cms_source_share_sync:source_shared:{task.share_code}"
            task_store.prepare_operation(
                task.id,
                operation_key,
                "cms_source_share_sync",
                {
                    "strm_mode": "source_shared",
                    "share_code": task.share_code,
                    "receive_code": task.receive_code,
                    "cid": config.cms_cid,
                    "local_path": config.cms_local_path,
                },
            )
            task_store.start_operation(task.id, operation_key)
            cms.share_sync_calls.append(("sourcecode", "7788", "0", "/media/share"))

            result = workflow.run_stage(sync_task)
            operation = task_store.find_operation(task.id, operation_key)

        self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
        self.assertIn("人工", result.message)
        self.assertEqual(cms.share_sync_calls, [("sourcecode", "7788", "0", "/media/share")])
        self.assertEqual(operation.status, "uncertain")


if __name__ == "__main__":
    unittest.main()
