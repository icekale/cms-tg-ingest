#!/usr/bin/env python3
"""Offline diagnostics for cms-tg-ingest deployments."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
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


@dataclass(frozen=True)
class AuditIssue:
    row_id: int
    issue_type: str
    message: str

    def to_text(self) -> str:
        return f"{self.issue_type} row={self.row_id}: {self.message}"


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
    if workflow == "self_share_sync" and not _env_value(env, "SELF_SHARE_RECEIVE_CID"):
        warnings.append("SELF_SHARE_RECEIVE_CID is required for self_share_sync")
    if _env_value(env, "OPENAI_CLASSIFY_ENABLED").lower() in {"1", "true", "yes", "on"} and not _env_value(env, "OPENAI_API_KEY"):
        warnings.append("OPENAI_API_KEY is required when OpenAI fallback is enabled")
    if _env_value(env, "EMBY_BASE_URL") and not _env_value(env, "EMBY_API_KEY"):
        warnings.append("EMBY_API_KEY is required when EMBY_BASE_URL is set")
    if _env_value(env, "WEB_ENABLED").lower() in {"1", "true", "yes", "on"}:
        try:
            port = int(_env_value(env, "WEB_PORT") or "8787")
            if port <= 0 or port > 65535:
                warnings.append("WEB_PORT must be between 1 and 65535")
        except ValueError:
            warnings.append("WEB_PORT must be an integer")
    if warnings:
        return CheckItem("optional_env", False, "; ".join(warnings))
    return CheckItem("optional_env", True, "optional feature variables are consistent")


def _check_filesystem(env: Mapping[str, str], filesystem: Filesystem) -> CheckItem:
    problems: list[str] = []
    db_path = Path(_env_value(env, "DB_PATH") or "/data/submissions.db")
    if not filesystem.exists(db_path.parent):
        problems.append(f"DB directory does not exist: {db_path.parent}")
    task_db_path = Path(_env_value(env, "TASK_DB_PATH") or "/data/tasks.db")
    if not filesystem.exists(task_db_path.parent):
        problems.append(f"TASK_DB directory does not exist: {task_db_path.parent}")
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


def _extract_tmdb_id(value: str) -> str:
    match = re.search(r"tmdb(?:id)?[=_\-](\d+)", str(value or ""), re.I)
    return match.group(1) if match else ""


def _parse_recognition(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iter_strm_files(path_value: str | None) -> list[Path]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.exists():
        return []
    if path.is_file() and path.suffix.lower() == ".strm":
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.strm"))
    return []


def audit_submission_db(db_path: str | Path) -> list[AuditIssue]:
    db_path = Path(db_path)
    if not db_path.exists():
        return [AuditIssue(0, "db_missing", f"database does not exist: {db_path}")]
    issues: list[AuditIssue] = []
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'submissions'"
        ).fetchone()
        if not table:
            return [AuditIssue(0, "db_missing", "submissions table does not exist")]
        rows = conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()
    for sqlite_row in rows:
        row = dict(sqlite_row)
        row_id = int(row.get("id") or 0)
        title = str(row.get("title") or row.get("url") or "").strip()
        recognition = _parse_recognition(row.get("recognition_json"))
        expected_tmdb = str(recognition.get("tmdb_id") or "").strip()
        if expected_tmdb:
            seen_path_tmdb: set[str] = set()
            for key in ("dest_path", "source_path", "emby_path"):
                value = str(row.get(key) or "")
                path_tmdb = _extract_tmdb_id(value)
                if not path_tmdb or path_tmdb in seen_path_tmdb:
                    continue
                seen_path_tmdb.add(path_tmdb)
                if path_tmdb != expected_tmdb:
                    issues.append(
                        AuditIssue(
                            row_id,
                            "tmdb_mismatch",
                            f"{title} 任务 TMDB {expected_tmdb}，路径 TMDB {path_tmdb}，字段 {key}",
                        )
                    )
        own_share_code = str(row.get("own_share_code") or "").strip()
        receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
        expected_marker = f"/s/{own_share_code}_{receive_code}_" if own_share_code else ""
        for strm_path in _iter_strm_files(row.get("dest_path")):
            text = strm_path.read_text(encoding="utf-8", errors="replace")
            if "/d/" in text:
                issues.append(AuditIssue(row_id, "direct_strm", f"{title} 发现直链 STRM：{strm_path}"))
                continue
            if expected_marker and expected_marker not in text:
                issues.append(
                    AuditIssue(
                        row_id,
                        "unexpected_strm",
                        f"{title} STRM 不是预期的分享链接：{strm_path}",
                    )
                )
    return issues


def format_audit_summary(issues: list[AuditIssue], sample_limit: int = 10) -> str:
    if not issues:
        return "cms-tg-ingest DB audit summary\nOK no quality issues found"
    counts = Counter(issue.issue_type for issue in issues)
    row_ids = {issue.row_id for issue in issues if issue.row_id}
    lines = [
        "cms-tg-ingest DB audit summary",
        f"issues={len(issues)} affected_rows={len(row_ids)}",
    ]
    for issue_type, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"{issue_type}: {count}")
    limit = max(0, int(sample_limit))
    if limit:
        lines.append(f"samples(first {min(limit, len(issues))})")
        for issue in issues[:limit]:
            lines.append(issue.to_text())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline cms-tg-ingest deployment checks.")
    parser.add_argument("--quiet", action="store_true", help="print only failing checks")
    parser.add_argument("--audit-db", help="audit submissions.db for TMDB and STRM quality issues")
    parser.add_argument("--audit-summary", action="store_true", help="print condensed audit counts instead of every issue")
    parser.add_argument("--audit-sample-limit", type=int, default=10, help="number of audit samples to show in summary mode")
    args = parser.parse_args(argv)
    report = run_checks()
    if args.quiet:
        lines = ["cms-tg-ingest doctor"] + [f"FAIL {item.name}: {item.message}" for item in report.items if not item.ok]
        print("\n".join(lines))
    else:
        print(report.to_text())
    audit_issues: list[AuditIssue] = []
    if args.audit_db:
        audit_issues = audit_submission_db(args.audit_db)
        if args.audit_summary:
            print(format_audit_summary(audit_issues, sample_limit=args.audit_sample_limit))
        elif audit_issues:
            print("cms-tg-ingest DB audit")
            for issue in audit_issues:
                print("FAIL " + issue.to_text())
        elif not args.quiet:
            print("cms-tg-ingest DB audit\nOK no quality issues found")
    return 0 if report.ok and not audit_issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
