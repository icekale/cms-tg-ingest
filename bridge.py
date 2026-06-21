#!/usr/bin/env python3
"""Telegram-to-CMS bridge for 115 share links."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import html as html_lib
from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from app.models import TaskStage, TaskStatus
from app.task_bridge import ensure_task_for_link, record_failure, record_submission_event, sync_task_from_submission
from app.task_runner import StageResult, TaskRunner
from app.task_store import TaskStore
from app.web import start_web_server

LINK_RE = re.compile(r"https?://(?:www\.)?(?:115cdn|115|anxia)\.com/s/[^\s<>'\"]+", re.I)
TRAILING_PUNCT = ".,;)。），]】》>"
LOG = logging.getLogger("cms-tg-ingest")
LAST_TELEGRAM_TRANSIENT_ERROR_AT: str | None = None
HELP_TEXT = """直接发送 115 分享链接即可自动提交 CMS。\n\n支持：\n- 一条消息多个 115 链接\n- 自动跳过重复链接\n- 识别不确定时用按钮确认分类\n- 自动尝试确认 Emby 是否入库\n- /status 查看最近任务\n- /metrics 查看任务统计\n- /clear_history 清理已结束历史\n- /help 查看帮助\n\n示例：\nhttps://115cdn.com/s/xxxx?password=abcd"""
MENU_BUTTONS = {
    "📊 统计": "/metrics",
    "📋 最近任务": "/status",
    "🕘 历史": "/history",
    "🧯 巡检": "/quality",
    "🧹 清理历史": "/clear_history",
    "🩺 健康检查": "/health",
    "❓ 帮助": "/help",
}
TERMINAL_STATUS_KEYWORDS = ("done", "finish", "success", "complete", "完成", "成功", "failed", "error", "失败", "timeout", "超时")
TERMINAL_EMBY_STATUSES = {"confirmed", "timeout", "disabled", "failed", "error"}
TERMINAL_MOVE_STATUSES = {"moved", "skipped", "failed", "error", "conflict"}
OPENAI_CATEGORY_LABELS = ["华语电影", "欧美电影", "亚洲电影", "动漫电影", "国产电视", "外国电视", "番剧", "纪录片"]


@dataclass(frozen=True)
class ShareKey:
    share_code: str
    receive_code: str


def extract_share_links(text: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for match in LINK_RE.finditer(text or ""):
        link = match.group(0).rstrip(TRAILING_PUNCT)
        key = link.lower()
        if key not in seen:
            seen.add(key)
            links.append(link)
    return links


def parse_bool_env(value: str | None, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled", "enable"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_share_link(url: str) -> ShareKey:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "s":
        raise ValueError("链接格式错误：未找到分享码")
    share_code = parts[1].strip().lower()
    query = urllib.parse.parse_qs(parsed.query)
    receive_code = (query.get("password") or query.get("pwd") or query.get("code") or [""])[0].strip()
    return ShareKey(share_code=share_code, receive_code=receive_code)


@dataclass
class Config:
    tg_bot_token: str
    tg_allowed_chat_id: str
    cms_base_url: str
    cms_username: str
    cms_password: str
    poll_timeout: int = 30
    http_timeout: int = 60
    db_path: str = "/data/submissions.db"
    status_poll_seconds: int = 300
    status_poll_interval: int = 20
    emby_base_url: str = ""
    emby_api_key: str = ""
    emby_user_id: str = ""
    strm_source_roots: str = "/mnt/user/Unraid/strm/转存"
    strm_library_map: str = ""
    move_conflict_policy: str = "skip"
    strm_stable_seconds: int = 30
    openai_classify_enabled: bool = False
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    openai_high_confidence: float = 0.75
    openai_suggest_confidence: float = 0.45
    workflow_mode: str = "direct"
    p115_cookie_path: str = "/config/115-cookies.txt"
    self_share_receive_cid: str = ""
    self_share_strm_root: str = "/mnt/user/Unraid/strm/share"
    self_share_cms_local_path: str = "/media/share"
    self_share_cms_cid: str = "0"
    self_share_cleanup_after_emby: bool = False
    self_share_source_cleanup_parent_ids: str = ""
    self_share_auto_organize_retry_seconds: int = 90
    status_repair_enabled: bool = True
    status_repair_interval_seconds: int = 300
    status_repair_limit: int = 50
    cms_parent_cid_category_map: str = ""
    task_db_path: str = "/data/tasks.db"
    web_enabled: bool = False
    web_host: str = "0.0.0.0"
    web_port: int = 8787
    web_token: str = ""
    task_worker_interval_seconds: int = 5
    task_max_retries: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        required = [
            "TG_BOT_TOKEN",
            "TG_ALLOWED_CHAT_ID",
            "CMS_BASE_URL",
            "CMS_USERNAME",
            "CMS_PASSWORD",
        ]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))
        return cls(
            tg_bot_token=os.environ["TG_BOT_TOKEN"],
            tg_allowed_chat_id=os.environ["TG_ALLOWED_CHAT_ID"],
            cms_base_url=os.environ["CMS_BASE_URL"].rstrip("/"),
            cms_username=os.environ["CMS_USERNAME"],
            cms_password=os.environ["CMS_PASSWORD"],
            poll_timeout=int(os.environ.get("TG_POLL_TIMEOUT", "30")),
            http_timeout=int(os.environ.get("HTTP_TIMEOUT", "60")),
            db_path=os.environ.get("DB_PATH", "/data/submissions.db"),
            status_poll_seconds=int(os.environ.get("STATUS_POLL_SECONDS", "300")),
            status_poll_interval=int(os.environ.get("STATUS_POLL_INTERVAL", "20")),
            emby_base_url=(os.environ.get("EMBY_BASE_URL") or os.environ.get("EMBY_HOST_PORT") or "").rstrip("/"),
            emby_api_key=os.environ.get("EMBY_API_KEY", ""),
            emby_user_id=os.environ.get("EMBY_USER_ID", ""),
            strm_source_roots=os.environ.get("STRM_SOURCE_ROOTS", "/mnt/user/Unraid/strm/转存"),
            strm_library_map=os.environ.get("STRM_LIBRARY_MAP", ""),
            move_conflict_policy=os.environ.get("MOVE_CONFLICT_POLICY", "skip"),
            strm_stable_seconds=int(os.environ.get("STRM_STABLE_SECONDS", "30")),
            openai_classify_enabled=parse_bool_env(os.environ.get("OPENAI_CLASSIFY_ENABLED"), False),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            openai_high_confidence=env_float("OPENAI_HIGH_CONFIDENCE", 0.75),
            openai_suggest_confidence=env_float("OPENAI_SUGGEST_CONFIDENCE", 0.45),
            workflow_mode=os.environ.get("WORKFLOW_MODE", "direct").strip().lower() or "direct",
            p115_cookie_path=os.environ.get("P115_COOKIE_PATH", "/config/115-cookies.txt"),
            self_share_receive_cid=os.environ.get("SELF_SHARE_RECEIVE_CID", ""),
            self_share_strm_root=os.environ.get("SELF_SHARE_STRM_ROOT", "/mnt/user/Unraid/strm/share"),
            self_share_cms_local_path=os.environ.get("SELF_SHARE_CMS_LOCAL_PATH", "/media/share"),
            self_share_cms_cid=os.environ.get("SELF_SHARE_CMS_CID", "0"),
            self_share_cleanup_after_emby=parse_bool_env(os.environ.get("SELF_SHARE_CLEANUP_AFTER_EMBY"), False),
            self_share_source_cleanup_parent_ids=os.environ.get("SELF_SHARE_SOURCE_CLEANUP_PARENT_IDS", ""),
            self_share_auto_organize_retry_seconds=int(os.environ.get("SELF_SHARE_AUTO_ORGANIZE_RETRY_SECONDS", "90")),
            status_repair_enabled=parse_bool_env(os.environ.get("STATUS_REPAIR_ENABLED"), True),
            status_repair_interval_seconds=int(os.environ.get("STATUS_REPAIR_INTERVAL_SECONDS", "300")),
            status_repair_limit=int(os.environ.get("STATUS_REPAIR_LIMIT", "50")),
            cms_parent_cid_category_map=os.environ.get("CMS_PARENT_CID_CATEGORY_MAP", ""),
            task_db_path=os.environ.get("TASK_DB_PATH", "/data/tasks.db"),
            web_enabled=parse_bool_env(os.environ.get("WEB_ENABLED"), False),
            web_host=os.environ.get("WEB_HOST", "0.0.0.0"),
            web_port=int(os.environ.get("WEB_PORT", "8787")),
            web_token=os.environ.get("WEB_TOKEN", ""),
            task_worker_interval_seconds=int(os.environ.get("TASK_WORKER_INTERVAL_SECONDS", "5")),
            task_max_retries=int(os.environ.get("TASK_MAX_RETRIES", "3")),
        )


def create_task_store(config: Config) -> TaskStore:
    return TaskStore(config.task_db_path)


def maybe_start_web_server(config: Config, task_store: TaskStore, starter=start_web_server):
    if not config.web_enabled:
        return None
    server = starter(task_store, config.web_host, config.web_port, web_token=config.web_token)
    LOG.info("v0.2 web admin started host=%s port=%s", config.web_host, config.web_port)
    return server


def best_effort_task_sync(action: str, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        LOG.debug("TaskStore sync failed during %s", action, exc_info=True)
        return None


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
                SET status = ?, title = COALESCE(?, title), last_error = ?, updated_at = ?
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
                    time.time(),
                    row_id,
                ),
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
                  AND COALESCE(own_share_file_name, '') <> ''
                  AND lower(COALESCE(move_status, '')) <> 'moved'
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def missing_self_share_library_candidates(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM submissions
                WHERE workflow_mode = 'self_share_sync'
                  AND lower(COALESCE(move_status, '')) = 'moved'
                  AND COALESCE(dest_path, '') <> ''
                  AND COALESCE(own_share_file_name, '') <> ''
                  AND COALESCE(own_share_code, '') <> ''
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
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


@dataclass
class MoveConfig:
    source_roots: list[Path]
    library_roots: dict[str, Path]
    conflict_policy: str = "skip"
    stable_seconds: int = 0

    @classmethod
    def from_config(cls, config: Config) -> "MoveConfig":
        source_roots = [Path(part).expanduser() for part in split_env_list(config.strm_source_roots)]
        library_roots = default_library_roots()
        if config.strm_library_map.strip():
            library_roots.update(parse_library_map(config.strm_library_map))
        return cls(
            source_roots=source_roots,
            library_roots=library_roots,
            conflict_policy=config.move_conflict_policy or "skip",
            stable_seconds=max(0, int(config.strm_stable_seconds)),
        )


@dataclass
class MovePlan:
    status: str
    reason: str
    source_path: Path | None = None
    dest_path: Path | None = None
    category: str | None = None


@dataclass
class SelfShareConfig:
    enabled: bool = False
    strm_root: Path = Path("/mnt/user/Unraid/strm/share")
    cms_local_path: str = "/media/share"
    cms_cid: str = "0"
    excluded_parent_ids: set[str] | None = None
    cleanup_after_emby: bool = False
    source_cleanup_parent_ids: set[str] | None = None
    auto_organize_retry_seconds: int = 90
    parent_cid_category_map: dict[str, str] | None = None

    @classmethod
    def from_config(cls, config: Config, cms: "CmsClient | None" = None) -> "SelfShareConfig":
        excluded = set()
        if cms:
            try:
                excluded.update(cms.auto_organize_excluded_parent_ids())
            except Exception:
                LOG.debug("Failed to load CMS auto organize excluded folders", exc_info=True)
        return cls(
            enabled=config.workflow_mode == "self_share_sync",
            strm_root=Path(config.self_share_strm_root).expanduser(),
            cms_local_path=config.self_share_cms_local_path,
            cms_cid=config.self_share_cms_cid,
            excluded_parent_ids=excluded,
            cleanup_after_emby=config.self_share_cleanup_after_emby,
            source_cleanup_parent_ids=set(split_env_list(config.self_share_source_cleanup_parent_ids)),
            auto_organize_retry_seconds=max(0, int(config.self_share_auto_organize_retry_seconds)),
            parent_cid_category_map=parse_parent_cid_category_map(config.cms_parent_cid_category_map),
        )


def split_env_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[|,]", value or "") if part.strip()]


def default_library_roots() -> dict[str, Path]:
    base = Path("/mnt/user/Unraid/strm/转存")
    return {
        "华语电影": base / "Movie/电影/华语电影",
        "欧美电影": base / "Movie/电影/欧美电影",
        "亚洲电影": base / "Movie/电影/亚洲电影",
        "动漫电影": base / "Movie/电影/动漫电影",
        "国产电视": base / "TVCN",
        "外国电视": base / "TV",
        "番剧": base / "Dongman",
    }


CMS_PARENT_CID_CATEGORY_MAP: dict[str, str] = {}


def parse_library_map(value: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for item in split_env_list(value):
        if "=" not in item:
            continue
        key, path = item.split("=", 1)
        key = key.strip()
        path = path.strip()
        if key and path:
            result[key] = Path(path).expanduser()
    return result


def safe_resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_relative_to(path: Path, root: Path) -> bool:
    path = safe_resolve(path)
    root = safe_resolve(root)
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_under_any_root(path: Path, roots: list[Path]) -> bool:
    return any(is_relative_to(path, root) for root in roots)


def has_strm_file(path: Path) -> bool:
    try:
        return any(child.is_file() and child.suffix.lower() == ".strm" for child in path.rglob("*"))
    except OSError:
        return False


def newest_mtime(path: Path) -> float:
    newest = 0.0
    try:
        newest = path.stat().st_mtime
        for child in path.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


def is_directory_stable(path: Path, stable_seconds: int) -> bool:
    if stable_seconds <= 0:
        return True
    mtime = newest_mtime(path)
    return bool(mtime and time.time() - mtime >= stable_seconds)


def destination_for_category(category: str, media_dir_name: str, config: MoveConfig) -> Path | None:
    root = config.library_roots.get(category)
    if not root:
        return None
    return safe_resolve(root / media_dir_name)


def library_category_for_path(path: Path | None, config: MoveConfig) -> str:
    if not path:
        return ""
    for category, root in config.library_roots.items():
        if is_relative_to(path, root):
            return category
    return ""


def library_media_root_for_path(path: Path, config: MoveConfig) -> tuple[Path, str] | None:
    resolved = safe_resolve(path)
    for category, root in config.library_roots.items():
        root = safe_resolve(root)
        if not is_relative_to(resolved, root):
            continue
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if not rel.parts:
            return None
        return safe_resolve(root / rel.parts[0]), category
    return None


def extract_tmdb_id_from_name(value: str) -> str:
    match = re.search(r"tmdb(?:id)?[=_\-](\d+)", str(value or ""), re.I)
    return match.group(1) if match else ""


def extract_year_from_name(value: str) -> str:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def media_type_for_category(category: str) -> str:
    if category in {"华语电影", "欧美电影", "亚洲电影", "动漫电影"}:
        return "movie"
    if category in {"国产电视", "外国电视", "番剧"}:
        return "tv"
    return ""


def candidate_tokens(recognition: dict[str, Any], share_name: str = "") -> list[str]:
    tokens = []
    for value in (recognition.get("tmdb_id"), recognition.get("title"), recognition.get("share_name"), share_name):
        value = str(value or "").strip()
        if value:
            tokens.append(value)
    normalized = []
    seen = set()
    for token in tokens:
        norm = normalize_text(token)
        if norm and norm not in seen:
            seen.add(norm)
            normalized.append(norm)
    return normalized


def find_strm_source_dir(config: MoveConfig, recognition: dict[str, Any], share_name: str = "") -> Path | None:
    tokens = candidate_tokens(recognition, share_name)
    if not tokens:
        return None
    matches: list[tuple[int, int, float, Path]] = []
    for root in config.source_roots:
        root = safe_resolve(root)
        if not root.exists():
            continue
        try:
            dirs = [p for p in root.rglob("*") if p.is_dir()]
        except OSError:
            continue
        for path in dirs:
            name_norm = normalize_text(path.name)
            full_norm = normalize_text(str(path))
            name_match = any(token in name_norm for token in tokens)
            full_match = any(token in full_norm for token in tokens)
            if not name_match and not full_match:
                continue
            if not has_strm_file(path):
                continue
            score = 2 if name_match else 1
            depth = -len(path.relative_to(root).parts)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            matches.append((score, depth, mtime, path))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return safe_resolve(matches[0][3])


def find_recent_library_strm_source_dir(config: MoveConfig, row: dict[str, Any], recognition: dict[str, Any], share_name: str = "") -> tuple[Path, str] | None:
    try:
        since = float(row.get("created_at") or row.get("updated_at") or 0) - 60
    except (TypeError, ValueError):
        since = 0
    tokens = candidate_tokens(recognition, share_name)
    candidates: dict[Path, tuple[str, float, bool]] = {}
    for root in config.library_roots.values():
        root = safe_resolve(root)
        if not root.exists():
            continue
        try:
            dirs = [p for p in root.rglob("*") if p.is_dir()]
        except OSError:
            continue
        for path in dirs:
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            media = library_media_root_for_path(path, config)
            if not media:
                continue
            media_root, category = media
            if not has_strm_file(media_root):
                continue
            mtime = newest_mtime(media_root)
            name_norm = normalize_text(str(media_root))
            token_match = bool(tokens and any(token in name_norm for token in tokens))
            old = candidates.get(media_root)
            if not old or mtime > old[1] or token_match:
                candidates[media_root] = (category, mtime, token_match)
    if not candidates:
        return None
    token_matches = [(path, data) for path, data in candidates.items() if data[2]]
    if len(token_matches) != 1:
        return None
    path, (category, _mtime, _token_match) = token_matches[0]
    return safe_resolve(path), category


def plan_strm_move(source_path: Path | None, category: str, config: MoveConfig) -> MovePlan:
    if not source_path:
        return MovePlan(status="skipped", reason="未找到 STRM 源目录", category=category)
    source = safe_resolve(source_path)
    if not source.exists() or not source.is_dir():
        return MovePlan(status="skipped", reason="STRM 源目录不存在", source_path=source, category=category)
    if not has_strm_file(source):
        return MovePlan(status="skipped", reason="源目录不包含 STRM 文件", source_path=source, category=category)
    if not is_directory_stable(source, config.stable_seconds):
        return MovePlan(status="skipped", reason="STRM 源目录仍在更新", source_path=source, category=category)
    dest = destination_for_category(category, source.name, config)
    if not dest:
        return MovePlan(status="skipped", reason=f"分类未映射到媒体库：{category}", source_path=source, category=category)
    if not is_under_any_root(dest, list(config.library_roots.values())):
        return MovePlan(status="error", reason="目标目录不在媒体库白名单内", source_path=source, dest_path=dest, category=category)
    library_root = safe_resolve(config.library_roots[category])
    if is_relative_to(source, library_root):
        return MovePlan(status="skipped", reason="已在目标媒体库，无需移动", source_path=source, dest_path=source, category=category)
    if is_under_any_root(source, list(config.library_roots.values())):
        return MovePlan(status="skipped", reason="已在其他媒体库，跳过跨库移动", source_path=source, dest_path=dest, category=category)
    if not is_under_any_root(source, config.source_roots):
        return MovePlan(status="error", reason="源目录不在允许范围内", source_path=source, category=category)
    if source == library_root:
        return MovePlan(status="error", reason="源目录不能是媒体库根目录", source_path=source, dest_path=dest, category=category)
    if dest.exists():
        return MovePlan(status="conflict", reason="目标目录已存在，按策略跳过", source_path=source, dest_path=dest, category=category)
    return MovePlan(status="pending", reason="ready", source_path=source, dest_path=dest, category=category)


def execute_strm_move(plan: MovePlan, store: SubmissionStore, row: dict[str, Any]) -> dict[str, Any]:
    if plan.status != "pending":
        return store.update_move(
            int(row["id"]),
            plan.status,
            source_path=str(plan.source_path) if plan.source_path else None,
            dest_path=str(plan.dest_path) if plan.dest_path else None,
            category_final=plan.category,
            error=plan.reason,
        ) or row
    assert plan.source_path is not None and plan.dest_path is not None
    plan.dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(plan.source_path), str(plan.dest_path))
    except Exception as exc:
        return store.update_move(
            int(row["id"]),
            "error",
            source_path=str(plan.source_path),
            dest_path=str(plan.dest_path),
            category_final=plan.category,
            error=str(exc),
        ) or row
    return store.update_move(
        int(row["id"]),
        "moved",
        source_path=str(plan.source_path),
        dest_path=str(plan.dest_path),
        category_final=plan.category,
    ) or row


def validate_self_share_strm_source(source: Path, row: dict[str, Any]) -> str:
    if str(row.get("workflow_mode") or "") != "self_share_sync":
        return ""
    if not source.exists() or not source.is_dir():
        return ""
    expected_tmdb = expected_task_tmdb_id(parse_recognition_json(row), row)
    folder_tmdb = extract_tmdb_id_from_name(str(source))
    if expected_tmdb and folder_tmdb and expected_tmdb != folder_tmdb:
        return f"任务 TMDB {expected_tmdb} 与文件夹 TMDB {folder_tmdb} 不一致，阻止移动 STRM"
    own_share_code = str(row.get("own_share_code") or "").strip()
    if not own_share_code:
        return "等待自有分享码，暂不移动 STRM"
    receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
    expected_marker = f"/s/{own_share_code}_{receive_code}_"
    for path in sorted(source.rglob("*.strm")):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if "/d/" in text:
            return f"发现直链 STRM：{path}"
        if expected_marker not in text:
            return f"STRM 不是预期的分享链接：{path}"
    return ""


def merge_self_share_strm_folder(plan: MovePlan, store: SubmissionStore, row: dict[str, Any]) -> dict[str, Any]:
    if plan.status in {"pending", "conflict"} and plan.source_path and plan.dest_path:
        source = safe_resolve(plan.source_path)
        issue = validate_self_share_strm_source(source, row)
        if issue:
            return store.update_move(
                int(row["id"]),
                "error",
                source_path=str(source),
                dest_path=str(safe_resolve(plan.dest_path)),
                category_final=plan.category,
                error=issue,
            ) or row
    if plan.status != "conflict" or not plan.source_path or not plan.dest_path:
        return execute_strm_move(plan, store, row)
    source = safe_resolve(plan.source_path)
    dest = safe_resolve(plan.dest_path)
    if not source.exists() or not source.is_dir():
        return execute_strm_move(MovePlan("skipped", "STRM 源目录不存在", source, dest, plan.category), store, row)
    if not dest.exists() or not dest.is_dir():
        return execute_strm_move(MovePlan("pending", "ready", source, dest, plan.category), store, row)
    try:
        for child in source.rglob("*"):
            if not child.is_file():
                continue
            relative = child.relative_to(source)
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)
        shutil.rmtree(source)
    except Exception as exc:
        return store.update_move(
            int(row["id"]),
            "error",
            source_path=str(source),
            dest_path=str(dest),
            category_final=plan.category,
            error=str(exc),
        ) or row
    return store.update_move(
        int(row["id"]),
        "moved",
        source_path=str(source),
        dest_path=str(dest),
        category_final=plan.category,
        error="目标目录已存在，已合并并覆盖同名 STRM",
    ) or row


