import json
import tempfile
import unittest
from pathlib import Path

from app.config import MoveConfig
from app.models import TaskStage, TaskStatus
from app.task_runner import StageOutcome
from app.task_store import TaskStore
from app.workflows.direct import DirectTaskWorkflow
import bridge


class CmsFake:
    def __init__(self):
        self.add_calls = []
        self.details = []

    def add_share_down(self, url):
        self.add_calls.append(url)
        return {"data": {"id": "cms-1", "name": "示例电影"}}

    def get_share_down_detail(self, task_id):
        return self.details.pop(0) if self.details else {"status": "done", "name": "示例电影"}


class ForbiddenP115:
    def __init__(self):
        self.calls = []

    def receive_share_to_cid(self, *_args, **_kwargs):
        self.calls.append("receive")
        raise AssertionError("direct workflow must not use P115")

    def create_long_share(self, *_args, **_kwargs):
        self.calls.append("create")
        raise AssertionError("direct workflow must not use P115")

    def delete_file(self, *_args, **_kwargs):
        self.calls.append("delete")
        raise AssertionError("direct workflow must not clean P115 source")


class OpenAIFake:
    def __init__(self):
        self.calls = []

    def classify_media(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("direct workflow must not call OpenAI")


class EmbyFake:
    enabled = True

    def __init__(self, path):
        self.path = str(path)
        self.items = []
        self.refreshed = []

    def find_item_by_tmdb(self, tmdb_id):
        return next((item for item in self.items if item.get("ProviderIds", {}).get("Tmdb") == str(tmdb_id)), None)

    def recent_items(self, limit=30):
        return self.items[:limit]

    def refresh_library_for_path(self, path):
        self.refreshed.append(str(path))
        return "电影库"

    def library_name_for_item(self, item):
        return item.get("LibraryName")


class DirectWorkflowTests(unittest.TestCase):
    def _task(self, task_store, stage, submission_id=None, metadata=None, strm_mode="direct"):
        task = task_store.upsert_task(
            "abc",
            "1234",
            "https://115cdn.com/s/abc?password=1234",
            strm_mode=strm_mode,
        )
        task = task_store.record_event(
            task.id,
            stage,
            TaskStatus.RUNNING,
            "test",
            submission_id=submission_id,
            metadata_patch=metadata,
        )
        task_store.enqueue_task(task.id, stage, next_run_at=1.0)
        return task_store.claim_next_runnable("worker", now=1.0)

    def _workflow(self, tmp, emby=None, source_roots=None, library_roots=None):
        cms = CmsFake()
        submissions = bridge.SubmissionStore(Path(tmp) / "submissions.db")
        move_config = MoveConfig(
            source_roots=source_roots or [],
            library_roots=library_roots or {},
            stable_seconds=0,
        )
        workflow = DirectTaskWorkflow(cms, submissions, move_config, emby=emby, now=lambda: 100.0)
        workflow.forbidden_p115 = ForbiddenP115()
        workflow.openai_classifier = OpenAIFake()
        return workflow, cms, submissions

    def _row(self, submissions, recognition=None, cms_task_id=None, share_code="abc"):
        row = submissions.upsert_submission(
            bridge.ShareKey(share_code, "1234"),
            f"https://115cdn.com/s/{share_code}?password=1234",
            "submitted",
            cms_task_id=cms_task_id,
            title="示例电影",
        )
        if recognition is not None:
            row = submissions.update_recognition(int(row["id"]), recognition, "cms_resolved") or row
        return row

    def test_received_submits_cms_once_and_reuses_submission_without_p115(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, cms, submissions = self._workflow(tmp)
            tasks = TaskStore(Path(tmp) / "tasks.db")
            first = self._task(tasks, TaskStage.RECEIVED)

            first_result = workflow.run_stage(first)
            second = self._task(tasks, TaskStage.RECEIVED, first_result.metadata["submission_id"])
            second_result = workflow.run_stage(second)
            row = submissions.find_by_id(first_result.metadata["submission_id"])

        self.assertEqual(first_result.outcome, StageOutcome.COMPLETE)
        self.assertEqual(second_result.outcome, StageOutcome.COMPLETE)
        self.assertEqual(cms.add_calls, ["https://115cdn.com/s/abc?password=1234"])
        self.assertEqual(row["cms_task_id"], "cms-1")
        self.assertEqual(workflow.forbidden_p115.calls, [])

    def test_organizing_defers_until_cms_reaches_terminal_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, cms, submissions = self._workflow(tmp)
            tasks = TaskStore(Path(tmp) / "tasks.db")
            row = self._row(submissions, cms_task_id="cms-1")
            cms.details = [{"status": "running"}, {"status": "done", "name": "示例电影"}]

            waiting = workflow.run_stage(self._task(tasks, TaskStage.ORGANIZING, row["id"]))
            done = workflow.run_stage(self._task(tasks, TaskStage.ORGANIZING, row["id"]))
            status = submissions.find_by_id(row["id"])["status"]

        self.assertEqual(waiting.outcome, StageOutcome.DEFER)
        self.assertEqual(waiting.message, "等待 CMS 整理完成")
        self.assertEqual(done.outcome, StageOutcome.COMPLETE)
        self.assertEqual(status, "done")

    def test_recognizing_uses_saved_cms_category_and_needs_action_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, _cms, submissions = self._workflow(tmp)
            tasks = TaskStore(Path(tmp) / "tasks.db")
            row = self._row(
                submissions,
                {"title": "示例电影", "category": "欧美电影", "tmdb_id": "123", "type": "movie"},
            )
            recognized = workflow.run_stage(self._task(tasks, TaskStage.RECOGNIZING, row["id"]))
            missing_row = self._row(submissions, share_code="missing")
            missing = workflow.run_stage(self._task(tasks, TaskStage.RECOGNIZING, missing_row["id"]))

        self.assertEqual(recognized.outcome, StageOutcome.COMPLETE)
        self.assertEqual(recognized.metadata["direct_strm"], True)
        self.assertEqual(recognized.metadata["strm_mode"], "direct")
        self.assertEqual(missing.outcome, StageOutcome.NEEDS_ACTION)
        self.assertEqual(missing.message, "CMS 尚未给出媒体分类")
        self.assertEqual(workflow.openai_classifier.calls, [])

    def test_direct_strm_is_validated_moved_without_cleanup_and_confirmed_in_emby(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            library_root = root / "library"
            source = source_root / "Example-[tmdb=123]"
            source.mkdir(parents=True)
            (source / "movie.strm").write_text("https://115.com/d/file-id/movie.mkv", encoding="utf-8")
            emby = EmbyFake(library_root)
            workflow, _cms, submissions = self._workflow(
                tmp,
                emby=emby,
                source_roots=[source_root],
                library_roots={"欧美电影": library_root},
            )
            tasks = TaskStore(root / "tasks.db")
            row = self._row(
                submissions,
                {"title": "Example", "category": "欧美电影", "tmdb_id": "123", "type": "movie", "share_name": "Example"},
            )

            ready = workflow.run_stage(self._task(tasks, TaskStage.STRM_READY, row["id"]))
            moved = workflow.run_stage(
                self._task(
                    tasks,
                    TaskStage.MOVED,
                    row["id"],
                    {"source_path": str(source), "direct_strm": True, "strm_mode": "direct"},
                )
            )
            dest = library_root / source.name
            emby.items = [{"Id": "emby-1", "Name": "Example", "Path": str(dest), "ProviderIds": {"Tmdb": "123"}, "LibraryName": "电影库"}]
            confirmed = workflow.run_stage(
                self._task(tasks, TaskStage.EMBY_CONFIRMED, row["id"], {"dest_path": str(dest), "direct_strm": True})
            )
            stored = submissions.find_by_id(row["id"])

        self.assertEqual(ready.outcome, StageOutcome.COMPLETE)
        self.assertTrue(ready.metadata["direct_strm"])
        self.assertTrue(ready.metadata["strm_mode_locked"])
        self.assertEqual(moved.outcome, StageOutcome.COMPLETE)
        self.assertEqual(confirmed.outcome, StageOutcome.COMPLETE)
        self.assertEqual(stored["emby_status"], "confirmed")
        self.assertEqual(stored["emby_item_id"], "emby-1")
        self.assertEqual(workflow.forbidden_p115.calls, [])
        self.assertEqual(emby.refreshed, [str(dest.resolve())])

    def test_direct_workflow_rejects_shared_mode_before_cms_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow, cms, _submissions = self._workflow(tmp)
            tasks = TaskStore(Path(tmp) / "tasks.db")
            result = workflow.run_stage(self._task(tasks, TaskStage.RECEIVED, strm_mode="shared"))

        self.assertEqual(result.outcome, StageOutcome.FAILED)
        self.assertEqual(result.error_type, "strm_mode_mismatch")
        self.assertEqual(cms.add_calls, [])

    def test_direct_workflow_ignores_persisted_source_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed_root = root / "allowed"
            unsafe_source = root / "unsafe" / "Example-[tmdb=123]"
            unsafe_source.mkdir(parents=True)
            (unsafe_source / "movie.strm").write_text("https://115.com/d/file-id/movie.mkv", encoding="utf-8")
            workflow, _cms, submissions = self._workflow(
                tmp,
                source_roots=[allowed_root],
                library_roots={"欧美电影": root / "library"},
            )
            tasks = TaskStore(root / "tasks.db")
            row = self._row(
                submissions,
                {"title": "Example", "category": "欧美电影", "tmdb_id": "123", "type": "movie"},
            )
            task = self._task(tasks, TaskStage.STRM_READY, row["id"], {"source_path": str(unsafe_source)})

            result = workflow.run_stage(task)
            exists_after = unsafe_source.exists()

        self.assertEqual(result.outcome, StageOutcome.DEFER)
        self.assertFalse(result.metadata.get("strm_mode_locked", False))
        self.assertTrue(exists_after)


if __name__ == "__main__":
    unittest.main()
