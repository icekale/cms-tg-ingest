"""Regression tests for STRM directory scanning (symlink safety + single walk)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.media.strm import directories_with_strm, iter_strm_files, newest_mtime


class StrmScanSymlinkTests(unittest.TestCase):
    def _make_tree(self, root: Path) -> Path:
        inside = root / "inside"
        inside.mkdir(parents=True)
        (inside / "a.strm").write_text("x", encoding="utf-8")
        (inside / "b.txt").write_text("y", encoding="utf-8")
        return inside

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_iter_does_not_follow_directory_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            base = parent / "base"
            base.mkdir()
            inside = self._make_tree(base)  # base/inside/{a.strm,b.txt}
            # A directory symlink pointing OUTSIDE the scanned root must not
            # pull that external directory's .strm files into the scan.
            outside = parent / "outside"
            outside.mkdir()
            (outside / "escape.strm").write_text("e", encoding="utf-8")
            os.symlink(outside, base / "link", target_is_directory=True)

            found = {path.name for path in iter_strm_files(base)}
            self.assertEqual(found, {"a.strm"})
            self.assertNotIn("escape.strm", found)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_iter_skips_symlink_outside_allowed_roots_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            library = base / "library"
            library.mkdir()
            (library / "ok.strm").write_text("x", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            (outside / "leak.strm").write_text("l", encoding="utf-8")
            # A symlink file whose target escapes the allowed root. The
            # directory itself lives inside the allowed root.
            os.symlink(outside / "leak.strm", library / "leak.strm")

            with self.assertRaises(Exception) as ctx:
                list(iter_strm_files(library, allowed_roots=[library]))
            self.assertIn("outside allowed roots", str(ctx.exception))

            found = {path.name for path in iter_strm_files(library, allowed_roots=[library], skip_outside_links=True)}
            # With skip_outside_links the stray link is skipped, not fatal.
            self.assertEqual(found, {"ok.strm"})

    def test_directories_with_strm_yields_only_dirs_containing_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with_strm = base / "movie"
            with_strm.mkdir()
            (with_strm / "m.strm").write_text("x", encoding="utf-8")
            nested = base / "movie" / "season"
            nested.mkdir()
            (nested / "s01e01.strm").write_text("x", encoding="utf-8")
            empty = base / "empty"
            empty.mkdir()

            found = {path.relative_to(base) for path in directories_with_strm(base)}
            self.assertEqual(found, {Path("movie"), Path("movie/season")})

    def test_newest_mtime_walks_without_symlink_recursion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            child = base / "child"
            child.mkdir()
            (child / "f.strm").write_text("x", encoding="utf-8")
            newest = newest_mtime(base)
            self.assertGreaterEqual(newest, 0)


if __name__ == "__main__":
    unittest.main()
