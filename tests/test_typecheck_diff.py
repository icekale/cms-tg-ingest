"""scripts/typecheck_diff.py 的改动行解析：只认新增/修改行，纯删除的 hunk 不算。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from typecheck_diff import parse_hunks  # noqa: E402


class ParseHunksTests(unittest.TestCase):
    def test_collects_added_lines_from_each_hunk(self):
        diff = (
            "diff --git a/app/x.py b/app/x.py\n"
            "--- a/app/x.py\n"
            "+++ b/app/x.py\n"
            "@@ -10,0 +11,2 @@\n"
            "+new_a\n"
            "+new_b\n"
            "@@ -30 +33 @@\n"
            "-old\n"
            "+new_c\n"
        )
        self.assertEqual(parse_hunks(diff), {"app/x.py": {11, 12, 33}})

    def test_new_file_marks_every_line(self):
        diff = (
            "diff --git a/scripts/new.py b/scripts/new.py\n"
            "--- /dev/null\n"
            "+++ b/scripts/new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+a\n+b\n+c\n"
        )
        self.assertEqual(parse_hunks(diff), {"scripts/new.py": {1, 2, 3}})

    def test_deletion_only_hunk_is_dropped(self):
        diff = (
            "diff --git a/app/x.py b/app/x.py\n"
            "--- a/app/x.py\n"
            "+++ b/app/x.py\n"
            "@@ -3,2 +2,0 @@\n"
            "-gone_a\n-gone_b\n"
        )
        self.assertEqual(parse_hunks(diff), {})
    def test_test_files_are_skipped(self):
        diff = (
            "diff --git a/tests/t.py b/tests/t.py\n"
            "--- a/tests/t.py\n"
            "+++ b/tests/t.py\n"
            "@@ -0,0 +1 @@\n"
            "+line\n"
        )
        self.assertEqual(parse_hunks(diff), {})


if __name__ == "__main__":
    unittest.main()