class SelfShareWorkflow:
    def __init__(self, config: SelfShareConfig, cms: CmsClient, p115: P115WebClient, store: SubmissionStore):
        self.config = config
        self.cms = cms
        self.p115 = p115
        self.store = store

    def prepare(self, row: dict[str, Any], recognition: dict[str, Any], share_name: str) -> tuple[dict[str, Any], Path | None]:
        if not self.config.enabled:
            return row, None
        row_id = int(row["id"])
        if not row.get("workflow_mode"):
            row = self.store.update_self_share(row_id, workflow_mode="self_share_sync", workflow_phase="submitted") or row
        if not row.get("own_share_file_id"):
            self.cms.run_auto_organize()
            row = self.store.update_self_share(row_id, workflow_phase="auto_organize_submitted") or row
            folder = self.p115.find_organized_folder(
                recognition,
                share_name,
                excluded_parent_ids=self.config.excluded_parent_ids or set(),
                min_update_time=float(row.get("created_at") or 0),
            )
            if not folder:
                return row, None
            category = category_for_115_parent_id(str(folder.get("parent_id") or ""), self.config.parent_cid_category_map)
            if category and not row.get("category_choice") and hasattr(self.store, "update_category"):
                row = self.store.update_category(row_id, category, "selected") or row
            enriched = enrich_recognition_from_self_share_folder(recognition, folder, category, share_name)
            if hasattr(self.store, "update_recognition") and (enriched.get("tmdb_id") or enriched.get("category")):
                row = self.store.update_recognition(row_id, enriched, "self_share_resolved") or row
                recognition.update(enriched)
            row = self.store.update_self_share(
                row_id,
                workflow_phase="organized_found",
                own_share_file_id=folder.get("file_id"),
                own_share_file_name=folder.get("file_name"),
            ) or row
        if not row.get("own_share_code"):
            share = self.p115.create_long_share(str(row.get("own_share_file_id") or ""))
            row = self.store.update_self_share(
                row_id,
                workflow_phase="own_share_created",
                own_share_code=share.get("share_code"),
                own_share_receive_code=share.get("receive_code"),
                own_share_url=share.get("share_url"),
            ) or row
        if self.config.cleanup_after_emby and str(row.get("cleanup_status") or "").lower() not in {"deleted", "pending"}:
            cleanup_self_share_source_residue(
                self.p115,
                row,
                recognition,
                share_name,
                self.config.source_cleanup_parent_ids,
            )
            row, _line = cleanup_own_share_source(self.store, row, self.p115)
        if row.get("share_sync_status") != "submitted":
            self.cms.add_share115_sync_task(
                str(row.get("own_share_code") or ""),
                str(row.get("own_share_receive_code") or ""),
                cid=self.config.cms_cid,
                local_path=self.config.cms_local_path,
            )
            row = self.store.update_self_share(row_id, workflow_phase="share_sync_submitted", share_sync_status="submitted") or row
        return row, find_self_share_strm_source_dir(self.config, row, recognition, share_name)


