import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import TaskStage, TaskStatus
from app.quality import QualityIssue, inspect_task_files, format_task_quality_report, scan_task_quality
from app.task_store import TaskStore


class TaskQualityTests(unittest.TestCase):
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

            self.assertEqual([issue.code for issue in issues], ["direct_strm", "missing_dest"])
            self.assertIn("TaskStore 轻量巡检", report)
            self.assertIn("直链电影", report)
            self.assertIn("发现直链 STRM", report)
            self.assertIn("缺目录电影", report)
            self.assertIn("目标目录不存在", report)


if __name__ == "__main__":
    unittest.main()
