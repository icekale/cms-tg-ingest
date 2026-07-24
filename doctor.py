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
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED_ENV = ("TG_BOT_TOKEN", "TG_ALLOWED_CHAT_ID", "CMS_BASE_URL", "CMS_USERNAME", "CMS_PASSWORD")
SECRET_MARKERS = ("TOKEN", "PASSWORD", "KEY", "COOKIE", "SECRET")


class Filesystem(Protocol):
    def exists(self, path: Path) -> bool: ...
    def is_file(self, path: Path) -> bool: ...
    def is_dir(self, path: Path) -> bool: ...
    def is_writable(self, path: Path) -> bool: ...


class RealFilesystem:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_writable(self, path: Path) -> bool:
        return os.access(path, os.W_OK)


class MemoryFilesystem:
    def __init__(self, existing_paths: set[str | Path]):
        self.existing_paths = {str(Path(path)) for path in existing_paths}

    def exists(self, path: Path) -> bool:
        return str(path) in self.existing_paths

    def is_file(self, path: Path) -> bool:
        return self.exists(path)

    def is_dir(self, path: Path) -> bool:
        return self.exists(path)

    def is_writable(self, path: Path) -> bool:
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


def _env_bool(env: Mapping[str, str], name: str) -> bool:
    return _env_value(env, name).lower() in {"1", "true", "yes", "on", "enabled", "enable"}


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
    task_engine_enabled = _env_bool(env, "TASK_ENGINE_ENABLED")
    if workflow not in {"direct", "self_share_sync"}:
        warnings.append("WORKFLOW_MODE should be direct or self_share_sync")
    if task_engine_enabled and workflow != "self_share_sync":
        warnings.append("Task engine currently requires WORKFLOW_MODE=self_share_sync")
    if workflow == "self_share_sync" and not _env_value(env, "P115_COOKIE_PATH"):
        warnings.append("P115_COOKIE_PATH is required for self_share_sync")
    if workflow == "self_share_sync" and not _env_value(env, "SELF_SHARE_RECEIVE_CID"):
        warnings.append("SELF_SHARE_RECEIVE_CID is required for self_share_sync")
    if workflow == "self_share_sync":
        try:
            cloud_poll_seconds = int(_env_value(env, "SELF_SHARE_CLOUD_POLL_SECONDS") or "30")
            if cloud_poll_seconds < 30:
                warnings.append("SELF_SHARE_CLOUD_POLL_SECONDS must be at least 30 seconds")
        except ValueError:
            warnings.append("SELF_SHARE_CLOUD_POLL_SECONDS must be an integer")
        try:
            cloud_timeout_seconds = int(_env_value(env, "SELF_SHARE_CLOUD_TIMEOUT_SECONDS") or "86400")
            if cloud_timeout_seconds <= 0:
                warnings.append("SELF_SHARE_CLOUD_TIMEOUT_SECONDS must be a positive number")
        except ValueError:
            warnings.append("SELF_SHARE_CLOUD_TIMEOUT_SECONDS must be a positive number")
    if _env_bool(env, "OPENAI_CLASSIFY_ENABLED") and not _env_value(env, "OPENAI_API_KEY"):
        warnings.append("OPENAI_API_KEY is required when OpenAI fallback is enabled")
    if _env_value(env, "EMBY_BASE_URL") and not _env_value(env, "EMBY_API_KEY"):
        warnings.append("EMBY_API_KEY is required when EMBY_BASE_URL is set")
    if _env_bool(env, "WEB_ENABLED"):
        if not _env_value(env, "WEB_TOKEN"):
            warnings.append("WEB_TOKEN is required when WEB_ENABLED is enabled")
        try:
            port = int(_env_value(env, "WEB_PORT") or "8787")
            if port <= 0 or port > 65535:
                warnings.append("WEB_PORT must be between 1 and 65535")
        except ValueError:
            warnings.append("WEB_PORT must be an integer")
    if task_engine_enabled and _env_value(env, "TASK_WORKER_INTERVAL_SECONDS"):
        try:
            interval = float(_env_value(env, "TASK_WORKER_INTERVAL_SECONDS"))
            if interval <= 0:
                warnings.append("TASK_WORKER_INTERVAL_SECONDS must be a positive number")
        except ValueError:
            warnings.append("TASK_WORKER_INTERVAL_SECONDS must be a positive number")
    if warnings:
        return CheckItem("optional_env", False, "; ".join(warnings))
    return CheckItem("optional_env", True, "optional feature variables are consistent")


