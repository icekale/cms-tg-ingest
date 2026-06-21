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
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


class FakeEmby:
    enabled = True

    def __init__(self):
        self.items_by_tmdb = {}
        self.recent = []

    def find_item_by_tmdb(self, tmdb_id):
        return self.items_by_tmdb.get(str(tmdb_id))

    def recent_items(self, limit=30):
        return self.recent[:limit]

    def library_name_for_item(self, item):
        return item.get("LibraryName")


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

    def test_recognizing_stage_persists_openai_suggestion_and_reuses_without_recalling_openai(self):
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
            self.assertEqual(len(classifier.calls), 1)
            self.assertEqual(second.metadata["recognition"]["category_status"], "openai_suggested")
            self.assertEqual(second.metadata["recognition"]["category_suggestion"], "外国电视")

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

    def test_strm_ready_stage_defers_until_own_share_strm_source_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = self._workflow(tmp)
            row = self._self_share_row()
            task = self._claim_task("abc", "1234", TaskStage.STRM_READY, {"submission_id": row["id"]}, row["id"])

            waiting = workflow.run_stage(task)
            self._write_strm(self.config.strm_root / row["own_share_file_name"])
            ready = workflow.run_stage(task)

            self.assertEqual(waiting.outcome, StageOutcome.DEFER)
            self.assertIn("等待自有分享 STRM", waiting.message)
            self.assertEqual(ready.outcome, StageOutcome.COMPLETE)
            self.assertEqual(ready.metadata["category"], "华语电影")
            self.assertEqual(ready.metadata["source_path"], str(bridge.safe_resolve(self.config.strm_root / row["own_share_file_name"])))
            self.assertEqual(ready.metadata["recognition"]["tmdb_id"], "123456")

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
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path=str(self.config.strm_root / row["own_share_file_name"]),
                dest_path="/library/movies/S-双喜-2025-[tmdb=123456]",
                category_final="华语电影",
            ) or row
            task = self._claim_task("abc", "1234", TaskStage.EMBY_CONFIRMED, {"submission_id": row["id"]}, row["id"])

            waiting = workflow.run_stage(task)
            emby.items_by_tmdb["123456"] = {
                "Id": "emby-item",
                "Name": "双喜",
                "Path": "/library/movies/S-双喜-2025-[tmdb=123456]",
                "ParentId": "parent-id",
                "LibraryName": "电影库",
            }
            confirmed = workflow.run_stage(task)
            stored = self.submissions.find_by_id(int(row["id"]))

            self.assertEqual(waiting.outcome, StageOutcome.DEFER)
            self.assertEqual(confirmed.outcome, StageOutcome.COMPLETE)
            self.assertEqual(stored["emby_status"], "confirmed")
            self.assertEqual(stored["emby_item_id"], "emby-item")
            self.assertEqual(stored["emby_parent"], "电影库")
            self.assertEqual(confirmed.metadata["library"], "电影库")
            self.assertEqual(stored["cleanup_status"], None)

    def test_cleaned_stage_requires_emby_confirmed_and_own_share_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            row = self._self_share_row()
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path="/library/dest",
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

    def test_cleaned_stage_deletes_source_after_emby_confirmed_and_own_share_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup = FakeCleanupClient()
            workflow = self._workflow(tmp, cleanup_client=cleanup)
            row = self._self_share_row()
            row = self.submissions.update_move(
                int(row["id"]),
                "moved",
                source_path="/share/source",
                dest_path="/library/dest",
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
