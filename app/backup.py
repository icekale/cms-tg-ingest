from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .task_store import TaskStore


LOG = logging.getLogger("cms-tg-ingest")
BACKUP_STATE_KEY = "backup_last_result"
BACKUP_RUN_DATE_KEY = "backup_last_run_date"
_BACKUP_NAME_RE = re.compile(r"^(?P<stem>.+)-(?P<timestamp>\d{8}T\d{6}Z)\.db$")


@dataclass(frozen=True)
class BackupResult:
    status: str
    started_at: str
    finished_at: str
    files: list[str]
    skipped: list[str]
    errors: list[str]

    @property
    def error(self) -> str:
        return "; ".join(self.errors)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["error"] = self.error
        return payload


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat(timespec="seconds")


def _retire_old_backups(destination: Path, stems: set[str], cutoff: float) -> None:
    for stem in stems:
        for path in destination.glob(f"{stem}-*.db"):
            match = _BACKUP_NAME_RE.match(path.name)
            if not match or match.group("stem") != stem:
                continue
            try:
                created_at = datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
            if created_at < cutoff:
                try:
                    path.unlink()
                except OSError:
                    continue


def backup_sqlite_databases(
    sources: Iterable[str | Path],
    destination: str | Path,
    *,
    now: float | None = None,
    retention_days: int = 14,
) -> BackupResult:
    started = time.time() if now is None else float(now)
    started_at = _iso_timestamp(started)
    destination_path = Path(destination).expanduser()
    files: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    source_paths = [Path(source).expanduser() for source in sources]
    timestamp = _timestamp(started)

    try:
        destination_path.mkdir(parents=True, exist_ok=True)
        os.chmod(destination_path, 0o700)
    except OSError as exc:
        finished = time.time()
        return BackupResult(
            "failed",
            started_at,
            _iso_timestamp(finished),
            [],
            [],
            [f"backup directory: {exc}"],
        )

    generated_stems: set[str] = set()
    for source in source_paths:
        source_text = str(source)
        if not source.is_file():
            skipped.append(source_text)
            continue
        stem = source.stem
        generated_stems.add(stem)
        target = destination_path / f"{stem}-{timestamp}.db"
        temporary = destination_path / f".{stem}-{timestamp}.db.tmp"
        try:
            with sqlite3.connect(source) as source_connection, sqlite3.connect(temporary) as target_connection:
                source_connection.backup(target_connection)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            os.chmod(target, 0o600)
            files.append(str(target))
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"{source_text}: {exc}")
            try:
                temporary.unlink()
            except OSError:
                pass

    retention = max(1, int(retention_days))
    _retire_old_backups(destination_path, generated_stems, started - retention * 86400)
    finished = time.time()
    if files and (errors or skipped):
        status = "partial"
    elif files:
        status = "succeeded"
    elif errors:
        status = "failed"
    else:
        status = "skipped"
    return BackupResult(status, started_at, _iso_timestamp(finished), files, skipped, errors)


class BackupScheduler:
    def __init__(
        self,
        store: TaskStore,
        sources: Iterable[str | Path],
        destination: str | Path,
        *,
        run_time: str = "03:30",
        timezone_name: str = "Asia/Shanghai",
        retention_days: int = 14,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.sources = tuple(Path(source).expanduser() for source in sources)
        self.destination = Path(destination).expanduser()
        self.run_time = self._parse_time(run_time)
        try:
            self.timezone = ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("backup timezone must be a valid IANA timezone") from exc
        self.retention_days = max(1, int(retention_days))
        self.enabled = bool(enabled)

    @staticmethod
    def _parse_time(value: str) -> datetime_time:
        try:
            hour, minute = (int(part) for part in str(value).split(":", 1))
            return datetime_time(hour, minute)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("backup time must use HH:MM format") from exc

    def _local_now(self, now: float | None = None) -> datetime:
        current = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(float(now), timezone.utc)
        return current.astimezone(self.timezone)

    def next_run_at(self, now: float | None = None) -> datetime:
        local_now = self._local_now(now)
        candidate = local_now.replace(
            hour=self.run_time.hour,
            minute=self.run_time.minute,
            second=0,
            microsecond=0,
        )
        if local_now >= candidate:
            candidate += timedelta(days=1)
        return candidate

    def run_once(self, now: float | None = None) -> BackupResult:
        result = backup_sqlite_databases(
            self.sources,
            self.destination,
            now=now,
            retention_days=self.retention_days,
        )
        updated_at = time.time() if now is None else float(now)
        self.store.set_runtime_state(
            BACKUP_STATE_KEY,
            json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
            updated_at=updated_at,
        )
        return result

    def run_if_due(self, now: float | None = None) -> BackupResult | None:
        if not self.enabled:
            return None
        local_now = self._local_now(now)
        scheduled = local_now.replace(
            hour=self.run_time.hour,
            minute=self.run_time.minute,
            second=0,
            microsecond=0,
        )
        if local_now < scheduled:
            return None
        run_date = local_now.date().isoformat()
        state = self.store.get_runtime_state(BACKUP_RUN_DATE_KEY)
        if state and str(state.get("value") or "") == run_date:
            return None
        result = self.run_once(now)
        # Keep failed runs retryable; a transient disk or SQLite error should
        # not suppress the next scheduler tick until tomorrow.
        if result.status != "failed":
            self.store.set_runtime_state(BACKUP_RUN_DATE_KEY, run_date, updated_at=local_now.timestamp())
        return result


def start_backup_loop(
    scheduler: BackupScheduler,
    stop_event: threading.Event,
    *,
    interval_seconds: int = 30,
) -> threading.Thread:
    def loop() -> None:
        while not stop_event.is_set():
            try:
                result = scheduler.run_if_due()
                if result is not None:
                    status = result.status
                    if result.error:
                        status = f"{status}: {result.error}"
                    LOG.info("Database backup completed status=%s files=%s", status, len(result.files))
            except Exception as exc:
                scheduler.store.set_runtime_state(
                    BACKUP_STATE_KEY,
                    json.dumps(
                        {
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                            "files": [],
                            "skipped": [],
                            "errors": [str(exc)],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                LOG.exception("Database backup loop failed: %s", exc)
            stop_event.wait(max(1, int(interval_seconds)))

    thread = threading.Thread(target=loop, name="database-backup", daemon=True)
    thread.start()
    return thread
