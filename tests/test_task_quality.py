import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.models import TaskStage, TaskStatus
from app.quality import QualityIssue, inspect_task_files, format_task_quality_report, scan_task_quality
from app.task_store import TaskStore


class TaskQualityTests(unittest.TestCase):
    def test_direct_mode_accepts_direct_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "movie.strm").write_text("http://cms/d/direct-file.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare", expected_mode="direct")

            self.assertEqual(issues, [])

    def test_direct_mode_flags_share_strm_as_unexpected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "movie.strm").write_text("http://cms/s/ownshare_1212_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare", expected_mode="direct")

            self.assertEqual([issue.code for issue in issues], ["unexpected_strm"])

    def test_allowed_roots_generator_is_reusable_for_strm_file_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed_root = root / "library"
            dest = allowed_root / "Movie"
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("http://cms/s/ownshare_1212_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            issues = inspect_task_files(
                task,
                dest_path=dest,
                own_share_code="ownshare",
                allowed_roots=(path for path in (allowed_root,)),
            )

            self.assertEqual(issues, [])

    def test_source_shared_mode_accepts_original_share_strm_without_own_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "movie.strm").write_text("http://cms/s/sourcecode_7788_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            issues = inspect_task_files(
                task,
                dest_path=dest,
                expected_mode="source_shared",
                own_share_code="different-own-share",
            )

            self.assertEqual(issues, [])

    def test_scan_task_quality_reports_invalid_persisted_strm_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "movie.strm").write_text("http://cms/s/sourcecode_7788_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            invalid_task = replace(
                task,
                title="Invalid mode",
                metadata={"dest_path": str(dest), "strm_mode": "not-a-mode"},
            )

            issues = scan_task_quality(store, tasks=[invalid_task])

            self.assertEqual([issue.code for issue in issues], ["invalid_strm_mode"])
            self.assertEqual(issues[0].task_id, task.id)

    def test_scan_task_quality_uses_task_number_when_received_title_is_share_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("secret-share", "", "https://115cdn.com/s/secret-share")
            invalid_task = replace(
                task,
                title="",
                metadata={"received_title": "secret-share", "dest_path": str(dest), "strm_mode": "invalid"},
            )
            issue = scan_task_quality(store, tasks=[invalid_task])[0]
            self.assertEqual(issue.title, f"任务 #{task.id}")
            self.assertNotIn("secret-share", issue.title)

    def test_flags_direct_strm_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "movie.strm").write_text("http://cms/d/direct-file.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            task = store.record_event(task.id, TaskStage.MOVED, TaskStatus.SUCCEEDED, "moved")

            issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare")

            self.assertEqual(issues, [QualityIssue("direct_strm", "发现直链 STRM", str(dest / "movie.strm"))])

    def test_flags_uppercase_direct_strm_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "MOVIE.STRM").write_text("http://cms/d/direct-file.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare")

            self.assertEqual(issues, [QualityIssue("direct_strm", "发现直链 STRM", str(dest / "MOVIE.STRM"))])

    def test_accepts_self_share_strm_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "movie.strm").write_text("http://cms/s/ownshare_1212_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare")

            self.assertEqual(issues, [])

    def test_accepts_self_share_strm_url_with_custom_receive_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            (dest / "movie.strm").write_text("http://cms/s/ownshare_abcd_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare", own_share_receive_code="abcd")

            self.assertEqual(issues, [])

    def test_flags_missing_dest_and_missing_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")

            missing = inspect_task_files(task, dest_path=root / "missing", own_share_code="ownshare")
            empty_dir = root / "empty"
            empty_dir.mkdir()
            empty = inspect_task_files(task, dest_path=empty_dir, own_share_code="ownshare")

            self.assertEqual(missing[0].code, "missing_dest")
            self.assertEqual(empty[0].code, "missing_strm")

    def test_unreadable_strm_is_reported_without_aborting_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            bad = dest / "bad.strm"
            good = dest / "good.strm"
            bad.write_text("ignored", encoding="utf-8")
            good.write_text("http://cms/s/ownshare_1212_fileid.mkv", encoding="utf-8")
            task = TaskStore(root / "tasks.db").upsert_task("abc", "", "https://115cdn.com/s/abc")
            original_read_text = Path.read_text

            def read_text(path, *args, **kwargs):
                if path == bad:
                    raise OSError("permission denied")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=read_text):
                issues = inspect_task_files(task, dest_path=dest, own_share_code="ownshare")

            self.assertEqual([issue.code for issue in issues], ["unreadable_strm"])
            self.assertIn(str(bad), issues[0].detail)

    def test_scan_reuses_directory_reads_but_keeps_per_task_marker_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Movie"
            dest.mkdir()
            strm = dest / "movie.strm"
            strm.write_text("http://cms/s/first_1212_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            tasks = []
            for share_code, own_share_code in (("first-task", "first"), ("second-task", "second")):
                task = store.upsert_task(share_code, "", f"https://115cdn.com/s/{share_code}")
                tasks.append(
                    store.record_event(
                        task.id,
                        TaskStage.MOVED,
                        TaskStatus.SUCCEEDED,
                        "moved",
                        metadata_patch={"dest_path": str(dest), "own_share_code": own_share_code},
                    )
                )
            original_read_text = Path.read_text
            reads = []

            def read_text(path, *args, **kwargs):
                reads.append(path)
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=read_text):
                issues = scan_task_quality(store, tasks=tasks)

            self.assertEqual(reads, [strm])
            self.assertEqual([(issue.task_id, issue.code) for issue in issues], [(tasks[1].id, "unexpected_strm")])

    def test_scan_accepts_any_live_share_identity_for_same_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Series"
            dest.mkdir()
            (dest / "old.strm").write_text("http://cms/s/oldshare_1212_fileid.mkv", encoding="utf-8")
            (dest / "new.strm").write_text("http://cms/s/newshare_1212_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            tasks = []
            for share_code, own_share_code in (("old-task", "oldshare"), ("new-task", "newshare")):
                task = store.upsert_task(share_code, "", f"https://115cdn.com/s/{share_code}")
                tasks.append(
                    store.record_event(
                        task.id,
                        TaskStage.MOVED,
                        TaskStatus.SUCCEEDED,
                        "moved",
                        metadata_patch={
                            "dest_path": str(dest),
                            "own_share_code": own_share_code,
                            "own_share_receive_code": "1212",
                            "tmdb_id": "1416",
                        },
                    )
                )

            issues = scan_task_quality(
                store,
                tasks=tasks,
                share_identity_resolver=lambda current: (("oldshare", "1212"), ("newshare", "1212")),
            )

            self.assertEqual(issues, [])

    def test_scan_flags_share_code_outside_live_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "Series"
            dest.mkdir()
            (dest / "dead.strm").write_text("http://cms/s/deadshare_1212_fileid.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("live-task", "", "https://115cdn.com/s/live-task")
            task = store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={
                    "dest_path": str(dest),
                    "own_share_code": "newshare",
                    "own_share_receive_code": "1212",
                    "tmdb_id": "1416",
                },
            )

            issues = scan_task_quality(
                store,
                tasks=[task],
                share_identity_resolver=lambda current: (("oldshare", "1212"), ("newshare", "1212")),
            )

            self.assertEqual([(issue.task_id, issue.code) for issue in issues], [(task.id, "unexpected_strm")])

    def test_scan_rejects_strm_symlink_target_outside_allowed_root_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed_root = root / "library"
            dest = allowed_root / "Movie"
            outside = root / "outside" / "movie.strm"
            dest.mkdir(parents=True)
            outside.parent.mkdir()
            outside.write_text("http://cms/d/outside.mkv", encoding="utf-8")
            (dest / "movie.strm").symlink_to(outside)
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("symlink", "", "https://115cdn.com/s/symlink")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                metadata_patch={"dest_path": str(dest), "own_share_code": "own"},
            )

            with patch.object(Path, "read_text", side_effect=AssertionError("unsafe STRM was read")):
                issues = scan_task_quality(store, allowed_roots=[allowed_root])

            self.assertEqual([issue.code for issue in issues], ["unsafe_metadata"])



    def test_scan_task_quality_flags_local_taskstore_file_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / "tasks.db")
            missing = store.upsert_task("missing", "", "https://115cdn.com/s/missing")
            store.record_event(
                missing.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                title="缺目录电影",
                metadata_patch={"dest_path": str(root / "missing-dest"), "own_share_code": "ownmissing"},
            )
            direct_dest = root / "direct-dest"
            direct_dest.mkdir()
            (direct_dest / "movie.strm").write_text("https://115.com/d/direct-file.mkv", encoding="utf-8")
            direct = store.upsert_task("direct", "", "https://115cdn.com/s/direct")
            store.record_event(
                direct.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                title="直链电影",
                metadata_patch={"dest_path": str(direct_dest), "own_share_code": "owndirect"},
            )
            custom_dest = root / "custom-dest"
            custom_dest.mkdir()
            (custom_dest / "movie.strm").write_text("https://115.com/s/owncustom_abcd_file.mkv", encoding="utf-8")
            custom = store.upsert_task("custom", "", "https://115cdn.com/s/custom")
            store.record_event(
                custom.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                title="自定义提取码电影",
                metadata_patch={
                    "dest_path": str(custom_dest),
                    "own_share_code": "owncustom",
                    "own_share_receive_code": "abcd",
                },
            )

            issues = scan_task_quality(store)
            report = format_task_quality_report(issues)
            text = report.to_plain()

            self.assertEqual([issue.code for issue in issues], ["direct_strm", "missing_dest"])
            self.assertIn("TaskStore 轻量巡检", text)
            self.assertIn("直链电影", text)
            self.assertIn("发现直链 STRM", text)
            self.assertIn("缺目录电影", text)
            self.assertIn("目标目录不存在", text)


if __name__ == "__main__":
    unittest.main()
