import json
import tempfile
import os
import sqlite3
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import bridge
from app.clients import cms as cms_client
from app.models import TaskStage, TaskStatus
from app.task_runner import StageOutcome, TaskRunner
from app.task_store import TaskStore, operation_scope


class FakeCms:
    def __init__(self):
        self.plain_share_down_calls = []
        self.auto_organize_calls = 0
        self.share_sync_calls = []
        self.playback_results = []
        self.playback_calls = []

    def add_share_down(self, share_code, receive_code, *args, **kwargs):
        self.plain_share_down_calls.append((share_code, receive_code, args, kwargs))

    def run_auto_organize(self):
        self.auto_organize_calls += 1

    def add_share115_sync_task(self, own_code, own_pwd, cid, local_path):
        self.share_sync_calls.append((own_code, own_pwd, cid, local_path))

    def probe_strm_url(self, url):
        self.playback_calls.append(url)
        result = self.playback_results.pop(0) if self.playback_results else True
        if isinstance(result, BaseException):
            raise result
        return result


class FakeP115:
    def __init__(self):
        self.received = []
        self.receive_preparations = []
        self.receive_reconciliations = []
        self.received_target_visible = False
        self.folder = None
        self.created_shares = []
        self.find_organized_calls = []
        self.renamed = []
        self.share_statuses = []
        self.share_list_states = {}
        self.files_by_parent = {}
        self.list_file_calls = []
        self.search_hits = {}
        self.search_calls = []
        self.folder_paths = {}
        self.file_infos = {}
        self.file_info_calls = []

    def receive_share_to_cid(self, share_code, receive_code, receive_cid):
        intent = self.prepare_share_receive(share_code, receive_code, receive_cid)
        return self.execute_prepared_share_receive(intent)

    def prepare_share_receive(self, share_code, receive_code, receive_cid):
        self.receive_preparations.append((share_code, receive_code, receive_cid))
        return {
            "share_code": share_code,
            "receive_code": receive_code,
            "target_cid": receive_cid,
            "source_file_ids": ["file-a", "file-b"],
            "source_file_names": ["Root A", "Root B"],
            "title": "received title",
            "target_pre_call_file_ids": ["old-file"],
            "target_snapshot_complete": True,
        }

    def _receive_result(self, intent):
        return {
            "title": intent["title"],
            "file_ids": list(intent["source_file_ids"]),
            "received_items": [
                {
                    "file_id": "received-a",
                    "file_name": "Root A",
                    "parent_id": intent["target_cid"],
                    "is_folder": True,
                    "received_item_verified": True,
                },
                {
                    "file_id": "received-b",
                    "file_name": "Root B",
                    "parent_id": intent["target_cid"],
                    "is_folder": True,
                    "received_item_verified": True,
                },
            ],
            "received_items_complete": True,
            "received_expected_item_count": 2,
            "received_existing_file_ids": ["old-file"],
            "received_snapshot_complete": True,
            "response": {"state": True},
        }

    def execute_prepared_share_receive(self, intent):
        self.received.append((intent["share_code"], intent["receive_code"], intent["target_cid"]))
        self.received_target_visible = True
        return self._receive_result(intent)

    def reconcile_prepared_share_receive(self, intent):
        self.receive_reconciliations.append(dict(intent))
        return self._receive_result(intent) if self.received_target_visible else None

    def find_organized_folder(self, recognition, title, excluded_parent_ids=None, min_update_time=0, **kwargs):
        self.find_organized_calls.append((dict(recognition), title, excluded_parent_ids, min_update_time, kwargs))
        return self.folder

    def create_share(self, file_id):
        self.created_shares.append(file_id)
        suffix = len(self.created_shares)
        return {
            "share_code": "owncode" if suffix == 1 else f"owncode{suffix}",
            "receive_code": "ownpwd",
            "share_url": f"https://115.com/s/owncode{'' if suffix == 1 else suffix}?password=ownpwd",
        }

    def ensure_share_settings(self, share_code, receive_code):
        return {"share_code": share_code, "receive_code": "ownpwd"}

    def create_long_share(self, file_id, preferred_receive_code=""):
        created = self.create_share(file_id)
        settings = self.ensure_share_settings(
            created["share_code"],
            preferred_receive_code or created.get("receive_code") or "1212",
        )
        return {**created, **settings}

    def rename_file(self, file_id, file_name):
        self.renamed.append((str(file_id), str(file_name)))
        return {"state": True}

    def inspect_share(self, share_code, receive_code):
        if self.share_statuses:
            return self.share_statuses.pop(0)
        return {"available": True, "share_state": "0", "have_vio_file": False}

    def list_own_share_states(self, limit=100):
        if self.share_list_states:
            return dict(self.share_list_states)
        return {"owncode": {"share_state": "1", "have_vio_file": False, "create_time": 0}}

    def list_files(self, parent_id, limit=100):
        self.list_file_calls.append((str(parent_id), limit))
        return list(self.files_by_parent.get(str(parent_id), []))

    def search_files(self, search_value, limit=20):
        self.search_calls.append(str(search_value))
        return list(self.search_hits.get(str(search_value), []))

    def folder_path(self, folder_id):
        return list(self.folder_paths.get(str(folder_id), []))

    def file_info(self, file_id):
        self.file_info_calls.append(str(file_id))
        info = self.file_infos.get(str(file_id))
        return dict(info) if info else None


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def send_rich_message(self, chat_id, document, reply_markup=None):
        self.messages.append((chat_id, document.to_plain(), reply_markup))


class FakeEmby:
    enabled = True

    def __init__(self):
        self.items_by_tmdb = {}
        self.recent = []
        self.refreshed_paths = []

    def find_item_by_tmdb(self, tmdb_id):
        return self.items_by_tmdb.get(str(tmdb_id))

    def recent_items(self, limit=30):
        return self.recent[:limit]

    def library_name_for_item(self, item):
        return item.get("LibraryName")

    def refresh_library_for_path(self, item_path):
        self.refreshed_paths.append(str(item_path))
        return "电影库"


class FakeCleanupClient:
    def __init__(self):
        self.deleted = []
        self.parents = {}
        self.file_parent_id = self._file_parent_id

    def delete_file(self, file_id):
        self.deleted.append(file_id)

    def _file_parent_id(self, file_id):
        return str(self.parents.get(str(file_id), "") or "")

    def file_exists_in_parent(self, file_id, parent_id):
        return str(self.parents.get(str(file_id), "") or "") == str(parent_id or "")


