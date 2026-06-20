from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEXT_FILE_GLOBS = ("*.md", "*.py", "*.sh", "*.yml", "*.yaml", "*.example", "Dockerfile")
EXCLUDED_PARTS = {".git", ".worktrees", "__pycache__", "data"}


class SecretHygieneTests(unittest.TestCase):
    def iter_text_files(self):
        for glob in TEXT_FILE_GLOBS:
            for path in ROOT.rglob(glob):
                if EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts):
                    continue
                if path.relative_to(ROOT).as_posix() == "scripts/diagnostics.sh":
                    continue
                yield path

    def test_repository_text_files_do_not_contain_known_secret_shapes(self):
        patterns = [
            re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
            re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b"),
            re.compile(r"\b[A-Fa-f0-9]{32}\b"),
        ]
        marker_fragments = [
            "SESS" + "DATA",
            "bili" + "_jct",
            "Dede" + "User" + "ID",
            "yan" + "sy102",
            "192.168." + "5.28",
        ]
        failures = []
        for path in self.iter_text_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                if pattern.search(text):
                    failures.append(f"{path.relative_to(ROOT)} matches {pattern.pattern}")
            for marker in marker_fragments:
                if marker in text:
                    failures.append(f"{path.relative_to(ROOT)} contains marker {marker}")

        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
