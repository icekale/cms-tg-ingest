from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bridge import SeriesUpdateSourceConflict, ShareKey

class SubmissionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    share_code TEXT NOT NULL,
                    receive_code TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    cms_task_id TEXT,
                    title TEXT,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(share_code, receive_code)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_updated_at ON submissions(updated_at)")
            self._ensure_columns(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submissions_self_share_move
                ON submissions(workflow_mode, lower(COALESCE(move_status, '')), updated_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submissions_self_share_cleanup
                ON submissions(
                    workflow_mode,
                    lower(COALESCE(move_status, '')),
                    lower(COALESCE(emby_status, '')),
                    lower(COALESCE(cleanup_status, '')),
                    updated_at
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submissions_self_share_probe
                ON submissions(workflow_mode, move_status, emby_status, share_probe_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS parent_category_memory (
                    parent_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
        columns = {
            "category_choice": "TEXT",
            "category_status": "TEXT",
            "recognition_json": "TEXT",
            "emby_status": "TEXT",
            "emby_item_id": "TEXT",
            "emby_title": "TEXT",
            "emby_path": "TEXT",
            "emby_parent": "TEXT",
            "source_path": "TEXT",
            "dest_path": "TEXT",
            "move_status": "TEXT",
            "move_error": "TEXT",
            "move_started_at": "REAL",
            "move_finished_at": "REAL",
            "category_final": "TEXT",
            "workflow_mode": "TEXT",
            "workflow_phase": "TEXT",
            "own_share_file_id": "TEXT",
            "own_share_file_name": "TEXT",
            "own_share_code": "TEXT",
            "own_share_receive_code": "TEXT",
            "own_share_url": "TEXT",
            "share_sync_status": "TEXT",
            "cleanup_status": "TEXT",
            "cleanup_file_id": "TEXT",
            "cleanup_error": "TEXT",
            "cleanup_finished_at": "REAL",
            "share_probe_at": "REAL",
            "share_invalid_at": "REAL",
            "share_invalid_reason": "TEXT",
            "canonical_manifest_json": "TEXT",
            "share_alias_name": "TEXT",
            "share_alias_level": "INTEGER",
            "share_validation_status": "TEXT",
            "share_validation_error": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {name} {definition}")

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    def find_by_key(self, key: ShareKey) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM submissions WHERE share_code = ? AND receive_code = ?",
                (key.share_code, key.receive_code),
            ).fetchone()
        return self._row_to_dict(row)

    def upsert_submission(
        self,
        key: ShareKey,
        url: str,
        status: str,
        cms_task_id: str | None = None,
        title: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO submissions (share_code, receive_code, url, cms_task_id, title, status, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_code, receive_code) DO UPDATE SET
                    url = excluded.url,
                    cms_task_id = COALESCE(excluded.cms_task_id, submissions.cms_task_id),
                    title = COALESCE(excluded.title, submissions.title),
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (key.share_code, key.receive_code, url, cms_task_id, title, status, last_error, now, now),
            )
            row = conn.execute(
                "SELECT * FROM submissions WHERE share_code = ? AND receive_code = ?",
                (key.share_code, key.receive_code),
            ).fetchone()
        found = self._row_to_dict(row)
        if found is None:
            raise RuntimeError("保存任务记录失败")
        return found

    def update_status(self, row_id: int, status: str, title: str | None = None, last_error: str | None = None) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET status = ?,
                    title = CASE
                        WHEN workflow_mode = 'self_share_sync' THEN title
                        ELSE COALESCE(?, title)
                    END,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, title, last_error, time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def find_by_id(self, row_id: int) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def delete_submission(self, row_id: int) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute("DELETE FROM submissions WHERE id = ?", (int(row_id),))
        return int(cursor.rowcount or 0) == 1

    def update_category(self, row_id: int, choice: str | None, status: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET category_choice = ?, category_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (choice, status, time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def remember_parent_category(self, parent_id: str, category: str, source: str = "manual") -> None:
        parent_id = str(parent_id or "").strip()
        category = str(category or "").strip()
        if not parent_id or not category:
            return
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO parent_category_memory (parent_id, category, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(parent_id) DO UPDATE SET
                    category = excluded.category,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (parent_id, category, str(source or "").strip(), now, now),
            )

    def category_for_parent_id(self, parent_id: str) -> str:
        parent_id = str(parent_id or "").strip()
        if not parent_id:
            return ""
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT category FROM parent_category_memory WHERE parent_id = ?",
                (parent_id,),
            ).fetchone()
        return str(row["category"] or "").strip() if row else ""

    def update_recognition(self, row_id: int, recognition: dict[str, Any], category_status: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET recognition_json = ?, category_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(recognition, ensure_ascii=False, sort_keys=True), category_status, time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def update_emby(
        self,
        row_id: int,
        status: str,
        item_id: str | None = None,
        title: str | None = None,
        path: str | None = None,
        parent: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET emby_status = ?,
                    emby_item_id = COALESCE(?, emby_item_id),
                    emby_title = COALESCE(?, emby_title),
                    emby_path = COALESCE(?, emby_path),
                    emby_parent = COALESCE(?, emby_parent),
                    updated_at = ?
                WHERE id = ?
                """,
                (status, item_id, title, path, parent, time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def update_move(
        self,
        row_id: int,
        status: str,
        source_path: str | None = None,
        dest_path: str | None = None,
        category_final: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET move_status = ?,
                    source_path = COALESCE(?, source_path),
                    dest_path = COALESCE(?, dest_path),
                    category_final = COALESCE(?, category_final),
                    move_error = ?,
                    move_started_at = COALESCE(move_started_at, ?),
                    move_finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, source_path, dest_path, category_final, error, now, now, now, row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def update_self_share(
        self,
        row_id: int,
        workflow_mode: str | None = None,
        workflow_phase: str | None = None,
        own_share_file_id: str | None = None,
        own_share_file_name: str | None = None,
        own_share_code: str | None = None,
        own_share_receive_code: str | None = None,
        own_share_url: str | None = None,
        share_sync_status: str | None = None,
        canonical_manifest_json: str | None = None,
        share_alias_name: str | None = None,
        share_alias_level: int | None = None,
        share_validation_status: str | None = None,
        share_validation_error: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET workflow_mode = COALESCE(?, workflow_mode),
                    workflow_phase = COALESCE(?, workflow_phase),
                    own_share_file_id = COALESCE(?, own_share_file_id),
                    own_share_file_name = COALESCE(?, own_share_file_name),
                    own_share_code = COALESCE(?, own_share_code),
                    own_share_receive_code = COALESCE(?, own_share_receive_code),
                    own_share_url = COALESCE(?, own_share_url),
                    share_sync_status = COALESCE(?, share_sync_status),
                    canonical_manifest_json = COALESCE(?, canonical_manifest_json),
                    share_alias_name = COALESCE(?, share_alias_name),
                    share_alias_level = COALESCE(?, share_alias_level),
                    share_validation_status = COALESCE(?, share_validation_status),
                    share_validation_error = COALESCE(?, share_validation_error),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    workflow_mode,
                    workflow_phase,
                    own_share_file_id,
                    own_share_file_name,
                    own_share_code,
                    own_share_receive_code,
                    own_share_url,
                    share_sync_status,
                    canonical_manifest_json,
                    share_alias_name,
                    share_alias_level,
                    share_validation_status,
                    share_validation_error,
                    time.time(),
                    row_id,
                ),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def claim_self_share_restore_sync(
        self,
        row_id: int,
        retry_seconds: float = 60,
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        stale_before = timestamp - max(1.0, float(retry_seconds))
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE submissions
                SET workflow_phase = 'restore_share_sync_submitted',
                    share_sync_status = 'restore_submitted',
                    updated_at = ?
                WHERE id = ?
                  AND (
                      lower(COALESCE(workflow_phase, '')) <> 'restore_share_sync_submitted'
                      OR COALESCE(updated_at, 0) <= ?
                  )
                """,
                (timestamp, row_id, stale_before),
            )
            return bool(cursor.rowcount)

    def reset_self_share_for_update(self, row_id: int) -> dict[str, Any] | None:
        """Keep stable media identity while clearing only one self-share execution's state."""
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET cms_task_id = NULL,
                    status = 'received',
                    last_error = NULL,
                    workflow_mode = 'self_share_sync',
                    workflow_phase = 'update_requested',
                    own_share_file_id = NULL,
                    own_share_file_name = NULL,
                    own_share_code = NULL,
                    own_share_receive_code = NULL,
                    own_share_url = NULL,
                    share_sync_status = NULL,
                    canonical_manifest_json = NULL,
                    share_alias_name = NULL,
                    share_alias_level = NULL,
                    share_validation_status = NULL,
                    share_validation_error = NULL,
                    source_path = NULL,
                    dest_path = NULL,
                    move_status = NULL,
                    move_error = NULL,
                    move_started_at = NULL,
                    move_finished_at = NULL,
                    category_final = NULL,
                    emby_status = NULL,
                    emby_item_id = NULL,
                    emby_title = NULL,
                    emby_path = NULL,
                    emby_parent = NULL,
                    cleanup_status = NULL,
                    cleanup_file_id = NULL,
                    cleanup_error = NULL,
                    cleanup_finished_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def prepare_series_update_child(
        self,
        target_row_id: int,
        child_key: ShareKey,
        child_url: str,
        *,
        canonical_title: str | None = None,
        canonical_tmdb_id: str | None = None,
        canonical_category: str | None = None,
        canonical_recognition: dict[str, Any] | None = None,
        expected_child_exists: bool | None = None,
        expected_child_id: int | None = None,
        expected_child_updated_at: float | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_child = conn.execute(
                "SELECT id, updated_at, own_share_code FROM submissions WHERE share_code = ? AND receive_code = ?",
                (child_key.share_code, child_key.receive_code),
            ).fetchone()
            if expected_child_exists is False and existing_child is not None:
                raise SeriesUpdateSourceConflict("子任务提交记录在冻结后出现")
            if expected_child_exists is True:
                if (
                    existing_child is None
                    or int(existing_child["id"]) != int(expected_child_id)
                    or float(existing_child["updated_at"] or 0) != float(expected_child_updated_at)
                ):
                    raise SeriesUpdateSourceConflict("子任务提交记录在冻结后发生变化")
                if str(existing_child["own_share_code"] or "").strip():
                    raise SeriesUpdateSourceConflict("子任务分享已在冻结后创建")
            target = conn.execute(
                "SELECT * FROM submissions WHERE id = ?",
                (int(target_row_id),),
            ).fetchone()
            if target is None:
                return None
            if (
                str(target["share_code"] or "") == child_key.share_code
                and str(target["receive_code"] or "") == child_key.receive_code
            ):
                return None
            try:
                target_recognition = json.loads(str(target["recognition_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                target_recognition = {}
            if not isinstance(target_recognition, dict):
                target_recognition = {}
            child_title = str(target["title"] or "") if canonical_title is None else str(canonical_title)
            category = (
                str(
                    target["category_choice"]
                    or target["category_final"]
                    or target_recognition.get("category")
                    or ""
                ).strip()
                if canonical_category is None
                else str(canonical_category).strip()
            )
            child_recognition = dict(canonical_recognition) if canonical_recognition is not None else target_recognition
            if canonical_title is not None:
                child_recognition["title"] = child_title
            if canonical_tmdb_id is not None:
                child_recognition["tmdb_id"] = str(canonical_tmdb_id).strip()
            if canonical_category is not None:
                child_recognition["category"] = category
            recognition_json = json.dumps(child_recognition, ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                INSERT INTO submissions (
                    share_code, receive_code, url, title, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'received', ?, ?)
                ON CONFLICT(share_code, receive_code) DO UPDATE SET
                    url = excluded.url,
                    updated_at = excluded.updated_at
                """,
                (
                    child_key.share_code,
                    child_key.receive_code,
                    str(child_url),
                    child_title,
                    now,
                    now,
                ),
            )
            child = conn.execute(
                "SELECT id FROM submissions WHERE share_code = ? AND receive_code = ?",
                (child_key.share_code, child_key.receive_code),
            ).fetchone()
            if child is None or int(child["id"]) == int(target_row_id):
                return None
            conn.execute(
                """
                UPDATE submissions
                SET cms_task_id = NULL,
                    title = ?, status = 'received', last_error = NULL,
                    category_choice = ?, category_status = 'selected',
                    recognition_json = ?, workflow_mode = 'self_share_sync',
                    workflow_phase = 'update_requested',
                    own_share_file_id = NULL, own_share_file_name = NULL,
                    own_share_code = NULL, own_share_receive_code = NULL,
                    own_share_url = NULL, share_sync_status = NULL,
                    canonical_manifest_json = NULL, share_alias_name = NULL,
                    share_alias_level = NULL, share_validation_status = NULL,
                    share_validation_error = NULL, share_probe_at = NULL,
                    share_invalid_at = NULL, share_invalid_reason = NULL,
                    source_path = NULL,
                    dest_path = NULL, move_status = NULL, move_error = NULL,
                    move_started_at = NULL, move_finished_at = NULL,
                    category_final = NULL, emby_status = NULL,
                    emby_item_id = NULL, emby_title = NULL, emby_path = NULL,
                    emby_parent = NULL, cleanup_status = NULL,
                    cleanup_file_id = NULL, cleanup_error = NULL,
                    cleanup_finished_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    child_title,
                    category,
                    recognition_json,
                    now,
                    int(child["id"]),
                ),
            )
            prepared = conn.execute(
                "SELECT * FROM submissions WHERE id = ?",
                (int(child["id"]),),
            ).fetchone()
        return self._row_to_dict(prepared)

    def replace_self_share_source_file_id(self, row_id: int, file_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET own_share_file_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(file_id), time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def update_cleanup(
        self,
        row_id: int,
        status: str,
        file_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET cleanup_status = ?,
                    cleanup_file_id = COALESCE(?, cleanup_file_id),
                    cleanup_error = ?,
                    cleanup_finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, file_id, error, time.time(), time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def clear_finished_history(self) -> int:
        terminal_status_like = " OR ".join(["lower(status) LIKE ?"] * len(TERMINAL_STATUS_KEYWORDS))
        terminal_status_params = [f"%{keyword}%" for keyword in TERMINAL_STATUS_KEYWORDS]
        terminal_emby_placeholders = ",".join("?" for _ in TERMINAL_EMBY_STATUSES)
        terminal_move_placeholders = ",".join("?" for _ in TERMINAL_MOVE_STATUSES)
        where = f"""
            ({terminal_status_like})
            OR lower(COALESCE(emby_status, '')) IN ({terminal_emby_placeholders})
            OR lower(COALESCE(move_status, '')) IN ({terminal_move_placeholders})
        """
        params = terminal_status_params + list(TERMINAL_EMBY_STATUSES) + list(TERMINAL_MOVE_STATUSES)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(f"DELETE FROM submissions WHERE {where}", params)
        return int(cursor.rowcount or 0)

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM submissions ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def live_self_share_identities(self, dest_path: str, tmdb_id: str = "") -> tuple[tuple[str, str], ...]:
        """Return all moved, valid self-share identities for one media directory."""
        dest_path = str(dest_path or "").strip()
        target_tmdb = str(tmdb_id or "").strip()
        if not dest_path:
            return ()
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, own_share_code, own_share_receive_code, recognition_json
                FROM submissions
                WHERE workflow_mode = 'self_share_sync'
                  AND lower(COALESCE(move_status, '')) = 'moved'
                  AND COALESCE(dest_path, '') = ?
                  AND COALESCE(own_share_code, '') <> ''
                  AND lower(COALESCE(share_validation_status, '')) NOT IN ('invalid', 'unavailable')
                ORDER BY updated_at DESC, id DESC
                """,
                (dest_path,),
            ).fetchall()
        identities: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            try:
                recognition = json.loads(str(row["recognition_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                recognition = {}
            if not isinstance(recognition, dict):
                recognition = {}
            candidate_tmdb = str(recognition.get("tmdb_id") or "").strip()
            if target_tmdb and candidate_tmdb != target_tmdb:
                continue
            share_code = str(row["own_share_code"] or "").strip()
            if not share_code:
                continue
            receive_code = str(row["own_share_receive_code"] or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
            identity = (share_code, receive_code)
            if identity in seen:
                continue
            seen.add(identity)
            identities.append(identity)
        return tuple(identities)

    def latest_self_share_identity(self, dest_path: str, tmdb_id: str = "") -> tuple[str, str] | None:
        """Return the newest moved, valid self-share identity for one media directory."""
        identities = self.live_self_share_identities(dest_path, tmdb_id)
        return identities[0] if identities else None

    def all_confirmed_with_emby_path(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE emby_status = ? AND COALESCE(emby_path, '') <> ''
                ORDER BY updated_at DESC, id DESC
                """,
                ("confirmed",),
            ).fetchall()
        return [dict(row) for row in rows]

    def stranded_self_share_move_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE workflow_mode = 'self_share_sync'
                  AND lower(COALESCE(move_status, '')) <> 'moved'
                  AND (
                      (
                          lower(COALESCE(move_status, '')) = 'moving'
                          AND COALESCE(source_path, '') <> ''
                          AND COALESCE(dest_path, '') <> ''
                      )
                      OR (
                          COALESCE(own_share_file_name, '') <> ''
                          AND COALESCE(source_path, '') = ''
                          AND COALESCE(dest_path, '') = ''
                      )
                  )
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def invalid_self_share_move_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE workflow_mode = 'self_share_sync'
                  AND lower(COALESCE(move_status, '')) <> 'moved'
                  AND (COALESCE(source_path, '') <> '' OR COALESCE(dest_path, '') <> '')
                  AND NOT (
                      lower(COALESCE(move_status, '')) = 'error'
                      AND COALESCE(source_path, '') <> ''
                      AND COALESCE(dest_path, '') <> ''
                  )
                  AND NOT (
                      lower(COALESCE(move_status, '')) = 'moving'
                      AND COALESCE(source_path, '') <> ''
                      AND COALESCE(dest_path, '') <> ''
                  )
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def missing_self_share_library_candidates(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE workflow_mode = 'self_share_sync'
                  AND lower(COALESCE(move_status, '')) = 'moved'
                  AND lower(COALESCE(share_validation_status, '')) NOT IN ('invalid', 'unavailable')
                  AND COALESCE(dest_path, '') <> ''
                  AND COALESCE(own_share_file_name, '') <> ''
                  AND COALESCE(own_share_code, '') <> ''
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_self_share_cleanup_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE workflow_mode = 'self_share_sync'
                  AND lower(COALESCE(move_status, '')) = 'moved'
                  AND lower(COALESCE(emby_status, '')) = 'confirmed'
                  AND lower(COALESCE(cleanup_status, '')) IN ('pending', 'error')
                  AND COALESCE(own_share_file_id, '') <> ''
                  AND COALESCE(own_share_code, '') <> ''
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def self_share_probe_candidates(self, limit: int = 3) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE workflow_mode = 'self_share_sync'
                  AND lower(COALESCE(move_status, '')) = 'moved'
                  AND lower(COALESCE(emby_status, '')) = 'confirmed'
                  AND COALESCE(dest_path, '') <> ''
                  AND COALESCE(own_share_code, '') <> ''
                ORDER BY
                    CASE WHEN COALESCE(share_probe_at, 0) = 0 THEN 0 ELSE 1 END ASC,
                    created_at DESC,
                    share_probe_at ASC,
                    id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_share_probe(self, row_id: int) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET share_probe_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (time.time(), time.time(), row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def mark_invalid_share_cleaned(self, row_id: int, reason: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE submissions
                SET move_status = 'invalid_share_cleaned',
                    move_error = ?,
                    emby_status = 'invalid_share_cleaned',
                    share_invalid_at = ?,
                    share_invalid_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (reason, now, reason, now, row_id),
            )
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_dict(row)

    def stale_for_repair(self, limit: int = 50) -> list[dict[str, Any]]:
        repair_emby_statuses = ("timeout", "failed", "error")
        repair_category_statuses = ("uncertain", "probing", "openai_suggested")
        repair_statuses = ("submitted", "unknown", "pending")
        emby_placeholders = ",".join("?" for _ in repair_emby_statuses)
        category_placeholders = ",".join("?" for _ in repair_category_statuses)
        status_placeholders = ",".join("?" for _ in repair_statuses)
        params = [
            *repair_emby_statuses,
            *repair_category_statuses,
            *repair_statuses,
            max(1, int(limit)),
        ]
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM submissions
                WHERE lower(COALESCE(emby_status, '')) <> 'confirmed'
                  AND (
                    lower(COALESCE(emby_status, '')) IN ({emby_placeholders})
                    OR lower(COALESCE(category_status, '')) IN ({category_placeholders})
                    OR lower(COALESCE(status, '')) IN ({status_placeholders})
                  )
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]
