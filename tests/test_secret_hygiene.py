from pathlib import Path
import re
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEXT_FILE_GLOBS = ("*.md", "*.py", "*.sh", "*.yml", "*.yaml", "*.example", "Dockerfile")
EXCLUDED_PARTS = {".git", ".worktrees", "__pycache__", "data"}
IGNORED_GENERATED_PATH_PREFIXES = (("frontend", "node_modules"), ("frontend", "dist"))
PUBLIC_SAMPLE_HEX = {"542a1c1fe6ac4a5aab152" + "369079596b5"}


def is_excluded_path(path):
    parts = path.relative_to(ROOT).parts
    return bool(EXCLUDED_PARTS.intersection(parts)) or any(
        parts[:len(prefix)] == prefix for prefix in IGNORED_GENERATED_PATH_PREFIXES
    )


class SecretHygieneTests(unittest.TestCase):
    def iter_text_files(self):
        for glob in TEXT_FILE_GLOBS:
            for path in ROOT.rglob(glob):
                if is_excluded_path(path):
                    continue
                if path.relative_to(ROOT).as_posix() == "scripts/diagnostics.sh":
                    continue
                yield path

    def test_generated_frontend_directories_are_excluded(self):
        frontend_node_modules = ROOT / "frontend" / "node_modules" / "package" / "README.md"
        frontend_dist = ROOT / "frontend" / "dist" / "assets" / "README.md"
        tools_node_modules = ROOT / "tools" / "node_modules" / "README.md"
        tools_dist = ROOT / "tools" / "dist" / "README.md"

        with patch.object(type(ROOT), "rglob", return_value=(
            frontend_node_modules,
            frontend_dist,
            tools_node_modules,
            tools_dist,
        )):
            scanned = set(self.iter_text_files())

        self.assertNotIn(frontend_node_modules, scanned)
        self.assertNotIn(frontend_dist, scanned)
        self.assertIn(tools_node_modules, scanned)
        self.assertIn(tools_dist, scanned)

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
                matches = [match.group(0) for match in pattern.finditer(text) if match.group(0) not in PUBLIC_SAMPLE_HEX]
                if matches:
                    failures.append(f"{path.relative_to(ROOT)} matches {pattern.pattern}")
            for marker in marker_fragments:
                if marker in text:
                    failures.append(f"{path.relative_to(ROOT)} contains marker {marker}")

        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