def _check_runtime_safety(env: Mapping[str, str]) -> CheckItem:
    warnings: list[str] = []
    if _env_bool(env, "WEB_ENABLED"):
        host = _env_value(env, "WEB_HOST") or "0.0.0.0"
        if host in {"0.0.0.0", "::", "*"} and not _env_value(env, "WEB_TOKEN"):
            warnings.append("WEB is exposed on all interfaces without WEB_TOKEN")
    if _env_value(env, "TASK_MAX_CONCURRENT"):
        warnings.append("TASK_MAX_CONCURRENT is unsupported; TaskRunner intentionally uses one worker")
    if warnings:
        return CheckItem("runtime_safety", True, "WARNING " + "; ".join(warnings))
    return CheckItem("runtime_safety", True, "runtime safety settings are explicit")


def _check_filesystem(env: Mapping[str, str], filesystem: Filesystem) -> CheckItem:
    problems: list[str] = []
    db_path = Path(_env_value(env, "DB_PATH") or "/data/submissions.db")
    if not filesystem.exists(db_path.parent):
        problems.append(f"DB directory does not exist: {db_path.parent}")
    task_db_path = Path(_env_value(env, "TASK_DB_PATH") or "/data/tasks.db")
    if not filesystem.exists(task_db_path.parent):
        problems.append(f"TASK_DB directory does not exist: {task_db_path.parent}")
    elif _env_bool(env, "TASK_ENGINE_ENABLED") and not filesystem.is_writable(task_db_path.parent):
        problems.append(f"TASK_DB directory is not writable: {task_db_path.parent}")
    workflow = _env_value(env, "WORKFLOW_MODE") or "direct"
    if _env_bool(env, "HDHIVE_ENABLED"):
        token_path = Path(_env_value(env, "HDHIVE_TOKEN_CONFIG_PATH") or "/config/cms-config/hdhive-openapi.json")
        if not filesystem.is_file(token_path):
            problems.append(f"HDHive token file does not exist: {token_path}")
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


def _hdhive_next_run(run_time: datetime_time, timezone: ZoneInfo) -> str:
    now = datetime.now(timezone)
    candidate = now.replace(
        hour=run_time.hour,
        minute=run_time.minute,
        second=0,
        microsecond=0,
    )
    if now >= candidate:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="minutes")


def _hdhive_subscription_counts(db_path: Path) -> tuple[int, int, str]:
    if not db_path.exists():
        return 0, 0, "not-created"
    try:
        with closing(sqlite3.connect(db_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "hdhive_subscriptions" not in tables or "hdhive_subscription_items" not in tables:
                return 0, 0, "not-initialized"
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM hdhive_subscriptions WHERE status = 'active'"
                ).fetchone()[0]
            )
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM hdhive_subscription_items WHERE status = 'pending_confirmation'"
                ).fetchone()[0]
            )
            return active, pending, "ready"
    except sqlite3.Error as exc:
        return 0, 0, f"error: {exc}"


