import json
import tempfile
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from app.clients import cms as cms_client
from app.models import TaskStage, TaskStatus
from app.task_runner import StageOutcome
from app.task_store import TaskStore


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
        self.folder = None
        self.created_shares = []
        self.find_organized_calls = []
        self.renamed = []
        self.share_statuses = []
        self.files_by_parent = {}

    def receive_share_to_cid(self, share_code, receive_code, receive_cid):
        self.received.append((share_code, receive_code, receive_cid))
        return {"title": "received title", "file_ids": ["file-a", "file-b"]}

    def find_organized_folder(self, recognition, title, excluded_parent_ids=None, min_update_time=0, **kwargs):
        self.find_organized_calls.append((dict(recognition), title, excluded_parent_ids, min_update_time, kwargs))
        return self.folder

    def create_long_share(self, file_id):
        self.created_shares.append(file_id)
        suffix = len(self.created_shares)
        return {
            "share_code": "owncode" if suffix == 1 else f"owncode{suffix}",
            "receive_code": "ownpwd",
            "share_url": f"https://115.com/s/owncode{'' if suffix == 1 else suffix}?password=ownpwd",
        }

    def rename_file(self, file_id, file_name):
        self.renamed.append((str(file_id), str(file_name)))
        return {"state": True}

    def inspect_share(self, share_code, receive_code):
        if self.share_statuses:
            return self.share_statuses.pop(0)
        return {"available": True, "share_state": "0", "have_vio_file": False}

    def list_files(self, parent_id, limit=100):
        return list(self.files_by_parent.get(str(parent_id), []))


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


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

    def delete_file(self, file_id):
        self.deleted.append(file_id)


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
        }
        row = self.submissions.update_recognition(int(row["id"]), recognition, "self_share_resolved") or row
        row = self.submissions.update_category(int(row["id"]), category, "selected") or row
        return row

    def _write_strm(self, folder, name="movie.strm", content="https://115.com/s/owncode_ownpwd_/movie.mkv"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(content, encoding="utf-8")

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

    def test_received_stage_stops_when_115_receive_is_restricted(self):
        class RestrictedP115(FakeP115):
            def receive_share_to_cid(self, share_code, receive_code, receive_cid):
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
            task = self._claim_task("abc", "1234", TaskStage.RECEIVED)

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(self.p115.received, [])
            self.assertEqual(result.metadata["submission_id"], row["id"])
            self.assertEqual(result.metadata["received_title"], "received title")

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
            workflow = self._workflow(tmp)
            row = self._row()
            row = self.submissions.update_self_share(
                int(row["id"]),
                workflow_mode="self_share_sync",
                workflow_phase="auto_organize_submitted",
                own_share_file_id="share-snapshot-id",
                own_share_file_name="基督山伯爵士 4K原盘REMUX [HDR]",
            ) or row
            task = self._claim_task(
                "abc",
                "1234",
                TaskStage.OWN_SHARE_CREATED,
                {
                    "submission_id": row["id"],
                    "received_file_ids": ["share-snapshot-id"],
                    "own_share_file_id": "share-snapshot-id",
                },
                row["id"],
            )

            result = workflow.run_stage(task)

            self.assertEqual(result.outcome, StageOutcome.NEEDS_ACTION)
            self.assertEqual(self.p115.created_shares, [])

    def test_share_alias_stage_renames_root_and_persists_canonical_manifest(self):
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
            manifest = json.loads(stored["canonical_manifest_json"])

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertTrue(stored["share_alias_name"].startswith(f"asset-{task.id}-"))
            self.assertEqual(self.p115.renamed, [("folder-id", stored["share_alias_name"])])
            self.assertEqual(manifest["root_name"], "S-双喜-2025-[tmdb=123456]")
            self.assertEqual(manifest["category"], "华语电影")
            self.assertEqual(manifest["tmdb_id"], "123456")
            self.assertEqual(manifest["entries"], [])

    def test_share_alias_stage_keeps_direct_file_fallback_when_folder_is_gone(self):
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
            self.assertTrue(result.metadata["direct_file_share_fallback"])
            self.assertFalse(stored["share_alias_name"])

    def test_share_validation_upgrades_violation_warning_to_neutral_video_names(self):
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
            manifest = json.loads(stored["canonical_manifest_json"])

            self.assertEqual(result.outcome, StageOutcome.DEFER)
            self.assertEqual(stored["share_alias_level"], 2)
            self.assertEqual(self.p115.renamed[-1][0], "episode-id")
            self.assertIn("S03E02", self.p115.renamed[-1][1])
            self.assertEqual(stored["own_share_code"], "owncode")
            self.assertEqual(manifest["entries"][0]["canonical_path"], "Season 03/Troy.S03E02.2160p.mkv")
            self.assertTrue(manifest["entries"][0]["alias_path"].endswith(".mkv"))

    def test_level_two_violation_warning_is_accepted_as_risk_when_share_is_available(self):
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

            self.assertEqual(result.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["share_validation_status"], "warning")
            self.assertEqual(cleanup.deleted, ["folder-id"])
            self.assertEqual(stored["cleanup_status"], "deleted")

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

            self.tasks.enqueue_task(task.id, TaskStage.SHARE_VALIDATED, next_run_at=1.0)
            validation_task = self.tasks.claim_next_runnable("worker-2", now=1.0)
            validated = workflow.run_stage(validation_task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(validated.outcome, StageOutcome.COMPLETE)
            self.assertEqual(cleanup.deleted, ["folder-id"])
            self.assertEqual(stored["cleanup_status"], "deleted")
            self.assertEqual(self.cms.auto_organize_calls, 1)
            self.assertTrue(validated.metadata["cleanup_sync_requested"])

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
            def create_long_share(self, file_id):
                self.created_shares.append(file_id)
                if file_id == "series-id":
                    raise RuntimeError("目录不存在或已转移")
                return {
                    "share_code": "file-share",
                    "receive_code": "1212",
                    "share_url": "https://115cdn.com/s/file-share?password=1212",
                }

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
            self.assertEqual(result.metadata["direct_strm_removed"], 0)
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
            cms_index = FakeCmsCloudIndex(indexed_file_ids={"folder-id"})
            workflow = self._workflow(
                tmp,
                move_config=bridge.MoveConfig(source_roots=[], library_roots={"华语电影": library_root}),
                cms_cloud_index=cms_index,
            )
            row = self._self_share_row()
            row = self.submissions.update_cleanup(int(row["id"]), "deleted", file_id="folder-id") or row
            source = self.config.strm_root / row["own_share_file_name"]
            self._write_strm(source)
            task = self._claim_task("abc", "1234", TaskStage.CMS_DELETE_SETTLED, {"submission_id": row["id"]}, row["id"])

            waiting = workflow.run_stage(task)
            cms_index.indexed_file_ids.clear()
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

    def test_moved_stage_restores_expected_direct_file_share_episode_before_completion(self):
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

            restored = workflow.run_stage(task)
            completed = workflow.run_stage(task)

            target = episode_dir / "权力的游戏前传：龙族 (2022) - S03E03.strm"
            self.assertEqual(restored.outcome, StageOutcome.DEFER)
            self.assertEqual(completed.outcome, StageOutcome.COMPLETE)
            self.assertIn("/s/owncode_ownpwd_", target.read_text(encoding="utf-8"))
            self.assertFalse(generated.exists())
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
            task = self._claim_task("abc", "1234", TaskStage.CLEANED, {"submission_id": row["id"]}, row["id"])

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
            self.assertEqual(cleanup.deleted, ["folder-id"])
            self.assertEqual(stored["cleanup_status"], "deleted")
            self.assertEqual(result.metadata["cleanup_status"], "deleted")


if __name__ == "__main__":
    unittest.main()
