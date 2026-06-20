#!/usr/bin/env python3
"""Offline diagnostics for cms-tg-ingest deployments."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

REQUIRED_ENV = ("TG_BOT_TOKEN", "TG_ALLOWED_CHAT_ID", "CMS_BASE_URL", "CMS_USERNAME", "CMS_PASSWORD")
SECRET_MARKERS = ("TOKEN", "PASSWORD", "KEY", "COOKIE", "SECRET")


class Filesystem(Protocol):
    def exists(self, path: Path) -> bool: ...
    def is_file(self, path: Path) -> bool: ...
    def is_dir(self, path: Path) -> bool: ...


class RealFilesystem:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()


class MemoryFilesystem:
    def __init__(self, existing_paths: set[str | Path]):
        self.existing_paths = {str(Path(path)) for path in existing_paths}

    def exists(self, path: Path) -> bool:
        return str(path) in self.existing_paths

    def is_file(self, path: Path) -> bool:
        return self.exists(path)

    def is_dir(self, path: Path) -> bool:
        return self.exists(path)


@dataclass(frozen=True)
class CheckItem:
    name: str
    ok: bool
    message: str


@dataclass(frozen=True)
class DoctorReport:
    items: list[CheckItem]

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.items)

    def to_text(self) -> str:
        lines = ["cms-tg-ingest doctor"]
        for item in self.items:
            status = "OK" if item.ok else "FAIL"
            lines.append(f"{status} {item.name}: {item.message}")
        return "\n".join(lines)


def _env_value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "")).strip()


def _split_env_list(value: str) -> list[str]:
    parts: list[str] = []
    for raw in value.replace("|", ",").split(","):
        item = raw.strip()
        if item:
            parts.append(item)
    return parts


def _parse_library_map(value: str) -> list[Path]:
    paths: list[Path] = []
    for item in _split_env_list(value):
        if "=" not in item:
            continue
        _, path = item.split("=", 1)
        path = path.strip()
        if path:
            paths.append(Path(path))
    return paths


def _check_required_env(env: Mapping[str, str]) -> CheckItem:
    missing = [name for name in REQUIRED_ENV if not _env_value(env, name)]
    if missing:
        return CheckItem("required_env", False, "missing " + ", ".join(missing))
    return CheckItem("required_env", True, "all required variables are present")


def _check_optional_env(env: Mapping[str, str]) -> CheckItem:
    warnings: list[str] = []
    workflow = _env_value(env, "WORKFLOW_MODE") or "direct"
    if workflow not in {"direct", "self_share_sync"}:
        warnings.append("WORKFLOW_MODE should be direct or self_share_sync")
    if workflow == "self_share_sync" and not _env_value(env, "P115_COOKIE_PATH"):
        warnings.append("P115_COOKIE_PATH is required for self_share_sync")
    if _env_value(env, "OPENAI_CLASSIFY_ENABLED").lower() in {"1", "true", "yes", "on"} and not _env_value(env, "OPENAI_API_KEY"):
        warnings.append("OPENAI_API_KEY is required when OpenAI fallback is enabled")
    if _env_value(env, "EMBY_BASE_URL") and not _env_value(env, "EMBY_API_KEY"):
        warnings.append("EMBY_API_KEY is required when EMBY_BASE_URL is set")
    if warnings:
        return CheckItem("optional_env", False, "; ".join(warnings))
    return CheckItem("optional_env", True, "optional feature variables are consistent")


def _check_filesystem(env: Mapping[str, str], filesystem: Filesystem) -> CheckItem:
    problems: list[str] = []
    db_path = Path(_env_value(env, "DB_PATH") or "/data/submissions.db")
    if not filesystem.exists(db_path.parent):
        problems.append(f"DB directory does not exist: {db_path.parent}")
    workflow = _env_value(env, "WORKFLOW_MODE") or "direct"
    if workflow == "self_share_sync":
        cookie = Path(_env_value(env, "P115_COOKIE_PATH") or "/config/115-cookies.txt")
        if not filesystem.is_file(cookie):
            problems.append(f"115 cookie file does not exist: {cookie}")
        share_root = Path(_env_value(env, "SELF_SHARE_STRM_ROOT") or "/mnt/user/Unraid/strm/share")
        if not filesystem.is_dir(share_root):
            problems.append(f"self-share STRM root does not exist: {share_root}")
    for root in _split_env_list(_env_value(env, "STRM_SOURCE_ROOTS") or "/mnt/user/Unraid/strm/转存"):
        if not filesystem.is_dir(Path(root)):
            problems.append(f"STRM source root does not exist: {root}")
    for root in _parse_library_map(_env_value(env, "STRM_LIBRARY_MAP")):
        if not filesystem.is_dir(root):
            problems.append(f"library root does not exist: {root}")
    if problems:
        return CheckItem("filesystem", False, "; ".join(problems))
    return CheckItem("filesystem", True, "configured local paths exist")


def run_checks(env: Mapping[str, str] | None = None, filesystem: Filesystem | None = None) -> DoctorReport:
    env = os.environ if env is None else env
    filesystem = RealFilesystem() if filesystem is None else filesystem
    return DoctorReport([
        _check_required_env(env),
        _check_optional_env(env),
        _check_filesystem(env, filesystem),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline cms-tg-ingest deployment checks.")
    parser.add_argument("--quiet", action="store_true", help="print only failing checks")
    args = parser.parse_args(argv)
    report = run_checks()
    if args.quiet:
        lines = ["cms-tg-ingest doctor"] + [f"FAIL {item.name}: {item.message}" for item in report.items if not item.ok]
        print("\n".join(lines))
    else:
        print(report.to_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