def _check_hdhive_subscriptions(env: Mapping[str, str], filesystem: Filesystem) -> CheckItem:
    if not _env_bool(env, "HDHIVE_ENABLED"):
        return CheckItem("hdhive_subscriptions", True, "HDHive disabled")

    problems: list[str] = []
    token_path = Path(_env_value(env, "HDHIVE_TOKEN_CONFIG_PATH") or "/config/cms-config/hdhive-openapi.json")
    if not filesystem.is_file(token_path):
        problems.append(f"HDHive OAuth token file does not exist: {token_path}")

    time_value = _env_value(env, "HDHIVE_SUBSCRIPTION_TIME") or "01:30"
    timezone_value = _env_value(env, "HDHIVE_SUBSCRIPTION_TIMEZONE") or "Asia/Shanghai"
    try:
        if re.fullmatch(r"\d{2}:\d{2}", time_value) is None:
            raise ValueError
        hour, minute = (int(part) for part in time_value.split(":", 1))
        run_time = datetime_time(hour, minute)
    except ValueError:
        run_time = datetime_time(1, 30)
        problems.append("HDHIVE_SUBSCRIPTION_TIME must be a valid HH:MM time")
    try:
        timezone = ZoneInfo(timezone_value)
    except (ValueError, ZoneInfoNotFoundError):
        timezone = ZoneInfo("Asia/Shanghai")
        problems.append("HDHIVE_SUBSCRIPTION_TIMEZONE must be a valid IANA timezone")

    db_path = Path(_env_value(env, "TASK_DB_PATH") or "/data/tasks.db")
    active, pending, database_status = _hdhive_subscription_counts(db_path)
    if database_status.startswith("error:"):
        problems.append(f"HDHive subscription database cannot be read: {database_status[6:]}")
    message = (
        f"scheduler={'enabled' if _env_bool(env, 'HDHIVE_SUBSCRIPTION_AUTO_ENABLED') else 'disabled'} "
        f"schedule={time_value} {timezone_value} next_run={_hdhive_next_run(run_time, timezone)} "
        f"database={database_status} active={active} pending_confirmation={pending}"
    )
    if problems:
        return CheckItem("hdhive_subscriptions", False, "; ".join(problems) + "; " + message)
    return CheckItem("hdhive_subscriptions", True, message)


def run_checks(env: Mapping[str, str] | None = None, filesystem: Filesystem | None = None) -> DoctorReport:
    env = os.environ if env is None else env
    filesystem = RealFilesystem() if filesystem is None else filesystem
    return DoctorReport([
        _check_required_env(env),
        _check_optional_env(env),
        _check_runtime_safety(env),
        _check_filesystem(env, filesystem),
        _check_hdhive_subscriptions(env, filesystem),
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


def format_audit_summary(issues: list[AuditIssue], sample_limit: int = 10, group_by_row: bool = False) -> str:
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
        if group_by_row:
            grouped: dict[int, list[AuditIssue]] = {}
            for issue in issues:
                grouped.setdefault(issue.row_id, []).append(issue)
            row_items = sorted(
                grouped.items(),
                key=lambda item: (-len(item[1]), item[0]),
            )
            lines.append(f"row_samples(first {min(limit, len(row_items))})")
            for row_id, row_issues in row_items[:limit]:
                row_counts = Counter(issue.issue_type for issue in row_issues)
                count_text = ", ".join(
                    f"{issue_type}={count}"
                    for issue_type, count in sorted(row_counts.items(), key=lambda item: (-item[1], item[0]))
                )
                lines.append(f"row={row_id} issues={len(row_issues)} {count_text} sample={row_issues[0].message}")
        else:
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
    parser.add_argument("--audit-group-by-row", action="store_true", help="group audit summary samples by submission row")
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
            print(format_audit_summary(
                audit_issues,
                sample_limit=args.audit_sample_limit,
                group_by_row=args.audit_group_by_row,
            ))
        elif audit_issues:
            print("cms-tg-ingest DB audit")
            for issue in audit_issues:
                print("FAIL " + issue.to_text())
        elif not args.quiet:
            print("cms-tg-ingest DB audit\nOK no quality issues found")
    return 0 if report.ok and not audit_issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