class FakeClassifier:
    enabled = True
    high_confidence = 0.75
    suggest_confidence = 0.45

    def __init__(self, confidence=0.92):
        self.calls = []
        self.confidence = confidence

    def classify_media(self, recognition, share_name):
        self.calls.append((dict(recognition), share_name))
        return {
            "category": "外国电视",
            "confidence": self.confidence,
            "media_type": "tv",
            "title": "Fallback Show",
            "tmdb_id": "654321",
            "reason": "fake confidence",
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


class FakeTmdbSearchResolver(FakeTmdbResolver):
    def search(self, query, media_type):
        self.searches.append((query, media_type))
        if query == "Greys Anatomy" and media_type == "tv":
            return {
                "ok": True,
                "title": "实习医生格蕾",
                "type": "tv",
                "tmdb_id": "1416",
                "language": "en",
                "countries": ["US"],
                "genres": ["剧情"],
                "category": "外国电视",
                "source": "tmdb_api",
            }
        return {"ok": False}

class FakeTmdbHintResolver(FakeTmdbResolver):
    def lookup(self, tmdb_id, media_type, share_name):
        self.lookups.append((tmdb_id, media_type, share_name))
        if tmdb_id == "34307" and media_type == "tv":
            return {
                "ok": True,
                "title": "无耻之徒",
                "type": "tv",
                "tmdb_id": "34307",
                "language": "en",
                "countries": ["US"],
                "genres": ["剧情", "喜剧"],
                "category": "外国电视",
                "source": "tmdb_api",
            }
        return {"ok": False}


class FakeReacherTmdbResolver(FakeTmdbResolver):
    def search(self, query, media_type):
        self.searches.append((query, media_type))
        if query == "Reacher" and media_type == "tv":
            return {
                "ok": True,
                "title": "侠探杰克",
                "type": "tv",
                "tmdb_id": "108978",
                "language": "en",
                "countries": ["US"],
                "genres": ["剧情", "动作"],
                "category": "外国电视",
                "source": "tmdb_api",
            }
        return {"ok": False}


class FakeCmsCloudIndex:
    def __init__(self, folder=None, indexed_file_ids=None, cloud_output_folder=None):
        self.folder = folder
        self.calls = []
        self.indexed_file_ids = set(indexed_file_ids or [])
        self.cloud_output_folder = cloud_output_folder

    def folder_for_direct_strm(self, source, tmdb_id):
        self.calls.append((Path(source), tmdb_id))
        return self.folder

    def folder_for_cloud_output_name(self, file_name, started_at=0):
        self.calls.append(("cloud_output", file_name))
        return self.cloud_output_folder

    def has_file_id(self, file_id):
        return str(file_id) in self.indexed_file_ids


class BridgeSelfShareTaskWorkflowTests(unittest.TestCase):
    def _workflow(
        self,
        root,
        receive_cid="pending-cid",
        openai_classifier=None,
        tmdb_resolver=None,
        move_config=None,
        self_share_config=None,
        emby=None,
        cleanup_client=None,
        cms_cloud_index=None,
    ):
        self.cms = FakeCms()
        self.p115 = FakeP115()
        self.telegram = FakeTelegram()
        self.submissions = bridge.SubmissionStore(Path(root) / "submissions.db")
        self.tasks = TaskStore(Path(root) / "tasks.db")
        self.config = self_share_config or bridge.SelfShareConfig(
            enabled=True,
            strm_root=Path(root) / "share-strm",
            cms_cid="0",
            cms_local_path="/media/share",
            parent_cid_category_map={"movie-parent": "华语电影"},
            auto_organize_retry_seconds=30,
        )
        return bridge.BridgeSelfShareTaskWorkflow(
            self.cms,
            self.telegram,
            "chat-id",
            self.submissions,
            self.tasks,
            self.p115,
            self.config,
            move_config or bridge.MoveConfig(source_roots=[], library_roots={}),
            emby,
            openai_classifier,
            tmdb_resolver,
            cleanup_client=cleanup_client,
            receive_cid=receive_cid,
            cms_cloud_index=cms_cloud_index,
        )

    def _claim_task(self, share_code, receive_code, stage, metadata=None, submission_id=None):
        task = self.tasks.upsert_task(share_code, receive_code, f"https://115cdn.com/s/{share_code}?password={receive_code}")
        if metadata or submission_id is not None:
            if str(task.claimed_by or ""):
                # The task was already claimed by an earlier stage run in this
                # test. Persist the transition the way TaskRunner does (claim
                # CAS); the store refuses unguarded writes to claimed tasks.
                task = self.tasks.record_event(
                    task.id,
                    stage,
                    TaskStatus.RUNNING,
                    "metadata",
                    submission_id=submission_id,
                    metadata_patch=metadata,
                    expected_stage=task.current_stage,
                    expected_status=TaskStatus.RUNNING,
                    expected_claimed_by=task.claimed_by,
                    expected_claimed_at=task.claimed_at,
                    expected_claim_token=task.claim_token,
                    expected_updated_at=task.updated_at,
                )
            else:
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

    def _self_share_row(self, title="S-双喜-2025-[tmdb=123456]", category="华语电影", tmdb_id="123456"):
        row = self._row()
        row = self.submissions.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="share_sync_submitted",
            own_share_file_id="folder-id",
            own_share_file_name=title,
            own_share_code="owncode",
            own_share_receive_code="ownpwd",
            own_share_url="https://115.com/s/owncode?password=ownpwd",
            share_sync_status="submitted",
        ) or row
        recognition = {
            "title": title,
            "share_name": title,
            "category": category,
            "tmdb_id": tmdb_id,
            "type": "movie",
            "organized_parent_id": "movie-parent",
            "parent_id": "movie-parent",
        }
        row = self.submissions.update_recognition(int(row["id"]), recognition, "self_share_resolved") or row
        row = self.submissions.update_category(int(row["id"]), category, "selected") or row
        return row

    def _write_strm(self, folder, name="movie.strm", content="https://115.com/s/owncode_ownpwd_/movie.mkv"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(content, encoding="utf-8")

    @staticmethod
    def _receive_operation_key(task, receive_cid="pending-cid"):
        return f"{operation_scope(task)}:receive_share:{task.share_code}:{receive_cid}"

    @staticmethod
    def _create_share_operation_key(task, file_id):
        return f"{operation_scope(task)}:create_share:{file_id}"

    @staticmethod
    def _cms_share_sync_operation_key(task, share_code="owncode"):
        return f"{operation_scope(task)}:cms_share_sync:{share_code}"

    @staticmethod
    def _multi_targets():
        return [
            {
                "target_id": "dest-a",
                "file_ids": ["episode-a"],
                "folder": {
                    "file_id": "dest-a",
                    "file_name": "A-拆分剧集-[tmdb=259231]",
                    "parent_id": "movie-parent-a",
                    "category": "外国电视",
                },
                "recognition": {},
                "share": {"file_id": "dest-a", "status": "pending"},
                "strm": {"status": "pending", "move_status": "pending", "emby_status": "pending"},
            },
            {
                "target_id": "dest-b",
                "file_ids": ["episode-b"],
                "folder": {
                    "file_id": "dest-b",
                    "file_name": "B-拆分剧集-[tmdb=326917]",
                    "parent_id": "movie-parent-b",
                    "category": "番剧",
                },
                "recognition": {},
                "share": {"file_id": "dest-b", "status": "pending"},
                "strm": {"status": "pending", "move_status": "pending", "emby_status": "pending"},
            },
        ]

    def _multi_target_task(self, workflow, stage, *, targets=None, row=None):
        row = row or self._row()
        return self._claim_task(
            "abc",
            "1234",
            stage,
            {
                "submission_id": row["id"],
                "multi_target_version": 1,
                "organized_targets": targets or self._multi_targets(),
                "intake_identity": {
                    "root_ids": ["received-root"],
                    "files": [
                        {"id": "episode-a", "name": "01.mkv"},
                        {"id": "episode-b", "name": "02.mkv"},
                    ],
                },
            },
            row["id"],
        )

    @staticmethod
    def _delete_operation_key(task, operation_type, file_id):
        return f"{operation_scope(task)}:{operation_type}:{file_id}"

    def _cleanup_ready_row(self, root):
        row = self._self_share_row()
        dest = Path(root) / "library" / row["own_share_file_name"]
        self._write_strm(dest)
        row = self.submissions.update_move(
            int(row["id"]),
            "moved",
            source_path="/share/source",
            dest_path=str(dest),
            category_final="华语电影",
        ) or row
        return self.submissions.update_emby(int(row["id"]), "confirmed") or row

    def test_receive_started_before_post_never_posts_and_times_out_to_needs_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            intent = self.p115.prepare_share_receive("abc", "1234", "pending-cid")
            operation_key = self._receive_operation_key(task)
            self.tasks.prepare_operation(task.id, operation_key, "receive_share", intent)
            started = self.tasks.start_operation(task.id, operation_key)
            self.tasks.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            clock = [started.started_at + 1]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(self.tasks, workflow, worker_id="receive-recovery", now=lambda: clock[0])

            runner.run_once()

            waiting = self.tasks.find_task(task.id)
            self.assertEqual(waiting.status, TaskStatus.RUNNING)
            self.assertEqual(self.p115.received, [])
            self.assertEqual(len(self.p115.receive_reconciliations), 1)

            clock[0] = started.started_at + 3600
            runner.run_once()

            timed_out = self.tasks.find_task(task.id)
            self.assertEqual(timed_out.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(self.p115.received, [])

    def test_receive_crash_reconciles_without_second_receive(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            clock = [time.time()]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(self.tasks, workflow, worker_id="receive-crash", now=lambda: clock[0])
            complete_operation = self.tasks.complete_operation
            save_attempts = 0

            def fail_first_result_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving receive result")
                return complete_operation(*args, **kwargs)

            with patch.object(self.tasks, "complete_operation", side_effect=fail_first_result_save):
                runner.run_once()
                interrupted = self.tasks.find_task(task.id)
                self.assertEqual(interrupted.status, TaskStatus.FAILED)
                self.tasks.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
                runner.run_once()

            recovered = self.tasks.find_task(task.id)
            operation = self.tasks.find_operation(task.id, self._receive_operation_key(task))
            self.assertEqual(recovered.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(len(self.p115.received), 1)
            self.assertEqual(len(self.p115.receive_reconciliations), 1)

    def test_incomplete_receive_result_stays_started_and_never_posts_again(self):
        class IncompleteReceiveP115(FakeP115):
            def _incomplete_result(self, intent):
                result = self._receive_result(intent)
                result["received_items"] = result["received_items"][:1]
                result["received_items_complete"] = False
                return result

            def execute_prepared_share_receive(self, intent):
                self.received.append((intent["share_code"], intent["receive_code"], intent["target_cid"]))
                self.received_target_visible = True
                return self._incomplete_result(intent)

            def reconcile_prepared_share_receive(self, intent):
                self.receive_reconciliations.append(dict(intent))
                return self._incomplete_result(intent)

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            self.p115 = IncompleteReceiveP115()
            workflow.p115 = self.p115
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            clock = [time.time()]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(self.tasks, workflow, worker_id="receive-incomplete", now=lambda: clock[0])

            runner.run_once()

            operation_key = self._receive_operation_key(task)
            operation = self.tasks.find_operation(task.id, operation_key)
            waiting = self.tasks.find_task(task.id)
            self.assertEqual(operation.status, "started")
            self.assertEqual(waiting.current_stage, TaskStage.RECEIVED)
            self.assertEqual(waiting.status, TaskStatus.RUNNING)
            self.assertEqual(len(self.p115.received), 1)
            self.assertIsNone(self.submissions.find_by_key(bridge.ShareKey("abc", "1234")))

            clock[0] = operation.started_at + 3600
            runner.run_once()

            timed_out = self.tasks.find_task(task.id)
            self.assertEqual(timed_out.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(self.tasks.find_operation(task.id, operation_key).status, "started")
            self.assertEqual(len(self.p115.received), 1)
            self.assertEqual(len(self.p115.receive_reconciliations), 1)
            self.assertIsNone(self.submissions.find_by_key(bridge.ShareKey("abc", "1234")))

    def test_receive_saved_result_survives_discarded_taskrunner_stage_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            clock = [time.time()]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(self.tasks, workflow, worker_id="receive-discard", now=lambda: clock[0])
            complete_stage = self.tasks.complete_claimed_stage
            stage_save_attempts = 0

            def discard_first_stage_result(*args, **kwargs):
                nonlocal stage_save_attempts
                stage_save_attempts += 1
                if stage_save_attempts == 1:
                    return None
                return complete_stage(*args, **kwargs)

            with patch.object(self.tasks, "complete_claimed_stage", side_effect=discard_first_stage_result):
                runner.run_once()
                operation = self.tasks.find_operation(task.id, self._receive_operation_key(task))
                self.assertEqual(operation.status, "succeeded")
                self.tasks.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
                runner.run_once()

            recovered = self.tasks.find_task(task.id)
            self.assertEqual(recovered.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(len(self.p115.received), 1)
            self.assertEqual(self.p115.receive_reconciliations, [])

    def test_receive_restart_from_started_reconciles_visible_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            intent = self.p115.prepare_share_receive("abc", "1234", "pending-cid")
            operation_key = self._receive_operation_key(task)
            self.tasks.prepare_operation(task.id, operation_key, "receive_share", intent)
            self.tasks.start_operation(task.id, operation_key)
            self.p115.execute_prepared_share_receive(intent)
            self.tasks.enqueue_task(task.id, TaskStage.RECEIVED, next_run_at=0)
            runner = TaskRunner(self.tasks, workflow, worker_id="receive-restart", now=time.time)

            runner.run_once()

            recovered = self.tasks.find_task(task.id)
            operation = self.tasks.find_operation(task.id, operation_key)
            self.assertEqual(recovered.current_stage, TaskStage.ORGANIZING)
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(len(self.p115.received), 1)
            self.assertEqual(len(self.p115.receive_reconciliations), 1)

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
            self.assertFalse(result.metadata["tmdb_hint_normalized"])
            self.assertEqual(self.tasks.find_task(task.id).metadata["receive_target_cid"], "pending-cid")

    def test_receive_cid_is_pinned_after_receive(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="111")
            received_task = self._claim_task("abc", "1234", TaskStage.RECEIVED)

            received = workflow.run_stage(received_task)
            organizing_task = self.tasks.record_event(
                received_task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                received.message,
                metadata_patch=received.metadata,
                submission_id=received.metadata["submission_id"],
                expected_stage=received_task.current_stage,
                expected_status=TaskStatus.RUNNING,
                expected_claimed_by=received_task.claimed_by,
                expected_claimed_at=received_task.claimed_at,
                expected_claim_token=received_task.claim_token,
                expected_updated_at=received_task.updated_at,
            )
            self.tasks.set_self_share_receive_cid_override("222")

            result = workflow.run_stage(organizing_task)

            self.assertEqual(received.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [("abc", "1234", "111")])
            self.assertEqual(received.metadata["receive_target_cid"], "111")
            self.assertEqual(organizing_task.metadata["receive_target_cid"], "111")
            self.assertEqual(workflow._task_receive_cid(organizing_task), "111")
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)
            self.assertEqual(self.p115.find_organized_calls, [])

    def test_received_stage_stops_when_claimed_receive_cid_persistence_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED)

            with patch.object(workflow.task_store, "patch_claimed_metadata", return_value=None) as persist:
                result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(result.error_type, "receive_cid_persistence_stale_claim")
            self.assertIn("claim 已失效", result.message)
            self.assertEqual(result.metadata["receive_target_cid"], "pending-cid")
            self.assertEqual(result.metadata["receive_cid_persist_status"], "stale_claim")
            self.assertEqual(self.p115.received, [])
            self.assertIsNone(self.submissions.find_by_key(bridge.ShareKey("abc", "1234")))
            persist.assert_called_once_with(
                task.id,
                expected_claimed_by=task.claimed_by,
                expected_claimed_at=task.claimed_at,
                expected_claim_token=task.claim_token,
                expected_updated_at=task.updated_at,
                patch={"receive_target_cid": "pending-cid"},
            )

    def test_received_stage_stops_when_115_receive_is_restricted(self):
        class RestrictedP115(FakeP115):
            def execute_prepared_share_receive(self, intent):
                raise RuntimeError("你已被限制接收，如有疑问请联系客服")

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            workflow.p115 = RestrictedP115()
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("115 接收被限制", result.message)
            self.assertIsNone(self.submissions.find_by_key(bridge.ShareKey("abc", "1234")))

    def test_received_stage_reuses_existing_self_share_row_without_receiving_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="received_to_pending",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED, {"receive_target_cid": "pinned-cid"})

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [])
            self.assertEqual(result.metadata["submission_id"], row["id"])
            self.assertEqual(result.metadata["received_title"], "received title")
            self.assertEqual(result.metadata["receive_target_cid"], "pinned-cid")

    def test_received_stage_reuse_snapshots_identity_from_received_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            self.p115.files_by_parent["recv-folder-402"] = [
                {"fid": "video-mkv-402", "cid": "recv-folder-402", "n": "拆弹专家.2017.mkv"},
            ]
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="received_to_pending",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECEIVED,
                {
                    "receive_target_cid": "pending-cid",
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                },
            )

            result = workflow.run_stage(task)
            identity = result.metadata.get("intake_identity") or {}

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [])
            self.assertEqual(identity.get("root_ids"), ["recv-folder-402"])
            self.assertEqual(
                identity.get("files"),
                [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
            )
            self.assertNotIn("dest_id", identity)
            self.assertEqual(len({"share-fid-402", "recv-folder-402", "pending-cid", "video-mkv-402"}), 4)

    def test_received_stage_reuse_preserves_existing_intake_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            existing_identity = {
                "root_ids": ["recv-folder-402"],
                "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
            }
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="received_to_pending",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECEIVED,
                {
                    "receive_target_cid": "pending-cid",
                    "received_file_ids": ["share-fid-402"],
                    "intake_identity": existing_identity,
                },
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [])
            self.assertEqual(result.metadata.get("intake_identity"), existing_identity)
            self.assertEqual(self.p115.list_file_calls, [])

    def test_received_stage_defers_when_folder_list_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            original_list_files = self.p115.list_files

            def list_files(parent_id, limit=100):
                raise RuntimeError("115 risk control")

            self.p115.list_files = list_files
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED)

            result = workflow.run_stage(task)
            identity = result.metadata.get("intake_identity")

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待确认 115 接收后的本地文件", result.message)
            self.assertFalse(isinstance(identity, dict) and identity.get("files") == [])
            self.p115.list_files = original_list_files

    def test_cloud_task_receive_cid_prefers_cloud_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="configured-cid")
            task = self.tasks.upsert_cloud_task("btih:cloud-cid", "magnet:?xt=urn:btih:cloud-cid")
            task = self.tasks.record_event(
                task.id,
                TaskStage.CLOUD_DOWNLOADING,
                TaskStatus.RUNNING,
                "metadata",
                metadata_patch={"cloud_target_cid": "cloud-cid", "receive_target_cid": "ordinary-cid"},
            )

            self.assertEqual(workflow._task_receive_cid(task), "cloud-cid")

    def test_force_reprocess_receives_again_when_existing_row_has_no_downstream_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED, {"force_reprocess": True})

            result = workflow.run_stage(task)
            updated = self.submissions.find_by_key(bridge.ShareKey("abc", "1234"))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [("abc", "1234", "pending-cid")])
            self.assertEqual(result.metadata["submission_id"], row["id"])
            self.assertEqual(result.metadata["received_file_ids"], ["file-a", "file-b"])
            self.assertEqual(updated["workflow_phase"], "received_to_pending")

    def test_received_stage_snapshots_distinct_video_file_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115.files_by_parent["received-a"] = [
                {"fid": "video-a", "cid": "received-a", "n": "Movie.A.mkv"},
                {"fid": "sub-a", "cid": "received-a", "n": "Movie.A.ass"},
            ]
            self.p115.files_by_parent["received-b"] = [
                {"fid": "video-b", "cid": "received-b", "n": "Movie.B.mkv"},
            ]
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED, {"force_reprocess": True})

            result = workflow.run_stage(task)
            identity = result.metadata.get("intake_identity") or {}
            self.assertEqual(identity.get("root_ids"), ["received-a", "received-b"])
            self.assertEqual(
                {(item["id"], item["name"]) for item in identity.get("files") or []},
                {("video-a", "Movie.A.mkv"), ("video-b", "Movie.B.mkv")},
            )
            self.assertNotIn("file-a", {item["id"] for item in identity.get("files") or []})
            self.assertEqual(result.metadata.get("received_file_ids"), ["file-a", "file-b"])

    def test_force_reprocess_clears_existing_self_share_output_before_receiving(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            workflow._now = lambda: 2000000000.0
            row = self._self_share_row()
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECEIVED,
                {"force_reprocess": True, "submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)
            updated = self.submissions.find_by_key(bridge.ShareKey("abc", "1234"))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [("abc", "1234", "pending-cid")])
            self.assertTrue(result.metadata["self_share_reprocess_reset"])
            self.assertEqual(result.metadata["reprocess_started_at"], 2000000000.0)
            self.assertEqual(updated["workflow_phase"], "received_to_pending")
            self.assertIsNone(updated["own_share_file_id"])
            self.assertIsNone(updated["own_share_code"])

    def test_update_run_receives_again_after_completed_self_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._self_share_row(title="J-追更剧集-2026-[tmdb=1416]", category="外国电视", tmdb_id="1416")
            self.submissions.reset_self_share_for_update(int(row["id"]))
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECEIVED,
                {
                    "submission_id": row["id"],
                    "update_requested_run": 1,
                    "update_received_run": 0,
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            updated = self.submissions.find_by_key(bridge.ShareKey("abc", "1234"))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [("abc", "1234", "pending-cid")])
            self.assertEqual(result.metadata["update_received_run"], 1)
            self.assertEqual(updated["workflow_phase"], "received_to_pending")

    def test_update_run_only_searches_for_organize_results_after_update_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._row()
            self.submissions.reset_self_share_for_update(int(row["id"]))
            update_started_at = 2000000000.0
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {"submission_id": row["id"], "update_started_at": update_started_at},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(self.p115.find_organized_calls[0][3], update_started_at - 5)

    def test_reprocess_only_searches_for_organize_results_after_reprocess_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._row()
            self.submissions.reset_self_share_for_update(int(row["id"]))
            reprocess_started_at = 2000000000.0
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "force_reprocess": True,
                    "reprocess_started_at": reprocess_started_at,
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(self.p115.find_organized_calls[0][3], reprocess_started_at - 5)

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

    def test_resolve_intake_dest_folder_collects_all_hits_before_declaring_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.search_hits = {
                "episode-a.mkv": [
                    {"fid": "episode-a", "cid": "season-a", "n": "episode-a.mkv"},
                ],
                "episode-b.mkv": [
                    {"fid": "episode-b", "cid": "season-b", "n": "episode-b.mkv"},
                ],
            }
            workflow.p115.folder_paths = {
                "season-a": [
                    {"cid": "season-a", "n": "Season 01", "pid": "dest-a"},
                    {"cid": "dest-a", "n": "Show A", "pid": "tv-parent"},
                ],
                "season-b": [
                    {"cid": "season-b", "n": "Season 02", "pid": "dest-b"},
                    {"cid": "dest-b", "n": "Show B", "pid": "tv-parent"},
                ],
            }
            task_metadata = {
                "intake_identity": {
                    "files": [
                        {"id": "episode-a", "name": "episode-a.mkv"},
                        {"id": "episode-b", "name": "episode-b.mkv"},
                    ],
                    "root_ids": ["received-root"],
                }
            }

            status, folder, identity = workflow._resolve_intake_dest_folder(
                task_metadata,
                {},
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "conflict")
            self.assertIsNone(folder)
            self.assertIsNone(identity)

    def test_resolve_intake_dest_folder_does_not_bind_unrelated_persisted_own_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.files_by_parent["stale-dest"] = []
            task_metadata = {
                "intake_identity": {
                    "files": [{"id": "episode-a", "name": "episode-a.mkv"}],
                    "root_ids": ["received-root"],
                }
            }

            status, folder, identity = workflow._resolve_intake_dest_folder(
                task_metadata,
                {},
                own_share_file_id="stale-dest",
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "incomplete")
            self.assertIsNone(folder)
            self.assertIsNone(identity)

    def test_resolve_intake_dest_folder_rejects_persisted_season_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.search_hits["season-1"] = [
                {"cid": "season-1", "pid": "dest-a", "n": "Season 01"},
            ]
            task_metadata = {
                "intake_identity": {
                    "files": [{"id": "episode-a", "name": "episode-a.mkv"}],
                    "root_ids": ["received-root"],
                }
            }

            status, folder, identity = workflow._resolve_intake_dest_folder(
                task_metadata,
                {},
                own_share_file_id="season-1",
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "incomplete")
            self.assertIsNone(folder)
            self.assertIsNone(identity)

    def test_dest_is_receive_child_accepts_direct_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.files_by_parent["receive-root"] = [
                {"cid": "destination", "pid": "receive-root", "n": "Destination"},
            ]

            self.assertIs(workflow._dest_is_receive_child("destination", "receive-root"), True)

    def test_dest_is_receive_child_rejects_nested_receive_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.folder_paths["destination"] = [
                {"cid": "receive-root", "pid": "0", "n": "Receive"},
                {"cid": "intermediate", "pid": "receive-root", "n": "Intermediate"},
                {"cid": "destination", "pid": "intermediate", "n": "Destination"},
            ]

            self.assertIs(workflow._dest_is_receive_child("destination", "receive-root"), True)

    def test_resolve_intake_dest_folder_does_not_bind_incomplete_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.files_by_parent["legacy-dest"] = [
                {"fid": "episode-a", "cid": "legacy-dest", "n": "episode-a.mkv"},
            ]
            task_metadata = {
                "intake_identity": {
                    "files": [
                        {"id": "episode-a", "name": "episode-a.mkv"},
                        {"id": "episode-b", "name": "episode-b.mkv"},
                    ],
                    "root_ids": ["received-root"],
                }
            }

            status, folder, identity = workflow._resolve_intake_dest_folder(
                task_metadata,
                {},
                own_share_file_id="legacy-dest",
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "incomplete")
            self.assertIsNone(folder)
            self.assertIsNone(identity)

    def test_resolve_intake_dest_folders_defers_without_real_destination_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.search_hits = {
                "episode-a.mkv": [
                    {"fid": "episode-a", "cid": "season-a", "n": "episode-a.mkv"},
                ],
                "episode-b.mkv": [
                    {"fid": "episode-b", "cid": "season-b", "n": "episode-b.mkv"},
                ],
            }
            workflow.p115.folder_paths = {
                "season-a": [
                    {"cid": "season-a", "pid": "dest-a", "n": "Season 01"},
                ],
                "season-b": [
                    {"cid": "season-b", "pid": "dest-b", "n": "Season 02"},
                ],
            }
            task_metadata = {
                "intake_identity": {
                    "files": [
                        {"id": "episode-a", "name": "episode-a.mkv"},
                        {"id": "episode-b", "name": "episode-b.mkv"},
                    ],
                    "root_ids": ["received-root"],
                }
            }

            status, targets, identity = workflow._resolve_intake_dest_folders(
                task_metadata,
                {},
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "incomplete")
            self.assertEqual(targets, [])
            self.assertIsNone(identity)

    def test_resolve_intake_dest_folders_rejects_file_record_as_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.search_hits = {
                "episode-a.mkv": [
                    {"fid": "episode-a", "cid": "season-a", "n": "episode-a.mkv"},
                ],
                "episode-b.mkv": [
                    {"fid": "episode-b", "cid": "season-b", "n": "episode-b.mkv"},
                ],
                "dest-a": [
                    {"fid": "dest-a", "cid": "tv-parent", "n": "A", "fc": 0},
                ],
                "dest-b": [
                    {"cid": "dest-b", "pid": "tv-parent", "n": "B", "fc": 1},
                ],
            }
            workflow.p115.folder_paths = {
                "season-a": [{"cid": "season-a", "pid": "dest-a", "n": "Season 01"}],
                "season-b": [{"cid": "season-b", "pid": "dest-b", "n": "Season 02"}],
            }
            task_metadata = {
                "intake_identity": {
                    "files": [
                        {"id": "episode-a", "name": "episode-a.mkv"},
                        {"id": "episode-b", "name": "episode-b.mkv"},
                    ],
                    "root_ids": ["received-root"],
                }
            }

            status, targets, identity = workflow._resolve_intake_dest_folders(
                task_metadata,
                {},
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "incomplete")
            self.assertEqual(targets, [])
            self.assertIsNone(identity)

    def test_resolve_intake_dest_folders_rejects_destination_without_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115.search_hits = {
                "episode-a.mkv": [
                    {"fid": "episode-a", "cid": "season-a", "n": "episode-a.mkv"},
                ],
                "episode-b.mkv": [
                    {"fid": "episode-b", "cid": "season-b", "n": "episode-b.mkv"},
                ],
                "dest-a": [
                    {"cid": "dest-a", "n": "A", "fc": 1},
                ],
                "dest-b": [
                    {"cid": "dest-b", "pid": "tv-parent", "n": "B", "fc": 1},
                ],
            }
            workflow.p115.folder_paths = {
                "season-a": [{"cid": "season-a", "pid": "dest-a", "n": "Season 01"}],
                "season-b": [{"cid": "season-b", "pid": "dest-b", "n": "Season 02"}],
            }
            task_metadata = {
                "intake_identity": {
                    "files": [
                        {"id": "episode-a", "name": "episode-a.mkv"},
                        {"id": "episode-b", "name": "episode-b.mkv"},
                    ],
                    "root_ids": ["received-root"],
                }
            }

            status, targets, identity = workflow._resolve_intake_dest_folders(
                task_metadata,
                {},
                receive_cid="pending-cid",
            )

            self.assertEqual(status, "incomplete")
            self.assertEqual(targets, [])
            self.assertIsNone(identity)

    def test_organizing_stage_persists_multiple_complete_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="拆分剧集") or row
            row = self.submissions.update_self_share(
                int(row["id"]), workflow_phase="auto_organize_submitted"
            ) or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {
                    "title": "拆分剧集",
                    "share_name": "拆分剧集",
                    "tmdb_id": "123456",
                    "category": "外国电视",
                    "type": "tv",
                },
                "tmdb_hint_pending",
            ) or row
            self.p115.search_hits = {
                "123456": [
                    {"cid": "dest-b", "pid": "tv-parent", "n": "B-拆分剧集-[tmdb=123456]", "fc": 1},
                    {"cid": "dest-a", "pid": "tv-parent", "n": "A-拆分剧集-[tmdb=123456]", "fc": 1},
                ],
            }
            self.p115.files_by_parent = {
                "dest-a": [{"cid": "season-a", "pid": "dest-a", "n": "Season 01", "fc": 1}],
                "dest-b": [{"cid": "season-b", "pid": "dest-b", "n": "Season 02", "fc": 1}],
                "season-a": [{"fid": "episode-a", "cid": "season-a", "n": "01.mkv"}],
                "season-b": [{"fid": "episode-b", "cid": "season-b", "n": "02.mkv"}],
            }
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "intake_identity": {
                        "root_ids": ["received-root"],
                        "files": [
                            {"id": "episode-a", "name": "01.mkv"},
                            {"id": "episode-b", "name": "02.mkv"},
                        ],
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["multi_target_version"], 1)
            targets = result.metadata["organized_targets"]
            self.assertEqual([target["target_id"] for target in targets], ["dest-a", "dest-b"])
            self.assertEqual(targets[0]["file_ids"], ["episode-a"])
            self.assertEqual(targets[1]["file_ids"], ["episode-b"])
            for target in targets:
                self.assertEqual(
                    set(target),
                    {"target_id", "file_ids", "folder", "recognition", "share", "strm"},
                )
                self.assertEqual(set(target["folder"]), {"file_id", "file_name", "parent_id"})
                self.assertEqual(target["share"]["status"], "pending")
                self.assertEqual(target["strm"]["status"], "pending")
            self.assertEqual(result.metadata["intake_identity"]["root_ids"], ["received-root"])
            self.assertEqual(
                [item["id"] for item in result.metadata["intake_identity"]["files"]],
                ["episode-a", "episode-b"],
            )
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-a")

    def test_organizing_stage_rejects_folder_owned_by_different_tmdb_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="示例剧集 2025") or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "示例剧集", "type": "tv", "tmdb_id": "273114", "category": "国产电视"},
                "tmdb_resolved",
            ) or row
            owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
            self.tasks.record_event(
                owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "owner",
                metadata_patch={
                    "own_share_file_id": "shared-folder",
                    "tmdb_id": "9533",
                    "recognition": {"tmdb_id": "9533"},
                },
            )
            self.p115.folder = {
                "file_id": "shared-folder",
                "file_name": "S-示例剧集-2025-[tmdb=273114]",
                "parent_id": "tv-parent",
                "category": "国产电视",
            }
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("其他 TMDB 任务", result.message)
            self.assertFalse(stored["own_share_file_id"])

    def test_organizing_stage_allows_folder_owned_by_same_tmdb_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="示例剧集 2025") or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "示例剧集", "type": "tv", "tmdb_id": "273114", "category": "国产电视"},
                "tmdb_resolved",
            ) or row
            owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
            self.tasks.record_event(
                owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "owner",
                metadata_patch={
                    "own_share_file_id": "shared-folder",
                    "tmdb_id": "273114",
                    "recognition": {"tmdb_id": "273114"},
                },
            )
            self.p115.folder = {
                "file_id": "shared-folder",
                "file_name": "S-示例剧集-2025-[tmdb=273114]",
                "parent_id": "tv-parent",
                "category": "国产电视",
            }
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "shared-folder")

    def test_organizing_stage_rejects_folder_when_later_owner_has_different_tmdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="示例剧集 2025") or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "示例剧集", "type": "tv", "tmdb_id": "273114", "category": "国产电视"},
                "tmdb_resolved",
            ) or row
            mismatched_owner = self.tasks.upsert_task("mismatched", "", "https://115cdn.com/s/mismatched")
            self.tasks.record_event(
                mismatched_owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "mismatched owner",
                metadata_patch={
                    "own_share_file_id": "shared-folder",
                    "tmdb_id": "9533",
                    "recognition": {"tmdb_id": "9533"},
                },
            )
            matching_owner = self.tasks.upsert_task("matching", "", "https://115cdn.com/s/matching")
            self.tasks.record_event(
                matching_owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "matching owner",
                metadata_patch={
                    "own_share_file_id": "shared-folder",
                    "tmdb_id": "273114",
                    "recognition": {"tmdb_id": "273114"},
                },
            )
            owners = self.tasks.list_tasks_by_own_share_file_id("shared-folder")
            self.assertEqual([owner.id for owner in owners], [matching_owner.id, mismatched_owner.id])
            self.p115.folder = {
                "file_id": "shared-folder",
                "file_name": "S-示例剧集-2025-[tmdb=273114]",
                "parent_id": "tv-parent",
                "category": "国产电视",
            }
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("其他 TMDB 任务", result.message)
            self.assertFalse(stored["own_share_file_id"])

    def test_organizing_stage_rejects_folder_with_ambiguous_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="示例剧集 2025") or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "示例剧集", "type": "tv", "tmdb_id": "273114", "category": "国产电视"},
                "tmdb_resolved",
            ) or row
            owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
            self.tasks.record_event(
                owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "owner",
                metadata_patch={"own_share_file_id": "shared-folder"},
            )
            self.p115.folder = {
                "file_id": "shared-folder",
                "file_name": "S-示例剧集-2025-[tmdb=273114]",
                "parent_id": "tv-parent",
                "category": "国产电视",
            }
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("其他 TMDB 任务", result.message)
            self.assertFalse(stored["own_share_file_id"])

    def test_multi_target_alias_rejects_target_id_folder_id_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            targets = self._multi_targets()
            targets[0]["target_id"] = "different-target"
            task = self._multi_target_task(workflow, TaskStage.SHARE_ALIAS_PREPARED, targets=targets, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("目标", result.message)

    def test_multi_target_alias_rejects_persisted_share_file_id_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            targets = self._multi_targets()
            targets[0]["share"]["file_id"] = "different-share-file"
            task = self._multi_target_task(workflow, TaskStage.SHARE_ALIAS_PREPARED, targets=targets, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("身份", result.message)

    def test_recognizing_stage_uses_target_folder_identity_for_owner_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            task = self._multi_target_task(workflow, TaskStage.RECOGNIZING, row=row)
            task.metadata["recognition"] = {"tmdb_id": "111111"}
            owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
            self.tasks.record_event(
                owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "owner",
                metadata_patch={
                    "own_share_file_id": "dest-a",
                    "recognition": {"tmdb_id": "259231"},
                },
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["organized_targets"][0]["recognition"]["tmdb_id"], "259231")

    def test_recognizing_stage_rejects_first_target_tmdb_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            targets = self._multi_targets()
            targets[0]["recognition"] = {"tmdb_id": "999999"}
            task = self._multi_target_task(workflow, TaskStage.RECOGNIZING, targets=targets, row=row)
            task.metadata["intake_identity"]["dest_id"] = "dest-a"

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("TMDB", result.message)

    def test_recognizing_stage_preserves_target_specific_tmdb_and_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            task = self._multi_target_task(workflow, TaskStage.RECOGNIZING, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            targets = result.metadata["organized_targets"]
            self.assertEqual(targets[0]["recognition"]["tmdb_id"], "259231")
            self.assertEqual(targets[0]["recognition"]["category"], "外国电视")
            self.assertEqual(targets[1]["recognition"]["tmdb_id"], "326917")
            self.assertEqual(targets[1]["recognition"]["category"], "番剧")
            self.assertEqual(targets[0]["recognition"]["parent_id"], "movie-parent-a")
            self.assertEqual(targets[1]["recognition"]["parent_id"], "movie-parent-b")

    def test_share_alias_stage_keeps_alias_state_per_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            task = self._multi_target_task(workflow, TaskStage.SHARE_ALIAS_PREPARED, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            targets = result.metadata["organized_targets"]
            self.assertEqual(
                [target["share"]["alias_name"] for target in targets],
                ["A-拆分剧集-[tmdb=259231]", "B-拆分剧集-[tmdb=326917]"],
            )
            self.assertEqual([target["share"]["status"] for target in targets], ["alias_prepared", "alias_prepared"])

    def test_recognizing_stage_persists_deferred_target_recognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(
                tmp,
                tmdb_resolver=FakeTmdbResolver(),
                move_config=bridge.MoveConfig(
                    source_roots=[],
                    library_roots={"tv": Path(tmp) / "library"},
                ),
            )
            row = self._row()
            targets = self._multi_targets()
            for target in targets:
                target["folder"].pop("category", None)
            task = self._multi_target_task(workflow, TaskStage.RECOGNIZING, targets=targets, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            recognition = result.metadata["organized_targets"][0]["recognition"]
            self.assertEqual(recognition["category_status"], "waiting_cms_direct_strm")
            self.assertEqual(recognition["recognition_stage_status"], "defer")
            self.assertIn("等待 CMS 直链 STRM 分类", recognition["recognition_error"])

    def test_multi_target_alias_rejects_target_tmdb_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            targets = self._multi_targets()
            targets[0]["recognition"] = {"tmdb_id": "999999"}
            task = self._multi_target_task(workflow, TaskStage.SHARE_ALIAS_PREPARED, targets=targets, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("TMDB", result.message)
            self.assertEqual(workflow.p115.created_shares, [])

    def test_multi_target_own_share_rejects_target_tmdb_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            targets = self._multi_targets()
            targets[0]["recognition"] = {"tmdb_id": "999999"}
            task = self._multi_target_task(workflow, TaskStage.OWN_SHARE_CREATED, targets=targets, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("TMDB", result.message)
            self.assertEqual(workflow.p115.created_shares, [])

    def test_multi_target_ambiguous_recovery_needs_action_without_marking_success(self):
        class AmbiguousRecoveryP115(FakeP115):
            def find_own_share_by_title(self, title, min_create_time=0):
                return {"recovery_status": "ambiguous", "match_count": 2}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = AmbiguousRecoveryP115()
            row = self._row()
            targets = self._multi_targets()
            targets[0]["share"].update({"status": "succeeded", "code": "existing-a"})
            task = self._multi_target_task(workflow, TaskStage.OWN_SHARE_CREATED, targets=targets, row=row)
            operation_key = f"{operation_scope(task)}:create_share:dest-b"
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "create_share",
                {
                    "file_id": "dest-b",
                    "share_title": targets[1]["folder"]["file_name"],
                    "receive_code": "1212",
                    "requested_at": 1.0,
                },
            )
            self.tasks.start_operation(task.id, operation_key)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(result.metadata["share_recovery_status"], "ambiguous")
            self.assertEqual(result.metadata["share_recovery_match_count"], 2)
            result_targets = result.metadata["organized_targets"]
            self.assertEqual(result_targets[0]["share"]["status"], "succeeded")
            self.assertEqual(result_targets[1]["share"]["status"], "ambiguous")
            self.assertEqual(workflow.p115.created_shares, [])

    def test_own_share_stage_creates_and_reuses_one_journal_operation_per_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            task = self._multi_target_task(workflow, TaskStage.OWN_SHARE_CREATED, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            targets = result.metadata["organized_targets"]
            self.assertEqual([target["share"]["file_id"] for target in targets], ["dest-a", "dest-b"])
            self.assertEqual([target["share"]["status"] for target in targets], ["succeeded", "succeeded"])
            operations = self.tasks.list_operations(task.id)
            self.assertEqual(
                {
                    operation.operation_key
                    for operation in operations
                    if operation.operation_type == "create_share"
                },
                {
                    f"{operation_scope(task)}:create_share:dest-a",
                    f"{operation_scope(task)}:create_share:dest-b",
                },
            )
            self.assertEqual(self.p115.created_shares, ["dest-a", "dest-b"])

            for target in targets:
                share = workflow._journaled_create_share(
                    task,
                    target["share"]["file_id"],
                    target["folder"]["file_name"],
                    "ownpwd",
                )
                self.assertEqual(share["share_code"], target["share"]["code"])
            self.assertEqual(self.p115.created_shares, ["dest-a", "dest-b"])

    def test_own_share_stage_preserves_completed_target_when_later_target_is_pending(self):
        from app.clients.p115 import P115SharePendingError

        class PendingSecondTargetP115(FakeP115):
            def create_share(self, file_id):
                if file_id == "dest-b":
                    raise P115SharePendingError("processing")
                return super().create_share(file_id)

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = PendingSecondTargetP115()
            row = self._row()
            task = self._multi_target_task(workflow, TaskStage.OWN_SHARE_CREATED, row=row)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            targets = result.metadata["organized_targets"]
            self.assertEqual(targets[0]["share"]["status"], "succeeded")
            self.assertEqual(targets[0]["share"]["file_id"], "dest-a")
            self.assertEqual(targets[1]["share"]["status"], "pending")
            self.assertEqual(workflow.p115.created_shares, ["dest-a"])
            self.assertEqual(
                self.tasks.find_operation(task.id, f"{operation_scope(task)}:create_share:dest-a").status,
                "succeeded",
            )

    def test_recognizing_stage_stops_legacy_cross_tmdb_folder_before_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="示例剧集 2025") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="organized_found",
                own_share_file_id="shared-folder",
                own_share_file_name="S-示例剧集-2025-[tmdb=273114]",
            ) or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "示例剧集", "type": "tv", "tmdb_id": "273114", "category": "国产电视"},
                "tmdb_resolved",
            ) or row
            owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
            self.tasks.record_event(
                owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "owner",
                metadata_patch={
                    "own_share_file_id": "shared-folder",
                    "tmdb_id": "9533",
                    "recognition": {"tmdb_id": "9533"},
                },
            )
            task = self._claim_task("abc", "1234", TaskStage.RECOGNIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("其他 TMDB 任务", result.message)
            self.assertEqual(self.p115.list_file_calls, [])
            self.assertEqual(self.p115.created_shares, [])

    def test_own_share_stage_rechecks_late_folder_owner_before_operations(self):
        class RecoveryTrackingP115(FakeP115):
            def __init__(self):
                super().__init__()
                self.recovery_queries = []

            def find_own_share_by_title(self, title, min_create_time=0):
                self.recovery_queries.append((title, min_create_time))
                return None

        for pending_recovery in (False, True):
            with self.subTest(pending_recovery=pending_recovery), tempfile.TemporaryDirectory() as tmp:
                workflow = self._workflow(tmp)
                workflow.p115 = RecoveryTrackingP115()
                row = self._row()
                row = self.submissions.update_self_share(
                    int(row["id"]),
                    workflow_mode="self_share_sync",
                    workflow_phase="share_alias_prepared",
                    own_share_file_id="shared-folder",
                    own_share_file_name="S-示例剧集-2025-[tmdb=273114]",
                ) or row
                row = self.submissions.update_recognition(
                    int(row["id"]),
                    {"ok": True, "title": "示例剧集", "type": "tv", "tmdb_id": "273114", "category": "国产电视"},
                    "self_share_resolved",
                ) or row
                owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
                self.tasks.record_event(
                    owner.id,
                    TaskStage.ORGANIZING,
                    TaskStatus.PENDING,
                    "late owner",
                    metadata_patch={
                        "own_share_file_id": "shared-folder",
                        "tmdb_id": "9533",
                        "recognition": {"tmdb_id": "9533"},
                    },
                )
                metadata = {"submission_id": row["id"]}
                if pending_recovery:
                    metadata["share_create_status"] = "pending"
                    metadata["share_create_requested_at"] = 1000.0
                task = self._claim_task(
                    "abc",
                    "1234",
                    TaskStage.OWN_SHARE_CREATED,
                    metadata,
                    row["id"],
                )

                result = workflow.run_stage(task)
                stored = self.submissions.find_by_id(int(row["id"]))

                self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
                self.assertIn("其他 TMDB 任务", result.message)
                self.assertEqual(stored["own_share_file_id"], "shared-folder")
                self.assertIsNone(
                    self.tasks.find_operation(task.id, self._create_share_operation_key(task, "shared-folder"))
                )
                self.assertEqual(workflow.p115.created_shares, [])
                self.assertEqual(workflow.p115.recovery_queries, [])

    def test_own_share_stage_allows_late_same_tmdb_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="share_alias_prepared",
                own_share_file_id="shared-folder",
                own_share_file_name="S-示例剧集-2025-[tmdb=273114]",
            ) or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"ok": True, "title": "示例剧集", "type": "tv", "tmdb_id": "273114", "category": "国产电视"},
                "self_share_resolved",
            ) or row
            owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
            self.tasks.record_event(
                owner.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "late owner",
                metadata_patch={
                    "own_share_file_id": "shared-folder",
                    "tmdb_id": "273114",
                    "recognition": {"tmdb_id": "273114"},
                },
            )
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(workflow.p115.created_shares, ["shared-folder"])

    def test_organizing_stage_normalizes_explicit_tmdb_name_before_cms(self):
        class ExplicitTmdbResolver:
            enabled = True

            def lookup(self, tmdb_id, media_type, share_name):
                if media_type == "movie" and tmdb_id == "1228710":
                    return {
                        "ok": True,
                        "title": "星球大战：曼达洛人与古古",
                        "type": "movie",
                        "tmdb_id": tmdb_id,
                        "language": "en",
                        "countries": ["US"],
                        "category": "欧美电影",
                        "source": "tmdb_api",
                    }
                return {"ok": False}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=ExplicitTmdbResolver())
            raw_name = "123 (2026) {tmdb-1228710}.mkv"
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="received_to_pending",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_title": raw_name,
                    "received_file_ids": ["file-a"],
                    "received_items": [
                        {
                            "file_id": "file-a",
                            "file_name": raw_name,
                            "is_folder": False,
                            "received_item_verified": True,
                        },
                    ],
                    "received_items_complete": True,
                    "received_expected_item_count": 1,
                    "received_existing_file_ids": [],
                    "received_snapshot_complete": True,
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(
                self.p115.renamed,
                [("file-a", "星球大战：曼达洛人与古古 (2026) [tmdb=1228710].mkv")],
            )
            self.assertEqual(self.cms.auto_organize_calls, 1)
            recognition = json.loads(stored["recognition_json"])
            self.assertEqual(recognition["tmdb_id"], "1228710")
            self.assertEqual(recognition["category"], "欧美电影")

    def test_organizing_stage_does_not_call_cms_until_explicit_tmdb_item_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbHintResolver())
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="received_to_pending",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_title": "123 (2026) {tmdb-1228710}",
                    "received_file_ids": ["source-id"],
                    "received_items_complete": False,
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("暂不触发 CMS 整理", result.message)
            self.assertEqual(self.cms.auto_organize_calls, 0)
            self.assertEqual(self.p115.renamed, [])

    def test_organizing_stage_recovers_only_new_explicit_tmdb_item_after_delayed_receive(self):
        class ExplicitTmdbResolver:
            enabled = True

            def lookup(self, tmdb_id, media_type, share_name):
                if media_type == "movie" and tmdb_id == "1228710":
                    return {
                        "ok": True,
                        "title": "星球大战：曼达洛人与古古",
                        "type": "movie",
                        "tmdb_id": tmdb_id,
                        "category": "欧美电影",
                        "source": "tmdb_api",
                    }
                return {"ok": False}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=ExplicitTmdbResolver())
            self.p115.files_by_parent["pending-cid"] = [
                {
                    "fid": "old-local-id",
                    "pid": "pending-cid",
                    "n": "123 (2026) {tmdb-1228710}.mkv",
                },
                {
                    "fid": "new-local-id",
                    "pid": "pending-cid",
                    "n": "123 (2026) {tmdb-1228710}.mkv",
                },
            ]
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="received_to_pending",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_title": "123 (2026) {tmdb-1228710}",
                    "received_items_complete": False,
                    "received_expected_item_count": 1,
                    "received_existing_file_ids": ["old-local-id"],
                    "received_snapshot_complete": True,
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(
                self.p115.renamed,
                [("new-local-id", "星球大战：曼达洛人与古古 (2026) [tmdb=1228710].mkv")],
            )
            self.assertEqual(self.cms.auto_organize_calls, 1)

    def test_organizing_stage_does_not_use_unvalidated_received_file_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-snapshot-id"],
                    "received_title": "基督山伯爵士 4K原盘REMUX [HDR]",
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIsNone(stored["own_share_file_id"])

    def test_organizing_stage_ignores_folder_still_under_receive_cid(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            self.p115.folder = {
                "file_id": "local-pending-folder-id",
                "file_name": "基督山伯爵士 4K原盘REMUX [HDR]",
                "parent_id": "pending-cid",
            }
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertIn("pending-cid", self.p115.find_organized_calls[0][2])
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)
            self.assertIsNone(stored["own_share_file_id"])

    def test_recognizing_stage_rejects_unvalidated_received_file_id_after_manual_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._row()
            row = self.submissions.update_category(int(row["id"]), "欧美电影", "selected") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-snapshot-id"],
                    "organized_folder": {
                        "file_id": "share-snapshot-id",
                        "file_name": "基督山伯爵士 4K原盘REMUX [HDR]",
                        "parent_id": "pending-cid",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("可验证", result.message)
            self.assertEqual(result.metadata["own_share_file_id"], "")

    def test_organizing_stage_uses_tmdb_search_to_find_cms_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdb = FakeTmdbSearchResolver()
            workflow = self._workflow(tmp, tmdb_resolver=tmdb)
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="Greys.Anatomy.S22.1080p.DSNP.WEB-DL.DDP5.1.H.264-HiveWeb",
            ) or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            calls = []

            def find_organized_folder(recognition, title, excluded_parent_ids=None, min_update_time=0, **kwargs):
                calls.append((dict(recognition), title, kwargs))
                if recognition.get("tmdb_id") == "1416":
                    return {
                        "file_id": "folder-id",
                        "file_name": "S-实习医生格蕾-2005-[tmdb=1416]",
                        "parent_id": "tv-parent",
                        "category": "外国电视",
                    }
                return None

            self.p115.find_organized_folder = find_organized_folder
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            recognition = bridge.parse_recognition_json(stored)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(tmdb.searches, [("Greys Anatomy", "tv")])
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(calls[-1][0]["tmdb_id"], "1416")
            self.assertEqual(result.metadata["organized_folder"]["file_id"], "folder-id")
            self.assertEqual(result.metadata["organized_folder"]["category"], "外国电视")
            self.assertEqual(recognition["tmdb_id"], "1416")
            self.assertEqual(recognition["category"], "外国电视")
            self.assertEqual(stored["category_choice"], "外国电视")
            self.assertEqual(recognition["category_status"], "tmdb_search_resolved")
            self.assertEqual(stored["category_status"], "organized_found")

    def test_organizing_stage_uses_season_folder_child_video_to_find_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            episode_name = "Reacher.S01E01.2160p.UHD.BluRay.REMUX.mkv"
            self.p115.search_hits = {
                episode_name: [
                    {"fid": "ep1", "cid": "season-1", "n": episode_name},
                ],
                "108978": [
                    {
                        "cid": "dest-108978",
                        "n": "X-侠探杰克-2022-[tmdb=108978]",
                        "pid": "tv-parent",
                    },
                ],
            }
            self.p115.files_by_parent["dest-108978"] = [
                {"cid": "season-1", "n": "Season 1", "pid": "dest-108978"},
            ]
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="Season 3等3个文件(夹)",
            ) or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_title": "Season 3等3个文件(夹)",
                    "received_file_ids": ["share-fid-reacher"],
                    "received_items": [
                        {
                            "file_id": "recv-s1",
                            "file_name": "Season 1",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        },
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "108978",
                    "intake_identity": {
                        "root_ids": ["recv-s1"],
                        "files": [{"id": "ep1", "name": episode_name}],
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["organized_folder"]["file_id"], "dest-108978")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-108978")
            self.assertEqual(stored["own_share_file_id"], "dest-108978")
            self.assertNotEqual(stored["own_share_file_id"], "season-1")
            self.assertNotEqual(stored["own_share_file_id"], "recv-s1")
            self.assertNotEqual(stored["own_share_file_id"], "share-fid-reacher")
            self.assertEqual(self.p115.find_organized_calls, [])
            self.assertEqual(self.p115.renamed, [])

    def test_organizing_binds_movie_dest_from_moved_video_fid(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
                ],
                "441531": [
                    {
                        "cid": "dest-c-441531",
                        "n": "C-拆弹专家-2017-[tmdb=441531]",
                        "pid": "movie-parent",
                    },
                    {
                        "cid": "recv-folder-402",
                        "n": "拆弹专家 (2017) [tmdb=441531]",
                        "pid": "redundant-cid",
                    },
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-c-441531")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-c-441531")

    def test_organizing_binds_renamed_movie_from_dest_children_not_old_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            old_name = (
                "The Mandalorian and Grogu.2026.2160p.Ultra HD BluRay.REMUX."
                "DV.HDR.HEVC.TrueHD Dolby Atmos 7.1-WF.mkv"
            )
            self.p115.search_hits = {
                old_name: [
                    {
                        "fid": "unrelated-mando-s02",
                        "cid": "old-tv-season",
                        "n": "The.Mandalorian.S02E07.2020.mkv",
                    },
                ],
                "1228710": [
                    {
                        "cid": "dest-x-1228710",
                        "n": "X-星球大战：曼达洛人与古古-2026-[tmdb=1228710]",
                        "pid": "movie-parent",
                    },
                    {
                        "cid": "recv-folder-405",
                        "n": "星球大战：曼达洛人与古古 (2026) [tmdb=1228710]",
                        "pid": "redundant-cid",
                    },
                    {
                        "fid": "old-share-mkv",
                        "n": "星球大战：曼达洛人与古古 (2026) [tmdb=1228710].mkv",
                        "cid": "other-parent",
                    },
                ],
            }
            self.p115.files_by_parent = {
                "dest-x-1228710": [
                    {
                        "fid": "video-mkv-405",
                        "cid": "dest-x-1228710",
                        "n": "星球大战：曼达洛人与古古.2026.2160p.BluRay.DV.HDR.REMUX.mkv",
                    },
                ],
                "recv-folder-405": [],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-405"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-405",
                            "file_name": "星球大战：曼达洛人与古古 (2026) {tmdb-1228710}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "1228710",
                    "intake_identity": {
                        "root_ids": ["recv-folder-405"],
                        "files": [{"id": "video-mkv-405", "name": old_name}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-x-1228710")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-x-1228710")
            self.assertNotEqual(stored["own_share_file_id"], "recv-folder-405")
            self.assertEqual(
                len({"share-fid-405", "recv-folder-405", "dest-x-1228710", "video-mkv-405", "pending-cid"}),
                5,
            )

    def test_organizing_binds_renamed_episode_from_season_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {
                "Mystic.Nine.S01E01.mkv": [
                    {"fid": "unrelated-ep", "cid": "other-season", "n": "Other.S01E01.mkv"},
                ],
                "271016": [
                    {
                        "cid": "dest-j-271016",
                        "n": "J-九门-2026-[tmdb=271016]",
                        "pid": "tv-parent",
                    },
                ],
            }
            self.p115.files_by_parent = {
                "dest-j-271016": [
                    {"cid": "season-01", "n": "Season 01", "pid": "dest-j-271016"},
                ],
                "season-01": [
                    {
                        "fid": "ep-jiumen-01",
                        "cid": "season-01",
                        "n": "九门 (2026) - S01E01 - 第 1 集 - 2160p.mp4",
                    },
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "271016",
                    "intake_identity": {
                        "root_ids": ["recv-jiumen"],
                        "files": [{"id": "ep-jiumen-01", "name": "Mystic.Nine.S01E01.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-j-271016")
            self.assertNotEqual(stored["own_share_file_id"], "season-01")

    def test_organizing_binds_renamed_episodes_by_file_id_when_tmdb_search_is_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            old_e01 = "Lucky.S01E01.2026.2160p.ATVP.WEB-DL.DDP5.1.Atmos.HDR10P.H.265-HiveWeb.mkv"
            old_e02 = "Lucky.S01E02.2026.2160p.ATVP.WEB-DL.DDP5.1.Atmos.HDR10P.H.265-HiveWeb.mkv"
            self.p115.search_hits = {
                old_e01: [{"fid": "unrelated-lucky", "cid": "other-season", "n": "Lucky.2011.mkv"}],
                old_e02: [],
                "606952": [
                    {
                        "cid": "wrong-movie-606952",
                        "n": "X-幸运女神-2019-[tmdb=606952]",
                        "pid": "movie-parent",
                    },
                ],
            }
            self.p115.file_infos = {
                "ep-lucky-01": {
                    "fid": "ep-lucky-01",
                    "cid": "season-01",
                    "n": "幸运女神 (2026) - S01E01 - 第 1 集 - 2160p.mkv",
                },
            }
            self.p115.folder_paths["season-01"] = [
                {"cid": "dest-x-278624", "n": "X-幸运女神-2026-[tmdb=278624]", "pid": "tv-parent"},
                {"cid": "season-01", "n": "Season 01", "pid": "dest-x-278624"},
            ]
            self.p115.files_by_parent = {
                "wrong-movie-606952": [],
                "dest-x-278624": [
                    {"cid": "season-01", "n": "Season 01", "pid": "dest-x-278624"},
                ],
                "season-01": [
                    {
                        "fid": "ep-lucky-01",
                        "cid": "season-01",
                        "n": "幸运女神 (2026) - S01E01 - 第 1 集 - 2160p.mkv",
                    },
                    {
                        "fid": "ep-lucky-02",
                        "cid": "season-01",
                        "n": "幸运女神 (2026) - S01E02 - 第 2 集 - 2160p.mkv",
                    },
                ],
            }
            row = self._row()
            row = self.submissions.update_recognition(
                int(row["id"]),
                {
                    "ok": True,
                    "title": "幸运女神",
                    "tmdb_id": "606952",
                    "type": "movie",
                    "category": "欧美电影",
                    "category_status": "tmdb_search_resolved",
                },
                "tmdb_search_resolved",
            ) or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_items_complete": True,
                    "intake_identity": {
                        "root_ids": ["recv-lucky"],
                        "files": [
                            {"id": "ep-lucky-01", "name": old_e01},
                            {"id": "ep-lucky-02", "name": old_e02},
                        ],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-x-278624")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-x-278624")
            self.assertIn("ep-lucky-01", self.p115.file_info_calls)
            self.assertNotIn("ep-lucky-01", self.p115.search_calls)
            self.assertNotEqual(stored["own_share_file_id"], "wrong-movie-606952")
            self.assertNotEqual(stored["own_share_file_id"], "season-01")

    def test_recognizing_adopts_cms_dest_tmdb_after_wrong_title_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_recognition(
                int(row["id"]),
                {
                    "ok": True,
                    "title": "幸运女神",
                    "tmdb_id": "606952",
                    "type": "movie",
                    "category": "欧美电影",
                    "category_status": "tmdb_search_resolved",
                },
                "tmdb_search_resolved",
            ) or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="dest-x-278624",
                own_share_file_name="X-幸运女神-2026-[tmdb=278624]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {
                    "submission_id": row["id"],
                    "intake_identity": {
                        "root_ids": ["recv-lucky"],
                        "files": [{"id": "ep-lucky-01", "name": "Lucky.S01E01.mkv"}],
                        "dest_id": "dest-x-278624",
                    },
                    "organized_folder": {
                        "file_id": "dest-x-278624",
                        "file_name": "X-幸运女神-2026-[tmdb=278624]",
                        "parent_id": "tv-parent",
                        "category": "外国电视",
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            recognition = json.loads(stored["recognition_json"] or "{}")
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(recognition["tmdb_id"], "278624")
            self.assertEqual(recognition["category"], "外国电视")
            self.assertEqual(result.metadata["tmdb_id"], "278624")

    def test_organizing_binds_dest_from_video_parent_without_tmdb_folder_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-c-441531")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-c-441531")

    def test_organizing_defers_when_video_still_under_receive_cid(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "pending-cid", "n": "拆弹专家.2017.mkv"},
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "video-mkv-402",
                            "file_name": "拆弹专家.2017.mkv",
                            "is_folder": False,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["video-mkv-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)
            self.assertNotEqual(stored["own_share_file_id"], "pending-cid")

    def test_recognizing_keeps_intake_dest_without_tmdb_in_folder_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
                ],
            }
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="拆弹专家 (2017) {tmdb-441531}",
            ) or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            organize_meta = {
                "submission_id": row["id"],
                "received_file_ids": ["share-fid-402"],
                "received_items": [
                    {
                        "file_id": "recv-folder-402",
                        "file_name": "拆弹专家 (2017) {tmdb-441531}",
                        "is_folder": True,
                        "parent_id": "pending-cid",
                        "received_item_verified": True,
                    }
                ],
                "received_items_complete": True,
                "tmdb_hint_normalized": True,
                "tmdb_hint_id": "441531",
                "tmdb_hint_title": "拆弹专家",
                "tmdb_hint_category": "华语电影",
                "intake_identity": {
                    "root_ids": ["recv-folder-402"],
                    "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                },
            }
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, organize_meta, row["id"])
            organizing = workflow.run_stage(task)
            self.assertEqual(organizing.outcome, StageOutcome.COMPLETE)
            recognizing_task = self._claim_task("abc", "1234", TaskStage.RECOGNIZING, organizing.metadata, row["id"])
            recognizing = workflow.run_stage(recognizing_task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertFalse(
                recognizing.outcome == StageOutcome.NEEDS_ACTION and "TMDB 不一致" in str(recognizing.message or "")
            )
            self.assertEqual(stored["own_share_file_id"], "dest-c-441531")
            identity = recognizing.metadata.get("intake_identity") or organizing.metadata.get("intake_identity") or {}
            self.assertEqual(identity.get("dest_id") or stored.get("own_share_file_id"), "dest-c-441531")

    def test_organizing_defers_when_dest_folder_still_under_receive_cid(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "inbox-c-folder", "n": "拆弹专家.2017.mkv"},
                ],
            }
            self.p115.files_by_parent["pending-cid"] = [
                {"cid": "inbox-c-folder", "n": "C-拆弹专家-2017", "pid": "pending-cid"},
            ]
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)
            self.assertNotEqual(stored["own_share_file_id"], "inbox-c-folder")

    def test_organizing_defers_when_receive_cid_list_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "inbox-c-folder", "n": "拆弹专家.2017.mkv"},
                ],
            }
            original_list_files = self.p115.list_files

            def list_files(parent_id, limit=100):
                if str(parent_id) == "pending-cid":
                    raise RuntimeError("115 risk control")
                return original_list_files(parent_id, limit=limit)

            self.p115.list_files = list_files
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)
            self.assertNotEqual(stored["own_share_file_id"], "inbox-c-folder")

    def test_organizing_does_not_title_bind_when_intake_files_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.folder = {
                "file_id": "title-bound-dest",
                "file_name": "C-拆弹专家-2017-[tmdb=441531]",
                "parent_id": "movie-parent",
            }
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="stale-own",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertNotEqual(stored["own_share_file_id"], "title-bound-dest")
            identity = result.metadata.get("intake_identity") or {}
            self.assertNotEqual(identity.get("dest_id"), "title-bound-dest")

    def test_organizing_defers_incomplete_search_does_not_bind_stale_dest_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {}
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                        "dest_id": "stale-dest",
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertNotEqual(stored["own_share_file_id"], "stale-dest")

    def test_organizing_merges_season_files_into_existing_show_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {
                "Reacher.S03E01.mkv": [
                    {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
                ],
                "108978": [
                    {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
                    {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                    {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-399"],
                    "received_items": [
                        {
                            "file_id": "recv-s3",
                            "file_name": "Season 3",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "108978",
                    "intake_identity": {
                        "root_ids": ["recv-s3"],
                        "files": [{"id": "ep-s3-e1", "name": "Reacher.S03E01.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-108978")

    def test_organizing_second_task_can_reuse_existing_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {
                "Reacher.S03E02.mkv": [
                    {"fid": "ep-s3-e2", "cid": "season-3", "n": "Reacher.S03E02.mkv"},
                ],
                "108978": [
                    {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
                    {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                ],
            }
            row = self._row("def", "5678")
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "def",
                "5678",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "108978",
                    "intake_identity": {
                        "root_ids": ["recv-s3-task-b"],
                        "files": [{"id": "ep-s3-e2", "name": "Reacher.S03E02.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(stored["own_share_file_id"], "dest-108978")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-108978")

    def test_organizing_ignores_same_tmdb_dest_without_these_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {"108978": [
                {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
            ]}
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "108978",
                    "intake_identity": {
                        "root_ids": ["recv-s3"],
                        "files": [{"id": "ep-s3-e1", "name": "Reacher.S03E01.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIsNone(stored["own_share_file_id"])

    def test_organizing_defers_when_intake_files_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {"submission_id": row["id"], "intake_identity": {"root_ids": ["recv-folder-402"], "files": []}},
                row["id"],
            )
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)

    def test_organizing_merges_season_when_tmdb_search_omits_season_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {
                "Reacher.S03E01.mkv": [
                    {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
                ],
                "108978": [
                    {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                    {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
                ],
            }
            self.p115.files_by_parent["dest-108978"] = [
                {"cid": "season-3", "n": "Season 3", "pid": "dest-108978"},
            ]
            self.p115.files_by_parent["old-dest-108978"] = []
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-399"],
                    "received_items": [
                        {
                            "file_id": "recv-s3",
                            "file_name": "Season 3",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "108978",
                    "intake_identity": {
                        "root_ids": ["recv-s3"],
                        "files": [{"id": "ep-s3-e1", "name": "Reacher.S03E01.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-108978")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-108978")
            self.assertNotEqual(stored["own_share_file_id"], "season-3")
            self.assertNotEqual(stored["own_share_file_id"], "old-dest-108978")

    def test_organizing_defers_when_season_parent_is_not_in_tmdb_dests(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            self.p115.search_hits = {
                "Reacher.S03E01.mkv": [
                    {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
                ],
                "108978": [
                    {"cid": "dest-108978", "n": "X-侠探杰克-2022-[tmdb=108978]", "pid": "tv-parent"},
                    {"cid": "old-dest-108978", "n": "侠探杰克 (2022) {tmdb-108978}", "pid": "tv-parent"},
                ],
            }
            self.p115.files_by_parent["dest-108978"] = []
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-399"],
                    "received_items": [
                        {
                            "file_id": "recv-s3",
                            "file_name": "Season 3",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "108978",
                    "intake_identity": {
                        "root_ids": ["recv-s3"],
                        "files": [{"id": "ep-s3-e1", "name": "Reacher.S03E01.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 整理", result.message)
            self.assertNotEqual(stored["own_share_file_id"], "season-3")
            self.assertNotEqual(stored["own_share_file_id"], "old-dest-108978")

    def test_organizing_binds_movie_dest_when_tmdb_hits_are_only_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            self.p115.search_hits = {
                "拆弹专家.2017.mkv": [
                    {"fid": "video-mkv-402", "cid": "dest-c-441531", "n": "拆弹专家.2017.mkv"},
                ],
                "441531": [
                    {
                        "cid": "recv-folder-402",
                        "n": "拆弹专家 (2017) [tmdb=441531]",
                        "pid": "redundant-cid",
                    },
                ],
                "dest-c-441531": [
                    {
                        "cid": "dest-c-441531",
                        "n": "C-拆弹专家-2017-[tmdb=441531]",
                        "pid": "movie-parent",
                    },
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-402"],
                    "received_items": [
                        {
                            "file_id": "recv-folder-402",
                            "file_name": "拆弹专家 (2017) {tmdb-441531}",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "441531",
                    "tmdb_hint_title": "拆弹专家",
                    "tmdb_hint_category": "华语电影",
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-c-441531")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-c-441531")

    def test_organizing_defers_when_tmdb_search_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeReacherTmdbResolver())
            original_search = self.p115.search_files

            def search_files(search_value, limit=20):
                if str(search_value) == "108978":
                    raise RuntimeError("115 tmdb search failed")
                return original_search(search_value, limit=limit)

            self.p115.search_files = search_files
            self.p115.search_hits = {
                "Reacher.S03E01.mkv": [
                    {"fid": "ep-s3-e1", "cid": "season-3", "n": "Reacher.S03E01.mkv"},
                ],
            }
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-fid-399"],
                    "received_items": [
                        {
                            "file_id": "recv-s3",
                            "file_name": "Season 3",
                            "is_folder": True,
                            "parent_id": "pending-cid",
                            "received_item_verified": True,
                        }
                    ],
                    "received_items_complete": True,
                    "tmdb_hint_normalized": True,
                    "tmdb_hint_id": "108978",
                    "intake_identity": {
                        "root_ids": ["recv-s3"],
                        "files": [{"id": "ep-s3-e1", "name": "Reacher.S03E01.mkv"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertNotEqual(stored["own_share_file_id"], "season-3")

    def test_organizing_walks_up_season_from_folder_path_without_tmdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115.search_hits = {
                "九门.S01E01.mp4": [
                    {"fid": "ep1", "cid": "season-1", "n": "九门.S01E01.mp4"},
                ],
            }
            self.p115.folder_paths["season-1"] = [
                {"cid": "dest-271016", "n": "J-九门-2026-[tmdb=271016]", "pid": "tv-parent"},
                {"cid": "season-1", "n": "Season 01", "pid": "dest-271016"},
            ]
            self.p115.files_by_parent["dest-271016"] = [
                {"cid": "season-1", "n": "Season 01", "pid": "dest-271016"},
            ]
            self.p115.files_by_parent["season-1"] = [
                {"fid": "ep1", "n": "九门.S01E01.mp4", "cid": "season-1"},
            ]
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_items_complete": True,
                    "intake_identity": {
                        "root_ids": ["recv-s1"],
                        "files": [{"id": "ep1", "name": "九门.S01E01.mp4"}],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-271016")
            self.assertEqual(result.metadata["intake_identity"]["dest_id"], "dest-271016")
            self.assertNotEqual(stored["own_share_file_id"], "season-1")

    def test_organizing_reuses_dest_id_without_searching(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115.files_by_parent["dest-c-441531"] = [
                {"fid": "video-mkv-402", "n": "拆弹专家.2017.mkv", "cid": "dest-c-441531"},
            ]
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_items_complete": True,
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                        "dest_id": "dest-c-441531",
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-c-441531")
            self.assertEqual(self.p115.search_calls, [])

    def test_organizing_lists_dest_to_locate_remaining_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115.search_hits = {
                "九门.S01E01.mp4": [
                    {"fid": "ep1", "cid": "season-1", "n": "九门.S01E01.mp4"},
                ],
            }
            self.p115.folder_paths["season-1"] = [
                {"cid": "dest-271016", "n": "J-九门-2026-[tmdb=271016]", "pid": "tv-parent"},
                {"cid": "season-1", "n": "Season 01", "pid": "dest-271016"},
            ]
            self.p115.files_by_parent["dest-271016"] = [
                {"cid": "season-1", "n": "Season 01", "pid": "dest-271016"},
            ]
            self.p115.files_by_parent["season-1"] = [
                {"fid": "ep1", "n": "九门.S01E01.mp4", "cid": "season-1"},
                {"fid": "ep2", "n": "九门.S01E02.mp4", "cid": "season-1"},
            ]
            row = self._row()
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "received_items_complete": True,
                    "intake_identity": {
                        "root_ids": ["recv-s1"],
                        "files": [
                            {"id": "ep1", "name": "九门.S01E01.mp4"},
                            {"id": "ep2", "name": "九门.S01E02.mp4"},
                        ],
                    },
                },
                row["id"],
            )
            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_id"], "dest-271016")
            self.assertNotIn("九门.S01E02.mp4", self.p115.search_calls)

    def test_organizing_stage_persists_and_reuses_organized_scan_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbResolver())
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="目标影片 2026",
            ) or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            cursor = {
                "version": 1,
                "root_parent_ids": ["exists-root"],
                "queue": [{"parent_id": "child-1", "parts": ["Movie"], "depth": 1, "offset": 0}],
                "seen": ["exists-root", "child-1"],
            }
            calls = []

            def find_organized_folder(recognition, title, **kwargs):
                calls.append(kwargs)
                return {
                    "folder": None,
                    "organized_scan_cursor": {**cursor, "queue": [{"parent_id": "child-2", "parts": [], "depth": 1, "offset": 0}]},
                    "scan_complete": False,
                }

            workflow.p115.find_organized_folder = find_organized_folder
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {"submission_id": row["id"], "organized_scan_cursor": cursor},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(result.metadata["organized_scan_cursor"]["queue"][0]["parent_id"], "child-2")
            self.assertEqual(calls[0]["organized_scan_cursor"], cursor)
            self.assertTrue(calls[0]["return_scan_state"])

    def test_organizing_stage_keeps_total_115_lookup_budget_across_tmdb_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, tmdb_resolver=FakeTmdbSearchResolver())
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="Greys.Anatomy.S22.1080p.DSNP.WEB-DL",
            ) or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            calls = []

            def find_organized_folder(recognition, title, **kwargs):
                calls.append(kwargs)
                return {
                    "folder": None,
                    "organized_scan_cursor": None,
                    "scan_complete": True,
                    "request_count": 8,
                }

            workflow.p115.find_organized_folder = find_organized_folder
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(len(calls), 1)

    def test_organizing_stage_uses_tmdb_search_for_chinese_quality_title(self):
        class MonteCristoResolver(FakeTmdbResolver):
            def search(self, query, media_type):
                self.searches.append((query, media_type))
                if query == "基督山伯爵士" and media_type == "movie":
                    return {
                        "ok": True,
                        "title": "基督山伯爵",
                        "type": "movie",
                        "tmdb_id": "1084736",
                        "language": "fr",
                        "countries": ["FR"],
                        "genres": ["剧情"],
                        "category": "欧美电影",
                        "source": "tmdb_api",
                    }
                return {"ok": False}

        with tempfile.TemporaryDirectory() as tmp:
            tmdb = MonteCristoResolver()
            workflow = self._workflow(tmp, tmdb_resolver=tmdb)
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="基督山伯爵士 4K原盘REMUX [HDR 杜比视界] [中英双字 简繁中字]",
            ) or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            calls = []

            def find_organized_folder(recognition, title, excluded_parent_ids=None, min_update_time=0, **kwargs):
                calls.append((dict(recognition), title, kwargs))
                if recognition.get("tmdb_id") == "1084736":
                    return {
                        "file_id": "folder-id",
                        "file_name": "基督山伯爵士 4K原盘REMUX [HDR 杜比视界] [中英双字 简繁中字]",
                        "parent_id": "movie-parent",
                        "category": "欧美电影",
                    }
                return None

            self.p115.find_organized_folder = find_organized_folder
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            recognition = bridge.parse_recognition_json(stored)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(tmdb.searches, [("基督山伯爵士", "movie")])
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(calls[-1][0]["tmdb_id"], "1084736")
            self.assertEqual(result.metadata["organized_folder"]["file_id"], "folder-id")
            self.assertEqual(result.metadata["organized_folder"]["category"], "欧美电影")
            self.assertEqual(recognition["tmdb_id"], "1084736")
            self.assertEqual(recognition["category"], "欧美电影")
            self.assertEqual(stored["category_choice"], "欧美电影")
            self.assertEqual(recognition["category_status"], "tmdb_search_resolved")
            self.assertEqual(stored["category_status"], "organized_found")

    def test_recognizing_stage_uses_received_folder_video_name_for_tmdb_search(self):
        class ChildFileP115(FakeP115):
            def __init__(self):
                super().__init__()
                self.listed = []

            def list_files(self, parent_id, limit=20):
                self.listed.append((parent_id, limit))
                return [{"fid": "video-id", "n": "Le.Comte.de.Monte-Cristo.2024.2160p.BluRay.REMUX.HDR.DV.mkv"}]

        class MonteCristoResolver(FakeTmdbResolver):
            def search(self, query, media_type):
                self.searches.append((query, media_type))
                if query == "Le Comte de Monte Cristo" and media_type == "movie":
                    return {
                        "ok": True,
                        "title": "基督山伯爵",
                        "type": "movie",
                        "tmdb_id": "1084736",
                        "language": "fr",
                        "countries": ["FR"],
                        "genres": ["剧情"],
                        "category": "欧美电影",
                        "source": "tmdb_api",
                    }
                return {"ok": False}

        with tempfile.TemporaryDirectory() as tmp:
            resolver = MonteCristoResolver()
            workflow = self._workflow(tmp, tmdb_resolver=resolver)
            workflow.p115 = ChildFileP115()
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="organized_found",
                own_share_file_id="received-folder-id",
                own_share_file_name="基督山伯爵士 4K原盘REMUX [HDR]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {"submission_id": row["id"], "own_share_file_id": "received-folder-id"},
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            recognition = bridge.parse_recognition_json(stored)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "欧美电影")
            self.assertEqual(result.metadata["tmdb_id"], "1084736")
            self.assertEqual(stored["category_choice"], "欧美电影")
            self.assertEqual(recognition["tmdb_id"], "1084736")
            self.assertEqual(workflow.p115.listed, [("received-folder-id", 20)])

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

    def test_recognizing_stage_stops_for_manual_category_when_cms_parent_unmapped(self):
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

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(classifier.calls, [])
            self.assertEqual(tmdb.lookups, [])
            self.assertEqual(tmdb.searches, [])
            self.assertEqual(len(self.telegram.messages), 1)
            _, text, reply_markup = self.telegram.messages[0]
            self.assertIn("CMS 未能确定分类", text)
            self.assertIn("请选择分类", text)
            self.assertNotIn("OpenAI建议", text)
            self.assertEqual(reply_markup, bridge.category_keyboard(int(row["id"])))
            recognition = result.metadata["recognition"]
            self.assertEqual(recognition["category"], "")
            self.assertEqual(recognition["category_status"], "needs_action")
            self.assertEqual(recognition["tmdb_id"], "")
            self.assertEqual(recognition["organized_parent_id"], "unmapped-parent")

    def test_recognizing_stage_uses_tmdb_hint_when_parent_unmapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdb = FakeTmdbHintResolver()
            workflow = self._workflow(tmp, tmdb_resolver=tmdb)
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="无耻之徒 (2011) [tmdbid=34307]",
            ) or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="W-无耻之徒-2011-[tmdb=34307]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "folder-id",
                        "file_name": "W-无耻之徒-2011-[tmdb=34307]",
                        "parent_id": "unmapped-parent",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            recognition = bridge.parse_recognition_json(stored)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "外国电视")
            self.assertEqual(result.metadata["tmdb_id"], "34307")
            self.assertEqual(recognition["category"], "外国电视")
            self.assertEqual(recognition["category_status"], "tmdb_resolved")
            self.assertEqual(stored["category_choice"], "外国电视")
            self.assertEqual(stored["category_status"], "tmdb_resolved")
            self.assertEqual(tmdb.lookups[0][0], "34307")
            self.assertEqual(self.telegram.messages, [])

    def test_recognizing_stage_defers_for_cms_direct_strm_signal_when_category_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv_root = Path(tmp) / "library" / "tvcn"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"国产电视": tv_root}),
            )
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="翘楚 (2026)") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="Q-翘楚-2026-[tmdb=289271]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "folder-id",
                        "file_name": "Q-翘楚-2026-[tmdb=289271]",
                        "parent_id": "unmapped-parent",
                    },
                },
                row["id"],
            )

            waiting = workflow.run_stage(task)
            self._write_strm(tv_root / "Q-翘楚-2026-[tmdb=289271]" / "Season 01", content="http://cms/d/direct-link/ep01.mp4")
            resolved = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(waiting.outcome, StageOutcome.DEFER)
            self.assertIn("等待 CMS 直链 STRM 分类", waiting.message)
            self.assertEqual(self.telegram.messages, [])
            self.assertEqual(resolved.outcome, StageOutcome.COMPLETE)
            self.assertEqual(resolved.metadata["category"], "国产电视")
            self.assertEqual(stored["category_choice"], "国产电视")
            self.assertEqual(stored["category_status"], "self_share_resolved")

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

    def test_recognizing_stage_uses_remembered_manual_parent_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            classifier = FakeClassifier()
            workflow = self._workflow(tmp, openai_classifier=classifier, tmdb_resolver=FakeTmdbResolver())
            self.submissions.remember_parent_category("unmapped-parent", "国产电视", source="manual")
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="太行谣 (2026) {tmdb-323682}") or row
            row = self.submissions.update_self_share(int(row["id"]), workflow_mode="self_share_sync") or row
            organized_folder = {
                "file_id": "folder-id",
                "file_name": "T-太行谣-2026-[tmdb=323682]",
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
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "国产电视")
            self.assertEqual(stored["category_choice"], "国产电视")
            self.assertEqual(stored["category_status"], "self_share_resolved")
            self.assertEqual(classifier.calls, [])
            self.assertEqual(self.telegram.messages, [])

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

    def test_recognizing_stage_reuses_manual_prompt_without_recalling_openai_or_tmdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            classifier = FakeClassifier(confidence=0.5)
            tmdb = FakeTmdbResolver()
            workflow = self._workflow(tmp, openai_classifier=classifier, tmdb_resolver=tmdb)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="Suggest.Show.S01.2025") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="Suggest.Show.S01.2025",
            ) or row
            first_task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "folder-id",
                        "file_name": "Suggest.Show.S01.2025",
                        "parent_id": "unmapped-parent",
                    },
                },
                row["id"],
            )

            first = workflow.run_stage(first_task)
            second_task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {"submission_id": row["id"]},
                row["id"],
            )
            second = workflow.run_stage(second_task)

            self.assertEqual(first.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(second.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(classifier.calls, [])
            self.assertEqual(tmdb.lookups, [])
            self.assertEqual(tmdb.searches, [])
            self.assertEqual(second.metadata["recognition"]["category_status"], "needs_action")
            self.assertEqual(second.metadata["recognition"].get("category_suggestion"), None)

    def test_recognizing_stage_prompts_telegram_category_keyboard_without_openai_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            classifier = FakeClassifier(confidence=0.5)
            workflow = self._workflow(tmp, openai_classifier=classifier, tmdb_resolver=FakeTmdbResolver())
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="Suggest.Show.S01.2025") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="Suggest.Show.S01.2025",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "folder-id",
                        "file_name": "Suggest.Show.S01.2025",
                        "parent_id": "unmapped-parent",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(len(self.telegram.messages), 1)
            chat_id, text, reply_markup = self.telegram.messages[0]
            self.assertEqual(chat_id, "chat-id")
            self.assertIn("CMS 未能确定分类", text)
            self.assertNotIn("OpenAI建议", text)
            self.assertIn("请选择分类", text)
            self.assertEqual(reply_markup, bridge.category_keyboard(int(row["id"])))
            self.assertEqual(classifier.calls, [])

    def test_recognizing_stage_uses_manually_selected_category_after_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, openai_classifier=FakeClassifier(confidence=0.5), tmdb_resolver=FakeTmdbResolver())
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="Manual.Show.S01.2025") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="Manual.Show.S01.2025",
            ) or row
            row = self.submissions.update_category(int(row["id"]), "国产电视", "selected") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.RECOGNIZING,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "folder-id",
                        "file_name": "Manual.Show.S01.2025",
                        "parent_id": "unmapped-parent",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["category"], "国产电视")
            self.assertEqual(self.telegram.messages, [])

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

    def test_own_share_stage_recreates_share_when_dest_children_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115.files_by_parent["folder-id"] = [
                {"cid": "season-1", "n": "Season 01", "pid": "folder-id"},
                {"cid": "season-2", "n": "Season 02", "pid": "folder-id"},
                {"cid": "season-3", "n": "Season 03", "pid": "folder-id"},
            ]
            row = self._self_share_row()
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {
                    "submission_id": row["id"],
                    "own_share_file_id": "folder-id",
                    "own_share_child_ids": ["season-2"],
                    "operation_generation": 0,
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.created_shares, ["folder-id"])
            self.assertEqual(result.metadata["operation_generation"], 1)
            self.assertEqual(
                sorted(result.metadata["own_share_child_ids"]),
                ["season-1", "season-2", "season-3"],
            )
            self.assertEqual(stored["own_share_code"], "owncode")

    def test_create_share_crash_recovers_by_saved_title_without_second_send(self):
        class RecoveringCreateP115(FakeP115):
            def __init__(self):
                super().__init__()
                self.settings = []
                self.recovery_queries = []
                self.remote_share = None

            def create_share(self, file_id):
                self.created_shares.append(file_id)
                self.remote_share = {
                    "share_code": "recovered-code",
                    "receive_code": "generated-code",
                    "share_url": "https://115cdn.com/s/recovered-code",
                    "create_time": "1000.0",
                }
                return dict(self.remote_share)

            def find_own_share_by_title(self, title, min_create_time=0):
                self.recovery_queries.append((title, min_create_time))
                return dict(self.remote_share) if self.remote_share else None

            def ensure_share_settings(self, share_code, receive_code):
                self.settings.append((share_code, receive_code))
                return {"share_code": share_code, "receive_code": receive_code}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115 = RecoveringCreateP115()
            workflow.p115 = self.p115
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.record_event(
                task.id,
                TaskStage.OWN_SHARE_CREATED,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=row["id"],
                metadata_patch={"submission_id": row["id"]},
            )
            self.tasks.enqueue_task(task.id, TaskStage.OWN_SHARE_CREATED, next_run_at=0)
            clock = [1000.0]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(self.tasks, workflow, worker_id="share-create-crash", now=lambda: clock[0])
            complete_operation = self.tasks.complete_operation
            save_attempts = 0

            def fail_first_result_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving create result")
                return complete_operation(*args, **kwargs)

            with patch.object(self.tasks, "complete_operation", side_effect=fail_first_result_save):
                runner.run_once()
                self.tasks.enqueue_task(task.id, TaskStage.OWN_SHARE_CREATED, next_run_at=0)
                runner.run_once()

            recovered = self.tasks.find_task(task.id)
            operation = self.tasks.find_operation(task.id, self._create_share_operation_key(task, "folder-id"))
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(recovered.current_stage, TaskStage.SHARE_VALIDATED)
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(operation.request["share_title"], "S-双喜-2025-[tmdb=123456]")
            self.assertEqual(operation.request["requested_at"], 1000.0)
            self.assertEqual(self.p115.created_shares, ["folder-id"])
            self.assertEqual(
                self.p115.recovery_queries,
                [("S-双喜-2025-[tmdb=123456]", 1000.0)],
            )
            self.assertEqual(self.p115.settings, [("recovered-code", "1212")])
            self.assertEqual(stored["own_share_code"], "recovered-code")

    def test_create_share_recovery_refuses_interleaved_same_title_candidates(self):
        class InterleavedCreateP115(FakeP115):
            def __init__(self):
                super().__init__()
                self.remote_shares = []
                self.recovery_queries = []
                self.settings = []

            def create_share(self, file_id):
                self.created_shares.append(file_id)
                created = {
                    "share_code": "task-a-share",
                    "receive_code": "generated-a",
                    "share_url": "https://115cdn.com/s/task-a-share",
                    "create_time": "1000.0",
                }
                self.remote_shares.append(created)
                return dict(created)

            def find_own_share_by_title(self, title, min_create_time=0):
                self.recovery_queries.append((title, min_create_time))
                eligible = [
                    share
                    for share in self.remote_shares
                    if float(share["create_time"]) >= float(min_create_time)
                ]
                if len(eligible) > 1:
                    return {"recovery_status": "ambiguous", "match_count": len(eligible)}
                return dict(eligible[0]) if eligible else None

            def ensure_share_settings(self, share_code, receive_code):
                self.settings.append((share_code, receive_code))
                return {"share_code": share_code, "receive_code": receive_code}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.p115 = InterleavedCreateP115()
            workflow.p115 = self.p115
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="Same title",
            ) or row
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.record_event(
                task.id,
                TaskStage.OWN_SHARE_CREATED,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=row["id"],
                metadata_patch={"submission_id": row["id"]},
            )
            self.tasks.enqueue_task(task.id, TaskStage.OWN_SHARE_CREATED, next_run_at=0)
            workflow._now = lambda: 1000.0
            runner = TaskRunner(self.tasks, workflow, worker_id="same-title-a", now=lambda: 1000.0)
            complete_operation = self.tasks.complete_operation
            save_attempts = 0

            def fail_first_result_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving task A share")
                return complete_operation(*args, **kwargs)

            with patch.object(self.tasks, "complete_operation", side_effect=fail_first_result_save):
                with self.assertLogs("app.task_runner", level="WARNING"):
                    runner.run_once()

                self.p115.remote_shares.append(
                    {
                        "share_code": "task-b-share",
                        "receive_code": "generated-b",
                        "share_url": "https://115cdn.com/s/task-b-share",
                        "create_time": "1001.0",
                    }
                )
                self.tasks.enqueue_task(task.id, TaskStage.OWN_SHARE_CREATED, next_run_at=0)
                runner.run_once()

            recovered = self.tasks.find_task(task.id)
            operation = self.tasks.find_operation(task.id, self._create_share_operation_key(task, "folder-id"))
            stored = self.submissions.find_by_id(int(row["id"]))

        self.assertEqual(recovered.status, TaskStatus.NEEDS_ACTION)
        self.assertEqual(recovered.current_stage, TaskStage.OWN_SHARE_CREATED)
        self.assertIn("同名", recovered.error_summary)
        self.assertEqual(operation.status, "started")
        self.assertEqual(self.p115.created_shares, ["folder-id"])
        self.assertEqual(self.p115.settings, [])
        self.assertFalse(stored.get("own_share_code"))

    def test_direct_create_share_crash_recovers_by_actual_filename_without_second_send(self):
        class RecoveringDirectP115(FakeP115):
            def __init__(self):
                super().__init__()
                self.remote_share = None
                self.recovery_queries = []

            def create_share(self, file_id):
                self.created_shares.append(file_id)
                if file_id == "series-id":
                    raise RuntimeError("目录不存在或已转移")
                self.remote_share = {
                    "share_code": "direct-code",
                    "receive_code": "generated-code",
                    "share_url": "https://115cdn.com/s/direct-code",
                    "create_time": "1000.0",
                }
                return dict(self.remote_share)

            def find_own_share_by_title(self, title, min_create_time=0):
                self.recovery_queries.append((title, min_create_time))
                if title != "Actual Episode S03E03.mkv":
                    return None
                return dict(self.remote_share) if self.remote_share else None

            def ensure_share_settings(self, share_code, receive_code):
                return {"share_code": share_code, "receive_code": "1212"}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = RecoveringDirectP115()
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="organized_found",
                own_share_file_id="series-id",
                own_share_file_name="Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
            ) or row
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.record_event(
                task.id,
                TaskStage.OWN_SHARE_CREATED,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=row["id"],
                metadata_patch={
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "series-id",
                        "direct_file_id": "episode-id",
                        "direct_file_name": "Actual Episode S03E03.mkv",
                        "direct_parent_id": "season-id",
                        "direct_relative_path": "Season 03/Episode S03E03.strm",
                    },
                },
            )
            self.tasks.enqueue_task(task.id, TaskStage.OWN_SHARE_CREATED, next_run_at=0)
            workflow._now = lambda: 1000.999
            runner = TaskRunner(self.tasks, workflow, worker_id="direct-create-crash", now=lambda: 1000.999)
            complete_operation = self.tasks.complete_operation
            save_attempts = 0

            def fail_first_result_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving direct create result")
                return complete_operation(*args, **kwargs)

            with patch.object(self.tasks, "complete_operation", side_effect=fail_first_result_save):
                runner.run_once()
                self.tasks.enqueue_task(task.id, TaskStage.OWN_SHARE_CREATED, next_run_at=0)
                runner.run_once()

            recovered = self.tasks.find_task(task.id)
            operation = self.tasks.find_operation(task.id, self._create_share_operation_key(task, "episode-id"))
            self.assertEqual(recovered.current_stage, TaskStage.SHARE_VALIDATED)
            self.assertEqual(workflow.p115.created_shares, ["series-id", "episode-id"])
            self.assertEqual(workflow.p115.recovery_queries, [("Actual Episode S03E03.mkv", 1000.0)])
            self.assertEqual(operation.request["share_title"], "Actual Episode S03E03.mkv")
            self.assertEqual(operation.request["requested_at"], 1000.0)
            self.assertEqual(operation.request["direct_file_share_parent_id"], "season-id")
            self.assertTrue(recovered.metadata["direct_file_share"])

    def test_saved_share_row_reconstructs_created_at_after_stage_result_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow._now = lambda: 1000.999
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"]},
                row["id"],
            )
            own_share_metadata = workflow._own_share_metadata
            metadata_calls = 0

            def lose_first_stage_result(*args, **kwargs):
                nonlocal metadata_calls
                metadata_calls += 1
                if metadata_calls == 1:
                    raise RuntimeError("simulated crash after submission update")
                return own_share_metadata(*args, **kwargs)

            with patch.object(workflow, "_own_share_metadata", side_effect=lose_first_stage_result):
                with self.assertRaisesRegex(RuntimeError, "after submission update"):
                    workflow.run_stage(task)
                recovered = workflow.run_stage(task)

            operation = self.tasks.find_operation(task.id, self._create_share_operation_key(task, "folder-id"))
            self.assertEqual(operation.request["requested_at"], 1000.0)
            self.assertEqual(recovered.metadata["share_created_at"], 1000.0)

            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            cleanup_task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], **recovered.metadata},
                row["id"],
            )
            workflow._now = lambda: 1001.0

            review = workflow.run_stage(cleanup_task)

            self.assertEqual(review.outcome, StageOutcome.DEFER)
            self.assertNotIn("缺少自有分享创建时间", review.message)

    def test_direct_file_fallback_uses_distinct_create_operation_key(self):
        class FolderGoneP115(FakeP115):
            def __init__(self):
                super().__init__()
                self.settings = []

            def create_share(self, file_id):
                self.created_shares.append(file_id)
                if file_id == "series-id":
                    raise RuntimeError("目录不存在或已转移")
                return {
                    "share_code": "file-share",
                    "receive_code": "generated-code",
                    "share_url": "https://115cdn.com/s/file-share",
                }

            def ensure_share_settings(self, share_code, receive_code):
                self.settings.append((share_code, receive_code))
                return {"share_code": share_code, "receive_code": receive_code}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = FolderGoneP115()
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="organized_found",
                own_share_file_id="series-id",
                own_share_file_name="Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "series-id",
                        "direct_file_id": "episode-id",
                        "direct_relative_path": "Season 03/龙族.S03E03.strm",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            folder_operation = self.tasks.find_operation(
                task.id,
                self._create_share_operation_key(task, "series-id"),
            )
            file_operation = self.tasks.find_operation(
                task.id,
                self._create_share_operation_key(task, "episode-id"),
            )
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(folder_operation.status, "failed")
            self.assertEqual(file_operation.status, "succeeded")
            self.assertTrue(file_operation.operation_key.endswith(":episode-id"))
            self.assertEqual(workflow.p115.created_shares, ["series-id", "episode-id"])
            self.assertEqual(workflow.p115.settings, [("file-share", "1212")])

    def test_create_share_lost_start_race_never_sends(self):
        class ReconcilingP115(FakeP115):
            def find_own_share_by_title(self, title, min_create_time=0):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = ReconcilingP115()
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"]},
                row["id"],
            )
            operation_key = self._create_share_operation_key(task, "folder-id")
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "create_share",
                {
                    "file_id": "folder-id",
                    "share_title": "S-双喜-2025-[tmdb=123456]",
                    "receive_code": "1212",
                    "requested_at": 1000.0,
                },
            )
            start_operation = self.tasks.start_operation

            def lose_start_race(*args, **kwargs):
                start_operation(*args, **kwargs)
                return None

            with patch.object(self.tasks, "start_operation", side_effect=lose_start_race):
                result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(workflow.p115.created_shares, [])

    def test_create_share_succeeded_operation_reuses_saved_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"]},
                row["id"],
            )
            operation_key = self._create_share_operation_key(task, "folder-id")
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "create_share",
                {
                    "file_id": "folder-id",
                    "share_title": "S-双喜-2025-[tmdb=123456]",
                    "receive_code": "1212",
                    "requested_at": 1000.0,
                },
            )
            self.tasks.start_operation(task.id, operation_key)
            self.tasks.complete_operation(
                task.id,
                operation_key,
                {
                    "share_code": "saved-code",
                    "receive_code": "generated-code",
                    "share_url": "https://115cdn.com/s/saved-code",
                },
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(workflow.p115.created_shares, [])
            self.assertEqual(self.submissions.find_by_id(int(row["id"]))["own_share_code"], "saved-code")

    def test_own_share_stage_defers_when_115_is_still_creating_share(self):
        from app.clients.p115 import P115SharePendingError

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"]},
                row["id"],
            )
            workflow.p115.create_share = Mock(side_effect=P115SharePendingError("processing"))

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(result.delay_seconds, 1800)
            self.assertIn("等待 115 完成分享创建", result.message)
            self.assertEqual(result.metadata["share_create_status"], "pending")

    def test_own_share_stage_recovers_share_code_after_async_creation(self):
        class RecoveringP115(FakeP115):
            def find_own_share_by_title(self, title, min_create_time=0):
                self.recovery_query = (title, min_create_time)
                return {
                    "share_code": "recovered",
                    "receive_code": "1212",
                    "share_url": "https://115cdn.com/s/recovered",
                    "create_time": "123.0",
                }

            def ensure_share_settings(self, share_code, receive_code):
                return {"share_code": share_code, "receive_code": receive_code}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = RecoveringP115()
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {
                    "submission_id": row["id"],
                    "share_create_status": "pending",
                    "share_create_requested_at": 100.0,
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_code"], "recovered")
            self.assertEqual(stored["own_share_receive_code"], "1212")
            self.assertEqual(result.metadata["share_created_at"], 123.0)
            self.assertEqual(workflow.p115.created_shares, [])
            self.assertEqual(workflow.p115.recovery_query[0], "S-双喜-2025-[tmdb=123456]")

    def test_share_sync_stage_waits_for_another_task_to_finish_cms_share_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            waiting = self.tasks.upsert_task("previous", "1111", "https://115cdn.com/s/previous?password=1111")
            self.tasks.record_event(
                waiting.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM 源目录生成",
                next_run_at=10.0,
            )
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="owncode",
                own_share_receive_code="ownpwd",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_SYNC_SUBMITTED,
                {"submission_id": row["id"]},
                row["id"],
            )

            with patch.object(
                workflow.task_store,
                "list_recent_tasks",
                side_effect=AssertionError("share sync wait must use a SQL existence query"),
            ):
                result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待上一条 CMS 分享同步完成", result.message)
            self.assertEqual(result.metadata["share_sync_wait_task_id"], waiting.id)
            self.assertEqual(self.cms.share_sync_calls, [])

    def test_cms_share_sync_crash_advances_to_expected_strm_without_second_post(self):
        class CrashAfterPostCms(FakeCms):
            def add_share115_sync_task(self, own_code, own_pwd, cid, local_path):
                super().add_share115_sync_task(own_code, own_pwd, cid, local_path)
                if len(self.share_sync_calls) == 1:
                    raise RuntimeError("simulated crash after CMS accepted sync")
                return {"code": 200}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.cms = CrashAfterPostCms()
            workflow.cms = self.cms
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
                own_share_code="owncode",
                own_share_receive_code="ownpwd",
            ) or row
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.record_event(
                task.id,
                TaskStage.SHARE_SYNC_SUBMITTED,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=row["id"],
                metadata_patch={"submission_id": row["id"]},
            )
            self.tasks.enqueue_task(task.id, TaskStage.SHARE_SYNC_SUBMITTED, next_run_at=0)
            clock = [1000.0]
            runner = TaskRunner(self.tasks, workflow, worker_id="cms-sync-crash", now=lambda: clock[0])

            runner.run_once()
            interrupted = self.tasks.find_task(task.id)
            operation = self.tasks.find_operation(task.id, self._cms_share_sync_operation_key(task))
            self.assertEqual(interrupted.status, TaskStatus.FAILED)
            self.assertEqual(operation.status, "started")
            self.assertEqual(len(self.cms.share_sync_calls), 1)

            self.tasks.enqueue_task(task.id, TaskStage.SHARE_SYNC_SUBMITTED, next_run_at=0)
            runner.run_once()

            waiting = self.tasks.find_task(task.id)
            operation = self.tasks.find_operation(task.id, self._cms_share_sync_operation_key(task))
            self.assertEqual(waiting.current_stage, TaskStage.STRM_READY)
            self.assertEqual(waiting.metadata["cms_share_sync_outcome"], "unknown")
            self.assertEqual(operation.status, "uncertain")
            self.assertEqual(len(self.cms.share_sync_calls), 1)

            self._write_strm(self.config.strm_root / row["own_share_file_name"])
            runner.run_once()

            advanced = self.tasks.find_task(task.id)
            self.assertEqual(advanced.current_stage, TaskStage.CMS_DELETE_SETTLED)
            self.assertEqual(len(self.cms.share_sync_calls), 1)

    def test_cms_share_sync_unknown_outcome_times_out_without_second_post(self):
        class CrashAfterPostCms(FakeCms):
            def add_share115_sync_task(self, own_code, own_pwd, cid, local_path):
                super().add_share115_sync_task(own_code, own_pwd, cid, local_path)
                raise RuntimeError("simulated crash after CMS accepted sync")

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            self.cms = CrashAfterPostCms()
            workflow.cms = self.cms
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
                own_share_code="owncode",
                own_share_receive_code="ownpwd",
            ) or row
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.record_event(
                task.id,
                TaskStage.SHARE_SYNC_SUBMITTED,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=row["id"],
                metadata_patch={"submission_id": row["id"]},
            )
            self.tasks.enqueue_task(task.id, TaskStage.SHARE_SYNC_SUBMITTED, next_run_at=0)
            clock = [1000.0]
            runner = TaskRunner(self.tasks, workflow, worker_id="cms-sync-timeout", now=lambda: clock[0])

            runner.run_once()
            self.tasks.enqueue_task(task.id, TaskStage.SHARE_SYNC_SUBMITTED, next_run_at=0)
            runner.run_once()

            for _attempt in range(20):
                clock[0] += 300
                runner.run_once()

            timed_out = self.tasks.find_task(task.id)
            self.assertEqual(timed_out.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(timed_out.current_stage, TaskStage.NEEDS_ACTION)
            self.assertEqual(timed_out.metadata["cms_share_sync_outcome"], "unknown")
            self.assertEqual(len(self.cms.share_sync_calls), 1)

    def test_cms_share_sync_lost_start_race_never_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="owncode",
                own_share_receive_code="ownpwd",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_SYNC_SUBMITTED,
                {"submission_id": row["id"]},
                row["id"],
            )
            operation_key = self._cms_share_sync_operation_key(task)
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "cms_share_sync",
                {
                    "share_code": "owncode",
                    "receive_code": "ownpwd",
                    "cid": "0",
                    "local_path": "/media/share",
                },
            )
            start_operation = self.tasks.start_operation

            def lose_start_race(*args, **kwargs):
                start_operation(*args, **kwargs)
                return None

            with patch.object(self.tasks, "start_operation", side_effect=lose_start_race):
                result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["cms_share_sync_outcome"], "unknown")
            self.assertEqual(self.cms.share_sync_calls, [])

    def test_cms_share_sync_succeeded_operation_never_posts_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_code="owncode",
                own_share_receive_code="ownpwd",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_SYNC_SUBMITTED,
                {"submission_id": row["id"]},
                row["id"],
            )
            operation_key = self._cms_share_sync_operation_key(task)
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "cms_share_sync",
                {
                    "share_code": "owncode",
                    "receive_code": "ownpwd",
                    "cid": "0",
                    "local_path": "/media/share",
                },
            )
            self.tasks.start_operation(task.id, operation_key)
            self.tasks.complete_operation(task.id, operation_key, {"code": 200})

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["cms_share_sync_outcome"], "submitted")
            self.assertEqual(self.cms.share_sync_calls, [])

    def test_own_share_stage_rejects_unvalidated_received_file_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-snapshot-id"],
                    "own_share_file_id": "share-snapshot-id",
                    "organized_folder": {
                        "file_id": "share-snapshot-id",
                        "file_name": "基督山伯爵士 4K原盘REMUX [HDR]",
                        "parent_id": "pending-cid",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("可验证", result.message)
            self.assertEqual(self.p115.created_shares, [])

    def test_own_share_stage_rejects_received_file_id_without_folder_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, receive_cid="pending-cid")
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
                own_share_file_id="recv-root-id",
                own_share_file_name="基督山伯爵士 4K原盘REMUX [HDR]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-snapshot-id"],
                    "own_share_file_id": "recv-root-id",
                    "receive_target_cid": "pending-cid",
                    "organized_folder": {
                        "file_id": "recv-root-id",
                        "file_name": "基督山伯爵士 4K原盘REMUX [HDR]",
                        "parent_id": "pending-cid",
                    },
                    "intake_identity": {
                        "root_ids": ["recv-root-id"],
                        "files": [{"id": "video-fid-monte", "name": "Monte.Cristo.mkv"}],
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("可验证", result.message)
            self.assertEqual(self.p115.created_shares, [])

    def test_share_alias_stage_preserves_cms_folder_name_without_creating_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_phase="organized_found",
                own_share_code="",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_ALIAS_PREPARED,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["own_share_file_name"], "S-双喜-2025-[tmdb=123456]")
            self.assertFalse(stored["share_alias_name"])
            self.assertFalse(stored["canonical_manifest_json"])
            self.assertEqual(self.p115.renamed, [])

    def test_share_alias_stage_does_not_probe_legacy_rename_fallback(self):
        class FolderGoneP115(FakeP115):
            def rename_file(self, file_id, file_name):
                raise RuntimeError("分享的文件(夹)已被移动或删除")

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = FolderGoneP115()
            row = self._self_share_row()
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_ALIAS_PREPARED,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "folder-id",
                        "file_name": row["own_share_file_name"],
                        "direct_file_id": "episode-id",
                        "direct_relative_path": "Season 03/Episode 02.strm",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertNotIn("direct_file_share_fallback", result.metadata)
            self.assertFalse(stored["share_alias_name"])

    def test_share_validation_violation_keeps_existing_neutral_alias_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row(title="T-特洛伊-2004-[tmdb=652]", category="欧美电影", tmdb_id="652")
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_phase="own_share_created",
                share_alias_name="asset-1-folder",
                share_alias_level=1,
                canonical_manifest_json=json.dumps(
                    {
                        "version": 1,
                        "root_name": row["own_share_file_name"],
                        "alias_name": "asset-1-folder",
                        "category": "欧美电影",
                        "tmdb_id": "652",
                        "entries": [],
                    },
                    ensure_ascii=False,
                ),
            ) or row
            self.p115.share_statuses = [
                {"available": True, "share_state": "0", "have_vio_file": True},
            ]
            self.p115.files_by_parent = {
                "folder-id": [{"cid": "season-id", "n": "Season 03"}],
                "season-id": [{"fid": "episode-id", "n": "Troy.S03E02.2160p.mkv"}],
            }
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_VALIDATED,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(stored["share_alias_level"], 1)
            self.assertEqual(self.p115.renamed, [])
            self.assertEqual(stored["own_share_code"], "owncode")
            self.assertEqual(stored["share_validation_status"], "invalid")
            self.assertEqual(result.metadata["share_review_status"], "invalid")

    def test_level_two_violation_warning_stops_without_rebuilding_or_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            row = self._self_share_row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                share_alias_name="asset-1-folder",
                share_alias_level=2,
                share_validation_status="pending",
            ) or row
            self.p115.share_statuses = [
                {"available": True, "share_state": "0", "have_vio_file": True},
            ]
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_VALIDATED,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(stored["share_validation_status"], "invalid")
            self.assertEqual(cleanup.deleted, [])
            self.assertNotEqual(stored["cleanup_status"], "deleted")

    def test_share_validation_keeps_source_until_review_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            row = self._self_share_row()
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_VALIDATED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])
            self.assertEqual(result.metadata["share_review_status"], "pending")
            self.assertNotEqual(stored["cleanup_status"], "deleted")

    def test_share_validation_violation_stops_without_renaming_or_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            row = self._self_share_row()
            self.p115.share_statuses = [
                {"available": True, "share_state": "0", "have_vio_file": True},
            ]
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.SHARE_VALIDATED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(cleanup.deleted, [])
            self.assertEqual(self.p115.renamed, [])

    def test_cleaned_stage_waits_for_review_checkpoints_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            config = bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path(tmp) / "share-strm",
                cms_cid="0",
                cms_local_path="/media/share",
                review_grace_seconds=10,
                review_checkpoints_seconds=(5, 10),
                review_list_cache_seconds=300,
            )
            workflow = self._workflow(tmp, cleanup_client=cleanup, self_share_config=config)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 100.0

            before_first = workflow.run_stage(task)
            self.tasks.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                before_first.message,
                metadata_patch=before_first.metadata,
                submission_id=row["id"],
                expected_stage=task.current_stage,
                expected_status=TaskStatus.RUNNING,
                expected_claimed_by=task.claimed_by,
                expected_claimed_at=task.claimed_at,
                expected_claim_token=task.claim_token,
                expected_updated_at=task.updated_at,
            )
            self.tasks.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=105.0)
            task = self.tasks.claim_next_runnable("worker-1", now=105.0)
            workflow._now = lambda: 105.0
            after_first = workflow.run_stage(task)
            self.tasks.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                after_first.message,
                metadata_patch=after_first.metadata,
                submission_id=row["id"],
                expected_stage=task.current_stage,
                expected_status=TaskStatus.RUNNING,
                expected_claimed_by=task.claimed_by,
                expected_claimed_at=task.claimed_at,
                expected_claim_token=task.claim_token,
                expected_updated_at=task.updated_at,
            )
            self.tasks.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=110.0)
            task = self.tasks.claim_next_runnable("worker-2", now=110.0)
            workflow._now = lambda: 110.0
            after_final = workflow.run_stage(task)

            self.assertEqual(before_first.outcome, StageOutcome.DEFER)
            self.assertEqual(after_first.outcome, StageOutcome.DEFER)
            self.assertEqual(after_final.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])

    def test_delete_source_crash_reconciles_absence_without_second_delete(self):
        class AbsenceAwareCleanup(FakeCleanupClient):
            def __init__(self):
                super().__init__()
                self.files = {"source-parent": {"folder-id"}}
                self.existence_checks = []

            def delete_file(self, file_id):
                super().delete_file(file_id)
                self.files["source-parent"].discard(file_id)
                return {"state": True}

            def file_exists_in_parent(self, file_id, parent_id):
                self.existence_checks.append((file_id, parent_id))
                return file_id in self.files.get(parent_id, set())

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = AbsenceAwareCleanup()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._cleanup_ready_row(tmp)
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=row["id"],
                metadata_patch={
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
            )
            self.tasks.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=0)
            clock = [time.time()]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(self.tasks, workflow, worker_id="delete-source-crash", now=lambda: clock[0])
            complete_operation = self.tasks.complete_operation
            save_attempts = 0

            def fail_first_result_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving delete result")
                return complete_operation(*args, **kwargs)

            with patch.object(self.tasks, "complete_operation", side_effect=fail_first_result_save):
                runner.run_once()
                self.tasks.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=0)
                runner.run_once()

            operation = self.tasks.find_operation(
                task.id,
                self._delete_operation_key(task, "delete_source", "folder-id"),
            )
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(operation.request["parent_id"], "source-parent")
            self.assertEqual(cleanup.deleted, ["folder-id"])
            self.assertEqual(cleanup.existence_checks, [("folder-id", "source-parent")])
            self.assertEqual(stored["cleanup_status"], "deleted")

    def test_quality_cleanup_crash_reconciles_absence_without_second_delete(self):
        class AbsenceAwareCleanup(FakeCleanupClient):
            def __init__(self):
                super().__init__()
                self.present = {"folder-id"}
                self.existence_checks = []

            def delete_file(self, file_id):
                super().delete_file(file_id)
                self.present.discard(file_id)
                return {"state": True}

            def file_exists_in_parent(self, file_id, parent_id):
                self.existence_checks.append((file_id, parent_id))
                return file_id in self.present

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = AbsenceAwareCleanup()
            self._workflow(tmp, cleanup_client=cleanup)
            row = self._cleanup_ready_row(tmp)
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            task = self.tasks.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "cleanup ready",
                submission_id=row["id"],
                metadata_patch={
                    "submission_id": row["id"],
                    "share_review_status": "passed",
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
            )
            adapter = bridge._QualityRepairAdapter(self.submissions, self.tasks, cleanup)
            complete_operation = self.tasks.complete_operation
            save_attempts = 0

            def fail_first_result_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving quality delete")
                return complete_operation(*args, **kwargs)

            with patch.object(self.tasks, "complete_operation", side_effect=fail_first_result_save):
                with self.assertRaisesRegex(RuntimeError, "saving quality delete"):
                    adapter.cleanup(task, "quality-delete-crash")
                self.assertTrue(adapter.cleanup(task, "quality-delete-recovery"))

            operation = self.tasks.find_operation(
                task.id,
                self._delete_operation_key(task, "delete_source", "folder-id"),
            )
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(cleanup.deleted, ["folder-id"])
            self.assertEqual(cleanup.existence_checks, [("folder-id", "source-parent")])
            self.assertEqual(stored["cleanup_status"], "deleted")

    def test_quality_cleanup_reuses_already_deleted_submission_without_parent_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._workflow(tmp)
            row = self._self_share_row()
            row = self.submissions.update_cleanup(int(row["id"]), "deleted", file_id="folder-id") or row
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            task = self.tasks.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "already cleaned",
                submission_id=row["id"],
                metadata_patch={"submission_id": row["id"], "share_review_status": "passed"},
            )
            adapter = bridge._QualityRepairAdapter(self.submissions, self.tasks)

            self.assertTrue(adapter.cleanup(task, "quality-already-cleaned"))

    def test_delete_source_recovery_defers_when_parent_listing_fails(self):
        class UnavailableListingCleanup(FakeCleanupClient):
            def file_exists_in_parent(self, file_id, parent_id):
                raise RuntimeError("115 list unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = UnavailableListingCleanup()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._cleanup_ready_row(tmp)
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
                row["id"],
            )
            operation_key = self._delete_operation_key(task, "delete_source", "folder-id")
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "delete_source",
                {"file_id": "folder-id", "parent_id": "source-parent"},
            )
            self.tasks.start_operation(task.id, operation_key)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(cleanup.deleted, [])

    def test_delete_source_recovery_needs_action_when_file_remains_after_deadline(self):
        class PresentCleanup(FakeCleanupClient):
            def file_exists_in_parent(self, file_id, parent_id):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = PresentCleanup()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._cleanup_ready_row(tmp)
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
                row["id"],
            )
            operation_key = self._delete_operation_key(task, "delete_source", "folder-id")
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "delete_source",
                {"file_id": "folder-id", "parent_id": "source-parent"},
            )
            started = self.tasks.start_operation(task.id, operation_key)
            workflow._now = lambda: started.started_at + 301

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(cleanup.deleted, [])

    def test_delete_source_known_not_found_counts_as_success(self):
        class MissingSourceCleanup(FakeCleanupClient):
            def delete_file(self, file_id):
                self.deleted.append(file_id)
                raise RuntimeError("文件不存在")

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = MissingSourceCleanup()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._cleanup_ready_row(tmp)
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            operation = self.tasks.find_operation(
                task.id,
                self._delete_operation_key(task, "delete_source", "folder-id"),
            )
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(cleanup.deleted, ["folder-id"])
            self.assertEqual(stored["cleanup_status"], "deleted")

    def test_delete_source_lost_start_race_never_deletes(self):
        class PresentCleanup(FakeCleanupClient):
            def file_exists_in_parent(self, file_id, parent_id):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = PresentCleanup()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._cleanup_ready_row(tmp)
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
                row["id"],
            )
            operation_key = self._delete_operation_key(task, "delete_source", "folder-id")
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "delete_source",
                {"file_id": "folder-id", "parent_id": "source-parent"},
            )
            start_operation = self.tasks.start_operation

            def lose_start_race(*args, **kwargs):
                start_operation(*args, **kwargs)
                return None

            with patch.object(self.tasks, "start_operation", side_effect=lose_start_race):
                result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(cleanup.deleted, [])

    def test_delete_source_succeeded_operation_never_deletes_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._cleanup_ready_row(tmp)
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "organized_folder": {"file_id": "folder-id", "parent_id": "source-parent"},
                },
                row["id"],
            )
            operation_key = self._delete_operation_key(task, "delete_source", "folder-id")
            self.tasks.prepare_operation(
                task.id,
                operation_key,
                "delete_source",
                {"file_id": "folder-id", "parent_id": "source-parent"},
            )
            self.tasks.start_operation(task.id, operation_key)
            self.tasks.complete_operation(task.id, operation_key, {"state": True})

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])
            self.assertEqual(self.submissions.find_by_id(int(row["id"]))["cleanup_status"], "deleted")

    def test_delete_residue_crash_reconciles_absence_without_second_delete(self):
        class ResidueCleanup(FakeCleanupClient):
            def __init__(self):
                super().__init__()
                self.present = {"residue-id"}

            def find_source_residue_files(self, *args, **kwargs):
                if "residue-id" not in self.present:
                    return []
                return [{"file_id": "residue-id", "parent_id": "residue-parent"}]

            def delete_file(self, file_id):
                super().delete_file(file_id)
                self.present.discard(file_id)
                return {"state": True}

            def file_exists_in_parent(self, file_id, parent_id):
                return file_id in self.present

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = ResidueCleanup()
            config = bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path(tmp) / "share-strm",
                cms_cid="0",
                cms_local_path="/media/share",
                source_cleanup_parent_ids={"residue-parent"},
            )
            workflow = self._workflow(tmp, cleanup_client=cleanup, self_share_config=config)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._cleanup_ready_row(tmp)
            row = self.submissions.update_cleanup(int(row["id"]), "deleted", file_id="folder-id") or row
            task = self.tasks.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            self.tasks.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "metadata",
                submission_id=row["id"],
                metadata_patch={"submission_id": row["id"], "share_created_at": 100.0},
            )
            self.tasks.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=0)
            clock = [time.time()]
            workflow._now = lambda: clock[0]
            runner = TaskRunner(self.tasks, workflow, worker_id="delete-residue-crash", now=lambda: clock[0])
            complete_operation = self.tasks.complete_operation
            save_attempts = 0

            def fail_first_result_save(*args, **kwargs):
                nonlocal save_attempts
                save_attempts += 1
                if save_attempts == 1:
                    raise RuntimeError("simulated crash while saving residue delete")
                return complete_operation(*args, **kwargs)

            with patch.object(self.tasks, "complete_operation", side_effect=fail_first_result_save):
                runner.run_once()
                self.tasks.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=0)
                runner.run_once()

            operation = self.tasks.find_operation(
                task.id,
                self._delete_operation_key(task, "delete_residue", "residue-id"),
            )
            self.assertEqual(operation.status, "succeeded")
            self.assertEqual(cleanup.deleted, ["residue-id"])

    def test_cleaned_stage_skips_review_when_web_setting_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            self.tasks.set_self_share_review_mode_override("off")
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 100.0

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["share_review_status"], "passed")
            self.assertEqual(cleanup.deleted, [])

    def test_cleaned_stage_uses_ten_minute_web_setting_over_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            config = bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path(tmp) / "share-strm",
                cms_cid="0",
                cms_local_path="/media/share",
                review_grace_seconds=86400,
                review_checkpoints_seconds=(600, 3600, 21600, 86400),
                review_list_cache_seconds=300,
            )
            workflow = self._workflow(tmp, cleanup_client=cleanup, self_share_config=config)
            self.tasks.set_self_share_review_mode_override("ten_minutes")
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 700.0

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["share_review_checks"], [600])
            self.assertEqual(cleanup.deleted, [])

    def test_cleaned_stage_keeps_source_when_async_review_marks_share_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            config = bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path(tmp) / "share-strm",
                cms_cid="0",
                cms_local_path="/media/share",
                review_grace_seconds=1,
                review_checkpoints_seconds=(1,),
                review_list_cache_seconds=300,
            )
            workflow = self._workflow(tmp, cleanup_client=cleanup, self_share_config=config)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            self.p115.share_list_states = {
                "owncode": {"share_state": "6", "have_vio_file": False},
            }
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 101.0

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(cleanup.deleted, [])
            self.assertEqual(stored["share_validation_status"], "invalid")
            self.assertEqual(result.metadata["share_review_status"], "invalid")

    def test_cleaned_stage_defers_unknown_review_without_deleting_source(self):
        class UnavailableListP115(FakeP115):
            def list_own_share_states(self, limit=100):
                raise RuntimeError("115 service unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            config = bridge.SelfShareConfig(
                enabled=True,
                strm_root=Path(tmp) / "share-strm",
                cms_cid="0",
                cms_local_path="/media/share",
                review_grace_seconds=1,
                review_checkpoints_seconds=(1,),
                review_list_cache_seconds=300,
            )
            workflow = self._workflow(tmp, cleanup_client=cleanup, self_share_config=config)
            workflow.p115 = UnavailableListP115()
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 101.0

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("暂时无法确认", result.message)
            self.assertEqual(cleanup.deleted, [])

    def test_own_share_stage_waits_for_validation_before_deleting_115_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])
            self.assertEqual(stored["own_share_code"], "owncode")
            self.assertNotEqual(stored["cleanup_status"], "deleted")

            self.tasks.record_event(
                task.id,
                TaskStage.OWN_SHARE_CREATED,
                TaskStatus.RUNNING,
                result.message,
                metadata_patch=result.metadata,
                submission_id=row["id"],
            )
            self.tasks.enqueue_task(task.id, TaskStage.SHARE_VALIDATED, next_run_at=1.0)
            validation_task = self.tasks.claim_next_runnable("worker-2", now=1.0)
            validated = workflow.run_stage(validation_task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(validated.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])
            self.assertNotEqual(stored["cleanup_status"], "deleted")
            self.assertEqual(self.cms.auto_organize_calls, 0)
            self.assertEqual(validated.metadata["share_review_status"], "pending")

    def test_strm_ready_restores_canonical_name_and_does_not_move_when_playback_probe_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": library_root}),
            )
            row = self._self_share_row(title="T-特洛伊-2004-[tmdb=652]", category="欧美电影", tmdb_id="652")
            alias_name = "asset-1-folder"
            alias_video = "asset-1-001.mkv"
            manifest = {
                "version": 1,
                "root_name": row["own_share_file_name"],
                "alias_name": alias_name,
                "category": "欧美电影",
                "tmdb_id": "652",
                "entries": [
                    {
                        "file_id": "video-id",
                        "canonical_path": "Troy.2004.2160p.mkv",
                        "alias_path": alias_video,
                    }
                ],
            }
            row = self.submissions.update_self_share(
                int(row["id"]),
                share_alias_name=alias_name,
                share_alias_level=2,
                canonical_manifest_json=json.dumps(manifest, ensure_ascii=False),
                share_validation_status="warning",
            ) or row
            source = self.config.strm_root / alias_name
            self._write_strm(source, name="asset-1-001.strm")
            self.cms.playback_results = [False]
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.STRM_READY,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("播放验证", result.message)
            self.assertTrue((source / "Troy.2004.2160p.strm").exists())
            self.assertFalse((source / "asset-1-001.strm").exists())
            self.assertFalse((library_root / row["own_share_file_name"]).exists())

    def test_strm_ready_stops_retrying_when_cms_cannot_resolve_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row()
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source)
            error_type = getattr(cms_client, "CmsSharePlaybackUnavailableError", RuntimeError)
            self.cms.playback_results = [error_type("获取分享直连失败")]
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.STRM_READY,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertIn("停止自动探测", result.message)
            self.assertIn("115 风控", result.message)
            self.assertTrue(source.exists())

    def test_strm_ready_stage_defers_until_own_share_strm_source_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row()
            task = self._claim_task("abc", "1234", TaskStage.STRM_READY, {"submission_id": row["id"]}, row["id"])

            scan_calls_before = len(self.p115.find_organized_calls)
            waiting = workflow.run_stage(task)
            self._write_strm(self.config.strm_root / row["own_share_file_name"])
            ready = workflow.run_stage(task)

            self.assertEqual(waiting.outcome, StageOutcome.DEFER)
            self.assertIn("等待自有分享 STRM", waiting.message)
            self.assertLessEqual(waiting.delay_seconds, 5)
            self.assertEqual(ready.outcome, StageOutcome.COMPLETE)
            self.assertEqual(ready.metadata["category"], "华语电影")
            self.assertEqual(ready.metadata["source_path"], str(bridge.safe_resolve(self.config.strm_root / row["own_share_file_name"])))
            self.assertEqual(ready.metadata["recognition"]["tmdb_id"], "123456")
            self.assertEqual(len(self.p115.find_organized_calls), scan_calls_before)

    def test_strm_ready_stage_ignores_direct_strm_source_roots_while_waiting_for_share_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            direct_root = Path(tmp) / "direct-strm"
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[direct_root], library_roots={"华语电影": library_root}),
            )
            row = self._self_share_row()
            direct_dir = direct_root / row["own_share_file_name"]
            self._write_strm(direct_dir, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.STRM_READY, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待自有分享 STRM", result.message)
            self.assertNotIn("source_path", result.metadata)
            self.assertNotEqual(stored["move_status"], "moved")
            self.assertTrue((direct_dir / "movie.strm").exists())
            self.assertFalse((library_root / row["own_share_file_name"]).exists())

    def test_strm_ready_stage_rejects_direct_link_before_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row()
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.STRM_READY, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.FAILED)
            self.assertIn("发现直链 STRM", result.message)
            self.assertEqual(stored["move_status"], "error")
            self.assertTrue(source.exists())

    def test_strm_ready_stage_keeps_late_direct_library_strm_while_waiting_for_share_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            western_root = Path(tmp) / "library" / "western"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": western_root}),
            )
            row = self._self_share_row(title="Z-忠犬八公的故事-2009-[tmdb=28178]", category="欧美电影", tmdb_id="28178")
            direct_dir = western_root / row["own_share_file_name"]
            self._write_strm(direct_dir, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.STRM_READY, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待自有分享 STRM", result.message)
            self.assertTrue((direct_dir / "movie.strm").exists())
            self.assertNotIn("direct_strm_removed", result.metadata)

    def test_strm_ready_stage_uses_late_direct_strm_library_as_cms_category_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            asia_root = Path(tmp) / "library" / "asia"
            western_root = Path(tmp) / "library" / "western"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(
                    source_roots=[],
                    library_roots={"亚洲电影": asia_root, "欧美电影": western_root},
                ),
            )
            row = self._self_share_row(title="P-破墓-2024-[tmdb=838209]", category="欧美电影", tmdb_id="838209")
            direct_dir = asia_root / row["own_share_file_name"]
            self._write_strm(direct_dir, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.STRM_READY, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            recognition = bridge.parse_recognition_json(stored)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(result.metadata["category"], "亚洲电影")
            self.assertEqual(result.metadata["recognition"]["category"], "亚洲电影")
            self.assertEqual(stored["category_choice"], "亚洲电影")
            self.assertEqual(stored["category_status"], "self_share_resolved")
            self.assertEqual(recognition["category"], "亚洲电影")
            self.assertEqual(recognition["category_status"], "self_share_resolved")
            self.assertTrue((direct_dir / "movie.strm").exists())
            self.assertNotIn("direct_strm_removed", result.metadata)

    def test_organizing_stage_triggers_auto_organize_only_once_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            first = workflow.run_stage(task)
            second = workflow.run_stage(task)

            self.assertEqual(first.outcome, StageOutcome.DEFER)
            self.assertEqual(second.outcome, StageOutcome.DEFER)
            self.assertEqual(self.cms.auto_organize_calls, 1)

    def test_organizing_stage_reuses_persisted_folder_without_rescanning_115(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="organized_found",
                own_share_file_id="folder-id",
                own_share_file_name="S-双喜-2025-[tmdb=123456]",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["organized_folder"]["file_id"], "folder-id")
            self.assertEqual(self.cms.auto_organize_calls, 0)

    def test_organizing_stage_uses_cms_cloud_index_for_new_direct_strm_in_old_series_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv_root = Path(tmp) / "library" / "tv"
            folder_name = "Q-权力的游戏前传：龙族-2022-[tmdb=94997]"
            cms_index = FakeCmsCloudIndex(
                {"file_id": "series-id", "file_name": folder_name, "parent_id": "tv-parent"}
            )
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"外国电视": tv_root}),
                cms_cloud_index=cms_index,
            )
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="House.of.the.Dragon.S03.2022.2160p.HMAX.WEB-DL",
            ) or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"title": "权力的游戏前传：龙族", "type": "tv", "tmdb_id": "94997", "category": "外国电视"},
                "tmdb_search_resolved",
            ) or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            direct_dir = tv_root / folder_name / "Season 03"
            self._write_strm(direct_dir, content="http://cms/d/direct-pick.mkv?/episode.mkv")
            old_time = time.time() - 86400
            os.utime(direct_dir.parent, (old_time, old_time))
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["organized_folder"]["file_id"], "series-id")
            self.assertEqual(cms_index.calls, [(bridge.safe_resolve(direct_dir.parent), "94997")])
            self.assertEqual(self.p115.find_organized_calls, [])
            self.assertTrue((direct_dir / "movie.strm").exists())

    def test_organizing_stage_uses_cms_cloud_index_for_cloud_output_without_tmdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder_name = "Q-权力的游戏前传：龙族-2022-[tmdb=94997]"
            cms_index = FakeCmsCloudIndex(
                folder={
                    "file_id": "series-id",
                    "file_name": folder_name,
                    "parent_id": "tv-parent",
                    "direct_file_id": "episode-id",
                    "direct_relative_path": "Season 03/S03E05.strm",
                },
                cloud_output_folder={
                    "file_id": "series-id",
                    "file_name": folder_name,
                    "parent_id": "tv-parent",
                    "direct_file_id": "episode-id",
                }
            )
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"外国电视": Path(tmp) / "tv"}),
                cms_cloud_index=cms_index,
            )
            direct_folder = Path(tmp) / "tv" / folder_name / "Season 03"
            direct_folder.mkdir(parents=True)
            (direct_folder / "S03E05.strm").write_text("http://cms/d/episodepick.mkv?/episode.mkv", encoding="utf-8")
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
                own_share_file_id="series-id",
                own_share_file_name=folder_name,
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "cloud_output_name": "House.of.the.Dragon.S03E05.mkv",
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["organized_folder"]["file_id"], "series-id")
            self.assertEqual(result.metadata["organized_folder"]["direct_relative_path"], "Season 03/S03E05.strm")
            self.assertIn(("cloud_output", "House.of.the.Dragon.S03E05.mkv"), cms_index.calls)
            self.assertEqual(self.p115.find_organized_calls, [])

    def test_own_share_stage_falls_back_to_direct_file_when_folder_is_gone(self):
        class FolderGoneP115(FakeP115):
            def create_share(self, file_id):
                self.created_shares.append(file_id)
                if file_id == "series-id":
                    raise RuntimeError("目录不存在或已转移")
                return {
                    "share_code": "file-share",
                    "receive_code": "1212",
                    "share_url": "https://115cdn.com/s/file-share?password=1212",
                }

            def ensure_share_settings(self, share_code, receive_code):
                return {"share_code": share_code, "receive_code": receive_code}

        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            workflow.p115 = FolderGoneP115()
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="organized_found",
                own_share_file_id="series-id",
                own_share_file_name="Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {
                    "submission_id": row["id"],
                    "organized_folder": {
                        "file_id": "series-id",
                        "direct_file_id": "episode-id",
                        "direct_relative_path": "Season 03/权力的游戏前传：龙族 (2022) - S03E03.strm",
                    },
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            updated = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(workflow.p115.created_shares, ["series-id", "episode-id"])
            self.assertEqual(updated["own_share_file_id"], "episode-id")
            self.assertEqual(updated["own_share_code"], "file-share")
            self.assertTrue(result.metadata["direct_file_share"])

    def test_direct_file_fallback_rechecks_conflicting_owner_before_replacement(self):
        class FolderGoneP115(FakeP115):
            def create_share(self, file_id):
                self.created_shares.append(file_id)
                if file_id == "series-id":
                    raise RuntimeError("目录不存在或已转移")
                return {
                    "share_code": "file-share",
                    "receive_code": "1212",
                    "share_url": "https://115cdn.com/s/file-share?password=1212",
                }

        for owner_tmdb_id in ("9533", ""):
            with self.subTest(owner_tmdb_id=owner_tmdb_id), tempfile.TemporaryDirectory() as tmp:
                workflow = self._workflow(tmp)
                workflow.p115 = FolderGoneP115()
                row = self._row()
                row = self.submissions.update_self_share(
                    int(row["id"]),
                    workflow_mode="self_share_sync",
                    workflow_phase="share_alias_prepared",
                    own_share_file_id="series-id",
                    own_share_file_name="Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
                ) or row
                row = self.submissions.update_recognition(
                    int(row["id"]),
                    {"ok": True, "title": "龙族", "type": "tv", "tmdb_id": "94997", "category": "外国电视"},
                    "self_share_resolved",
                ) or row
                owner = self.tasks.upsert_task("owner", "", "https://115cdn.com/s/owner")
                owner_metadata = {"own_share_file_id": "episode-id"}
                if owner_tmdb_id:
                    owner_metadata.update(
                        {"tmdb_id": owner_tmdb_id, "recognition": {"tmdb_id": owner_tmdb_id}}
                    )
                self.tasks.record_event(
                    owner.id,
                    TaskStage.ORGANIZING,
                    TaskStatus.PENDING,
                    "fallback owner",
                    metadata_patch=owner_metadata,
                )
                task = self._claim_task(
                    "abc",
                    "1234",
                    TaskStage.OWN_SHARE_CREATED,
                    {
                        "submission_id": row["id"],
                        "organized_folder": {
                            "file_id": "series-id",
                            "direct_file_id": "episode-id",
                            "direct_relative_path": "Season 03/龙族.S03E03.strm",
                        },
                    },
                    row["id"],
                )

                with patch.object(
                    self.submissions,
                    "replace_self_share_source_file_id",
                    wraps=self.submissions.replace_self_share_source_file_id,
                ) as replace_source:
                    result = workflow.run_stage(task)
                stored = self.submissions.find_by_id(int(row["id"]))

                self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
                self.assertIn("其他 TMDB 任务", result.message)
                replace_source.assert_not_called()
                self.assertEqual(stored["own_share_file_id"], "series-id")
                self.assertFalse(stored["own_share_code"])
                self.assertIsNone(
                    self.tasks.find_operation(task.id, self._create_share_operation_key(task, "episode-id"))
                )
                self.assertEqual(workflow.p115.created_shares, ["series-id"])

    def test_production_index_direct_fallback_survives_result_loss_and_cleans_direct_parent(self):
        class FolderGoneP115(FakeP115):
            def create_share(self, file_id):
                self.created_shares.append(file_id)
                if file_id == "series-id":
                    raise RuntimeError("目录不存在或已转移")
                return {
                    "share_code": "file-share",
                    "receive_code": "1212",
                    "share_url": "https://115cdn.com/s/file-share?password=1212",
                }

            def ensure_share_settings(self, share_code, receive_code):
                return {"share_code": share_code, "receive_code": receive_code}

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cms-online.db"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    CREATE TABLE cloud_data (
                        fid TEXT PRIMARY KEY,
                        pid TEXT,
                        name TEXT,
                        pick_code TEXT,
                        is_dir INTEGER NOT NULL,
                        f_modify_time INTEGER
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO cloud_data (fid, pid, name, pick_code, is_dir, f_modify_time) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("episode-id", "season-id", "Actual Episode S03E03.mkv", "episodepick", 0, 0),
                        ("season-id", "series-id", "Season 03", "", 1, 0),
                        ("series-id", "tv-root", "Q-Dragon-2022-[tmdb=94997]", "", 1, 0),
                        ("tv-root", "0", "TV", "", 1, 0),
                    ],
                )
            indexed_source = Path(tmp) / "cms-library" / "Q-Dragon-2022-[tmdb=94997]"
            self._write_strm(
                indexed_source / "Season 03",
                "Episode S03E03.strm",
                "http://cms/d/episodepick.mkv?/episode.mkv",
            )
            cms_index = bridge.CmsCloudDataIndex(db_path)
            folder = cms_index.folder_for_direct_strm(indexed_source, "94997")
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup, cms_cloud_index=cms_index)
            workflow.p115 = FolderGoneP115()
            workflow._now = lambda: 1000.0
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="organized_found",
                own_share_file_id="series-id",
                own_share_file_name=folder["file_name"],
            ) or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {"title": "Dragon", "type": "tv", "tmdb_id": "94997", "category": "外国电视"},
                "cms_direct_strm_resolved",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {"submission_id": row["id"], "organized_folder": folder},
                row["id"],
            )
            own_share_metadata = workflow._own_share_metadata
            metadata_calls = 0

            def lose_first_stage_result(*args, **kwargs):
                nonlocal metadata_calls
                metadata_calls += 1
                if metadata_calls == 1:
                    raise RuntimeError("simulated direct stage result loss")
                return own_share_metadata(*args, **kwargs)

            with patch.object(workflow, "_own_share_metadata", side_effect=lose_first_stage_result):
                with self.assertRaisesRegex(RuntimeError, "direct stage result loss"):
                    workflow.run_stage(task)
                recovered = workflow.run_stage(task)

            self.assertTrue(recovered.metadata["direct_file_share"])
            self.assertEqual(recovered.metadata["direct_file_share_file_id"], "episode-id")
            self.assertEqual(recovered.metadata["direct_file_share_parent_id"], "season-id")
            incoming = workflow.self_share_config.strm_root / "incoming.strm"
            self._write_strm(
                incoming.parent,
                incoming.name,
                "https://115cdn.com/s/file-share_1212_/episode.mkv",
            )
            stored = self.submissions.find_by_id(int(row["id"]))
            prepared = workflow._prepare_direct_file_share_strm(
                SimpleNamespace(metadata=recovered.metadata),
                stored,
            )
            expected = prepared / folder["direct_relative_path"]
            self.assertTrue(expected.is_file())

            dest = Path(tmp) / "library" / stored["own_share_file_name"]
            direct_relative = Path(folder["direct_relative_path"])
            self._write_strm(
                dest / direct_relative.parent,
                direct_relative.name,
                "https://115cdn.com/s/file-share_1212_/episode.mkv",
            )
            stored = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(prepared),
                dest_path=str(dest),
                category_final="外国电视",
            ) or stored
            stored = self.submissions.update_emby(int(row["id"]), "confirmed") or stored
            self.tasks.set_self_share_review_mode_override("off")
            self.tasks.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.RUNNING,
                "cleanup",
                submission_id=stored["id"],
                metadata_patch={"submission_id": stored["id"], **recovered.metadata},
                metadata_delete_keys=("organized_folder",),
            )
            self.tasks.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=1.0)
            cleanup_task = self.tasks.claim_next_runnable("direct-cleanup", now=1.0)

            cleanup_result = workflow.run_stage(cleanup_task)

            delete_operation = self.tasks.find_operation(
                task.id,
                self._delete_operation_key(task, "delete_source", "episode-id"),
            )
            self.assertEqual(cleanup_result.outcome, StageOutcome.COMPLETE, cleanup_result.message)
            self.assertEqual(cleanup.deleted, ["episode-id"])
            self.assertEqual(delete_operation.request["parent_id"], "season-id")

    def test_strm_ready_stage_places_direct_file_share_in_canonical_episode_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row(title="Q-权力的游戏前传：龙族-2022-[tmdb=94997]", category="外国电视", tmdb_id="94997")
            row = self.submissions.replace_self_share_source_file_id(int(row["id"]), "episode-id") or row
            source_file = self.config.strm_root / "权力的游戏前传：龙族 (2022) - S03E03.strm"
            self._write_strm(source_file.parent, source_file.name, content="https://115cdn.com/s/owncode_ownpwd_/episode.mkv")
            relative = "Season 03/权力的游戏前传：龙族 (2022) - S03E03.strm"
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.STRM_READY,
                {
                    "submission_id": row["id"],
                    "direct_file_share": True,
                    "direct_file_share_file_id": "episode-id",
                    "direct_file_share_relative_path": relative,
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            expected = self.config.strm_root / row["own_share_file_name"] / relative

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["source_path"], str(bridge.safe_resolve(expected.parent.parent)))
            self.assertTrue(expected.exists())
            self.assertFalse(source_file.exists())

    def test_direct_file_share_rejects_absolute_folder_before_copy_or_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            outside = Path(tmp) / "outside" / "Movie"
            source_file = workflow.self_share_config.strm_root / "candidate.strm"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("https://115.com/s/owncode_ownpwd_/episode.mkv", encoding="utf-8")
            task = self._claim_task(
                "direct-boundary",
                "1234",
                TaskStage.STRM_READY,
                {
                    "direct_file_share": True,
                    "direct_file_share_file_id": "episode-id",
                    "direct_file_share_relative_path": "episode.strm",
                },
            )
            row = {
                "own_share_file_name": str(outside),
                "own_share_code": "owncode",
                "own_share_receive_code": "ownpwd",
            }

            prepared = workflow._prepare_direct_file_share_strm(task, row)

            self.assertIsNone(prepared)
            self.assertTrue(source_file.exists())
            self.assertFalse((outside / "episode.strm").exists())

    def test_organizing_stage_keeps_direct_strm_until_share_strm_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            western_root = Path(tmp) / "library" / "western"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": western_root}),
            )
            row = self._row()
            folder_name = "Z-蜘蛛侠-2002-[tmdb=557]"
            self.p115.folder = {
                "file_id": "folder-id",
                "file_name": folder_name,
                "parent_id": "western-parent",
            }
            direct_dir = western_root / folder_name
            self._write_strm(direct_dir, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertTrue((direct_dir / "movie.strm").exists())
            self.assertTrue(direct_dir.exists())

    def test_organizing_stage_uses_direct_strm_library_as_cms_category_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            asia_root = Path(tmp) / "library" / "asia"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"亚洲电影": asia_root}),
            )
            row = self._row()
            folder_name = "S-娑婆诃-2019-[tmdb=556509]"
            self.p115.folder = {
                "file_id": "folder-id",
                "file_name": folder_name,
                "parent_id": "unmapped-cms-parent",
            }
            direct_dir = asia_root / folder_name
            self._write_strm(direct_dir, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            organizing = workflow.run_stage(task)
            recognizing_task = self._claim_task("abc", "1234", TaskStage.RECOGNIZING, organizing.metadata, row["id"])
            recognizing = workflow.run_stage(recognizing_task)

            self.assertEqual(organizing.outcome, StageOutcome.COMPLETE)
            self.assertEqual(organizing.metadata["organized_folder"]["category"], "亚洲电影")
            self.assertTrue((direct_dir / "movie.strm").exists())
            self.assertEqual(recognizing.outcome, StageOutcome.COMPLETE)
            self.assertEqual(recognizing.metadata["category"], "亚洲电影")
            self.assertEqual(self.telegram.messages, [])

    def test_organizing_reprocess_ignores_direct_strm_older_than_current_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            western_root = Path(tmp) / "library" / "western"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": western_root}),
            )
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="悬案 (2026)") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            recognition = {
                "ok": True,
                "title": "悬案",
                "share_name": "悬案 (2026)",
                "tmdb_id": "273114",
                "type": "tv",
                "category": "国产电视",
                "category_status": "self_share_resolved",
            }
            row = self.submissions.update_recognition(
                int(row["id"]), recognition, "self_share_resolved"
            ) or row
            row = self.submissions.update_category(int(row["id"]), "国产电视", "selected") or row
            stale_dir = western_root / "unmatched-old-folder"
            self._write_strm(stale_dir, content="http://cms/d/stale/movie.mkv")
            stale_time = float(row["created_at"]) + 10
            os.utime(stale_dir / "movie.strm", (stale_time, stale_time))
            os.utime(stale_dir, (stale_time, stale_time))
            update_started_at = stale_time + 120
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {
                    "submission_id": row["id"],
                    "recognition": recognition,
                    "update_started_at": update_started_at,
                    "reprocess_started_at": update_started_at,
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            stored_recognition = bridge.parse_recognition_json(stored)

        self.assertEqual(result.outcome, StageOutcome.DEFER)
        self.assertEqual(stored["category_choice"], "国产电视")
        self.assertEqual(stored["category_status"], "selected")
        self.assertEqual(stored_recognition["category"], "国产电视")
        self.assertEqual(stored_recognition["category_status"], "self_share_resolved")

    def test_organizing_treats_nonfinite_current_run_timestamps_as_no_cutoff(self):
        for timestamp in (float("inf"), float("nan")):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as tmp:
                western_root = Path(tmp) / "library" / "western"
                workflow = self._workflow(
                    tmp,
                    move_config=bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": western_root}),
                )
                row = self._row()
                row = self.submissions.update_status(int(row["id"]), "received", title="悬案 (2026)") or row
                row = self.submissions.update_self_share(
                    int(row["id"]),
                    workflow_mode="self_share_sync",
                    workflow_phase="auto_organize_submitted",
                ) or row
                recognition = {
                    "ok": True,
                    "title": "悬案",
                    "share_name": "悬案 (2026)",
                    "tmdb_id": "273114",
                    "type": "tv",
                    "category": "国产电视",
                    "category_status": "self_share_resolved",
                }
                row = self.submissions.update_recognition(
                    int(row["id"]), recognition, "self_share_resolved"
                ) or row
                row = self.submissions.update_category(int(row["id"]), "国产电视", "selected") or row
                direct_dir = western_root / "unmatched-direct-folder"
                self._write_strm(direct_dir, content="http://cms/d/direct-link/movie.mkv")
                direct_time = float(row["created_at"]) + 10
                os.utime(direct_dir / "movie.strm", (direct_time, direct_time))
                os.utime(direct_dir, (direct_time, direct_time))
                task = self._claim_task(
                    "abc",
                    "1234",
                    TaskStage.ORGANIZING,
                    {
                        "submission_id": row["id"],
                        "recognition": recognition,
                        "update_started_at": timestamp,
                        "reprocess_started_at": timestamp,
                    },
                    row["id"],
                )

                result = workflow.run_stage(task)
                stored = self.submissions.find_by_id(int(row["id"]))
                stored_recognition = bridge.parse_recognition_json(stored)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(stored["category_choice"], "欧美电影")
            self.assertEqual(stored_recognition["category"], "欧美电影")

    def test_organizing_without_current_run_cutoff_preserves_existing_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_status(int(row["id"]), "received", title="悬案 (2026)") or row
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
            ) or row
            recognition = {
                "ok": True,
                "title": "悬案",
                "share_name": "悬案 (2026)",
                "tmdb_id": "273114",
                "type": "tv",
                "category": "国产电视",
                "category_status": "self_share_resolved",
            }
            row = self.submissions.update_recognition(
                int(row["id"]), recognition, "self_share_resolved"
            ) or row
            row = self.submissions.update_category(int(row["id"]), "国产电视", "selected") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.ORGANIZING,
                {"submission_id": row["id"], "recognition": recognition},
                row["id"],
            )

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            stored_recognition = bridge.parse_recognition_json(stored)

        self.assertEqual(result.outcome, StageOutcome.DEFER)
        self.assertEqual(stored["category_choice"], "国产电视")
        self.assertEqual(stored["category_status"], "self_share_resolved")
        self.assertEqual(stored_recognition["category"], "国产电视")
        self.assertEqual(stored_recognition["category_status"], "self_share_resolved")

    def test_organizing_stage_uses_recent_direct_strm_to_recover_wrong_tmdb_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            bangumi_root = Path(tmp) / "library" / "bangumi"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"番剧": bangumi_root}),
            )
            row = self._row()
            row = self.submissions.update_status(
                int(row["id"]),
                "received",
                title="JoJo's.Bizarre.Adventure.S06.1080p.NF.WEB-DL.AAC2.0.H.264-HiveWeb",
            ) or row
            row = self.submissions.update_recognition(
                int(row["id"]),
                {
                    "ok": True,
                    "title": "JOJO的奇妙冒险OVA",
                    "type": "tv",
                    "category": "番剧",
                    "tmdb_id": "60862",
                    "category_status": "tmdb_search_resolved",
                },
                "tmdb_search_resolved",
            ) or row
            row = self.submissions.update_category(int(row["id"]), "番剧", "selected") or row
            folder_name = "J-JOJO的奇妙冒险-2012-[tmdb=45790]"
            direct_dir = bangumi_root / folder_name
            self._write_strm(direct_dir / "Season 06", content="http://cms/d/direct-link/jojo.mkv")
            calls = []

            def find_organized_folder(recognition, title, excluded_parent_ids=None, min_update_time=0, **kwargs):
                calls.append((dict(recognition), title))
                if recognition.get("tmdb_id") == "45790":
                    return {
                        "file_id": "folder-id",
                        "file_name": folder_name,
                        "parent_id": "bangumi-parent",
                        "category": "番剧",
                    }
                return None

            self.p115.find_organized_folder = find_organized_folder
            task = self._claim_task("abc", "1234", TaskStage.ORGANIZING, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))
            recognition = bridge.parse_recognition_json(stored)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(calls[-1][0]["tmdb_id"], "45790")
            self.assertEqual(result.metadata["organized_folder"]["file_id"], "folder-id")
            # The stale direct_strm_removed counter field was removed; the
            # recovered stage must simply not claim anything was removed.
            self.assertNotIn("direct_strm_removed", result.metadata)
            self.assertTrue((direct_dir / "Season 06" / "movie.strm").exists())
            self.assertEqual(recognition["tmdb_id"], "45790")
            self.assertEqual(stored["own_share_file_name"], folder_name)

    def test_moved_stage_exposes_strm_stability_wait_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}, stable_seconds=30),
            )
            row = self._self_share_row()
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source)
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(result.message, "STRM 源目录仍在更新")
            self.assertIn("stable_remaining_seconds", result.metadata)
            self.assertGreaterEqual(result.metadata["stable_remaining_seconds"], 0)
            self.assertEqual(result.metadata["source_path"], str(bridge.safe_resolve(source)))

    def test_moved_stage_merges_own_share_strm_folder_into_category_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}),
            )
            row = self._self_share_row()
            source = self.config.strm_root / row["own_share_file_name"]
            dest = library_root / row["own_share_file_name"]
            self._write_strm(source)
            self._write_strm(dest, name="existing.strm")
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            moved = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertFalse(source.exists())
            self.assertTrue((dest / "movie.strm").exists())
            self.assertTrue((dest / "existing.strm").exists())
            self.assertEqual(moved["move_status"], "moved")
            self.assertEqual(result.metadata["dest_path"], str(bridge.safe_resolve(dest)))
            self.assertEqual(result.metadata["source_path"], str(bridge.safe_resolve(source)))
            self.assertEqual(result.metadata["category"], "华语电影")
            self.assertEqual(len(self.telegram.messages), 1)

    def test_cms_delete_settled_stage_waits_before_move_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            cms_index = FakeCmsCloudIndex(indexed_file_ids={"leftover-id"})
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}),
                cms_cloud_index=cms_index,
            )
            row = self._self_share_row()
            row = self.submissions.update_cleanup(int(row["id"]), "deleted", file_id="leftover-id") or row
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source)
            task = self._claim_task("abc", "1234", TaskStage.CMS_DELETE_SETTLED, {"submission_id": row["id"]}, row["id"])

            waiting = workflow.run_stage(task)
            cms_index.indexed_file_ids.discard("leftover-id")
            settled = workflow.run_stage(task)
            self.tasks.enqueue_task(task.id, TaskStage.MOVED, next_run_at=1.0)
            move_task = self.tasks.claim_next_runnable("worker-2", now=1.0)
            moved = workflow.run_stage(move_task)

            self.assertEqual(waiting.outcome, StageOutcome.DEFER)
            self.assertIn("CMS 清理源目录", waiting.message)
            self.assertEqual(settled.outcome, StageOutcome.COMPLETE)
            self.assertEqual(moved.outcome, StageOutcome.COMPLETE)
            self.assertFalse(source.exists())
            self.assertTrue((library_root / row["own_share_file_name"] / "movie.strm").exists())

    def test_cms_delete_settled_does_not_wait_for_library_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cms_index = FakeCmsCloudIndex(indexed_file_ids={"folder-id"})
            workflow = self._workflow(tmp, cms_cloud_index=cms_index)
            row = self._self_share_row()
            row = self.submissions.update_cleanup(int(row["id"]), "deleted", file_id="folder-id") or row
            task = self._claim_task("abc", "1234", TaskStage.CMS_DELETE_SETTLED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertTrue(cms_index.has_file_id("folder-id"))

    def test_moved_stage_merges_new_seasons_when_already_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "tv"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"外国电视": library_root}),
            )
            row = self._self_share_row(
                title="侠探杰克 (2022) {tmdb-108978}",
                category="外国电视",
                tmdb_id="108978",
            )
            source = self.config.strm_root / row["own_share_file_name"]
            dest = library_root / row["own_share_file_name"]
            self._write_strm(source / "Season 01", name="e01.strm")
            self._write_strm(source / "Season 03", name="e01.strm")
            self._write_strm(dest / "Season 02", name="e01.strm")
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(source),
                dest_path=str(dest),
                category_final="外国电视",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertTrue((dest / "Season 01" / "e01.strm").exists())
            self.assertTrue((dest / "Season 02" / "e01.strm").exists())
            self.assertTrue((dest / "Season 03" / "e01.strm").exists())
            self.assertFalse(source.exists())

    def test_alias_share_strm_moves_into_canonical_library_folder_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"欧美电影": library_root}),
            )
            row = self._self_share_row(title="T-特洛伊-2004-[tmdb=652]", category="欧美电影", tmdb_id="652")
            alias_name = "asset-1-folder"
            manifest = {
                "version": 1,
                "root_name": row["own_share_file_name"],
                "alias_name": alias_name,
                "category": "欧美电影",
                "tmdb_id": "652",
                "entries": [],
            }
            row = self.submissions.update_self_share(
                int(row["id"]),
                share_alias_name=alias_name,
                share_alias_level=1,
                canonical_manifest_json=json.dumps(manifest, ensure_ascii=False),
                share_validation_status="valid",
            ) or row
            source = self.config.strm_root / alias_name
            self._write_strm(source)
            ready_task = self._claim_task(
                "abc",
                "1234",
                TaskStage.STRM_READY,
                {"submission_id": row["id"]},
                row["id"],
            )

            ready = workflow.run_stage(ready_task)
            self.tasks.enqueue_task(ready_task.id, TaskStage.CMS_DELETE_SETTLED, next_run_at=1.0)
            settle_task = self.tasks.claim_next_runnable("worker-2", now=1.0)
            settled = workflow.run_stage(settle_task)
            self.tasks.enqueue_task(ready_task.id, TaskStage.MOVED, next_run_at=1.0)
            move_task = self.tasks.claim_next_runnable("worker-3", now=1.0)
            moved = workflow.run_stage(move_task)

            canonical_dest = library_root / row["own_share_file_name"]
            self.assertEqual(ready.outcome, StageOutcome.COMPLETE)
            self.assertTrue(ready.metadata["share_playback_validated"])
            self.assertEqual(settled.outcome, StageOutcome.COMPLETE)
            self.assertEqual(moved.outcome, StageOutcome.COMPLETE)
            self.assertTrue((canonical_dest / "movie.strm").is_file())
            self.assertFalse((library_root / alias_name).exists())

    def test_moved_stage_keeps_authoritative_cms_category_even_when_same_tmdb_exists_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            asia_root = Path(tmp) / "library" / "asia"
            western_root = Path(tmp) / "library" / "western"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(
                    source_roots=[],
                    library_roots={"亚洲电影": asia_root, "欧美电影": western_root},
                ),
            )
            row = self._self_share_row(title="W-无声-2020-[tmdb=606740]", category="亚洲电影", tmdb_id="606740")
            source = self.config.strm_root / row["own_share_file_name"]
            asia_dest = asia_root / row["own_share_file_name"]
            western_dest = western_root / row["own_share_file_name"]
            self._write_strm(source, content="http://cms/s/owncode_ownpwd_1.mkv")
            self._write_strm(western_dest, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            moved = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertFalse(source.exists())
            self.assertTrue(asia_dest.exists())
            self.assertTrue(western_dest.exists())
            self.assertIn("/s/owncode_ownpwd_", (asia_dest / "movie.strm").read_text(encoding="utf-8"))
            self.assertIn("/d/", (western_dest / "movie.strm").read_text(encoding="utf-8"))
            self.assertEqual(moved["category_final"], "亚洲电影")
            self.assertEqual(result.metadata["category"], "亚洲电影")

    def test_moved_stage_keeps_tmdb_resolved_category_even_when_same_tmdb_exists_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv_root = Path(tmp) / "library" / "tv"
            western_root = Path(tmp) / "library" / "western"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(
                    source_roots=[],
                    library_roots={"外国电视": tv_root, "欧美电影": western_root},
                ),
            )
            row = self._self_share_row(title="W-无耻之徒-2011-[tmdb=34307]", category="外国电视", tmdb_id="34307")
            recognition = bridge.parse_recognition_json(row)
            recognition["category_status"] = "tmdb_resolved"
            row = self.submissions.update_recognition(int(row["id"]), recognition, "tmdb_resolved") or row
            source = self.config.strm_root / row["own_share_file_name"]
            tv_dest = tv_root / row["own_share_file_name"]
            western_dest = western_root / row["own_share_file_name"]
            self._write_strm(source, content="http://cms/s/owncode_ownpwd_1.mkv")
            self._write_strm(western_dest, content="http://cms/d/direct-link/movie.mkv")
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            moved = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertFalse(source.exists())
            self.assertTrue(tv_dest.exists())
            self.assertTrue(western_dest.exists())
            self.assertIn("/s/owncode_ownpwd_", (tv_dest / "movie.strm").read_text(encoding="utf-8"))
            self.assertIn("/d/", (western_dest / "movie.strm").read_text(encoding="utf-8"))
            self.assertEqual(moved["category_final"], "外国电视")
            self.assertEqual(result.metadata["category"], "外国电视")

    def test_moved_stage_reuses_persisted_moved_row_when_dest_strm_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}),
            )
            row = self._self_share_row()
            source = self.config.strm_root / row["own_share_file_name"]
            dest = library_root / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(source),
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(result.metadata["dest_path"], str(bridge.safe_resolve(dest)))
            self.assertEqual(result.metadata["source_path"], str(bridge.safe_resolve(source)))
            self.assertEqual(result.metadata["category"], "华语电影")
            self.assertFalse(source.exists())
            self.assertEqual(self.telegram.messages, [])

    def test_moved_stage_requires_expected_direct_file_share_episode_before_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv_root = Path(tmp) / "library" / "tv"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"外国电视": tv_root}),
            )
            row = self._self_share_row(
                title="Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
                category="外国电视",
                tmdb_id="94997",
            )
            row = self.submissions.replace_self_share_source_file_id(int(row["id"]), "episode-id") or row
            dest = tv_root / row["own_share_file_name"]
            episode_dir = dest / "Season 03"
            self._write_strm(
                episode_dir,
                name="权力的游戏前传：龙族 (2022) - S03E02.strm",
                content="https://115.com/s/owncode_ownpwd_/S03E02.mkv",
            )
            self._write_strm(
                episode_dir,
                name="权力的游戏前传：龙族 (2022) - S03E03.strm",
                content="https://115.com/d/direct/S03E03.mkv",
            )
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest),
                category_final="外国电视",
            ) or row
            relative_path = "Season 03/权力的游戏前传：龙族 (2022) - S03E03.strm"
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.MOVED,
                {
                    "submission_id": row["id"],
                    "direct_file_share": True,
                    "direct_file_share_file_id": "episode-id",
                    "direct_file_share_relative_path": relative_path,
                },
                row["id"],
            )

            result = workflow.run_stage(task)
            updated = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("重新生成", result.message)
            self.assertTrue((episode_dir / "权力的游戏前传：龙族 (2022) - S03E03.strm").exists())
            self.assertEqual(self.cms.share_sync_calls, [("owncode", "ownpwd", "0", "/media/share")])
            self.assertEqual(updated["workflow_phase"], "restore_share_sync_submitted")

    def test_moved_stage_replaces_direct_existing_episode_target_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv_root = Path(tmp) / "library" / "tv"
            emby = FakeEmby()
            workflow = self._workflow(
                tmp,
                emby=emby,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"外国电视": tv_root}),
            )
            row = self._self_share_row(
                title="Q-权力的游戏前传：龙族-2022-[tmdb=94997]",
                category="外国电视",
                tmdb_id="94997",
            )
            row = self.submissions.replace_self_share_source_file_id(int(row["id"]), "episode-id") or row
            dest = tv_root / row["own_share_file_name"]
            episode_dir = dest / "Season 03"
            self._write_strm(
                episode_dir,
                name="权力的游戏前传：龙族 (2022) - S03E03.strm",
                content="https://115.com/d/direct/S03E03.mkv",
            )
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest),
                category_final="外国电视",
            ) or row
            relative_path = "Season 03/权力的游戏前传：龙族 (2022) - S03E03.strm"
            generated = self.config.strm_root / "权力的游戏前传：龙族 (2022) - S03E03.strm"
            self._write_strm(
                generated.parent,
                generated.name,
                content="https://115.com/s/owncode_ownpwd_/S03E03.mkv",
            )
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.MOVED,
                {
                    "submission_id": row["id"],
                    "direct_file_share": True,
                    "direct_file_share_file_id": "episode-id",
                    "direct_file_share_relative_path": relative_path,
                    "emby_refresh_requested": True,
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            target = episode_dir / "权力的游戏前传：龙族 (2022) - S03E03.strm"
            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("已用自有分享 STRM 恢复", result.message)
            self.assertEqual(target.read_text(encoding="utf-8"), "https://115.com/s/owncode_ownpwd_/S03E03.mkv")
            self.assertFalse((self.config.strm_root / row["own_share_file_name"] / relative_path).exists())
            self.assertEqual(self.cms.share_sync_calls, [])
            self.assertEqual(emby.refreshed_paths, [str(bridge.safe_resolve(dest))])

    def test_moved_stage_requests_emby_refresh_for_destination_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            emby = FakeEmby()
            workflow = self._workflow(
                tmp,
                emby=emby,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}),
            )
            row = self._self_share_row()
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source)
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            again = self._claim_task(
                "abc",
                "1234",
                TaskStage.MOVED,
                {
                    "submission_id": row["id"],
                    "emby_refresh_requested": True,
                    "dest_path": result.metadata["dest_path"],
                },
                row["id"],
            )
            workflow.run_stage(again)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(emby.refreshed_paths, [result.metadata["dest_path"]])
            self.assertTrue(result.metadata["emby_refresh_requested"])
            self.assertEqual(result.metadata["emby_refresh_library"], "电影库")

    def test_moved_stage_fails_when_source_folder_tmdb_mismatches_recognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}),
            )
            row = self._self_share_row(title="S-双喜-2025-[tmdb=123456]", tmdb_id="123456")
            source = self.config.strm_root / "S-错片-2025-[tmdb=999999]"
            row = self.submissions.update_self_share(
                int(row["id"]),
                own_share_file_name=source.name,
            ) or row
            self._write_strm(source)
            task = self._claim_task("abc", "1234", TaskStage.MOVED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.FAILED)
            self.assertIn("TMDB", result.message)
            self.assertTrue(source.exists())

    def test_moved_stage_rejects_direct_link_and_wrong_marker_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}),
            )
            cases = [
                ("direct", "https://115.com/d/direct-link/movie.mkv", "发现直链 STRM"),
                ("wrong", "https://115.com/s/othercode_otherpwd_/movie.mkv", "STRM 不是预期的分享链接"),
            ]
            for suffix, content, expected_error in cases:
                with self.subTest(suffix=suffix):
                    row = self._self_share_row(
                        title=f"S-双喜-{suffix}-2025-[tmdb=123456]",
                    )
                    source = self.config.strm_root / row["own_share_file_name"]
                    self._write_strm(source, content=content)
                    task = self._claim_task(
                        row["share_code"],
                        row["receive_code"],
                        TaskStage.MOVED,
                        {"submission_id": row["id"]},
                        row["id"],
                    )

                    result = workflow.run_stage(task)
                    failed = self.submissions.find_by_id(int(row["id"]))

                    self.assertEqual(result.outcome, StageOutcome.FAILED)
                    self.assertEqual(failed["move_status"], "error")
                    self.assertIn(expected_error, failed["move_error"])
                    self.assertTrue(source.exists())

    def test_emby_confirmed_stage_defers_until_match_then_stores_library_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            emby = FakeEmby()
            workflow = self._workflow(tmp, emby=emby)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / "movies" / "S-双喜-2025-[tmdb=123456]"
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.EMBY_CONFIRMED, {"submission_id": row["id"]}, row["id"])

            waiting = workflow.run_stage(task)
            emby.items_by_tmdb["123456"] = {
                "Id": "emby-item",
                "Name": "双喜",
                "Path": str(dest / "movie.strm"),
                "ParentId": "parent-id",
                "LibraryName": "电影库",
            }
            confirmed = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(waiting.outcome, StageOutcome.DEFER)
            self.assertEqual(waiting.delay_seconds, 5)
            self.assertEqual(confirmed.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["emby_status"], "confirmed")
            self.assertEqual(stored["emby_item_id"], "emby-item")
            self.assertEqual(stored["emby_parent"], "电影库")
            self.assertEqual(confirmed.metadata["library"], "电影库")

    def test_emby_confirmed_stage_accepts_existing_item_in_legacy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            emby = FakeEmby()
            workflow = self._workflow(tmp, emby=emby)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / "movies" / "S-双喜-2025-[tmdb=123456]"
            legacy = Path(tmp) / "library" / "movies" / "旧目录-双喜"
            self._write_strm(dest)
            legacy.mkdir(parents=True)
            (legacy / "movie.strm").write_text("https://115.com/s/owncode_ownpwd_/movie.mkv", encoding="utf-8")
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(
                int(row["id"]),
                "confirmed",
                item_id="legacy-emby-item",
                title="双喜",
                path=str(legacy / "movie.strm"),
                parent="电影库",
            ) or row
            emby.items_by_tmdb["123456"] = {
                "Id": "legacy-emby-item",
                "Name": "双喜",
                "Path": str(legacy / "movie.strm"),
                "ParentId": "parent-id",
                "LibraryName": "电影库",
                "ProviderIds": {"Tmdb": "123456"},
            }
            task = self._claim_task("abc", "1234", TaskStage.EMBY_CONFIRMED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertEqual(stored["emby_item_id"], "legacy-emby-item")

    def test_strm_ready_defers_while_share_sync_pending_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row(title="S-双喜-2025-[tmdb=123456]")
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source, content="https://115.com/s/othercode_otherpwd_/movie.mkv")
            row = self.submissions.update_self_share(
                int(row["id"]),
                share_sync_status="submitted",
            ) or row
            task = self._claim_task(
                row["share_code"],
                row["receive_code"],
                TaskStage.STRM_READY,
                {"submission_id": row["id"]},
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("等待自有分享 STRM 生成", result.message)
            stored = self.submissions.find_by_id(int(row["id"]))
            self.assertNotEqual(stored["move_status"], "error")
            self.assertEqual(stored["cleanup_status"], None)


    def test_emby_confirmed_stage_restores_missing_dest_after_cms_delete_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            emby = FakeEmby()
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                emby=emby,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}, stable_seconds=0),
            )
            row = self._self_share_row()
            dest = library_root / row["own_share_file_name"]
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.EMBY_CONFIRMED, {"submission_id": row["id"]}, row["id"])

            first = workflow.run_stage(task)
            second = workflow.run_stage(task)
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source)
            restored = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(first.outcome, StageOutcome.DEFER)
            self.assertEqual(second.outcome, StageOutcome.DEFER)
            self.assertEqual(self.cms.share_sync_calls, [("owncode", "ownpwd", "0", "/media/share")])
            self.assertEqual(stored["workflow_phase"], "restore_share_sync_submitted")
            self.assertEqual(stored["share_sync_status"], "restore_submitted")
            self.assertEqual(restored.outcome, StageOutcome.DEFER)
            self.assertTrue((dest / "movie.strm").exists())
            self.assertFalse(source.exists())

    def test_moved_stage_fails_permanent_restore_outcomes_but_defers_transient_move_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row()
            task = SimpleNamespace(metadata={})
            metadata = {"dest_path": str(Path(tmp) / "library" / "missing")}

            for restore_status in ("skipped", "error"):
                with self.subTest(restore_status=restore_status), patch(
                    "app.workflows.self_share.restore_missing_self_share_library_folder",
                    return_value=(restore_status, {"restore_reason": restore_status}),
                ):
                    result = workflow._restore_missing_moved_destination(task, row, metadata)

                self.assertEqual(result.outcome, StageOutcome.FAILED)
                self.assertEqual(result.metadata["restore_reason"], restore_status)

            with patch(
                "app.workflows.self_share.restore_missing_self_share_library_folder",
                return_value=("move_failed", {"restore_reason": "STRM 源目录仍在更新"}),
            ):
                result = workflow._restore_missing_moved_destination(task, row, metadata)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(result.metadata["restore_reason"], "STRM 源目录仍在更新")

    def test_emby_confirmed_stage_revalidates_stored_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            emby = FakeEmby()
            workflow = self._workflow(tmp, emby=emby)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / "movies" / "S-双喜-2025-[tmdb=123456]"
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(
                int(row["id"]),
                "confirmed",
                item_id="old-item",
                title="双喜",
                path=str(dest / "movie.strm"),
                parent="电影库",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.EMBY_CONFIRMED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(stored["emby_status"], "pending")
            self.assertIn("等待 Emby 确认", result.message)

    def test_cleaned_stage_revalidates_missing_dest_before_reporting_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            emby = FakeEmby()
            library_root = Path(tmp) / "library" / "movies"
            workflow = self._workflow(
                tmp,
                emby=emby,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}, stable_seconds=0),
                cleanup_client=FakeCleanupClient(),
            )
            row = self._self_share_row()
            dest = library_root / row["own_share_file_name"]
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed", item_id="old-item", path=str(dest / "movie.strm")) or row
            row = self.submissions.update_cleanup(int(row["id"]), "deleted", file_id="folder-id") or row
            task = self._claim_task("abc", "1234", TaskStage.CLEANED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertIn("目标 STRM 被 CMS 同步删除", result.message)
            self.assertEqual(self.cms.share_sync_calls, [("owncode", "ownpwd", "0", "/media/share")])

    def test_emby_confirmed_stage_defers_same_tmdb_match_outside_moved_dest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            emby = FakeEmby()
            workflow = self._workflow(tmp, emby=emby)
            row = self._self_share_row()
            dest_a = Path(tmp) / "library" / "A" / "S-双喜-2025-[tmdb=123456]"
            dest_b = Path(tmp) / "library" / "B" / "S-双喜-2025-[tmdb=123456]"
            self._write_strm(dest_a)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest_a),
                category_final="华语电影",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.EMBY_CONFIRMED, {"submission_id": row["id"]}, row["id"])

            emby.items_by_tmdb["123456"] = {
                "Id": "old-item",
                "Name": "双喜",
                "Path": str(dest_b),
                "ParentId": "parent-old",
                "LibraryName": "旧库",
            }
            outside = workflow.run_stage(task)
            stored_after_outside = self.submissions.find_by_id(int(row["id"]))
            emby.items_by_tmdb["123456"] = {
                "Id": "new-item",
                "Name": "双喜",
                "Path": str(dest_a / "movie.strm"),
                "ParentId": "parent-new",
                "LibraryName": "电影库",
            }
            inside = workflow.run_stage(task)
            stored_after_inside = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(outside.outcome, StageOutcome.DEFER)
            self.assertNotEqual(stored_after_outside["emby_status"], "confirmed")
            self.assertEqual(inside.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored_after_inside["emby_status"], "confirmed")
            self.assertEqual(stored_after_inside["emby_item_id"], "new-item")

    def test_emby_confirmed_stage_selects_in_dest_duplicate_tmdb_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            emby = FakeEmby()
            workflow = self._workflow(tmp, emby=emby)
            row = self._self_share_row()
            dest_a = Path(tmp) / "library" / "A" / "S-双喜-2025-[tmdb=123456]"
            dest_b = Path(tmp) / "library" / "B" / "S-双喜-2025-[tmdb=123456]"
            self._write_strm(dest_a)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path=str(dest_a),
                category_final="华语电影",
            ) or row
            outside = {
                "Id": "old-item",
                "Name": "双喜",
                "Path": str(dest_b),
                "ParentId": "parent-old",
                "LibraryName": "旧库",
                "ProviderIds": {"Tmdb": "123456"},
            }
            inside = {
                "Id": "new-item",
                "Name": "双喜",
                "Path": str(dest_a / "movie.strm"),
                "ParentId": "parent-new",
                "LibraryName": "电影库",
                "ProviderIds": {"Tmdb": "123456"},
            }
            emby.items_by_tmdb["123456"] = outside
            emby.recent = [outside, inside]
            task = self._claim_task("abc", "1234", TaskStage.EMBY_CONFIRMED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["emby_item_id"], "new-item")
            self.assertEqual(stored["emby_parent"], "电影库")

    def test_cleaned_stage_requires_emby_confirmed_and_own_share_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 101.0

            not_confirmed = workflow.run_stage(task)
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            with self.submissions._connection() as conn:
                conn.execute("UPDATE submissions SET own_share_code = '' WHERE id = ?", (row["id"],))
            missing_share = workflow.run_stage(task)

            self.assertEqual(not_confirmed.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(missing_share.outcome, StageOutcome.FAILED)
            self.assertEqual(cleanup.deleted, [])

    def test_cleaned_stage_completes_as_skipped_when_cleanup_client_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, cleanup_client=None)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / "dest"
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task("abc", "1234", TaskStage.CLEANED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertIn("清理已跳过", result.message)
            self.assertEqual(stored["cleanup_status"], "skipped")
            self.assertEqual(result.metadata["cleanup_status"], "skipped")
            self.assertEqual(result.metadata["cleanup_error"], "disabled")

    def test_cleaned_stage_skips_disabled_cleanup_before_own_share_prechecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp, cleanup_client=None)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / "dest"
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            with self.submissions._connection() as conn:
                conn.execute(
                    "UPDATE submissions SET own_share_code = '', own_share_file_id = '' WHERE id = ?",
                    (row["id"],),
                )
            task = self._claim_task("abc", "1234", TaskStage.CLEANED, {"submission_id": row["id"]}, row["id"])

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertIn("清理已跳过", result.message)
            self.assertEqual(stored["cleanup_status"], "skipped")
            self.assertEqual(stored["cleanup_error"], "disabled")
            self.assertEqual(result.metadata["cleanup_status"], "skipped")
            self.assertEqual(result.metadata["cleanup_error"], "disabled")

    def test_cleaned_stage_deletes_source_after_emby_confirmed_and_own_share_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 101.0

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])
            self.assertEqual(stored["cleanup_status"], "deleted")
            self.assertEqual(result.metadata["cleanup_status"], "deleted")

    def test_cleaned_stage_does_not_delete_library_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path=str(dest),
                category_final="华语电影",
            ) or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {"submission_id": row["id"], "share_created_at": 100.0},
                row["id"],
            )
            workflow._now = lambda: 101.0

            result = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])
            self.assertEqual(stored["cleanup_status"], "deleted")
            self.assertNotEqual(stored.get("cleanup_file_id"), "folder-id")

    def test_cleaned_stage_deletes_redundant_receive_root_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            cleanup.parents["recv-folder-402"] = "redundant-cid"
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            workflow.self_share_config.source_cleanup_parent_ids = {"redundant-cid"}
            row = self._self_share_row(title="C-拆弹专家-2017-[tmdb=441531]", tmdb_id="441531")
            row = self.submissions.update_self_share(int(row["id"]), own_share_file_id="dest-c-441531") or row
            dest = Path(tmp) / "library" / "C-拆弹专家-2017-[tmdb=441531]"
            self._write_strm(dest)
            row = self.submissions.update_move(int(row["id"]), "moved", dest_path=str(dest), category_final="华语电影") or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                        "dest_id": "dest-c-441531",
                    },
                },
                row["id"],
            )
            workflow._now = lambda: 101.0
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, ["recv-folder-402"])
            self.assertNotIn("dest-c-441531", cleanup.deleted)

    def test_cleaned_stage_needs_action_when_root_parent_is_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            cleanup.parents["recv-folder-402"] = "movie-parent"
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(int(row["id"]), "moved", dest_path=str(dest), category_final="华语电影") or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "Movie.mkv"}],
                        "dest_id": "folder-id",
                    },
                },
                row["id"],
            )
            workflow._now = lambda: 101.0
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(cleanup.deleted, [])

    def test_cleaned_stage_deletes_root_without_file_parent_id_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            cleanup.parents["recv-folder-402"] = "redundant-cid"
            del cleanup.file_parent_id
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            workflow.self_share_config.source_cleanup_parent_ids = {"redundant-cid"}
            row = self._self_share_row(title="C-拆弹专家-2017-[tmdb=441531]", tmdb_id="441531")
            row = self.submissions.update_self_share(int(row["id"]), own_share_file_id="dest-c-441531") or row
            dest = Path(tmp) / "library" / "C-拆弹专家-2017-[tmdb=441531]"
            self._write_strm(dest)
            row = self.submissions.update_move(int(row["id"]), "moved", dest_path=str(dest), category_final="华语电影") or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "拆弹专家.2017.mkv"}],
                        "dest_id": "dest-c-441531",
                    },
                },
                row["id"],
            )
            workflow._now = lambda: 101.0
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, ["recv-folder-402"])
            self.assertNotIn("dest-c-441531", cleanup.deleted)

    def test_cleaned_stage_skips_root_when_parent_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            del cleanup.file_parent_id
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            workflow.self_share_config.review_grace_seconds = 1
            workflow.self_share_config.review_checkpoints_seconds = (1,)
            row = self._self_share_row()
            dest = Path(tmp) / "library" / row["own_share_file_name"]
            self._write_strm(dest)
            row = self.submissions.update_move(int(row["id"]), "moved", dest_path=str(dest), category_final="华语电影") or row
            row = self.submissions.update_emby(int(row["id"]), "confirmed") or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.CLEANED,
                {
                    "submission_id": row["id"],
                    "share_created_at": 100.0,
                    "intake_identity": {
                        "root_ids": ["recv-folder-402"],
                        "files": [{"id": "video-mkv-402", "name": "Movie.mkv"}],
                        "dest_id": "folder-id",
                    },
                },
                row["id"],
            )
            workflow._now = lambda: 101.0
            result = workflow.run_stage(task)
            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, [])