class BridgeSelfShareTaskWorkflow:
    def __init__(
        self,
        cms,
        telegram,
        chat_id,
        store,
        task_store,
        p115,
        self_share_config,
        move_config,
        emby,
        openai_classifier,
        tmdb_resolver,
        cleanup_client=None,
        receive_cid="",
        organized_parent_id="",
        pending_title_prefix="",
        fallback_category="",
        task_db_path=None,
    ):
        self.cms = cms
        self.telegram = telegram
        self.chat_id = chat_id
        self.store = store
        self.task_store = task_store
        self.p115 = p115
        self.self_share_config = self_share_config
        self.move_config = move_config
        self.emby = emby
        self.openai_classifier = openai_classifier
        self.tmdb_resolver = tmdb_resolver
        self.cleanup_client = cleanup_client
        self.receive_cid = str(receive_cid or "").strip()
        self.organized_parent_id = str(organized_parent_id or "").strip()
        self.pending_title_prefix = str(pending_title_prefix or "").strip()
        self.fallback_category = str(fallback_category or "").strip()
        self.task_db_path = task_db_path

    def run_stage(self, task):
        if task.current_stage == TaskStage.RECEIVED:
            return self._stage_received(task)
        if task.current_stage == TaskStage.ORGANIZING:
            return self._stage_organizing(task)
        if task.current_stage == TaskStage.RECOGNIZING:
            return self._stage_recognizing(task)
        if task.current_stage == TaskStage.OWN_SHARE_CREATED:
            return self._stage_own_share_created(task)
        if task.current_stage == TaskStage.SHARE_SYNC_SUBMITTED:
            return self._stage_share_sync_submitted(task)
        if task.current_stage == TaskStage.STRM_READY:
            return self._stage_strm_ready(task)
        if task.current_stage == TaskStage.MOVED:
            return self._stage_moved(task)
        if task.current_stage == TaskStage.EMBY_CONFIRMED:
            return self._stage_emby_confirmed(task)
        if task.current_stage == TaskStage.CLEANED:
            return self._stage_cleaned(task)
        return StageResult.failed("阶段尚未实现", error_type="unsupported_stage")

    def _submission_row(self, task) -> dict[str, Any] | None:
        submission_id = task.metadata.get("submission_id") or task.submission_id
        if submission_id not in (None, ""):
            return self.store.find_by_id(int(submission_id))
        return self.store.find_by_key(ShareKey(task.share_code, task.receive_code))

    def _stage_received(self, task):
        if not self.self_share_config.enabled:
            return StageResult.failed("自分享工作流未启用", error_type="self_share_disabled")
        if not self.receive_cid:
            return StageResult.failed("缺少 115 接收目录 ID", error_type="missing_receive_cid")

        existing = self.store.find_by_key(ShareKey(task.share_code, task.receive_code))
        if self._has_received_self_share_state(existing):
            return StageResult.complete("已接收 115 分享到待整理", self._received_metadata(existing))

        received = self.p115.receive_share_to_cid(task.share_code, task.receive_code, self.receive_cid)
        title = str(received.get("title") or task.title or task.share_code).strip()
        row = self.store.upsert_submission(
            ShareKey(task.share_code, task.receive_code),
            task.url,
            "received",
            title=title,
        )
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="received_to_pending",
        ) or row
        return StageResult.complete(
            "已接收 115 分享到待整理",
            {
                "submission_id": int(row["id"]),
                "received_title": title,
                "received_file_ids": received.get("file_ids") or [],
            },
        )

    def _stage_organizing(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        self.cms.run_auto_organize()
        recognition = self._recognition_from_row(row)
        title = str(row.get("title") or task.title or task.share_code)
        folder = self.p115.find_organized_folder(
            recognition,
            title,
            excluded_parent_ids=self.self_share_config.excluded_parent_ids or set(),
            min_update_time=float(row.get("created_at") or 0),
        )
        if not folder:
            return StageResult.defer(
                "等待 CMS 整理完成",
                self.self_share_config.auto_organize_retry_seconds or 30,
                {"submission_id": int(row["id"])},
            )
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="organized_found",
            own_share_file_id=folder.get("file_id"),
            own_share_file_name=folder.get("file_name"),
        ) or row
        recognition.update(
            {
                "organized_parent_id": str(folder.get("parent_id") or ""),
                "parent_id": str(folder.get("parent_id") or ""),
            }
        )
        if hasattr(self.store, "update_recognition"):
            row = self.store.update_recognition(int(row["id"]), recognition, "organized_found") or row
        return StageResult.complete(
            "已找到 CMS 整理后的 115 文件夹",
            {"submission_id": int(row["id"]), "organized_folder": folder},
        )

    def _stage_recognizing(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        recognition = self._recognition_from_row(row)
        folder = task.metadata.get("organized_folder")
        if not isinstance(folder, dict):
            parent_id = self._organized_parent_id(task, recognition)
            folder = {
                "file_id": row.get("own_share_file_id"),
                "file_name": row.get("own_share_file_name"),
                "parent_id": parent_id,
            }
        file_id = str(folder.get("file_id") or "").strip()
        folder_name = str(folder.get("file_name") or row.get("own_share_file_name") or task.title or "").strip()
        share_name = str(row.get("title") or task.title or folder_name or task.share_code).strip()
        parent_id = self._organized_parent_id(task, recognition, folder)
        category = category_for_115_parent_id(
            parent_id,
            self.self_share_config.parent_cid_category_map,
        )
        tmdb_id = str(
            extract_tmdb_id_from_name(folder_name)
            or extract_tmdb_id_from_name(share_name)
            or recognition.get("tmdb_id")
            or ""
        ).strip()
        recognition.update(
            {
                "title": recognition.get("title") or folder_name or share_name,
                "share_name": recognition.get("share_name") or share_name,
                "tmdb_id": tmdb_id,
                "category": category or str(recognition.get("category") or ""),
                "organized_parent_id": parent_id,
                "parent_id": parent_id,
            }
        )
        if category:
            recognition = enrich_recognition_from_self_share_folder(recognition, folder, category, share_name)
            recognition["organized_parent_id"] = parent_id
            recognition["parent_id"] = parent_id
            tmdb_id = str(recognition.get("tmdb_id") or tmdb_id).strip()
        if not category and self._has_persisted_category_suggestion(recognition):
            return self._needs_action_recognition_result(row, recognition)
        if not category:
            recognition, should_prompt = resolve_category_with_fallbacks(
                recognition,
                share_name,
                openai_classifier=self.openai_classifier,
                tmdb_resolver=self.tmdb_resolver,
            )
            category = str(recognition.get("category") or "").strip()
            tmdb_id = str(recognition.get("tmdb_id") or tmdb_id).strip()
            if should_prompt and category:
                return self._needs_action_recognition_result(row, recognition)
            if should_prompt or not category:
                recognition, should_prompt = decide_category_prompt(self.store, row, recognition, self.move_config, share_name)
                category = str(recognition.get("category") or "").strip()
                tmdb_id = str(recognition.get("tmdb_id") or tmdb_id).strip()
                if should_prompt or not category:
                    return self._needs_action_recognition_result(row, recognition)
        if category and hasattr(self.store, "update_category"):
            row = self.store.update_category(int(row["id"]), category, "selected") or row
        if hasattr(self.store, "update_recognition"):
            row = self.store.update_recognition(int(row["id"]), recognition, "self_share_resolved") or row
        return StageResult.complete(
            "已识别整理后的 115 文件夹",
            {
                "submission_id": int(row["id"]),
                "recognition": recognition,
                "category": category,
                "tmdb_id": tmdb_id,
                "own_share_file_id": file_id,
            },
        )

    def _stage_own_share_created(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        file_id = str(task.metadata.get("own_share_file_id") or row.get("own_share_file_id") or "").strip()
        if not file_id:
            return StageResult.failed("缺少自有分享文件夹 ID", error_type="own_share_file_missing")
        if row.get("own_share_code"):
            return StageResult.complete("已存在自有 115 分享", self._own_share_metadata(row))
        share = self.p115.create_long_share(file_id)
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="own_share_created",
            own_share_code=share.get("share_code"),
            own_share_receive_code=share.get("receive_code"),
            own_share_url=share.get("share_url"),
        ) or row
        return StageResult.complete("已创建自有 115 分享", self._own_share_metadata(row))

    def _stage_share_sync_submitted(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        own_code = str(task.metadata.get("own_share_code") or row.get("own_share_code") or "").strip()
        own_pwd = str(task.metadata.get("own_share_receive_code") or row.get("own_share_receive_code") or "").strip()
        if not own_code:
            return StageResult.failed("缺少自有分享码", error_type="own_share_missing")
        if row.get("share_sync_status") != "submitted":
            self.cms.add_share115_sync_task(
                own_code,
                own_pwd,
                cid=self.self_share_config.cms_cid,
                local_path=self.self_share_config.cms_local_path,
            )
            row = self.store.update_self_share(
                int(row["id"]),
                workflow_phase="share_sync_submitted",
                share_sync_status="submitted",
            ) or row
        return StageResult.complete(
            "已提交 CMS 分享同步",
            {"submission_id": int(row["id"]), "share_sync_status": row.get("share_sync_status") or "submitted"},
        )

    def _stage_strm_ready(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        recognition = self._recognition_from_row(row)
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        source = find_self_share_strm_source_dir(self.self_share_config, row, recognition, share_name)
        metadata = {
            "submission_id": int(row["id"]),
            "category": final_category_for_move(row, recognition),
            "recognition": recognition,
        }
        if not source:
            return StageResult.defer(
                "等待自有分享 STRM 源目录生成",
                self.self_share_config.auto_organize_retry_seconds or 30,
                metadata,
            )
        metadata["source_path"] = str(source)
        return StageResult.complete("已找到自有分享 STRM 源目录", metadata)

    def _stage_moved(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if str(row.get("move_status") or "").lower() == "moved":
            metadata = self._move_metadata(row, task.metadata)
            dest_path = str(metadata.get("dest_path") or "").strip()
            if self._strm_destination_ready(dest_path):
                return StageResult.complete("STRM 已移动到媒体库", metadata)
            return StageResult.defer(
                "等待已移动 STRM 目标目录可用",
                self.self_share_config.auto_organize_retry_seconds or 30,
                metadata,
            )
        recognition = self._recognition_from_row(row)
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        source = find_self_share_strm_source_dir(self.self_share_config, row, recognition, share_name)
        category = final_category_for_move(row, recognition)
        move_config = move_config_for_workflow_source(self.move_config, source, self.self_share_config)
        plan = plan_strm_move(source, category, move_config)
        metadata = {
            "submission_id": int(row["id"]),
            "source_path": str(plan.source_path) if plan.source_path else "",
            "dest_path": str(plan.dest_path) if plan.dest_path else "",
            "category": category,
        }
        if is_move_plan_retryable(plan):
            return StageResult.defer(
                plan.reason,
                self.self_share_config.auto_organize_retry_seconds or 30,
                metadata,
            )
        moved_row = merge_self_share_strm_folder(plan, self.store, row)
        move_status = str(moved_row.get("move_status") or "").lower()
        metadata.update(
            {
                "source_path": str(moved_row.get("source_path") or metadata["source_path"]),
                "dest_path": str(moved_row.get("dest_path") or metadata["dest_path"]),
                "category": str(moved_row.get("category_final") or category),
            }
        )
        if move_status == "moved":
            send_move_result(self.telegram, self.chat_id, plan, moved_row)
            return StageResult.complete("STRM 已移动到媒体库", metadata)
        error = str(moved_row.get("move_error") or plan.reason or "STRM 移动失败")
        return StageResult.failed(error, error_type="strm_move_failed", metadata=metadata)

    def _stage_emby_confirmed(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if str(row.get("emby_status") or "").lower() == "confirmed":
            return StageResult.complete("Emby 已确认入库", self._emby_metadata(row))
        if not self.emby or not getattr(self.emby, "enabled", False):
            return StageResult.needs_action("Emby 确认未启用", {"submission_id": int(row["id"])})
        recognition = self._recognition_from_row(row)
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        recognition.setdefault("share_name", share_name)
        match = self._find_emby_match_for_moved_dest(recognition, row, task.metadata)
        if not match:
            return StageResult.defer(
                "等待 Emby 确认入库",
                self.self_share_config.auto_organize_retry_seconds or 30,
                {"submission_id": int(row["id"]), "recognition": recognition},
            )
        send_emby_confirmed(self.telegram, self.chat_id, self.store, row, match, self.emby, cleanup_client=None)
        updated = self.store.find_by_id(int(row["id"])) or row
        return StageResult.complete("Emby 已确认入库", self._emby_metadata(updated))

    def _stage_cleaned(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if str(row.get("cleanup_status") or "").lower() == "deleted":
            return StageResult.complete("115 转存源已删除，自有分享保留", self._cleanup_metadata(row))
        if str(row.get("move_status") or "").lower() != "moved":
            return StageResult.needs_action("等待 STRM 移动确认后再清理", {"submission_id": int(row["id"])})
        if str(row.get("emby_status") or "").lower() != "confirmed":
            return StageResult.needs_action("等待 Emby 确认后再清理", {"submission_id": int(row["id"])})
        if not str(row.get("own_share_code") or "").strip():
            return StageResult.failed("缺少自有分享码，拒绝清理 115 转存源", error_type="own_share_missing")
        if not str(row.get("own_share_file_id") or "").strip():
            return StageResult.failed("缺少自有分享文件夹 ID", error_type="own_share_file_missing")
        if not self.cleanup_client:
            return StageResult.needs_action("缺少 115 清理客户端", {"submission_id": int(row["id"])})
        updated, line = cleanup_own_share_source(self.store, row, self.cleanup_client)
        cleanup_status = str(updated.get("cleanup_status") or "").lower()
        if cleanup_status == "deleted":
            return StageResult.complete(line or "115 转存源已删除，自有分享保留", self._cleanup_metadata(updated))
        if cleanup_status == "error":
            return StageResult.failed(
                str(updated.get("cleanup_error") or line or "115 转存源删除失败"),
                error_type="cleanup_failed",
                metadata=self._cleanup_metadata(updated),
            )
        return StageResult.needs_action(line or "等待 115 转存源清理", self._cleanup_metadata(updated))

    def _recognition_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            recognition = json.loads(row.get("recognition_json") or "{}")
        except Exception:
            recognition = {}
        return recognition if isinstance(recognition, dict) else {}

    def _has_received_self_share_state(self, row: dict[str, Any] | None) -> bool:
        if not row or row.get("workflow_mode") != "self_share_sync":
            return False
        phase = str(row.get("workflow_phase") or "").strip()
        if phase in {"received", "received_to_pending", "auto_organize_submitted", "organized_found", "own_share_created", "share_sync_submitted"}:
            return True
        return any(row.get(key) for key in ("own_share_file_id", "own_share_code", "share_sync_status"))

    def _received_metadata(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "submission_id": int(row["id"]),
            "received_title": str(row.get("title") or ""),
            "received_file_ids": [],
        }

    def _has_persisted_category_suggestion(self, recognition: dict[str, Any]) -> bool:
        status = str(recognition.get("category_status") or "").strip()
        if status == "openai_suggested":
            return True
        return bool(recognition.get("category_suggestion") and status not in {"selected", "self_share_resolved", "tmdb_resolved", "tmdb_search_resolved", "openai_confident"})

    def _needs_action_recognition_result(self, row: dict[str, Any], recognition: dict[str, Any]):
        status = str(recognition.get("category_status") or "needs_action").strip()
        if hasattr(self.store, "update_recognition"):
            self.store.update_recognition(int(row["id"]), recognition, status)
        return StageResult.needs_action("等待人工确认分类", {"recognition": recognition})

    def _organized_parent_id(
        self,
        task,
        recognition: dict[str, Any],
        folder: dict[str, Any] | None = None,
    ) -> str:
        if folder:
            value = folder.get("parent_id") or folder.get("pid")
            if value:
                return str(value).strip()
        return str(
            task.metadata.get("organized_parent_id")
            or task.metadata.get("parent_id")
            or recognition.get("organized_parent_id")
            or recognition.get("parent_id")
            or self.organized_parent_id
            or ""
        ).strip()

    def _own_share_metadata(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "submission_id": int(row["id"]),
            "own_share_file_id": row.get("own_share_file_id"),
            "own_share_file_name": row.get("own_share_file_name"),
            "own_share_code": row.get("own_share_code"),
            "own_share_receive_code": row.get("own_share_receive_code"),
            "own_share_url": row.get("own_share_url"),
        }

    def _move_metadata(self, row: dict[str, Any], task_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        task_metadata = task_metadata or {}
        source_path = str(row.get("source_path") or task_metadata.get("source_path") or "")
        dest_path = str(row.get("dest_path") or task_metadata.get("dest_path") or "")
        return {
            "submission_id": int(row["id"]),
            "source_path": str(safe_resolve(Path(source_path))) if source_path else "",
            "dest_path": str(safe_resolve(Path(dest_path))) if dest_path else "",
            "category": str(row.get("category_final") or task_metadata.get("category") or ""),
        }

    def _strm_destination_ready(self, dest_path: str) -> bool:
        if not dest_path:
            return False
        dest = safe_resolve(Path(dest_path))
        if not dest.exists():
            return False
        if dest.is_file():
            return dest.suffix.lower() == ".strm"
        if not dest.is_dir():
            return False
        return has_strm_file(dest)

    def _emby_match_in_moved_dest(
        self,
        match: dict[str, Any],
        row: dict[str, Any],
        task_metadata: dict[str, Any] | None = None,
    ) -> bool:
        expected = str(row.get("dest_path") or (task_metadata or {}).get("dest_path") or "").strip()
        if not expected:
            return True
        actual = str(match.get("Path") or "").strip()
        if not actual:
            return False
        expected_path = safe_resolve(Path(expected))
        actual_path = safe_resolve(Path(actual))
        return actual_path == expected_path or is_relative_to(actual_path, expected_path)

    def _find_emby_match_for_moved_dest(
        self,
        recognition: dict[str, Any],
        row: dict[str, Any],
        task_metadata: dict[str, Any] | None = None,
    ) -> dict | None:
        expected = str(row.get("dest_path") or (task_metadata or {}).get("dest_path") or "").strip()
        if not expected:
            return find_emby_match(self.emby, recognition, row, recent_limit=30)
        tmdb_id = expected_task_tmdb_id(recognition, row)
        candidates: list[dict] = []
        if tmdb_id and hasattr(self.emby, "find_items_by_tmdb"):
            try:
                items = self.emby.find_items_by_tmdb(tmdb_id)
            except Exception:
                LOG.debug("Failed to query Emby duplicate TMDB candidates", exc_info=True)
                items = []
            if isinstance(items, list):
                candidates.extend(item for item in items if isinstance(item, dict))
        if tmdb_id and hasattr(self.emby, "find_item_by_tmdb"):
            match = self.emby.find_item_by_tmdb(tmdb_id)
            if isinstance(match, dict):
                candidates.append(match)
        if hasattr(self.emby, "recent_items"):
            candidates.extend(item for item in self.emby.recent_items(limit=100) if isinstance(item, dict))
        seen: set[str] = set()
        for item in candidates:
            key = str(item.get("Id") or item.get("Path") or id(item))
            if key in seen:
                continue
            seen.add(key)
            if tmdb_id:
                if item_tmdb_id(item) != tmdb_id:
                    continue
            elif not match_emby_item([item], recognition, row):
                continue
            if self._emby_match_in_moved_dest(item, row, task_metadata):
                return item
        return None

    def _emby_metadata(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "submission_id": int(row["id"]),
            "emby_status": row.get("emby_status"),
            "item_id": row.get("emby_item_id"),
            "title": row.get("emby_title"),
            "path": row.get("emby_path"),
            "parent": row.get("emby_parent"),
            "library": row.get("emby_parent"),
        }

    def _cleanup_metadata(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "submission_id": int(row["id"]),
            "cleanup_status": row.get("cleanup_status"),
            "cleanup_file_id": row.get("cleanup_file_id"),
            "cleanup_error": row.get("cleanup_error"),
        }


def enrich_recognition_from_self_share_folder(
    recognition: dict[str, Any],
    folder: dict[str, Any],
    category: str,
    share_name: str,
) -> dict[str, Any]:
    folder_name = str(folder.get("file_name") or "").strip()
    resolved_category = str(category or recognition.get("category") or "").strip()
    tmdb_id = str(
        recognition.get("tmdb_id")
        or extract_tmdb_id_from_name(folder_name)
        or extract_tmdb_id_from_name(share_name)
        or ""
    ).strip()
    enriched = dict(recognition)
    enriched.update(
        {
            "ok": True,
            "title": str(enriched.get("title") or folder_name or share_name),
            "type": str(enriched.get("type") or media_type_for_category(resolved_category)),
            "category": resolved_category,
            "tmdb_id": tmdb_id,
            "category_status": "self_share_resolved",
            "share_name": str(enriched.get("share_name") or share_name),
        }
    )
    return enriched


def find_self_share_strm_source_dir(
    config: SelfShareConfig,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str,
) -> Path | None:
    move_config = MoveConfig(source_roots=[config.strm_root], library_roots={}, stable_seconds=0)
    folder_name = str(row.get("own_share_file_name") or "").strip()
    if folder_name:
        candidate = safe_resolve(config.strm_root / folder_name)
        if candidate.exists() and has_strm_file(candidate):
            return candidate
    return find_strm_source_dir(move_config, recognition, share_name=share_name)


def select_move_source_for_workflow(
    existing_source: Path | None,
    prepared_self_share_source: Path | None,
    self_share_enabled: bool = False,
) -> Path | None:
    if self_share_enabled:
        return prepared_self_share_source
    return existing_source or prepared_self_share_source


def move_config_for_workflow_source(
    move_config: MoveConfig,
    source_dir: Path | None,
    self_share_config: SelfShareConfig | None = None,
) -> MoveConfig:
    if self_share_config and source_dir and is_relative_to(source_dir, self_share_config.strm_root):
        return MoveConfig(
            source_roots=[self_share_config.strm_root],
            library_roots=move_config.library_roots,
            conflict_policy=move_config.conflict_policy,
            stable_seconds=move_config.stable_seconds,
        )
    return move_config


def prepare_self_share_move_inputs(
    current_row: dict[str, Any],
    recognition: dict[str, Any],
    title: str,
    self_share_workflow: SelfShareWorkflow,
    existing_source: Path | None = None,
) -> tuple[dict[str, Any], Path | None, str]:
    prepared_row, prepared_source = self_share_workflow.prepare(current_row, recognition, title)
    source_dir = select_move_source_for_workflow(existing_source, prepared_source, self_share_enabled=True)
    return prepared_row, source_dir, final_category_for_move(prepared_row, recognition)


def resolve_self_share_recognition_before_prepare(
    store: Any,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str,
    openai_classifier: Any | None = None,
    tmdb_resolver: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not is_recognition_uncertain(recognition):
        return row, recognition
    resolved, should_prompt = resolve_category_with_fallbacks(
        recognition,
        share_name,
        openai_classifier=openai_classifier,
        tmdb_resolver=tmdb_resolver,
    )
    if should_prompt or not (resolved.get("tmdb_id") or resolved.get("category")):
        return row, recognition
    status = str(resolved.get("category_status") or "confident")
    updated = store.update_recognition(int(row["id"]), resolved, status) if hasattr(store, "update_recognition") else None
    return updated or row, resolved


def cleanup_own_share_source(store: SubmissionStore, row: dict[str, Any], cleanup_client: Any | None) -> tuple[dict[str, Any], str]:
    if not cleanup_client:
        return row, ""
    if row.get("cleanup_status") == "deleted":
        return row, "115转存源此前已删除。"
    file_id = str(row.get("own_share_file_id") or "").strip()
    if not file_id:
        return row, ""
    share_code = str(row.get("own_share_code") or "").strip()
    if not share_code:
        updated = store.update_cleanup(int(row["id"]), "pending", file_id=file_id, error="等待自有分享创建完成") or row
        return updated, "等待自有分享创建完成后再删除 115 转存源。"
    try:
        cleanup_client.delete_file(file_id)
    except Exception as exc:
        updated = store.update_cleanup(int(row["id"]), "error", file_id=file_id, error=str(exc)) or row
        return updated, f"115转存源删除失败：{exc}"
    updated = store.update_cleanup(int(row["id"]), "deleted", file_id=file_id) or row
    return updated, "115转存源已删除；自有分享保留。"


def cleanup_self_share_source_residue(
    cleanup_client: Any | None,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str,
    parent_ids: set[str] | None,
) -> int:
    if not cleanup_client or not parent_ids or not hasattr(cleanup_client, "find_source_residue_files"):
        return 0
    files = cleanup_client.find_source_residue_files(
        recognition,
        share_name,
        parent_ids,
        excluded_file_ids={str(row.get("own_share_file_id") or "").strip()},
        min_update_time=float(row.get("created_at") or 0),
    )
    deleted = 0
    for item in files:
        file_id = str(item.get("file_id") or "").strip()
        if not file_id:
            continue
        cleanup_client.delete_file(file_id)
        deleted += 1
    if deleted:
        LOG.info("Deleted %s receive-stage 115 residue files for row_id=%s", deleted, row.get("id"))
    return deleted


def category_for_self_share_row(row: dict[str, Any]) -> str:
    for key in ("category_final", "category_choice"):
        category = str(row.get(key) or "").strip()
        if category:
            return category
    return final_category_for_move(row, parse_recognition_json(row))


def repair_stranded_self_share_moves(store: Any, move_config: MoveConfig, limit: int = 50) -> int:
    repaired = 0
    for row in store.stranded_self_share_move_candidates(limit=max(1, int(limit))):
        category = category_for_self_share_row(row)
        folder_name = str(row.get("own_share_file_name") or "").strip()
        if not category or not folder_name:
            continue
        for source_root in move_config.source_roots:
            source = safe_resolve(Path(source_root) / folder_name)
            plan = plan_strm_move(source, category, move_config)
            if plan.status in {"pending", "conflict"}:
                updated = merge_self_share_strm_folder(plan, store, row)
                if updated.get("move_status") == "moved":
                    repaired += 1
                break
            if plan.status != "skipped":
                execute_strm_move(plan, store, row)
                break
    return repaired


def restore_missing_self_share_library_folders(
    store: Any,
    cms: Any,
    self_share_config: SelfShareConfig,
    move_config: MoveConfig,
    limit: int = 50,
) -> int:
    restored = 0
    if not hasattr(store, "missing_self_share_library_candidates"):
        return restored
    for row in store.missing_self_share_library_candidates(limit=max(1, int(limit))):
        dest = safe_resolve(Path(str(row.get("dest_path") or "")))
        if dest.exists() and has_strm_file(dest):
            continue
        category = category_for_self_share_row(row)
        folder_name = str(row.get("own_share_file_name") or "").strip()
        if not category or not folder_name:
            continue
        source = safe_resolve(self_share_config.strm_root / folder_name)
        restore_move_config = MoveConfig(
            source_roots=[self_share_config.strm_root],
            library_roots=move_config.library_roots,
            conflict_policy=move_config.conflict_policy,
            stable_seconds=move_config.stable_seconds,
        )
        plan = plan_strm_move(source, category, restore_move_config)
        if plan.status in {"pending", "conflict"}:
            updated = merge_self_share_strm_folder(plan, store, row)
            if str(updated.get("move_status") or "").lower() == "moved":
                restored += 1
            continue
        if plan.status == "skipped" and plan.reason in {"STRM 源目录不存在", "源目录不包含 STRM 文件", "未找到 STRM 源目录"}:
            share_code = str(row.get("own_share_code") or "").strip()
            receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
            if not share_code:
                continue
            cms.add_share115_sync_task(
                share_code,
                receive_code,
                cid=self_share_config.cms_cid,
                local_path=self_share_config.cms_local_path,
            )
            if hasattr(store, "update_self_share"):
                store.update_self_share(
                    int(row["id"]),
                    workflow_phase="restore_share_sync_submitted",
                    share_sync_status="restore_submitted",
                )
    return restored


def cleanup_pending_self_share_sources(store: Any, cleanup_client: Any | None, limit: int = 50) -> int:
    if not cleanup_client or not hasattr(store, "pending_self_share_cleanup_candidates"):
        return 0
    cleaned = 0
    for row in store.pending_self_share_cleanup_candidates(limit=max(1, int(limit))):
        updated, _line = cleanup_own_share_source(store, row, cleanup_client)
        if str(updated.get("cleanup_status") or "").lower() == "deleted":
            cleaned += 1
    return cleaned


def is_move_plan_retryable(plan: MovePlan) -> bool:
    return plan.status == "skipped" and plan.reason in {
        "未找到 STRM 源目录",
        "STRM 源目录不存在",
        "源目录不包含 STRM 文件",
        "STRM 源目录仍在更新",
    }


def should_attempt_strm_move(row: dict[str, Any], self_share_enabled: bool = False) -> bool:
    move_status = str(row.get("move_status") or "").lower()
    if not move_status:
        return True
    return bool(self_share_enabled and move_status in {"skipped", "conflict", "error"})


def should_defer_for_probing(row: dict[str, Any], recognition: dict[str, Any], self_share_enabled: bool = False) -> bool:
    return (
        not self_share_enabled
        and row.get("category_status") == "probing"
        and is_recognition_uncertain(recognition)
    )


CATEGORY_ALIASES = {
    "动画电影": "动漫电影",
}


def map_category_label(label: str, recognition: dict[str, Any]) -> str:
    label = str(label or "").strip()
    label = CATEGORY_ALIASES.get(label, label)
    if label in default_library_roots() or label == "纪录片":
        return label
    return label


def should_wait_for_category(row: dict[str, Any]) -> bool:
    return str(row.get("category_status") or "") == "uncertain" and not row.get("category_choice")



def final_category_for_move(row: dict[str, Any], recognition: dict[str, Any]) -> str:
    for value in (
        row.get("category_choice"),
        row.get("category_final"),
        recognition.get("category"),
    ):
        value = str(value or "").strip()
        if value:
            return map_category_label(value, recognition)
    media_type = str(recognition.get("type") or "")
    if media_type == "movie":
        return "欧美电影"
    if media_type == "tv":
        return "外国电视"
    return ""

def format_history(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "暂无历史记录。"
    lines = ["最近历史："]
    for idx, row in enumerate(rows, 1):
        label = format_task_label(row)
        move = row.get("move_status") or "-"
        emby = row.get("emby_status") or "-"
        category = row.get("category_final") or row.get("category_choice") or row.get("category_status") or "-"
        lines.append(f"{idx}. {label} | 分类:{category} | 移动:{move} | Emby:{emby}")
    failure_summary = format_failure_summary(rows)
    if failure_summary:
        lines.append(failure_summary)
    library_summary = format_library_summary(rows)
    if library_summary:
        lines.append(library_summary)
    return "\n".join(lines)


def format_failure_summary(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        if str(row.get("status") or "").lower() != "failed":
            continue
        reason = str(row.get("last_error") or "").strip()
        if not reason:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    if not counts:
        return ""
    parts = [f"{reason}({count})" for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    return "最近失败原因：" + ", ".join(parts)


def format_library_summary(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        if str(row.get("emby_status") or "").lower() != "confirmed":
            continue
        parent = str(row.get("emby_parent") or "").strip()
        if not parent:
            continue
        counts[parent] = counts.get(parent, 0) + 1
    if not counts:
        return ""
    parts = [f"{name}({count})" for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    return "最近入库媒体库：" + ", ".join(parts)


def parse_recognition_json(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("recognition_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def expected_task_tmdb_id(recognition: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    row = row or {}
    explicit = str(recognition.get("tmdb_id") or "").strip()
    if explicit:
        return explicit
    for value in (
        row.get("title"),
        recognition.get("share_name"),
        row.get("url"),
        row.get("own_share_file_name"),
        row.get("dest_path"),
        row.get("source_path"),
        row.get("emby_path"),
    ):
        tmdb_id = extract_tmdb_id_from_name(str(value or ""))
        if tmdb_id:
            return tmdb_id
    return ""


def item_tmdb_id(item: dict[str, Any]) -> str:
    provider_ids = item.get("ProviderIds") or item.get("ProviderIDs") or {}
    tmdb_id = str(provider_ids.get("Tmdb") or provider_ids.get("TMDB") or "").strip()
    if tmdb_id:
        return tmdb_id
    return extract_tmdb_id_from_name(" ".join(str(item.get(k) or "") for k in ("Path", "Name", "OriginalTitle")))


def quality_issue_for_row(row: dict[str, Any]) -> str:
    if str(row.get("emby_status") or "").lower() != "confirmed":
        return ""
    recognition = parse_recognition_json(row)
    expected_tmdb = expected_task_tmdb_id(recognition, row)
    actual_tmdb = extract_tmdb_id_from_name(" ".join(str(row.get(k) or "") for k in ("emby_path", "source_path", "dest_path")))
    if expected_tmdb and actual_tmdb and expected_tmdb != actual_tmdb:
        return f"疑似错配：任务 TMDB {expected_tmdb}，Emby 路径 TMDB {actual_tmdb}"
    task_title = str(row.get("title") or recognition.get("share_name") or "").strip()
    emby_title = str(row.get("emby_title") or "").strip()
    task_norm = normalize_text(task_title)
    emby_norm = normalize_text(emby_title)
    has_cjk_task_title = bool(re.search(r"[\u4e00-\u9fff]", task_title))
    if has_cjk_task_title and task_norm and emby_norm and emby_norm not in task_norm and task_norm not in emby_norm:
        return f"疑似错配：任务 {task_title}，Emby {emby_title}"
    return ""


def format_quality_report(rows: list[dict[str, Any]]) -> str:
    issues: list[str] = []
    for row in rows:
        issue = quality_issue_for_row(row)
        if not issue:
            continue
        label = format_task_label(row)
        emby_title = str(row.get("emby_title") or "-")
        issues.append(f"{len(issues) + 1}. {label} -> {emby_title}：{issue}")
    if not issues:
        return "最近任务未发现明显错配。"
    return "质量巡检：发现疑似错配\n" + "\n".join(issues)


def quality_issue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if quality_issue_for_row(row)]


def quality_keyboard(rows: list[dict[str, Any]], limit: int = 8) -> dict[str, Any] | None:
    buttons = []
    for row in quality_issue_rows(rows)[:limit]:
        row_id = int(row["id"])
        buttons.append([{"text": f"重新确认：{row_id}", "callback_data": f"emby_recheck:{row_id}"}])
    return {"inline_keyboard": buttons} if buttons else None


def parse_emby_recheck_callback(data: str) -> int | None:
    parts = str(data or "").split(":")
    if len(parts) != 2 or parts[0] != "emby_recheck":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def recheck_emby_row(store: Any, row: dict[str, Any], emby: Any | None) -> tuple[dict[str, Any] | None, str]:
    if not emby or not getattr(emby, "enabled", False):
        return None, "Emby 确认未启用。"
    recognition = parse_recognition_json(row)
    recognition.setdefault("share_name", row.get("title") or "")
    expected_tmdb = expected_task_tmdb_id(recognition, row)
    if not expected_tmdb:
        return None, "无法从任务中提取 TMDB，不能精确重新确认。"
    match = emby.find_item_by_tmdb(expected_tmdb) if hasattr(emby, "find_item_by_tmdb") else None
    if not match:
        match = match_emby_item(emby.recent_items(limit=100), recognition, row)
    if not match:
        return None, f"未找到 TMDB {expected_tmdb} 对应的 Emby 条目。"
    try:
        library_name = emby.library_name_for_item(match)
    except Exception:
        LOG.debug("Failed to resolve Emby library name during recheck", exc_info=True)
        library_name = None
    updated = store.update_emby(
        int(row["id"]),
        "confirmed",
        item_id=str(match.get("Id") or ""),
        title=str(match.get("Name") or ""),
        path=str(match.get("Path") or ""),
        parent=library_name or emby_parent_label(match),
    )
    title = str(match.get("Name") or format_task_label(row))
    library = str((updated or {}).get("emby_parent") or library_name or "未知")
    return updated, f"已重新确认 Emby：{title}\n媒体库：{library}"


def count_field_values(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get(field) or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return status_counts


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def build_metrics_snapshot(store: SubmissionStore) -> dict[str, Any]:
    rows = store.recent(limit=200)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "total": len(rows),
        "status_counts": count_field_values(rows, "status"),
        "emby_status_counts": count_field_values(rows, "emby_status"),
        "move_status_counts": count_field_values(rows, "move_status"),
        "failure_summary": format_failure_summary(rows),
        "library_summary": format_library_summary(rows),
        "telegram_last_transient_error_at": LAST_TELEGRAM_TRANSIENT_ERROR_AT,
    }


def write_metrics_snapshot(store: SubmissionStore, metrics_path: str | Path) -> None:
    payload = build_metrics_snapshot(store)
    path = Path(metrics_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def format_metrics(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "任务统计：",
            f"生成时间：{payload.get('generated_at') or '-'}",
            f"总数：{payload.get('total', 0)}",
            f"任务：{format_counts(payload.get('status_counts') or {})}",
            f"Emby：{format_counts(payload.get('emby_status_counts') or {})}",
            f"移动：{format_counts(payload.get('move_status_counts') or {})}",
            f"失败：{payload.get('failure_summary') or '-'}",
            f"媒体库：{payload.get('library_summary') or '-'}",
            f"Telegram瞬时错误：{payload.get('telegram_last_transient_error_at') or '-'}",
        ]
    )


def metrics_path_for_store(store: SubmissionStore) -> Path:
    return store.db_path.parent / "metrics.json"


def normalize_emby_parents(store: SubmissionStore, emby: EmbyClient | None) -> int:
    if not emby or not emby.enabled:
        return 0
    changed = 0
    for row in store.all_confirmed_with_emby_path():
        parent = str(row.get("emby_parent") or "").strip()
        if not parent or not (parent.isdigit() or parent.lower() in {"series", "season", "movie", "boxset", "folder"}):
            continue
        library_name = emby.library_name_for_item({"Path": row.get("emby_path") or ""})
        if not library_name or library_name == parent:
            continue
        updated = store.update_emby(
            int(row["id"]),
            str(row.get("emby_status") or "confirmed"),
            item_id=str(row.get("emby_item_id") or "") or None,
            title=str(row.get("emby_title") or "") or None,
            path=str(row.get("emby_path") or "") or None,
            parent=library_name,
        )
        if updated:
            changed += 1
    return changed


def format_health(
    move_config: MoveConfig,
    cms_ok: bool,
    emby_ok: bool,
    telegram_ok: bool | None = None,
    telegram_last_error_at: str | None = None,
    openai_enabled: bool | None = None,
    openai_ok: bool | None = None,
) -> str:
    source_ok = all(safe_resolve(root).exists() for root in move_config.source_roots)
    lib_ok = all(safe_resolve(root).exists() for root in move_config.library_roots.values())
    lines = [f"CMS: {'OK' if cms_ok else 'FAIL'}"]
    if telegram_ok is not None:
        lines.append(f"Telegram: {'OK' if telegram_ok else 'FAIL'}")
    if telegram_last_error_at:
        lines.append(f"Telegram最近瞬时错误: {telegram_last_error_at}")
    if openai_enabled is not None:
        if openai_enabled:
            lines.append(f"OpenAI分类兜底: {'OK' if openai_ok else 'FAIL'}")
        else:
            lines.append("OpenAI分类兜底: DISABLED")
    lines.extend(
        [
            f"Emby: {'OK' if emby_ok else 'FAIL'}",
            f"STRM源: {'OK' if source_ok else 'FAIL'} ({len(move_config.source_roots)})",
            f"媒体库映射: {'OK' if lib_ok else 'FAIL'} ({len(move_config.library_roots)})",
            f"冲突策略: {move_config.conflict_policy}",
        ]
    )
    return "\n".join(lines)


class HttpJson:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def request(self, url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Accept": "application/json"}
        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Cannot reach {url}: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON response from {url}: {raw[:300]}") from exc


class FormHttp:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def request(
        self,
        url: str,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        body = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
        req_headers = {"Accept": "application/json, text/plain, */*"}
        if data is not None:
            req_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body_text[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Non-JSON response from {url}: {raw[:300]}") from exc


def load_cookie_value(value_or_path: str) -> str:
    value = str(value_or_path or "").strip()
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return value


def p115_file_id(item: dict[str, Any]) -> str:
    return str(item.get("cid") or item.get("fid") or item.get("file_id") or "").strip()


def p115_parent_id(item: dict[str, Any]) -> str:
    return str(item.get("pid") or item.get("parent_id") or "").strip()


def p115_residue_file_id(item: dict[str, Any]) -> str:
    return str(item.get("fid") or item.get("file_id") or item.get("cid") or "").strip()


def p115_residue_parent_id(item: dict[str, Any]) -> str:
    return str(item.get("cid") or item.get("pid") or item.get("parent_id") or "").strip()


def parse_parent_cid_category_map(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in split_env_list(value):
        if "=" not in item:
            continue
        parent_id, category = item.split("=", 1)
        parent_id = parent_id.strip()
        category = category.strip()
        if parent_id and category:
            result[parent_id] = category
    return result


def category_for_115_parent_id(parent_id: str, mapping: dict[str, str] | None = None) -> str:
    category_map = mapping if mapping is not None else CMS_PARENT_CID_CATEGORY_MAP
    return category_map.get(str(parent_id or "").strip(), "")


def p115_file_name(item: dict[str, Any]) -> str:
    return str(item.get("n") or item.get("file_name") or item.get("name") or "").strip()


def select_organized_115_folder(
    items: list[dict[str, Any]],
    recognition: dict[str, Any],
    share_name: str,
    excluded_parent_ids: set[str] | None = None,
) -> dict[str, str] | None:
    excluded = {str(value) for value in (excluded_parent_ids or set()) if str(value)}
    tokens = candidate_tokens(recognition, share_name)
    tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
    if tmdb_id:
        tokens.insert(0, tmdb_id)
    matches: list[tuple[int, float, dict[str, str]]] = []
    for item in items:
        file_id = p115_file_id(item)
        name = p115_file_name(item)
        if not file_id or not name:
            continue
        if "fid" in item and "cid" in item:
            continue
        if p115_parent_id(item) in excluded:
            continue
        norm_name = normalize_text(name)
        score = 0
        if tmdb_id and tmdb_id in name:
            score += 8
        if any(token and token in norm_name for token in tokens):
            score += 3
        if "[tmdb" in name.lower() or "{tmdb" in name.lower():
            score += 2
        if score <= 0:
            continue
        try:
            update_time = float(item.get("tu") or item.get("t") or item.get("te") or 0)
        except (TypeError, ValueError):
            update_time = 0.0
        matches.append((score, update_time, {"file_id": file_id, "file_name": name, "parent_id": p115_parent_id(item)}))
    if not matches:
        return None
    matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return matches[0][2]


def select_recent_tmdb_115_folder(
    items: list[dict[str, Any]],
    year: str,
    excluded_parent_ids: set[str] | None = None,
    min_update_time: float = 0,
) -> dict[str, str] | None:
    excluded = {str(value) for value in (excluded_parent_ids or set()) if str(value)}
    matches: list[tuple[float, dict[str, str]]] = []
    for item in items:
        file_id = p115_file_id(item)
        name = p115_file_name(item)
        if not file_id or not name:
            continue
        if "fid" in item and "cid" in item:
            continue
        if p115_parent_id(item) in excluded:
            continue
        low_name = name.lower()
        if year and year not in name:
            continue
        if "[tmdb" not in low_name and "{tmdb" not in low_name:
            continue
        try:
            update_time = float(item.get("tu") or item.get("t") or item.get("te") or 0)
        except (TypeError, ValueError):
            update_time = 0.0
        if min_update_time and update_time and update_time < min_update_time:
            continue
        matches.append((update_time, {"file_id": file_id, "file_name": name, "parent_id": p115_parent_id(item)}))
    if not matches:
        return None
    matches.sort(key=lambda value: value[0], reverse=True)
    return matches[0][1]


def select_source_residue_115_files(
    items: list[dict[str, Any]],
    recognition: dict[str, Any],
    share_name: str,
    excluded_file_ids: set[str] | None = None,
    min_update_time: float = 0,
) -> list[dict[str, str]]:
    excluded = {str(value) for value in (excluded_file_ids or set()) if str(value)}
    tokens = candidate_tokens(recognition, share_name)
    year = extract_year_from_name(share_name) or extract_year_from_name(str(recognition.get("title") or ""))
    matches: list[tuple[int, float, dict[str, str]]] = []
    for item in items:
        file_id = p115_residue_file_id(item)
        name = p115_file_name(item)
        if not file_id or not name or file_id in excluded:
            continue
        update_time = as_float(item.get("tu") or item.get("t") or item.get("te"), 0.0)
        if min_update_time and update_time and update_time < min_update_time:
            continue
        norm_name = normalize_text(name)
        score = 0
        if any(token and token in norm_name for token in tokens):
            score += 5
        if year and year in name:
            score += 2
        if score < 5:
            continue
        matches.append(
            (
                score,
                update_time,
                {
                    "file_id": file_id,
                    "file_name": name,
                    "parent_id": p115_residue_parent_id(item),
                },
            )
        )
    matches.sort(key=lambda value: (value[0], value[1]), reverse=True)
    return [match[2] for match in matches]


class P115WebClient:
    def __init__(self, cookie: str, http: Any | None = None, timeout: int = 60):
        self.cookie = load_cookie_value(cookie)
        self.http = http or FormHttp(timeout)
        if not self.cookie:
            raise RuntimeError("115 cookie is empty")

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "Origin": "https://115.com",
            "Referer": "https://115.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        }

    def _request(self, url: str, method: str = "GET", data: dict | None = None, params: dict | None = None) -> dict:
        return self.http.request(url, method=method, data=data, params=params, headers=self._headers())

    @staticmethod
    def _ensure_state(resp: dict, fallback: str) -> dict:
        if resp.get("state") is True:
            return resp
        if "state" not in resp and resp.get("code") in {0, "", None}:
            return resp
        raise RuntimeError(str(resp.get("error") or resp.get("message") or fallback))

    def search_files(self, search_value: str, limit: int = 20) -> list[dict[str, Any]]:
        resp = self._request(
            "https://webapi.115.com/files/search",
            params={"search_value": search_value, "limit": limit, "offset": 0, "fc_mix": 1},
        )
        self._ensure_state(resp, "115 search failed")
        return iter_items(resp.get("data") or resp)

    def share_snap(self, share_code: str, receive_code: str, cid: str = "0", limit: int = 100) -> dict[str, Any]:
        resp = self._request(
            "https://webapi.115.com/share/snap",
            params={
                "share_code": share_code,
                "receive_code": receive_code,
                "cid": cid,
                "offset": 0,
                "limit": limit,
            },
        )
        self._ensure_state(resp, "115 share snap failed")
        return resp

    def receive_share_to_cid(self, share_code: str, receive_code: str, target_cid: str) -> dict[str, Any]:
        snap = self.share_snap(share_code, receive_code, cid="0", limit=100)
        data = snap.get("data") if isinstance(snap.get("data"), dict) else {}
        items = iter_items(data.get("list") or data)
        file_ids = [str(item.get("fid") or item.get("cid") or item.get("file_id") or "").strip() for item in items]
        file_ids = [file_id for file_id in file_ids if file_id]
        if not file_ids:
            raise RuntimeError("115 share snap did not return file ids")
        resp = self._request(
            "https://webapi.115.com/share/receive",
            method="POST",
            data={
                "share_code": share_code,
                "receive_code": receive_code,
                "file_id": ",".join(file_ids),
                "cid": str(target_cid),
            },
        )
        self._ensure_state(resp, "115 receive share failed")
        info = data.get("shareinfo") if isinstance(data.get("shareinfo"), dict) else {}
        receive_data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        title = str(receive_data.get("receive_title") or info.get("share_title") or (items[0].get("n") if items else "") or "").strip()
        return {"title": title, "file_ids": file_ids, "response": resp}

    def list_files(self, parent_id: str, limit: int = 100) -> list[dict[str, Any]]:
        resp = self._request(
            "https://webapi.115.com/files",
            params={"cid": str(parent_id), "limit": limit, "offset": 0, "show_dir": 1, "fc_mix": 1},
        )
        self._ensure_state(resp, "115 list files failed")
        return iter_items(resp.get("data") or resp)

    def find_source_residue_files(
        self,
        recognition: dict[str, Any],
        share_name: str,
        parent_ids: set[str],
        excluded_file_ids: set[str] | None = None,
        min_update_time: float = 0,
    ) -> list[dict[str, str]]:
        items: list[dict[str, Any]] = []
        for parent_id in parent_ids:
            parent_id = str(parent_id or "").strip()
            if parent_id:
                items.extend(self.list_files(parent_id, limit=100))
        return select_source_residue_115_files(
            items,
            recognition,
            share_name,
            excluded_file_ids=excluded_file_ids,
            min_update_time=min_update_time,
        )

    def find_organized_folder(
        self,
        recognition: dict[str, Any],
        share_name: str,
        excluded_parent_ids: set[str] | None = None,
        min_update_time: float = 0,
    ) -> dict[str, str] | None:
        search_values = candidate_tokens(recognition, share_name)
        tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
        if tmdb_id:
            search_values.insert(0, tmdb_id)
        seen = set()
        items: list[dict[str, Any]] = []
        for value in search_values:
            value = str(value or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            items.extend(self.search_files(value, limit=20))
        selected = select_organized_115_folder(items, recognition, share_name, excluded_parent_ids=excluded_parent_ids)
        if selected:
            return selected
        # If CMS/TMDB already identified the item, do not guess by year; wait for the exact TMDB folder.
        if tmdb_id:
            return None
        year = extract_year_from_name(share_name)
        if year:
            fallback_items: list[dict[str, Any]] = []
            for value in (f"{year} tmdb", year):
                if value in seen:
                    continue
                seen.add(value)
                fallback_items.extend(self.search_files(value, limit=20))
            return select_recent_tmdb_115_folder(fallback_items, year, excluded_parent_ids=excluded_parent_ids, min_update_time=min_update_time)
        return None

    def create_long_share(self, file_id: str) -> dict[str, str]:
        resp = self._request(
            "https://webapi.115.com/share/send",
            method="POST",
            data={"file_ids": str(file_id), "ignore_warn": 1},
        )
        self._ensure_state(resp, "115 create share failed")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        share_code = str(resp.get("share_code") or data.get("share_code") or "").strip()
        receive_code = str(resp.get("receive_code") or data.get("receive_code") or "").strip()
        share_url = str(resp.get("share_url") or data.get("share_url") or "").strip()
        if not share_code:
            raise RuntimeError("115 create share did not return share_code")
        update = self._request(
            "https://webapi.115.com/share/updateshare",
            method="POST",
            data={
                "share_code": share_code,
                "receive_code": receive_code or "1212",
                "share_duration": -1,
                "auto_fill_recvcode": 1,
            },
        )
        self._ensure_state(update, "115 update share failed")
        return {"share_code": share_code, "receive_code": receive_code or "1212", "share_url": share_url}

    def delete_file(self, file_id: str) -> dict:
        resp = self._request(
            "https://webapi.115.com/rb/delete",
            method="POST",
            data={"fid": str(file_id), "ignore_warn": 1},
        )
        return self._ensure_state(resp, "115 delete failed")


class OpenAIClassifier:
    def __init__(self, config: Config, http: HttpJson | None = None):
        self.config = config
        self.http = http or HttpJson(config.http_timeout)
        self.high_confidence = config.openai_high_confidence
        self.suggest_confidence = config.openai_suggest_confidence

    @property
    def enabled(self) -> bool:
        return bool(self.config.openai_classify_enabled and self.config.openai_api_key)

    def healthcheck(self) -> bool:
        return self.enabled

    def classify_media(self, recognition: dict[str, Any], share_name: str) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenAI classifier is disabled")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "category": {"type": "string", "enum": OPENAI_CATEGORY_LABELS},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "media_type": {"type": "string", "enum": ["movie", "tv", "documentary", "unknown"]},
                "title": {"type": "string"},
                "tmdb_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["category", "confidence", "media_type", "title", "tmdb_id", "reason"],
        }
        user_payload = {
            "share_name": share_name,
            "cms_recognition": {
                key: recognition.get(key)
                for key in ("ok", "title", "type", "category", "tmdb_id", "raw_msg")
            },
            "allowed_categories": OPENAI_CATEGORY_LABELS,
        }
        payload = {
            "model": self.config.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": "你是媒体文件分类器。只根据文件名、标题、年份、CMS识别结果判断应放入哪个媒体库分类。不要编造无法从输入推断的信息。",
                },
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "max_output_tokens": 300,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "media_category",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        resp = self.http.request(
            f"{self.config.openai_base_url}/responses",
            method="POST",
            payload=payload,
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            },
        )
        parsed = self._extract_json(resp)
        category = str(parsed.get("category") or "").strip()
        if category not in OPENAI_CATEGORY_LABELS:
            raise RuntimeError(f"OpenAI returned unsupported category: {category}")
        parsed["confidence"] = max(0.0, min(1.0, as_float(parsed.get("confidence"), 0.0)))
        return parsed

    @staticmethod
    def _extract_json(resp: dict[str, Any]) -> dict[str, Any]:
        text = str(resp.get("output_text") or "").strip()
        if not text:
            for item in resp.get("output") or []:
                for content in item.get("content") or []:
                    if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                        text = str(content.get("text") or "").strip()
                        if text:
                            break
                if text:
                    break
        if not text and isinstance(resp.get("choices"), list):
            message = ((resp["choices"][0] or {}).get("message") or {}) if resp["choices"] else {}
            text = str(message.get("content") or "").strip()
        if not text:
            raise RuntimeError("OpenAI returned empty classification")
        return json.loads(text)


class CmsClient:
    def __init__(self, config: Config, http: HttpJson | None = None):
        self.config = config
        self.http = http or HttpJson(config.http_timeout)
        self.token = ""

    def login(self) -> None:
        resp = self.http.request(
            f"{self.config.cms_base_url}/api/auth/login",
            method="POST",
            payload={"username": self.config.cms_username, "password": self.config.cms_password},
        )
        token = ((resp.get("data") or {}).get("token") or "").strip()
        if resp.get("code") != 200 or not token:
            raise RuntimeError(resp.get("msg") or "CMS login failed")
        self.token = token

    def _authorized(self, path: str, payload: dict | None = None, method: str = "POST", params: dict | None = None) -> dict:
        if not self.token:
            self.login()
        if params:
            path = path + "?" + urllib.parse.urlencode(params)
        return self.http.request(
            f"{self.config.cms_base_url}{path}",
            method=method,
            payload=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def add_share_down(self, url: str) -> dict:
        resp = self._authorized("/api/cloud/add_share_down", payload={"url": url})
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS rejected the share link")
        return resp

    def list_share_down(self, page_size: int = 20) -> list[dict]:
        resp = self._authorized("/api/share_down/list", method="GET", params={"page": 1, "page_size": page_size})
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS share_down list failed")
        return iter_items(resp.get("data"))

    def get_share_down_detail(self, task_id: str) -> dict:
        try:
            for item in self.list_share_down(page_size=50):
                item_id = item.get("id") or item.get("task_id") or item.get("taskId")
                if str(item_id) == str(task_id):
                    return item
        except Exception as exc:
            LOG.debug("CMS status probe failed error=%s", exc)
        return {"status": "unknown"}

    def get_share_down_by_key(self, key: ShareKey) -> dict:
        for item in self.list_share_down(page_size=100):
            if str(item.get("share_id") or "").lower() == key.share_code and str(item.get("share_pwd") or "") == key.receive_code:
                return item
        return {}

    def recognize_media(self, path: str) -> dict:
        resp = self._authorized("/api/media/file_recognize", payload={"path": path})
        return resp

    def run_auto_organize(self) -> dict:
        resp = self._authorized("/api/sync/auto_organize", method="GET")
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS auto organize failed")
        return resp

    def add_share115_sync_task(self, share_code: str, receive_code: str, cid: str = "0", local_path: str = "/media/share") -> dict:
        resp = self._authorized(
            "/api/sync/share115",
            payload={
                "share_code": share_code,
                "receive_code": receive_code,
                "cid": cid,
                "local_path": local_path,
            },
        )
        if resp.get("code") != 200:
            raise RuntimeError(resp.get("msg") or "CMS share115 sync failed")
        return resp

    def auto_organize_excluded_parent_ids(self) -> set[str]:
        resp = self._authorized("/api/config/auto_organize", method="GET")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        return {
            str(data.get(key) or "").strip()
            for key in ("NEW_MEDIA_CID", "REDUNDANT_DATA_CID", "NEW_MEDIA_EXISTS_CID", "NEW_MEDIA_FAILED_CID")
            if str(data.get(key) or "").strip()
        }

    def healthcheck(self) -> bool:
        try:
            self.list_share_down(page_size=1)
        except Exception:
            return False
        return True


class TelegramClient:
    def __init__(self, token: str, http: HttpJson | None = None, timeout: int = 60):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.http = http or HttpJson(timeout)

    @staticmethod
    def _is_transient_get_updates_error(exc: Exception) -> bool:
        text = str(exc).lower()
        if "cannot reach" not in text:
            return False
        return any(
            token in text
            for token in (
                "unexpected_eof_while_reading",
                "handshake operation timed out",
                "read operation timed out",
                "connection reset by peer",
                "network unreachable",
            )
        )

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        global LAST_TELEGRAM_TRANSIENT_ERROR_AT
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            params["offset"] = offset
        url = self.base_url + "/getUpdates?" + urllib.parse.urlencode(params)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                resp = self.http.request(url)
                break
            except RuntimeError as exc:
                last_error = exc
                if attempt == 1 or not self._is_transient_get_updates_error(exc):
                    raise
                LAST_TELEGRAM_TRANSIENT_ERROR_AT = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                LOG.warning("Telegram getUpdates transient error, retrying once: %s", exc)
                time.sleep(1)
        else:
            assert last_error is not None
            raise last_error
        if not resp.get("ok"):
            raise RuntimeError(resp.get("description") or "Telegram getUpdates failed")
        return resp.get("result") or []

    def healthcheck(self) -> bool:
        try:
            resp = self.http.request(self.base_url + "/getMe")
        except Exception:
            return False
        return bool(resp.get("ok"))

    def send_message(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.http.request(
            self.base_url + "/sendMessage",
            method="POST",
            payload=payload,
        )

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
        self.http.request(
            self.base_url + "/answerCallbackQuery",
            method="POST",
            payload={"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert},
        )


def menu_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📊 统计"}, {"text": "📋 最近任务"}],
            [{"text": "🕘 历史"}, {"text": "🧹 清理历史"}],
            [{"text": "🩺 健康检查"}, {"text": "❓ 帮助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def clear_history_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ 确认清理", "callback_data": "clear_history:confirm"},
                {"text": "取消", "callback_data": "clear_history:cancel"},
            ]
        ]
    }


def send_menu_message(telegram: TelegramClient, chat_id: int | str, text: str) -> None:
    try:
        telegram.send_message(chat_id, text, reply_markup=menu_keyboard())
    except TypeError:
        telegram.send_message(chat_id, text)


def log_polling_error(telegram: TelegramClient, exc: Exception) -> None:
    if telegram._is_transient_get_updates_error(exc):
        LOG.warning("Telegram polling transient error; retrying soon: %s", exc)
        return
    LOG.exception("Polling loop failed; retrying soon")


class EmbyClient:
    def __init__(self, base_url: str, api_key: str, user_id: str = "", http: HttpJson | None = None, timeout: int = 60):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.user_id = user_id or ""
        self.http = http or HttpJson(timeout)
        self._library_roots: list[tuple[Path, str]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        if not self.enabled:
            raise RuntimeError("Emby confirmation is disabled")
        params = dict(params or {})
        params["api_key"] = self.api_key
        url = self.base_url + path + "?" + urllib.parse.urlencode(params)
        return self.http.request(url, method="GET")

    def get_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        users = self._get("/Users")
        if isinstance(users, list) and users:
            self.user_id = str(users[0].get("Id") or "")
        if not self.user_id:
            raise RuntimeError("Cannot determine Emby user id")
        return self.user_id

    def recent_items(self, limit: int = 20) -> list[dict]:
        user_id = self.get_user_id()
        resp = self._get(
            f"/Users/{user_id}/Items",
            {
                "Recursive": "true",
                "Limit": str(limit),
                "Fields": "Path,ProviderIds,DateCreated,MediaSources,ParentId,Overview",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
            },
        )
        if isinstance(resp, dict):
            return [item for item in resp.get("Items") or [] if isinstance(item, dict)]
        return []

    def find_item_by_tmdb(self, tmdb_id: str) -> dict | None:
        tmdb_id = str(tmdb_id or "").strip()
        if not tmdb_id:
            return None
        user_id = self.get_user_id()
        resp = self._get(
            f"/Users/{user_id}/Items",
            {
                "Recursive": "true",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "Path,ProviderIds,ParentId,MediaSources",
                "Limit": "10",
            },
        )
        items = [item for item in (resp.get("Items") if isinstance(resp, dict) else []) or [] if isinstance(item, dict)]
        for item in items:
            if item_tmdb_id(item) == tmdb_id:
                return item
        return None

    def library_roots(self) -> list[tuple[Path, str]]:
        if self._library_roots is not None:
            return self._library_roots
        resp = self._get("/Library/VirtualFolders/Query")
        items = resp.get("Items") if isinstance(resp, dict) else resp
        roots: list[tuple[Path, str]] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("Name") or "").strip()
                if not name:
                    continue
                raw_paths: list[str] = []
                for value in item.get("Locations") or []:
                    if value:
                        raw_paths.append(str(value))
                path_infos = (item.get("LibraryOptions") or {}).get("PathInfos") or []
                for info in path_infos:
                    if isinstance(info, dict) and info.get("Path"):
                        raw_paths.append(str(info.get("Path")))
                for raw_path in raw_paths:
                    roots.append((safe_resolve(Path(raw_path)), name))
        roots.sort(key=lambda pair: len(pair[0].parts), reverse=True)
        self._library_roots = roots
        return roots

    def library_name_for_item(self, item: dict) -> str | None:
        raw_path = str(item.get("Path") or "").strip()
        if not raw_path:
            return None
        item_path = safe_resolve(Path(raw_path))
        for root, name in self.library_roots():
            if is_relative_to(item_path, root):
                return name
        return None

def iter_items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "items", "records", "data", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def extract_task_info(resp: dict) -> tuple[str | None, str | None]:
    data = resp.get("data")
    candidates: list[dict[str, Any]] = []
    if isinstance(data, dict):
        candidates.append(data)
    candidates.append(resp)
    for item in candidates:
        task_id = item.get("id") or item.get("task_id") or item.get("taskId")
        title = item.get("name") or item.get("title") or item.get("share_name") or item.get("file_name")
        if task_id or title:
            return (str(task_id) if task_id is not None else None, str(title) if title else None)
    return None, None


def classify_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "login" in low or "unauthorized" in low or "401" in low:
        return "CMS 登录失败"
    if "cannot reach" in low or "timed out" in low or "connection" in low:
        return "CMS 服务不可用"
    if "提取码" in text or "password" in low or "失效" in text or "不存在" in text:
        return "115 分享失效或提取码错误"
    if "链接格式" in text:
        return "链接格式错误"
    return "CMS API 返回异常"




CATEGORY_LABELS = {
    "cn_movie": "华语电影",
    "western_movie": "欧美电影",
    "asian_movie": "亚洲电影",
    "anime_movie": "动漫电影",
    "cn_tv": "国产电视",
    "foreign_tv": "外国电视",
    "bangumi": "番剧",
    "documentary": "纪录片",
    "skip": "跳过",
}


def normalize_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def clean_share_title(value: str) -> str:
    text = re.sub(r"\{?\[?tmdb(?:id)?[=_\-]\d+\]?\}?", "", str(value or ""), flags=re.I)
    text = re.sub(r"\(\d{4}[^)]*\)", "", text)
    return text.strip()


def infer_region_category(media_type: str, title: str, language: str = "") -> str:
    if media_type == "tv":
        if language in {"zh", "cn", "中文", "普通话", "汉语"}:
            return "国产电视"
        return "外国电视"
    if media_type == "movie":
        if language in {"zh", "cn", "中文", "普通话", "汉语"} or re.search(r"[\u4e00-\u9fff]", title):
            return "华语电影"
        return "欧美电影"
    return ""


class TmdbWebResolver:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return True

    def lookup(self, tmdb_id: str, media_type: str, share_name: str) -> dict[str, Any]:
        url = f"https://www.themoviedb.org/{media_type}/{tmdb_id}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception:
            LOG.debug("TMDB web lookup failed media_type=%s tmdb_id=%s", media_type, tmdb_id, exc_info=True)
            return {"ok": False, "type": media_type, "tmdb_id": tmdb_id}
        title = extract_tmdb_page_title(raw)
        language = extract_tmdb_default_language(raw)
        if not title:
            return {"ok": False, "type": media_type, "tmdb_id": tmdb_id}
        return {
            "ok": True,
            "title": title,
            "type": media_type,
            "tmdb_id": tmdb_id,
            "language": language,
            "source": "tmdb_web",
        }

    def search(self, query: str, media_type: str = "tv") -> dict[str, Any]:
        query = str(query or "").strip()
        media_type = "movie" if media_type == "movie" else "tv"
        if not query:
            return {"ok": False, "type": media_type}
        url = "https://www.themoviedb.org/search?" + urllib.parse.urlencode({"query": query})
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception:
            LOG.debug("TMDB web search failed media_type=%s query=%s", media_type, query, exc_info=True)
            return {"ok": False, "type": media_type}
        pattern = re.compile(rf'href="/{re.escape(media_type)}/(\d+)[^"]*"[^>]*>.*?alt="([^"]+)"', re.I | re.S)
        match = pattern.search(raw)
        if not match:
            match = re.search(rf'href="/{re.escape(media_type)}/(\d+)[^"]*"', raw, re.I)
        if not match:
            return {"ok": False, "type": media_type}
        title = html_lib.unescape(match.group(2)).strip() if match.lastindex and match.lastindex >= 2 else query
        return {"ok": True, "title": title, "type": media_type, "tmdb_id": match.group(1), "source": "tmdb_search"}


def extract_tmdb_search_query(share_name: str) -> str:
    text = str(share_name or "")
    match = re.search(r"([A-Za-z][A-Za-z0-9]+(?:\.[A-Za-z0-9]+){2,})\.S\d{1,2}", text, re.I)
    if match:
        return re.sub(r"\.+", " ", match.group(1)).strip()
    match = re.search(r"([A-Za-z][A-Za-z0-9]+(?:\.[A-Za-z0-9]+){1,})\.(?:19|20)\d{2}", text, re.I)
    if match:
        return re.sub(r"\.+", " ", match.group(1)).strip()
    return ""


def extract_tmdb_page_title(html: str) -> str:
    match = re.search(r'<meta property="og:title" content="([^"]+)"', html, re.I)
    if not match:
        match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if not match:
        return ""
    title = html_lib.unescape(match.group(1))
    title = re.sub(r"\s+—\s+The Movie Database.*$", "", title)
    title = re.sub(r"\s+\((?:TV Series|Movie).*?\)\s*$", "", title)
    return title.strip()


def extract_tmdb_default_language(html: str) -> str:
    match = re.search(r"<strong><bdi>默认语言</bdi></strong>\s*([^<]+)</p>", html, re.I)
    return html_lib.unescape(match.group(1)).strip() if match else ""


def tmdb_match_score(tmdb_result: dict[str, Any], share_name: str) -> int:
    if not tmdb_result.get("ok"):
        return 0
    share_norm = normalize_text(clean_share_title(share_name))
    title_norm = normalize_text(str(tmdb_result.get("title") or ""))
    if not share_norm or not title_norm:
        return 0
    if title_norm in share_norm or share_norm in title_norm:
        return 10
    return 0


def apply_tmdb_hint_resolution(
    recognition: dict[str, Any],
    share_name: str,
    tmdb_resolver: Any | None,
) -> tuple[dict[str, Any], bool]:
    if not is_recognition_uncertain(recognition):
        return recognition, False
    tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
    if not tmdb_id or not tmdb_resolver or not getattr(tmdb_resolver, "enabled", False):
        return recognition, True
    candidates = []
    for media_type in ("tv", "movie"):
        try:
            item = tmdb_resolver.lookup(tmdb_id, media_type, share_name)
        except Exception:
            LOG.debug("TMDB resolver failed", exc_info=True)
            item = {"ok": False}
        score = tmdb_match_score(item, share_name)
        if score:
            candidates.append((score, item))
    if not candidates:
        return recognition, True
    candidates.sort(key=lambda value: value[0], reverse=True)
    best = candidates[0][1]
    media_type = str(best.get("type") or "")
    category = infer_region_category(media_type, str(best.get("title") or ""), str(best.get("language") or ""))
    if not category:
        return recognition, True
    enriched = dict(recognition)
    enriched.update(
        {
            "ok": True,
            "title": str(best.get("title") or recognition.get("title") or share_name),
            "type": media_type,
            "category": category,
            "tmdb_id": tmdb_id,
            "category_status": "tmdb_resolved",
            "openai_source": "tmdb_web",
        }
    )
    return enriched, False


def apply_tmdb_search_resolution(
    recognition: dict[str, Any],
    share_name: str,
    tmdb_resolver: Any | None,
) -> tuple[dict[str, Any], bool]:
    if not is_recognition_uncertain(recognition):
        return recognition, False
    if recognition.get("tmdb_id") or not tmdb_resolver or not getattr(tmdb_resolver, "enabled", False):
        return recognition, True
    query = extract_tmdb_search_query(share_name)
    if not query:
        return recognition, True
    media_type = "tv" if re.search(r"\bS\d{1,2}\b|\.S\d{1,2}", str(share_name or ""), re.I) else "movie"
    try:
        item = tmdb_resolver.search(query, media_type)
    except Exception:
        LOG.debug("TMDB search resolver failed", exc_info=True)
        return recognition, True
    if not item or not item.get("ok") or not item.get("tmdb_id"):
        return recognition, True
    category = infer_region_category(str(item.get("type") or media_type), str(item.get("title") or ""), str(item.get("language") or ""))
    if not category:
        return recognition, True
    enriched = dict(recognition)
    enriched.update(
        {
            "ok": True,
            "title": str(item.get("title") or query),
            "type": str(item.get("type") or media_type),
            "category": category,
            "tmdb_id": str(item.get("tmdb_id") or ""),
            "category_status": "tmdb_search_resolved",
            "openai_source": "tmdb_search",
        }
    )
    return enriched, False


def normalize_recognition(resp: dict) -> dict[str, Any]:
    data = resp.get("data") if isinstance(resp, dict) else None
    data = data if isinstance(data, dict) else {}
    tmdb = data.get("tmdb_info") if isinstance(data.get("tmdb_info"), dict) else {}
    video = data.get("video_info") if isinstance(data.get("video_info"), dict) else {}
    media_type = data.get("type") or tmdb.get("type") or video.get("type")
    title = tmdb.get("title") or tmdb.get("name") or data.get("title") or data.get("name") or video.get("name")
    tmdb_id = tmdb.get("tmdb_id") or tmdb.get("id") or data.get("tmdb_id")
    return {
        "ok": resp.get("code") == 200 if isinstance(resp, dict) else False,
        "title": str(title) if title else "",
        "type": str(media_type) if media_type else "",
        "category": str(data.get("category") or ""),
        "tmdb_id": str(tmdb_id) if tmdb_id else "",
        "raw_msg": str(resp.get("msg") or "") if isinstance(resp, dict) else "",
        "category_suggestion": str(data.get("category_suggestion") or ""),
        "category_status": str(data.get("category_status") or ""),
        "openai_confidence": as_float(data.get("openai_confidence"), 0.0),
        "openai_reason": str(data.get("openai_reason") or ""),
        "openai_source": str(data.get("openai_source") or ""),
    }


def is_recognition_uncertain(result: dict[str, Any]) -> bool:
    if not result.get("ok"):
        return True
    if not result.get("tmdb_id") and not result.get("title"):
        return True
    if result.get("type") not in {"movie", "tv"}:
        return True
    if not result.get("category"):
        return True
    return False



def apply_openai_category_fallback(
    recognition: dict[str, Any],
    share_name: str,
    openai_classifier: Any | None,
) -> tuple[dict[str, Any], bool]:
    if not is_recognition_uncertain(recognition):
        return recognition, False
    if not openai_classifier or not getattr(openai_classifier, "enabled", False):
        return recognition, True
    try:
        result = openai_classifier.classify_media(recognition, share_name)
    except Exception:
        LOG.debug("OpenAI category fallback failed", exc_info=True)
        return recognition, True
    category = map_category_label(str(result.get("category") or ""), recognition)
    if category not in OPENAI_CATEGORY_LABELS:
        return recognition, True
    confidence = max(0.0, min(1.0, as_float(result.get("confidence"), 0.0)))
    media_type = str(result.get("media_type") or "")
    if media_type == "documentary":
        media_type = media_type_for_category(category) or "movie"
    if media_type not in {"movie", "tv"}:
        media_type = media_type_for_category(category)
    enriched = dict(recognition)
    enriched.update(
        {
            "category": category,
            "category_suggestion": category,
            "openai_confidence": confidence,
            "openai_reason": str(result.get("reason") or ""),
            "openai_source": "openai",
            "title": str(result.get("title") or recognition.get("title") or share_name or ""),
            "tmdb_id": str(result.get("tmdb_id") or recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or ""),
        }
    )
    if confidence >= getattr(openai_classifier, "high_confidence", 0.75):
        enriched.update({"ok": True, "type": media_type, "category_status": "openai_confident"})
        return enriched, False
    if confidence >= getattr(openai_classifier, "suggest_confidence", 0.45):
        enriched.update({"type": media_type, "category_status": "openai_suggested"})
        return enriched, True
    return recognition, True


def resolve_category_with_fallbacks(
    recognition: dict[str, Any],
    share_name: str,
    openai_classifier: Any | None = None,
    tmdb_resolver: Any | None = None,
) -> tuple[dict[str, Any], bool]:
    resolved, should_prompt = apply_tmdb_hint_resolution(recognition, share_name, tmdb_resolver)
    if not should_prompt:
        return resolved, False
    resolved, should_prompt = apply_tmdb_search_resolution(resolved, share_name, tmdb_resolver)
    if not should_prompt:
        return resolved, False
    return apply_openai_category_fallback(resolved, share_name, openai_classifier)


def category_for_emby_item(item: dict[str, Any], emby: Any | None = None) -> str:
    if emby:
        try:
            library = str(emby.library_name_for_item(item) or "")
        except Exception:
            library = ""
        if "外国电视" in library or "TV" in library:
            return "外国电视"
        if "国产电视" in library or "TVCN" in library:
            return "国产电视"
        if "番剧" in library or "Dongman" in library:
            return "番剧"
        if "华语电影" in library:
            return "华语电影"
        if "欧美电影" in library:
            return "欧美电影"
        if "亚洲电影" in library:
            return "亚洲电影"
        if "动漫电影" in library:
            return "动漫电影"
    path = str(item.get("Path") or "")
    if "/TV/" in path or "/TV\\" in path:
        return "外国电视"
    if "/TVCN/" in path or "/TVCN\\" in path:
        return "国产电视"
    if "/Dongman/" in path or "/Dongman\\" in path:
        return "番剧"
    return ""


def media_type_for_emby_item(item: dict[str, Any], category: str) -> str:
    item_type = str(item.get("Type") or "").lower()
    if item_type in {"series", "season", "episode"}:
        return "tv"
    return media_type_for_category(category)


def resolve_category_or_existing_import(
    telegram: TelegramClient,
    chat_id: int | str,
    store: SubmissionStore,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str,
    move_config: MoveConfig | None = None,
    emby: Any | None = None,
    openai_classifier: Any | None = None,
    tmdb_resolver: Any | None = None,
) -> tuple[dict[str, Any], bool]:
    tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
    if emby and getattr(emby, "enabled", False) and tmdb_id and hasattr(emby, "find_item_by_tmdb"):
        item = emby.find_item_by_tmdb(tmdb_id)
        if item:
            category = category_for_emby_item(item, emby)
            if category:
                resolved = dict(recognition)
                resolved.update(
                    {
                        "ok": True,
                        "title": str(item.get("Name") or recognition.get("title") or share_name),
                        "type": media_type_for_emby_item(item, category),
                        "category": category,
                        "tmdb_id": tmdb_id,
                        "category_status": "cms_emby_resolved",
                        "share_name": share_name,
                    }
                )
                store.update_recognition(int(row["id"]), resolved, "confident")
                store.update_category(int(row["id"]), category, "selected")
                path = str(item.get("Path") or "")
                if path and hasattr(store, "update_move"):
                    store.update_move(
                        int(row["id"]),
                        "skipped",
                        source_path=path,
                        dest_path=path,
                        category_final=category,
                        error="CMS/Emby 已入库，无需人工分类",
                    )
                parent = None
                try:
                    parent = emby.library_name_for_item(item)
                except Exception:
                    parent = None
                store.update_emby(
                    int(row["id"]),
                    "confirmed",
                    item_id=str(item.get("Id") or ""),
                    title=str(item.get("Name") or ""),
                    path=path,
                    parent=parent or emby_parent_label(item),
                )
                return resolved, False
    resolved = resolve_category_or_prompt(
        telegram,
        chat_id,
        store,
        row,
        recognition,
        share_name,
        openai_classifier=openai_classifier,
        tmdb_resolver=tmdb_resolver,
    )
    return resolved, should_wait_for_category(store.find_by_id(int(row["id"])) if hasattr(store, "find_by_id") else row)


def category_keyboard(row_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "华语电影", "callback_data": f"cat:{row_id}:cn_movie"},
                {"text": "欧美电影", "callback_data": f"cat:{row_id}:western_movie"},
            ],
            [
                {"text": "亚洲电影", "callback_data": f"cat:{row_id}:asian_movie"},
                {"text": "动漫电影", "callback_data": f"cat:{row_id}:anime_movie"},
            ],
            [
                {"text": "国产电视", "callback_data": f"cat:{row_id}:cn_tv"},
                {"text": "外国电视", "callback_data": f"cat:{row_id}:foreign_tv"},
            ],
            [
                {"text": "番剧", "callback_data": f"cat:{row_id}:bangumi"},
                {"text": "纪录片", "callback_data": f"cat:{row_id}:documentary"},
            ],
            [{"text": "跳过", "callback_data": f"cat:{row_id}:skip"}],
        ]
    }


def maybe_request_category_confirmation(
    telegram: TelegramClient,
    chat_id: int | str,
    store: SubmissionStore,
    row: dict[str, Any],
    recognition_resp: dict,
) -> dict[str, Any]:
    result = normalize_recognition(recognition_resp)
    force_uncertain = result.get("category_status") == "openai_suggested"
    status = "uncertain" if force_uncertain or is_recognition_uncertain(result) else "confident"
    updated = store.update_recognition(int(row["id"]), result, status) or row
    if status == "uncertain":
        message = f"CMS 识别不确定：{format_task_label(updated)}\n"
        if result.get("category_suggestion"):
            confidence = as_float(result.get("openai_confidence"), 0.0)
            reason = str(result.get("openai_reason") or "")
            message += f"OpenAI建议：{result.get('category_suggestion')}（置信度 {confidence:.2f}）\n"
            if reason:
                message += f"理由：{reason[:80]}\n"
        message += "请选择建议分类："
        telegram.send_message(
            chat_id,
            message,
            reply_markup=category_keyboard(int(row["id"])),
        )
    return result


def resolve_category_or_prompt(
    telegram: TelegramClient,
    chat_id: int | str,
    store: SubmissionStore,
    row: dict[str, Any],
    recognition: dict[str, Any],
    share_name: str,
    openai_classifier: Any | None = None,
    tmdb_resolver: Any | None = None,
) -> dict[str, Any]:
    recognition, should_prompt = resolve_category_with_fallbacks(recognition, share_name, openai_classifier, tmdb_resolver)
    if should_prompt:
        resolved = maybe_request_category_confirmation(
            telegram,
            chat_id,
            store,
            row,
            {"code": 200 if recognition.get("ok") else 500, "data": recognition, "msg": recognition.get("raw_msg")},
        )
        resolved["share_name"] = share_name
        return resolved
    status = str(recognition.get("category_status") or "confident")
    store.update_recognition(int(row["id"]), recognition, status)
    recognition["share_name"] = share_name
    return recognition


def enrich_recognition_from_library_path(
    recognition: dict[str, Any],
    source_dir: Path,
    category: str,
) -> dict[str, Any]:
    enriched = dict(recognition)
    enriched.update(
        {
            "ok": True,
            "category": category,
            "type": enriched.get("type") or media_type_for_category(category),
            "title": enriched.get("title") or source_dir.name,
            "source_path": str(source_dir),
            "tmdb_id": enriched.get("tmdb_id") or extract_tmdb_id_from_name(source_dir.name),
        }
    )
    return enriched


def decide_category_prompt(
    store: SubmissionStore,
    row: dict[str, Any],
    recognition: dict[str, Any],
    move_config: MoveConfig | None,
    share_name: str,
) -> tuple[dict[str, Any], bool]:
    if not move_config or not is_recognition_uncertain(recognition):
        return recognition, True
    found = find_recent_library_strm_source_dir(move_config, row, recognition, share_name=share_name)
    if found:
        source_dir, category = found
        return enrich_recognition_from_library_path(recognition, source_dir, category), True
    if find_strm_source_dir(move_config, recognition, share_name=share_name):
        return recognition, True
    store.update_recognition(int(row["id"]), recognition, "probing")
    return recognition, False


def parse_category_callback(data: str) -> tuple[int, str] | None:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "cat" or parts[2] not in CATEGORY_LABELS:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def handle_callback_query(callback_query: dict, telegram: TelegramClient, allowed_chat_id: str, store: SubmissionStore, emby: EmbyClient | None = None) -> None:
    sender_id = ((callback_query.get("from") or {}).get("id"))
    message = callback_query.get("message") or {}
    chat_id = ((message.get("chat") or {}).get("id"))
    if str(chat_id) != str(allowed_chat_id) or str(sender_id) != str(allowed_chat_id):
        LOG.info("Ignoring unauthorized callback chat_id=%s sender_id=%s", chat_id, sender_id)
        return
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    if data == "clear_history:cancel":
        telegram.answer_callback_query(callback_id, "已取消清理", show_alert=False)
        return
    if data == "clear_history:confirm":
        removed = store.clear_finished_history()
        write_metrics_snapshot(store, metrics_path_for_store(store))
        telegram.answer_callback_query(callback_id, f"已清理 {removed} 条", show_alert=False)
        telegram.send_message(chat_id, f"已清理 {removed} 条已结束历史记录。正在处理中的任务已保留。")
        return
    recheck_row_id = parse_emby_recheck_callback(data)
    if recheck_row_id is not None:
        row = store.find_by_id(recheck_row_id)
        if not row:
            telegram.answer_callback_query(callback_id, "任务不存在或已过期", show_alert=True)
            return
        updated, message = recheck_emby_row(store, row, emby)
        telegram.answer_callback_query(callback_id, "已执行重新确认" if updated else "重新确认未完成", show_alert=False)
        telegram.send_message(chat_id, message)
        return
    parsed = parse_category_callback(data)
    if not parsed:
        telegram.answer_callback_query(callback_id, "不支持的操作", show_alert=True)
        return
    row_id, category_key = parsed
    row = store.find_by_id(row_id)
    if not row:
        telegram.answer_callback_query(callback_id, "任务不存在或已过期", show_alert=True)
        return
    label = CATEGORY_LABELS[category_key]
    status = "skipped" if category_key == "skip" else "selected"
    updated = store.update_category(row_id, None if category_key == "skip" else label, status)
    telegram.answer_callback_query(callback_id, f"已记录分类：{label}", show_alert=False)
    if updated:
        telegram.send_message(chat_id, f"已记录分类：{label}\n{format_task_label(updated)}")


def match_emby_item(items: list[dict], recognition: dict[str, Any], row: dict[str, Any] | None = None) -> dict | None:
    tmdb_id = expected_task_tmdb_id(recognition, row)
    if tmdb_id:
        for item in items:
            if item_tmdb_id(item) == tmdb_id:
                return item
        return None
    title_norm = normalize_text(str(recognition.get("title") or recognition.get("share_name") or ""))
    if not title_norm:
        return None
    for item in items:
        haystack = normalize_text(" ".join(str(item.get(k) or "") for k in ("Name", "OriginalTitle", "Path")))
        if title_norm and title_norm in haystack:
            return item
    return None


def find_emby_match(emby: Any, recognition: dict[str, Any], row: dict[str, Any] | None = None, recent_limit: int = 30) -> dict | None:
    tmdb_id = expected_task_tmdb_id(recognition, row)
    if tmdb_id and hasattr(emby, "find_item_by_tmdb"):
        match = emby.find_item_by_tmdb(tmdb_id)
        if match:
            return match
        return None
    return match_emby_item(emby.recent_items(limit=recent_limit), recognition, row)


def repair_stale_submission(store: Any, row: dict[str, Any], emby: Any | None, move_config: MoveConfig | None = None) -> bool:
    if not emby or not getattr(emby, "enabled", False) or not hasattr(emby, "find_item_by_tmdb"):
        return False
    recognition = parse_recognition_json(row)
    share_name = str(row.get("title") or recognition.get("share_name") or "")
    recognition.setdefault("share_name", share_name)
    tmdb_id = expected_task_tmdb_id(recognition, row)
    if not tmdb_id:
        return False
    item = emby.find_item_by_tmdb(tmdb_id)
    if not item:
        return False
    category = category_for_emby_item(item, emby)
    if not category:
        return False
    item_path = str(item.get("Path") or "")
    item_name = str(item.get("Name") or recognition.get("title") or share_name)
    resolved = dict(recognition)
    resolved.update(
        {
            "ok": True,
            "title": item_name,
            "type": media_type_for_emby_item(item, category),
            "category": category,
            "tmdb_id": tmdb_id,
            "category_status": "cms_emby_resolved",
            "share_name": share_name,
        }
    )
    row_id = int(row["id"])
    store.update_recognition(row_id, resolved, "confident")
    store.update_category(row_id, category, "selected")
    if str(row.get("move_status") or "").lower() != "moved" and hasattr(store, "update_move"):
        store.update_move(
            row_id,
            "skipped",
            source_path=item_path,
            dest_path=item_path,
            category_final=category,
            error="CMS/Emby 已入库，自愈修复",
        )
    try:
        parent = emby.library_name_for_item(item)
    except Exception:
        LOG.debug("Failed to resolve Emby library name during status repair", exc_info=True)
        parent = None
    store.update_emby(
        row_id,
        "confirmed",
        item_id=str(item.get("Id") or ""),
        title=item_name,
        path=item_path,
        parent=parent or emby_parent_label(item),
    )
    if (
        str(row.get("workflow_mode") or "") == "self_share_sync"
        and str(row.get("move_status") or "").lower() == "moved"
        and str(row.get("cleanup_status") or "").lower() not in {"pending", "deleted"}
        and str(row.get("own_share_file_id") or "").strip()
        and hasattr(store, "update_cleanup")
    ):
        store.update_cleanup(row_id, "pending", file_id=str(row.get("own_share_file_id") or ""), error="等待确认后删除 115 转存源")
    return True


def repair_stale_submissions(store: Any, emby: Any | None, move_config: MoveConfig | None = None, limit: int = 50) -> int:
    repaired = 0
    for row in store.stale_for_repair(limit=max(1, int(limit))):
        try:
            if repair_stale_submission(store, row, emby, move_config=move_config):
                repaired += 1
        except Exception:
            LOG.debug("Status repair failed for row id=%s", row.get("id"), exc_info=True)
    return repaired


def start_status_repair_loop(
    store: Any,
    emby: Any | None,
    move_config: MoveConfig | None = None,
    cms: Any | None = None,
    self_share_config: SelfShareConfig | None = None,
    cleanup_client: Any | None = None,
    interval_seconds: int = 300,
    limit: int = 50,
) -> threading.Thread | None:
    if interval_seconds <= 0:
        return None

    def loop() -> None:
        while True:
            try:
                repaired = repair_stale_submissions(store, emby, move_config=move_config, limit=limit)
                moved = repair_stranded_self_share_moves(store, move_config, limit=limit) if move_config else 0
                restored = (
                    restore_missing_self_share_library_folders(store, cms, self_share_config, move_config, limit=limit)
                    if cms and self_share_config and move_config
                    else 0
                )
                cleaned = cleanup_pending_self_share_sources(store, cleanup_client, limit=limit)
                if repaired:
                    LOG.info("Status repair fixed %s stale submissions", repaired)
                if moved:
                    LOG.info("Status repair moved %s stranded self-share STRM folders", moved)
                if restored:
                    LOG.info("Status repair restored %s missing self-share STRM folders", restored)
                if cleaned:
                    LOG.info("Status repair cleaned %s pending self-share source folders", cleaned)
                if repaired or moved or restored or cleaned:
                    write_metrics_snapshot(store, metrics_path_for_store(store))
            except Exception:
                LOG.debug("Status repair loop failed", exc_info=True)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="status-repair", daemon=True)
    thread.start()
    return thread


def emby_parent_label(item: dict) -> str:
    return str(item.get("ParentId") or item.get("CollectionType") or item.get("Type") or "未知")


def send_move_result(telegram: TelegramClient, chat_id: int | str, move_plan: MovePlan, moved_row: dict[str, Any]) -> None:
    if str(moved_row.get("move_status") or "").lower() == "moved":
        telegram.send_message(chat_id, f"STRM 已移动：{moved_row.get('dest_path')}")
    elif move_plan.status in {"conflict", "error"}:
        telegram.send_message(chat_id, f"STRM 未移动：{move_plan.reason}\n源：{move_plan.source_path or '-'}\n目标：{move_plan.dest_path or '-'}")


def send_emby_confirmed(
    telegram: TelegramClient,
    chat_id: int | str,
    store: SubmissionStore,
    row: dict[str, Any],
    item: dict,
    emby: EmbyClient | None = None,
    cleanup_client: Any | None = None,
) -> None:
    debug_details = LOG.isEnabledFor(logging.DEBUG)
    library_name = None
    if emby:
        try:
            library_name = emby.library_name_for_item(item)
        except Exception:
            LOG.debug("Failed to resolve Emby library name", exc_info=True)
    parent_label = library_name or emby_parent_label(item)
    updated = store.update_emby(
        int(row["id"]),
        "confirmed",
        item_id=str(item.get("Id") or ""),
        title=str(item.get("Name") or ""),
        path=str(item.get("Path") or ""),
        parent=parent_label,
    ) or row
    library_line = (
        f"媒体库：{updated.get('emby_parent') or library_name}"
        if library_name
        else f"媒体库未解析，父级/类型：{updated.get('emby_parent') or '未知'}"
    )
    lines = [
        f"Emby 已确认入库：{updated.get('emby_title') or item.get('Name') or format_task_label(updated)}",
        library_line,
    ]
    if cleanup_client:
        updated, cleanup_line = cleanup_own_share_source(store, updated, cleanup_client)
        if cleanup_line:
            lines.append(cleanup_line)
    if debug_details or not library_name:
        lines.extend(
            [
                f"ItemId：{updated.get('emby_item_id') or item.get('Id') or '-'}",
                f"路径：{updated.get('emby_path') or item.get('Path') or '-'}",
            ]
        )
    telegram.send_message(
        chat_id,
        "\n".join(lines),
    )


def send_emby_timeout(telegram: TelegramClient, chat_id: int | str, store: SubmissionStore, row: dict[str, Any]) -> None:
    updated = store.update_emby(int(row["id"]), "timeout") or row
    telegram.send_message(
        chat_id,
        f"CMS 已提交，但暂未在 Emby 确认入库：{format_task_label(updated)}\n可以稍后用 /status 查看，或到 Emby/CMS 后台确认。",
    )

def format_task_label(row: dict[str, Any]) -> str:
    task_id = row.get("cms_task_id")
    title = row.get("title") or row.get("share_code") or "任务"
    return f"{title} #{task_id}" if task_id else str(title)


def sync_cms_status_task_event(task_store: TaskStore | None, row: dict[str, Any], status: str, title: str | None = None):
    row_for_task = dict(row)
    if title:
        row_for_task["title"] = title
    normalized = str(status or "").lower()
    task_status = TaskStatus.SUCCEEDED if is_terminal_status(normalized) and normalized not in {"failed", "error"} else TaskStatus.RUNNING
    if normalized in {"failed", "error"}:
        task_status = TaskStatus.FAILED
    return best_effort_task_sync(
        "cms_status",
        record_submission_event,
        task_store,
        row_for_task,
        TaskStage.ORGANIZED,
        task_status,
        f"CMS 状态：{status or 'unknown'}",
        error_summary=str(row.get("last_error") or "") if task_status == TaskStatus.FAILED else "",
    )


def sync_needs_action_task_event(task_store: TaskStore | None, row: dict[str, Any], reason: str):
    return best_effort_task_sync(
        "needs_action",
        record_submission_event,
        task_store,
        row,
        TaskStage.NEEDS_ACTION,
        TaskStatus.NEEDS_ACTION,
        reason,
        error_summary=reason,
    )


def sync_self_share_task_events(task_store: TaskStore | None, row: dict[str, Any]) -> None:
    if not task_store:
        return
    if row.get("own_share_code"):
        best_effort_task_sync(
            "own_share_created",
            record_submission_event,
            task_store,
            row,
            TaskStage.OWN_SHARE_CREATED,
            TaskStatus.SUCCEEDED,
            "已创建自有 115 分享",
        )
    if str(row.get("share_sync_status") or "").lower() in {"submitted", "restore_submitted"}:
        best_effort_task_sync(
            "share_sync_submitted",
            record_submission_event,
            task_store,
            row,
            TaskStage.SHARE_SYNC_SUBMITTED,
            TaskStatus.RUNNING,
            "已提交 CMS 分享同步",
        )


def sync_strm_ready_task_event(task_store: TaskStore | None, row: dict[str, Any], move_plan: MovePlan) -> None:
    if not task_store:
        return
    if move_plan.status in {"pending", "conflict"}:
        best_effort_task_sync(
            "strm_ready",
            record_submission_event,
            task_store,
            row,
            TaskStage.STRM_READY,
            TaskStatus.RUNNING,
            "已找到 STRM 源目录",
        )
    elif move_plan.status == "error":
        best_effort_task_sync(
            "strm_ready_failed",
            record_submission_event,
            task_store,
            row,
            TaskStage.STRM_READY,
            TaskStatus.FAILED,
            move_plan.reason,
            error_summary=move_plan.reason,
        )


def sync_move_task_event(task_store: TaskStore | None, row: dict[str, Any]):
    move_status = str(row.get("move_status") or "").lower()
    if move_status == "moved":
        return best_effort_task_sync(
            "moved",
            record_submission_event,
            task_store,
            row,
            TaskStage.MOVED,
            TaskStatus.SUCCEEDED,
            f"STRM 已移动：{row.get('dest_path') or '-'}",
        )
    if move_status in {"error", "failed"}:
        reason = str(row.get("move_error") or "STRM 移动失败")
        return best_effort_task_sync(
            "move_failed",
            record_submission_event,
            task_store,
            row,
            TaskStage.MOVED,
            TaskStatus.FAILED,
            reason,
            error_summary=reason,
        )
    return None


def sync_emby_task_event(task_store: TaskStore | None, row: dict[str, Any]):
    emby_status = str(row.get("emby_status") or "").lower()
    if emby_status == "confirmed":
        return best_effort_task_sync(
            "emby_confirmed",
            record_submission_event,
            task_store,
            row,
            TaskStage.EMBY_CONFIRMED,
            TaskStatus.SUCCEEDED,
            f"Emby 已确认：{row.get('emby_title') or row.get('title') or row.get('share_code')}",
        )
    if emby_status == "timeout":
        return best_effort_task_sync(
            "emby_timeout",
            record_submission_event,
            task_store,
            row,
            TaskStage.EMBY_CONFIRMED,
            TaskStatus.FAILED,
            "Emby 确认超时",
            error_summary="Emby 确认超时",
        )
    if emby_status == "disabled":
        return best_effort_task_sync(
            "emby_disabled",
            record_submission_event,
            task_store,
            row,
            TaskStage.EMBY_CONFIRMED,
            TaskStatus.NEEDS_ACTION,
            "Emby 确认未启用",
            error_summary="Emby 确认未启用",
        )
    return None


def sync_cleanup_task_event(task_store: TaskStore | None, row: dict[str, Any]):
    if str(row.get("cleanup_status") or "").lower() != "deleted":
        return None
    return best_effort_task_sync(
        "cleaned",
        record_submission_event,
        task_store,
        row,
        TaskStage.CLEANED,
        TaskStatus.SUCCEEDED,
        "115 转存源已删除，自有分享保留",
    )


def format_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "暂无记录。直接发送 115 分享链接即可创建任务。"
    lines = ["最近任务："]
    for idx, row in enumerate(rows, 1):
        status = row.get("status") or "unknown"
        label = format_task_label(row)
        err = f"，{row['last_error']}" if row.get("last_error") else ""
        lines.append(f"{idx}. {label}：{status}{err}")
    failure_summary = format_failure_summary(rows)
    if failure_summary:
        lines.append(failure_summary)
    return "\n".join(lines)


def is_terminal_status(status: str) -> bool:
    low = str(status or "").lower()
    return any(word in low for word in TERMINAL_STATUS_KEYWORDS)


def should_skip_existing_submission(row: dict[str, Any] | None, self_share_enabled: bool = False) -> bool:
    if not row:
        return False
    if str(row.get("status") or "").strip().lower() in {"failed", "error"}:
        return False
    if not self_share_enabled:
        return True
    if str(row.get("workflow_mode") or "") == "self_share_sync":
        return True
    progress_keys = (
        "own_share_file_id",
        "own_share_code",
        "share_sync_status",
        "move_status",
        "emby_status",
        "cleanup_status",
        "source_path",
        "dest_path",
    )
    return any(row.get(key) for key in progress_keys)


def start_status_poll(
    cms: CmsClient,
    telegram: TelegramClient,
    chat_id: int | str,
    store: SubmissionStore,
    row: dict[str, Any],
    max_seconds: int = 300,
    interval: int = 20,
    emby: EmbyClient | None = None,
    move_config: MoveConfig | None = None,
    openai_classifier: OpenAIClassifier | None = None,
    tmdb_resolver: Any | None = None,
    self_share_workflow: SelfShareWorkflow | None = None,
    cleanup_client: Any | None = None,
    task_store: TaskStore | None = None,
) -> None:
    task_id = row.get("cms_task_id")
    if max_seconds <= 0:
        return

    def worker() -> None:
        nonlocal task_id, row
        deadline = time.time() + max_seconds
        last_status = row.get("status") or "submitted"
        recognition: dict[str, Any] = {"title": row.get("title") or row.get("share_code") or ""}
        recognition_checked = False
        while time.time() < deadline:
            time.sleep(max(1, interval))
            try:
                if self_share_workflow and not task_id:
                    title = row.get("title") or row.get("share_code") or ""
                    updated = store.update_status(int(row["id"]), "organizing", title=str(title or "")) or row
                    sync_cms_status_task_event(task_store, updated, "organizing", title=str(title or ""))
                    recognition = normalize_recognition({"code": 500, "msg": "waiting for CMS organize"})
                    recognition["share_name"] = str(title or "")
                    recognition_checked = True
                    current_row = store.find_by_id(int(row["id"])) or updated
                    if move_config and should_attempt_strm_move(current_row, self_share_enabled=True):
                        current_row, recognition = resolve_self_share_recognition_before_prepare(
                            store,
                            current_row,
                            recognition,
                            str(title or ""),
                            openai_classifier=openai_classifier,
                            tmdb_resolver=tmdb_resolver,
                        )
                        source_dir = safe_resolve(Path(str(recognition.get("source_path")))) if recognition.get("source_path") else None
                        current_row, source_dir, category = prepare_self_share_move_inputs(
                            current_row,
                            recognition,
                            str(title or ""),
                            self_share_workflow,
                            source_dir,
                        )
                        sync_self_share_task_events(task_store, current_row)
                        if not source_dir:
                            row = current_row
                            continue
                        active_move_config = move_config_for_workflow_source(move_config, source_dir, self_share_workflow.config)
                        move_plan = plan_strm_move(source_dir, category, active_move_config)
                        sync_strm_ready_task_event(task_store, current_row, move_plan)
                        if is_move_plan_retryable(move_plan):
                            row = current_row
                            continue
                        moved_row = merge_self_share_strm_folder(move_plan, store, current_row)
                        sync_move_task_event(task_store, moved_row)
                        send_move_result(telegram, chat_id, move_plan, moved_row)
                        row = moved_row
                        if moved_row.get("dest_path"):
                            recognition["dest_path"] = moved_row.get("dest_path")
                    if emby and emby.enabled:
                        try:
                            match = find_emby_match(emby, recognition, store.find_by_id(int(row["id"])) or updated, recent_limit=30)
                            if match:
                                confirmed_row = store.find_by_id(int(row["id"])) or updated
                                send_emby_confirmed(telegram, chat_id, store, confirmed_row, match, emby, cleanup_client=cleanup_client)
                                latest_row = store.find_by_id(int(row["id"])) or confirmed_row
                                sync_emby_task_event(task_store, latest_row)
                                sync_cleanup_task_event(task_store, latest_row)
                                return
                        except Exception:
                            LOG.debug("Emby confirmation probe failed", exc_info=True)
                    last_status = "organizing"
                    continue
                if not task_id:
                    key = ShareKey(str(row.get("share_code") or ""), str(row.get("receive_code") or ""))
                    found_task = cms.get_share_down_by_key(key)
                    found_task_id = found_task.get("id") or found_task.get("task_id") or found_task.get("taskId")
                    if found_task_id:
                        task_id = str(found_task_id)
                        row = store.upsert_submission(
                            key,
                            str(row.get("url") or ""),
                            str(row.get("status") or "submitted"),
                            cms_task_id=task_id,
                            title=found_task.get("share_name") or found_task.get("name") or row.get("title"),
                        )
                        best_effort_task_sync(
                            "late_cms_task_id",
                            record_submission_event,
                            task_store,
                            row,
                            TaskStage.CMS_SUBMITTED,
                            TaskStatus.RUNNING,
                            "已找到 CMS 任务 ID",
                        )
                    else:
                        continue
                detail = cms.get_share_down_detail(str(task_id))
                status = str(detail.get("status") or detail.get("state") or detail.get("task_status") or last_status)
                title = detail.get("name") or detail.get("title") or detail.get("share_name") or row.get("title")
                updated = store.update_status(int(row["id"]), status, title=title) or row
                sync_cms_status_task_event(task_store, updated, status, title=str(title or ""))
                if title and not recognition_checked:
                    recognition_checked = True
                    try:
                        recognition_resp = cms.recognize_media(str(title))
                        recognition = normalize_recognition(recognition_resp)
                    except Exception:
                        LOG.debug("CMS recognition failed", exc_info=True)
                        recognition = normalize_recognition({"code": 500, "msg": "recognition failed"})
                    recognition["share_name"] = str(title)
                    recognition, should_prompt = decide_category_prompt(store, updated, recognition, move_config, str(title))
                    if should_prompt:
                        sync_needs_action_task_event(task_store, updated, "等待人工确认分类")
                        recognition, _should_prompt = resolve_category_or_existing_import(
                            telegram,
                            chat_id,
                            store,
                            updated,
                            recognition,
                            str(title),
                            move_config=move_config,
                            emby=emby,
                            openai_classifier=openai_classifier,
                            tmdb_resolver=tmdb_resolver,
                        )
                if move_config and recognition_checked:
                    current_row = store.find_by_id(int(row["id"])) or updated
                    if should_attempt_strm_move(current_row, self_share_enabled=bool(self_share_workflow)):
                        if should_defer_for_probing(current_row, recognition, self_share_enabled=bool(self_share_workflow)):
                            recognition, should_prompt = decide_category_prompt(store, current_row, recognition, move_config, str(title or ""))
                            if not should_prompt:
                                row = current_row
                                continue
                            sync_needs_action_task_event(task_store, current_row, "等待人工确认分类")
                            recognition, _should_prompt = resolve_category_or_existing_import(
                                telegram,
                                chat_id,
                                store,
                                current_row,
                                recognition,
                                str(title or ""),
                                move_config=move_config,
                                emby=emby,
                                openai_classifier=openai_classifier,
                                tmdb_resolver=tmdb_resolver,
                            )
                            current_row = store.find_by_id(int(row["id"])) or current_row
                        if should_wait_for_category(current_row):
                            row = current_row
                            continue
                        source_dir = safe_resolve(Path(str(recognition.get("source_path")))) if recognition.get("source_path") else None
                        if self_share_workflow:
                            current_row, recognition = resolve_self_share_recognition_before_prepare(
                                store,
                                current_row,
                                recognition,
                                str(title or ""),
                                openai_classifier=openai_classifier,
                                tmdb_resolver=tmdb_resolver,
                            )
                            current_row, source_dir, category = prepare_self_share_move_inputs(
                                current_row,
                                recognition,
                                str(title or ""),
                                self_share_workflow,
                                source_dir,
                            )
                            sync_self_share_task_events(task_store, current_row)
                        else:
                            category = final_category_for_move(current_row, recognition)
                        if not source_dir:
                            source_dir = find_strm_source_dir(move_config, recognition, share_name=str(title or ""))
                        active_move_config = move_config_for_workflow_source(
                            move_config,
                            source_dir,
                            self_share_workflow.config if self_share_workflow else None,
                        )
                        move_plan = plan_strm_move(source_dir, category, active_move_config)
                        sync_strm_ready_task_event(task_store, current_row, move_plan)
                        if is_move_plan_retryable(move_plan):
                            row = current_row
                            continue
                        moved_row = merge_self_share_strm_folder(move_plan, store, current_row) if self_share_workflow else execute_strm_move(move_plan, store, current_row)
                        sync_move_task_event(task_store, moved_row)
                        send_move_result(telegram, chat_id, move_plan, moved_row)
                        row = moved_row
                        if moved_row.get("dest_path"):
                            recognition["dest_path"] = moved_row.get("dest_path")
                if emby and emby.enabled:
                    try:
                        match = find_emby_match(emby, recognition, store.find_by_id(int(row["id"])) or updated, recent_limit=30)
                        if match:
                            confirmed_row = store.find_by_id(int(row["id"])) or updated
                            send_emby_confirmed(telegram, chat_id, store, confirmed_row, match, emby, cleanup_client=cleanup_client)
                            latest_row = store.find_by_id(int(row["id"])) or confirmed_row
                            sync_emby_task_event(task_store, latest_row)
                            sync_cleanup_task_event(task_store, latest_row)
                            return
                    except Exception:
                        LOG.debug("Emby confirmation probe failed", exc_info=True)
                if updated and status != last_status and is_terminal_status(status):
                    telegram.send_message(chat_id, f"CMS 任务状态更新：{format_task_label(updated)}：{status}")
                last_status = status
            except Exception:
                LOG.debug("Status poll failed", exc_info=True)
        updated = store.update_status(int(row["id"]), last_status) or row
        if emby and emby.enabled:
            send_emby_timeout(telegram, chat_id, store, updated)
            sync_emby_task_event(task_store, store.find_by_id(int(row["id"])) or updated)
        else:
            disabled_row = store.update_emby(int(row["id"]), "disabled") or row
            sync_emby_task_event(task_store, disabled_row)
            telegram.send_message(chat_id, f"CMS 已提交：{format_task_label(updated)}。Emby 确认未启用，后续请看 CMS/Emby 后台状态。")

    threading.Thread(target=worker, daemon=True).start()


def handle_update(
    update: dict,
    cms: CmsClient,
    telegram: TelegramClient,
    allowed_chat_id: str,
    store: SubmissionStore,
    poll_status: bool = True,
    status_poll_seconds: int = 300,
    status_poll_interval: int = 20,
    emby: EmbyClient | None = None,
    move_config: MoveConfig | None = None,
    openai_classifier: OpenAIClassifier | None = None,
    tmdb_resolver: Any | None = None,
    self_share_workflow: SelfShareWorkflow | None = None,
    cleanup_client: Any | None = None,
    self_share_receive_cid: str = "",
    task_store: TaskStore | None = None,
) -> None:
    if update.get("callback_query"):
        handle_callback_query(update.get("callback_query") or {}, telegram, allowed_chat_id, store, emby=emby)
        return

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if str(chat_id) != str(allowed_chat_id) or str(sender_id) != str(allowed_chat_id):
        LOG.info("Ignoring message from unauthorized chat_id=%s sender_id=%s", chat_id, sender_id)
        return

    text = (message.get("text") or message.get("caption") or "").strip()
    text = MENU_BUTTONS.get(text, text)
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    if command == "/help":
        send_menu_message(telegram, chat_id, HELP_TEXT)
        return
    if command == "/status":
        telegram.send_message(chat_id, format_status(store.recent(limit=8)))
        return
    if command == "/metrics":
        payload = build_metrics_snapshot(store)
        write_metrics_snapshot(store, metrics_path_for_store(store))
        telegram.send_message(chat_id, format_metrics(payload))
        return
    if command == "/history":
        telegram.send_message(chat_id, format_history(store.recent(limit=10)))
        return
    if command == "/quality":
        rows = store.recent(limit=50)
        telegram.send_message(chat_id, format_quality_report(rows), reply_markup=quality_keyboard(rows))
        return
    if command == "/clear_history":
        telegram.send_message(
            chat_id,
            "确认清理已结束历史记录？\n只会删除 bot 本地记录，不会删除 STRM、CMS 任务或 Emby 内容；正在处理中的任务会保留。",
            reply_markup=clear_history_keyboard(),
        )
        return
    if command == "/health":
        telegram_ok = telegram.healthcheck() if hasattr(telegram, "healthcheck") else None
        cms_ok = cms.healthcheck() if hasattr(cms, "healthcheck") else True
        telegram.send_message(
            chat_id,
            format_health(
                move_config or MoveConfig.from_config(Config.from_env()),
                cms_ok=cms_ok,
                emby_ok=bool(emby and emby.enabled),
                telegram_ok=telegram_ok,
                telegram_last_error_at=LAST_TELEGRAM_TRANSIENT_ERROR_AT,
                openai_enabled=bool(openai_classifier and openai_classifier.enabled),
                openai_ok=openai_classifier.healthcheck() if openai_classifier else False,
            ),
        )
        return

    links = extract_share_links(text)
    if not links:
        return

    result_lines = [f"收到 {len(links)} 个链接："]
    for index, link in enumerate(links, 1):
        try:
            key = normalize_share_link(link)
            best_effort_task_sync(
                "received",
                ensure_task_for_link,
                task_store,
                key.share_code,
                key.receive_code,
                link,
            )
            existing = store.find_by_key(key)
            if should_skip_existing_submission(existing, self_share_enabled=bool(self_share_workflow)):
                best_effort_task_sync("existing_submission", sync_task_from_submission, task_store, existing, "链接已存在")
                result_lines.append(f"{index}. 已存在：{format_task_label(existing)}")
                continue
            if self_share_workflow:
                if not cleanup_client or not hasattr(cleanup_client, "receive_share_to_cid"):
                    raise RuntimeError("self_share_sync requires P115 receive client")
                if not str(self_share_receive_cid or "").strip():
                    raise RuntimeError("SELF_SHARE_RECEIVE_CID is required for self_share_sync")
                received = cleanup_client.receive_share_to_cid(key.share_code, key.receive_code, str(self_share_receive_cid).strip())
                row = store.upsert_submission(key, link, "received", title=received.get("title"))
                row = store.update_self_share(row["id"], workflow_mode="self_share_sync", workflow_phase="received_to_pending") or row
                best_effort_task_sync(
                    "self_share_received",
                    record_submission_event,
                    task_store,
                    row,
                    TaskStage.RECEIVED,
                    TaskStatus.RUNNING,
                    "已接收 115 分享到待整理",
                )
                result_lines.append(f"{index}. 已接收：{format_task_label(row)}")
                LOG.info("Received 115 share without CMS plain submit: share_code=%s cid=%s", key.share_code, self_share_receive_cid)
                if poll_status:
                    start_status_poll(
                        cms,
                        telegram,
                        chat_id,
                        store,
                        row,
                        status_poll_seconds,
                        status_poll_interval,
                        emby=emby,
                        move_config=move_config,
                        openai_classifier=openai_classifier,
                        tmdb_resolver=tmdb_resolver,
                        self_share_workflow=self_share_workflow,
                        cleanup_client=cleanup_client,
                        task_store=task_store,
                    )
                continue
            resp = cms.add_share_down(link)
            task_id, title = extract_task_info(resp)
            row = store.upsert_submission(key, link, "submitted", cms_task_id=task_id, title=title)
            best_effort_task_sync(
                "cms_submitted",
                record_submission_event,
                task_store,
                row,
                TaskStage.CMS_SUBMITTED,
                TaskStatus.RUNNING,
                "已提交 CMS",
            )
            result_lines.append(f"{index}. 已提交：{format_task_label(row)}")
            LOG.info("Submitted share link to CMS: share_code=%s task_id=%s", key.share_code, task_id)
            if poll_status:
                start_status_poll(
                    cms,
                    telegram,
                    chat_id,
                    store,
                    row,
                    status_poll_seconds,
                    status_poll_interval,
                    emby=emby,
                    move_config=move_config,
                    openai_classifier=openai_classifier,
                    tmdb_resolver=tmdb_resolver,
                    self_share_workflow=self_share_workflow,
                    cleanup_client=cleanup_client,
                    task_store=task_store,
                )
        except Exception as exc:  # keep bot alive and report the failed link
            LOG.exception("Failed to submit link")
            category = classify_error(exc)
            try:
                key = normalize_share_link(link)
                store.upsert_submission(key, link, "failed", last_error=category)
                best_effort_task_sync(
                    "submit_failed",
                    record_failure,
                    task_store,
                    {"share_code": key.share_code, "receive_code": key.receive_code, "url": link},
                    TaskStage.CMS_SUBMITTED,
                    category,
                    error_type="cms_submit_failed",
                    error_detail=str(exc),
                )
            except Exception:
                LOG.debug("Failed to record failed submission", exc_info=True)
            result_lines.append(f"{index}. 失败：{category}")
    telegram.send_message(chat_id, "\n".join(result_lines))
    try:
        write_metrics_snapshot(store, metrics_path_for_store(store))
    except Exception:
        LOG.debug("Failed to write metrics snapshot", exc_info=True)


def run_forever(config: Config) -> None:
    cms = CmsClient(config)
    telegram = TelegramClient(config.tg_bot_token, timeout=config.http_timeout)
    emby = EmbyClient(config.emby_base_url, config.emby_api_key, config.emby_user_id, timeout=config.http_timeout)
    openai_classifier = OpenAIClassifier(config)
    tmdb_resolver = TmdbWebResolver(timeout=min(config.http_timeout, 20))
    store = SubmissionStore(config.db_path)
    task_store = create_task_store(config)
    maybe_start_web_server(config, task_store)
    self_share_config = SelfShareConfig.from_config(config, cms)
    p115 = P115WebClient(config.p115_cookie_path, timeout=config.http_timeout) if self_share_config.enabled else None
    self_share_workflow = SelfShareWorkflow(self_share_config, cms, p115, store) if p115 else None
    move_config = MoveConfig.from_config(config)
    if self_share_config.enabled:
        roots = [self_share_config.strm_root] + [root for root in move_config.source_roots if safe_resolve(root) != safe_resolve(self_share_config.strm_root)]
        move_config = MoveConfig(
            source_roots=roots,
            library_roots=move_config.library_roots,
            conflict_policy=move_config.conflict_policy,
            stable_seconds=move_config.stable_seconds,
        )
    offset = None
    LOG.info("cms-tg-ingest started db_path=%s", config.db_path)
    try:
        repaired = normalize_emby_parents(store, emby)
        if repaired:
            LOG.info("Normalized historical Emby parent labels: %s", repaired)
        write_metrics_snapshot(store, metrics_path_for_store(store))
    except Exception:
        LOG.debug("Failed to write startup metrics snapshot", exc_info=True)
    if config.status_repair_enabled:
        start_status_repair_loop(
            store,
            emby,
            move_config=move_config,
            cms=cms,
            self_share_config=self_share_config,
            cleanup_client=p115 if self_share_config.cleanup_after_emby else None,
            interval_seconds=max(1, int(config.status_repair_interval_seconds)),
            limit=max(1, int(config.status_repair_limit)),
        )
    while True:
        try:
            updates = telegram.get_updates(offset=offset, timeout=config.poll_timeout)
            for update in updates:
                offset = int(update["update_id"]) + 1
                handle_update(
                    update,
                    cms,
                    telegram,
                    config.tg_allowed_chat_id,
                    store,
                    status_poll_seconds=config.status_poll_seconds,
                    status_poll_interval=config.status_poll_interval,
                    emby=emby,
                    move_config=move_config,
                    openai_classifier=openai_classifier,
                    tmdb_resolver=tmdb_resolver,
                    self_share_workflow=self_share_workflow,
                    cleanup_client=p115 if self_share_config.enabled else None,
                    self_share_receive_cid=config.self_share_receive_cid,
                    task_store=task_store,
                )
        except Exception as exc:
            log_polling_error(telegram, exc)
            time.sleep(5)


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    run_forever(Config.from_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