class DirectTaskEngineBridgeTests(unittest.TestCase):
    def test_direct_task_engine_intake_queues_without_cms_submission(self):
        class CmsWithoutDirectSubmission:
            def __init__(self):
                self.calls = 0

            def add_share_down(self, _url):
                self.calls += 1
                raise AssertionError("direct task engine must submit CMS from the worker")

        with tempfile.TemporaryDirectory() as tmp:
            cms = CmsWithoutDirectSubmission()
            telegram = FakeTelegram()
            submissions = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            task_store = TaskStore(Path(tmp) / "tasks.db", default_strm_mode="direct")

            bridge.handle_update(
                {
                    "message": {
                        "chat": {"id": "chat"},
                        "from": {"id": "chat"},
                        "text": "https://115cdn.com/s/direct?password=1234",
                    }
                },
                cms,
                telegram,
                "chat",
                submissions,
                poll_status=False,
                task_store=task_store,
                task_engine_enabled=True,
            )

            task = task_store.find_task_by_share_key("direct", "1234")

        self.assertEqual(cms.calls, 0)
        self.assertIsNotNone(task)
        self.assertEqual(task.metadata["strm_mode"], "direct")

    def test_direct_task_engine_run_forever_constructs_runtime_dependencies_once(self):
        captured = {}

        class FakeCmsClient:
            def __init__(self, *_args, **_kwargs):
                pass

        class FakeSelfShareConfig:
            enabled = False

            @classmethod
            def from_config(cls, _config, _cms):
                return cls()

        class FakeTelegramClient:
            def __init__(self, *_args, **_kwargs):
                self.messages = []

            def get_updates(self, **_kwargs):
                return [{"update_id": 1}]

            def send_rich_message(self, chat_id, document, reply_markup=None):
                self.messages.append((chat_id, document.to_plain(), reply_markup))

        class FakeTaskRunner:
            def __init__(self, _store, workflow, **kwargs):
                captured["workflow"] = workflow
                captured["p115_client"] = kwargs.get("p115_client")
                captured["worker_id"] = kwargs.get("worker_id")

            def start(self):
                captured["started"] = True

            def stop(self, **_kwargs):
                captured["stopped"] = True

        def capture_web_server(*_args, **kwargs):
            captured["web_background_jobs"] = kwargs["background_jobs"]
            captured["web_log_hub"] = kwargs.get("log_hub")
            return None

        def capture_update(*_args, **kwargs):
            captured["update_background_jobs"] = kwargs["background_jobs"]
            stop_event.set()

        with tempfile.TemporaryDirectory() as tmp:
            stop_event = __import__("threading").Event()
            hub = object()
            config = bridge.Config(
                "token",
                "chat",
                "http://cms.test",
                "user",
                "password",
                db_path=str(Path(tmp) / "submissions.db"),
                task_db_path=str(Path(tmp) / "tasks.db"),
                task_engine_enabled=True,
                strm_default_mode="direct",
                status_repair_enabled=False,
                web_enabled=False,
            )

            with patch.object(bridge, "CmsClient", FakeCmsClient), patch.object(
                bridge, "TelegramClient", FakeTelegramClient
            ), patch.object(bridge, "SelfShareConfig", FakeSelfShareConfig), patch.object(
                bridge, "TaskRunner", FakeTaskRunner
            ), patch.object(bridge, "normalize_emby_parents", lambda *_args, **_kwargs: 0), patch.object(
                bridge, "write_metrics_snapshot", lambda *_args, **_kwargs: None
            ), patch.object(bridge, "call_maybe_start_web_server", capture_web_server), patch.object(
                bridge, "handle_update", capture_update
            ), patch.object(
                bridge, "start_status_repair_loop", lambda *_args, **_kwargs: None
            ):
                bridge.run_forever(config, stop_event=stop_event, log_hub=hub)

        self.assertTrue(captured["started"])
        self.assertTrue(captured["stopped"])
        self.assertIsNone(captured["p115_client"])
        self.assertIs(captured["web_background_jobs"], captured["update_background_jobs"])
        self.assertIs(captured["web_log_hub"], hub)
        self.assertEqual(
            captured["update_background_jobs"].submit("after-shutdown", lambda: None).outcome,
            "closed",
        )
        worker_parts = captured["worker_id"].split(":")
        self.assertGreaterEqual(len(worker_parts), 3)
        self.assertEqual(worker_parts[-2], str(os.getpid()))
        self.assertEqual(len(worker_parts[-1]), 12)


if __name__ == "__main__":
    unittest.main()
