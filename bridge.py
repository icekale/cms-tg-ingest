#!/usr/bin/env python3
"""Telegram-to-CMS bridge for 115 share links."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import inspect
from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from app.clients.cms import CmsClient
from app.cms_cloud_index import CmsCloudDataIndex
from app.clients.emby import EmbyClient
from app.clients.http import FormHttp, HttpJson, load_cookie_value
from app.clients.p115 import (
    CMS_PARENT_CID_CATEGORY_MAP,
    P115RiskControlError,
    P115ShareUnavailableError,
    P115WebClient,
    category_for_115_parent_id,
    infer_category_from_115_item,
    infer_category_from_115_path,
    p115_file_id,
    p115_file_name,
    p115_is_folder,
    p115_parent_id,
    p115_residue_file_id,
    p115_residue_parent_id,
    select_organized_115_folder,
    select_recent_tmdb_115_folder,
    select_source_residue_115_files,
)

from app.config import (
    Config,
    MoveConfig,
    MovePlan,
    SelfShareConfig,
    default_library_roots,
    env_float,
    is_relative_to,
    is_under_any_root,
    parse_bool_env,
    parse_library_map,
    parse_parent_cid_category_map,
    safe_resolve,
    split_env_list,
)

from app.media.classify import (
    ASIAN_MOVIE_COUNTRY_MARKERS,
    ASIAN_MOVIE_LANGUAGE_MARKERS,
    CATEGORY_ALIASES,
    CHINESE_COUNTRY_MARKERS,
    CHINESE_LANGUAGE_MARKERS,
    INDIAN_MOVIE_MARKERS,
    TmdbApiResolver,
    TmdbWebResolver,
    apply_tmdb_hint_resolution,
    apply_tmdb_search_resolution,
    candidate_tokens,
    clean_share_title,
    expected_task_tmdb_id,
    extract_primary_chinese_title,
    extract_tmdb_default_language,
    extract_tmdb_id_from_name,
    extract_tmdb_page_title,
    extract_tmdb_search_query,
    extract_year_from_name,
    final_category_for_move,
    has_indian_movie_hint,
    infer_region_category,
    is_recognition_uncertain,
    item_tmdb_id,
    language_matches,
    map_category_label,
    media_type_for_category,
    normalized_tmdb_language,
    parse_recognition_json,
    tmdb_match_score,
    user_movie_category_bucket,
)
from app.media.sources import parse_media_sources

from app.media.strm import (
    category_for_self_share_row,
    category_from_existing_library_folder,
    category_from_existing_library_match,
    cleanup_direct_strm_for_organized_folder,
    cleanup_pending_self_share_sources,
    destination_for_category,
    execute_strm_move,
    find_recent_library_strm_source_dir,
    find_self_share_strm_source_dir,
    find_strm_source_dir,
    has_strm_file,
    is_directory_stable,
    library_category_for_path,
    library_media_root_for_path,
    merge_self_share_strm_folder,
    move_config_for_workflow_source,
    newest_mtime,
    plan_strm_move,
    prepare_self_share_move_inputs,
    remove_direct_strm_files,
    repair_stranded_self_share_moves,
    restore_missing_self_share_library_folder,
    restore_missing_self_share_library_folders,
    select_move_source_for_workflow,
    validate_self_share_strm_destination,
    validate_self_share_strm_source,
)

from app.models import TaskStage, TaskStatus
from app.quality import format_task_quality_report, scan_task_quality
from app.task_bridge import ensure_task_for_link, record_failure, record_submission_event, sync_task_from_submission
from app.task_engine import decide_retry, stage_display_name
from app.task_health import format_taskstore_health
from app.self_share_health import start_invalid_self_share_probe_loop
from app.telegram_ui import (
    clear_history_keyboard,
    format_counts,
    format_failure_summary,
    format_history,
    format_library_summary,
    format_metrics,
    format_quality_report,
    format_status,
    format_taskstore_history,
    format_taskstore_status,
    menu_keyboard,
    quality_issue_for_row,
    quality_issue_rows,
    quality_keyboard,
    task_action_keyboard,
    truncate_text,
)
from app.task_runner import StageResult, TaskRunner
from app.task_store import TaskStore
from app.web import start_web_server
from app.workflows.self_share import (
    BridgeSelfShareTaskWorkflow,
    SelfShareWorkflow,
    apply_openai_category_fallback,
    category_keyboard,
    cleanup_own_share_source,
    cleanup_self_share_source_residue,
    emby_parent_label,
    enrich_recognition_from_self_share_folder,
    find_emby_match,
    format_task_label,
    has_authoritative_category,
    is_115_receive_restricted_error,
    is_move_plan_retryable,
    match_emby_item,
    resolve_category_with_fallbacks,
    resolve_self_share_recognition_before_prepare,
    send_emby_confirmed,
    send_move_result,
    should_attempt_strm_move,
    should_defer_for_probing,
)

LINK_RE = re.compile(r"https?://(?:www\.)?(?:115cdn|115|anxia)\.com/s/[^\s<>'\"]+", re.I)
TRAILING_PUNCT = ".,;)。），]】》>"
LOG = logging.getLogger("cms-tg-ingest")
LAST_TELEGRAM_TRANSIENT_ERROR_AT: str | None = None
ED2K_HELP_EXAMPLE = "ed2k://|file|Example.mkv|10|" + "0123456789ABCDEF" * 2 + "|/"
HELP_TEXT = """直接发送 115 分享链接即可自动提交 CMS。\n\n支持：\n- 一条消息多个 115 分享、磁力或 ED2K 链接\n- 磁力/ED2K 会进入 115 云下载，再复用 CMS 整理和分享 STRM 流程\n- 自动跳过重复链接\n- 识别不确定时用按钮确认分类\n- 自动尝试确认 Emby 是否入库\n- 已完成剧集可在“最近任务”点“追更”，或发送“追更 115链接”\n- /status 查看最近任务\n- /metrics 查看任务统计\n- /clear_history 清理已结束历史\n- /help 查看帮助\n\n示例：\nhttps://115cdn.com/s/xxxx?password=abcd\n""" + ED2K_HELP_EXAMPLE
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
TERMINAL_EMBY_STATUSES = {"confirmed", "timeout", "disabled", "failed", "error", "invalid_share_cleaned"}
TERMINAL_MOVE_STATUSES = {"moved", "skipped", "failed", "error", "conflict", "invalid_share_cleaned"}
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


def create_task_store(config: Config) -> TaskStore:
    return TaskStore(config.task_db_path)


def maybe_start_web_server(config: Config, task_store: TaskStore, submission_store: Any | None = None, starter=start_web_server):
    if not config.web_enabled:
        return None
    kwargs = {
        "web_token": config.web_token,
        "task_engine_enabled": config.task_engine_enabled,
    }
    if submission_store is not None:
        kwargs["submission_store"] = submission_store
    server = starter(task_store, config.web_host, config.web_port, **kwargs)
    LOG.info("v0.2 web admin started host=%s port=%s", config.web_host, config.web_port)
    return server


def call_maybe_start_web_server(config: Config, task_store: TaskStore, submission_store: Any | None = None):
    try:
        parameters = inspect.signature(maybe_start_web_server).parameters
    except (TypeError, ValueError):
        parameters = {}
    supports_submission_store = "submission_store" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if supports_submission_store:
        return maybe_start_web_server(config, task_store, submission_store=submission_store)
    return maybe_start_web_server(config, task_store)


def stop_web_server(server: Any | None, join_timeout: float = 5) -> None:
    if server is None:
        return
    shutdown = getattr(server, "shutdown", None)
    if callable(shutdown):
        shutdown()
    close = getattr(server, "server_close", None)
    if callable(close):
        close()
    thread = getattr(server, "_cms_thread", None)
    if isinstance(thread, threading.Thread) and thread is not threading.current_thread():
        thread.join(max(0.0, float(join_timeout)))


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
                ORDER BY COALESCE(share_probe_at, 0) ASC, id ASC
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

def should_wait_for_category(row: dict[str, Any]) -> bool:
    return str(row.get("category_status") or "") == "uncertain" and not row.get("category_choice")
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
    task_health: str | None = None,
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
    if task_health:
        lines.extend(["", task_health])
    return "\n".join(lines)


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
                "remote end closed",
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
    if "taskstore" in low:
        return "TaskStore 未启用或不可用"
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


def remember_manual_parent_category(store: SubmissionStore, row: dict[str, Any], category: str) -> None:
    if not hasattr(store, "remember_parent_category"):
        return
    try:
        recognition = json.loads(row.get("recognition_json") or "{}")
    except Exception:
        recognition = {}
    if not isinstance(recognition, dict):
        recognition = {}
    parent_id = str(recognition.get("organized_parent_id") or recognition.get("parent_id") or "").strip()
    if parent_id:
        store.remember_parent_category(parent_id, category, source="manual")


def handle_callback_query(
    callback_query: dict,
    telegram: TelegramClient,
    allowed_chat_id: str,
    store: SubmissionStore,
    emby: EmbyClient | None = None,
    task_store: TaskStore | None = None,
) -> None:
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
    task_action = parse_task_action_callback(data)
    if task_action:
        action, task_id = task_action
        if handle_task_action_callback(action, task_id, callback_id, chat_id, telegram, task_store, store):
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
    if updated and category_key != "skip":
        remember_manual_parent_category(store, updated, label)
    if updated and task_store and category_key == "skip":
        task = task_store.upsert_task(
            str(updated.get("share_code") or ""),
            str(updated.get("receive_code") or ""),
            str(updated.get("url") or ""),
        )
        task_store.record_event(
            task.id,
            TaskStage.FAILED,
            TaskStatus.FAILED,
            "已跳过分类，任务停止",
            error_type="category_skipped",
            error_summary="已跳过分类，任务停止",
            submission_id=int(updated["id"]),
            metadata_patch={"submission_id": int(updated["id"])},
            clear_claim=True,
        )
    if updated and task_store and category_key != "skip":
        task = task_store.upsert_task(
            str(updated.get("share_code") or ""),
            str(updated.get("receive_code") or ""),
            str(updated.get("url") or ""),
        )
        task_store.record_event(
            task.id,
            TaskStage.RECOGNIZING,
            TaskStatus.PENDING,
            "已选择分类，重新识别",
            category=label,
            submission_id=int(updated["id"]),
            metadata_patch={"submission_id": int(updated["id"])},
            next_run_at=time.time(),
            clear_claim=True,
        )
    telegram.answer_callback_query(callback_id, f"已记录分类：{label}", show_alert=False)
    if updated:
        telegram.send_message(chat_id, f"已记录分类：{label}\n{format_task_label(updated)}")


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


def start_self_share_maintenance_loop(
    store: Any,
    cms: Any,
    self_share_config: SelfShareConfig,
    move_config: MoveConfig,
    interval_seconds: int = 15,
    limit: int = 50,
) -> threading.Thread | None:
    if interval_seconds <= 0:
        return None

    def loop() -> None:
        while True:
            try:
                restored = restore_missing_self_share_library_folders(
                    store,
                    cms,
                    self_share_config,
                    move_config,
                    limit=limit,
                )
                if restored:
                    LOG.info("Self-share maintenance restored %s missing STRM folders", restored)
                    write_metrics_snapshot(store, metrics_path_for_store(store))
            except Exception:
                LOG.debug("Self-share maintenance loop failed", exc_info=True)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="self-share-maintenance", daemon=True)
    thread.start()
    return thread


def send_emby_timeout(telegram: TelegramClient, chat_id: int | str, store: SubmissionStore, row: dict[str, Any]) -> None:
    updated = store.update_emby(int(row["id"]), "timeout") or row
    telegram.send_message(
        chat_id,
        f"CMS 已提交，但暂未在 Emby 确认入库：{format_task_label(updated)}\n可以稍后用 /status 查看，或到 Emby/CMS 后台确认。",
    )

RUNNABLE_TASK_STAGES = {
    TaskStage.RECEIVED,
    TaskStage.ORGANIZING,
    TaskStage.RECOGNIZING,
    TaskStage.OWN_SHARE_CREATED,
    TaskStage.SHARE_SYNC_SUBMITTED,
    TaskStage.STRM_READY,
    TaskStage.MOVED,
    TaskStage.EMBY_CONFIRMED,
    TaskStage.CLEANED,
}
RETRY_STAGE_METADATA_KEYS = ("retry_stage", "last_actionable_stage", "failed_stage", "retry_from_stage")


def retry_stage_for_intake(task) -> TaskStage:
    if task.current_stage in RUNNABLE_TASK_STAGES:
        return task.current_stage
    for key in RETRY_STAGE_METADATA_KEYS:
        raw = task.metadata.get(key)
        if not raw:
            continue
        try:
            stage = TaskStage(str(raw))
        except ValueError:
            continue
        if stage in RUNNABLE_TASK_STAGES:
            return stage
    return TaskStage.RECEIVED


def completion_drift_retry_stage(task, submission: dict[str, Any] | None = None) -> TaskStage | None:
    if task.status != TaskStatus.SUCCEEDED or task.current_stage != TaskStage.CLEANED:
        return None
    dest_path = str(task.metadata.get("dest_path") or "").strip()
    if not dest_path:
        return None
    dest = safe_resolve(Path(dest_path))
    if submission and str(submission.get("workflow_mode") or "") == "self_share_sync":
        required_relative_path = ""
        if task.metadata.get("direct_file_share"):
            required_relative_path = str(task.metadata.get("direct_file_share_relative_path") or "").strip()
        if not validate_self_share_strm_destination(dest, submission, required_relative_path):
            return None
        return TaskStage.EMBY_CONFIRMED
    if dest.exists() and (dest.is_file() or has_strm_file(dest)):
        return None
    return TaskStage.EMBY_CONFIRMED


def format_task_snapshot(task) -> str:
    title = task.title or task.metadata.get("received_title") or task.share_code
    return f"#{task.id} {title}｜{stage_display_name(task.current_stage)}｜{task.status.value}"


def format_task_intake_reply(task) -> str:
    if task.metadata.get("retry_from_stage") == TaskStage.CLEANED.value and task.metadata.get("retry_stage"):
        return f"任务已重新检查入库状态：{format_task_snapshot(task)}"
    if task.status == TaskStatus.SUCCEEDED and task.current_stage == TaskStage.CLEANED:
        dest = task.metadata.get("dest_path") or ""
        parent = task.metadata.get("emby_parent") or ""
        suffix = f"\n媒体库：{parent}\n路径：{dest}" if parent or dest else ""
        return f"任务已完成：{format_task_snapshot(task)}{suffix}"
    if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION}:
        return f"任务需要处理：{format_task_snapshot(task)}\n原因：{task.error_summary or '无详细错误'}"
    if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
        return f"任务处理中/已在队列中：{format_task_snapshot(task)}"
    return f"任务已接收：{format_task_snapshot(task)}"


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
def parse_task_action_callback(data: str) -> tuple[str, int] | None:
    parts = str(data or "").split(":")
    if len(parts) != 2 or parts[0] not in {"task_detail", "task_retry", "task_emby", "task_restore", "task_reprocess", "task_update"}:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


def start_series_update_task(
    task: Any | None,
    store: SubmissionStore | None,
    task_store: TaskStore | None,
    *,
    source: str,
) -> tuple[Any | None, str]:
    if task is None or store is None or task_store is None:
        return None, "not_eligible"
    if task.status != TaskStatus.SUCCEEDED or task.current_stage != TaskStage.CLEANED:
        return None, "not_eligible"
    category = str(task.category or task.metadata.get("category") or task.metadata.get("category_final") or "").strip()
    if category not in {"国产电视", "外国电视", "番剧"}:
        return None, "not_eligible"
    submission_id = task.submission_id or task.metadata.get("submission_id")
    if submission_id in (None, ""):
        return None, "not_eligible"
    row = store.find_by_id(int(submission_id))
    if not row:
        failed = task_store.record_event(
            task.id,
            TaskStage.FAILED,
            TaskStatus.FAILED,
            "追更准备失败：原任务记录不存在",
            error_type="submission_missing",
            error_summary="原任务记录不存在",
            clear_claim=True,
        )
        return failed, "failed"
    if str(row.get("workflow_mode") or "") != "self_share_sync":
        return None, "not_eligible"
    try:
        previous_requested = int(task.metadata.get("update_requested_run") or 0)
        previous_received = int(task.metadata.get("update_received_run") or 0)
    except (TypeError, ValueError):
        previous_requested = previous_received = 0
    update_run = max(previous_requested, previous_received) + 1
    update_started_at = time.time()
    previous_share_code = str(row.get("own_share_code") or "").strip()
    task_store.record_event(
        task.id,
        TaskStage.RECEIVED,
        TaskStatus.PENDING,
        f"{source}开始追更，准备接收当前分享内容",
        title=str(row.get("title") or task.title or "") or None,
        tmdb_id=str(task.tmdb_id or "") or None,
        category=category,
        submission_id=int(row["id"]),
        metadata_patch={
            "submission_id": int(row["id"]),
            "update_requested_run": update_run,
            "update_received_run": update_run - 1,
            "update_started_at": update_started_at,
            "previous_own_share_code": previous_share_code,
        },
        metadata_delete_keys=(
            "own_share_file_id",
            "own_share_file_name",
            "own_share_code",
            "own_share_receive_code",
            "own_share_url",
            "share_sync_status",
            "canonical_manifest_json",
            "share_alias_name",
            "share_alias_level",
            "share_validation_status",
            "share_validation_error",
            "source_path",
            "dest_path",
            "category_final",
            "move_status",
            "move_error",
            "emby_status",
            "emby_item_id",
            "emby_title",
            "emby_path",
            "emby_parent",
            "cleanup_status",
            "cleanup_file_id",
            "cleanup_error",
            "emby_refresh_requested",
            "emby_refresh_library",
            "emby_refresh_error",
            "organized_folder",
            "recognition",
            "received_file_ids",
            "direct_strm_removed",
            "_defer_stage",
            "_defer_message",
            "_defer_count",
        ),
        next_run_at=-1,
        clear_claim=True,
    )
    updated_row = store.reset_self_share_for_update(int(row["id"]))
    if not updated_row:
        failed = task_store.record_event(
            task.id,
            TaskStage.FAILED,
            TaskStatus.FAILED,
            "追更准备失败：原任务记录不存在",
            error_type="submission_missing",
            error_summary="原任务记录不存在",
            clear_claim=True,
        )
        return failed, "failed"
    updated = task_store.enqueue_task(task.id, TaskStage.RECEIVED, message="追更已入队", next_run_at=0)
    return updated, "started"


def handle_task_action_callback(
    action: str,
    task_id: int,
    callback_id: str,
    chat_id: int | str,
    telegram: Any,
    task_store: TaskStore | None,
    store: SubmissionStore | None = None,
) -> bool:
    if task_store is None:
        telegram.answer_callback_query(callback_id, "任务引擎未启用", show_alert=True)
        return True
    task = task_store.find_task(task_id)
    if not task:
        telegram.answer_callback_query(callback_id, "任务不存在或已过期", show_alert=True)
        return True
    if action == "task_detail":
        events = task_store.list_events(task_id)[-5:]
        event_lines = [f"- {stage_display_name(TaskStage(event['stage'])) if event.get('stage') in TaskStage._value2member_map_ else event.get('stage')} / {event.get('status')}: {event.get('message')}" for event in events]
        text = format_task_intake_reply(task)
        if event_lines:
            text += "\n最近事件：\n" + "\n".join(event_lines)
        telegram.answer_callback_query(callback_id, "已发送任务详情", show_alert=False)
        telegram.send_message(chat_id, text, reply_markup=task_action_keyboard([task]))
        return True
    if action == "task_retry":
        decision = decide_retry(task)
        target_stage = decision.stage or retry_stage_for_intake(task)
        task_store.record_event(
            task_id,
            target_stage,
            TaskStatus.PENDING,
            "TG 按钮触发重试",
            increment_retry=True,
            metadata_patch={"retry_from_stage": task.current_stage.value, "retry_stage": target_stage.value},
            clear_claim=True,
        )
        updated = task_store.enqueue_task(task_id, target_stage, message="TG 按钮重试已入队", next_run_at=0)
        telegram.answer_callback_query(callback_id, "已重新入队", show_alert=False)
        telegram.send_message(chat_id, f"已重新入队：{format_task_snapshot(updated)}")
        return True
    if action == "task_emby":
        updated = task_store.enqueue_task(task_id, TaskStage.EMBY_CONFIRMED, message="TG 按钮触发 Emby 检查", next_run_at=0)
        telegram.answer_callback_query(callback_id, "已加入 Emby 检查队列", show_alert=False)
        telegram.send_message(chat_id, f"已加入 Emby 检查队列：{format_task_snapshot(updated)}")
        return True
    if action == "task_restore":
        task_store.record_event(
            task_id,
            TaskStage.EMBY_CONFIRMED,
            TaskStatus.PENDING,
            "TG 按钮触发 STRM 恢复",
            metadata_patch={"retry_from_stage": task.current_stage.value, "retry_stage": TaskStage.EMBY_CONFIRMED.value},
            clear_claim=True,
        )
        updated = task_store.enqueue_task(task_id, TaskStage.EMBY_CONFIRMED, message="TG 按钮 STRM 恢复已入队", next_run_at=0)
        telegram.answer_callback_query(callback_id, "已加入 STRM 恢复队列", show_alert=False)
        telegram.send_message(chat_id, f"已加入 STRM 恢复队列：{format_task_snapshot(updated)}")
        return True
    if action == "task_reprocess":
        updated = task_store.reprocess_task(task_id, message="TG 按钮触发从头重跑", next_run_at=0)
        telegram.answer_callback_query(callback_id, "已从头重跑", show_alert=False)
        telegram.send_message(chat_id, f"已从头重跑：{format_task_snapshot(updated)}")
        return True
    if action == "task_update":
        if task.status != TaskStatus.SUCCEEDED or task.current_stage != TaskStage.CLEANED:
            telegram.answer_callback_query(callback_id, "当前任务尚未完成，暂不能追更", show_alert=True)
            return True
        category = str(task.category or task.metadata.get("category") or task.metadata.get("category_final") or "").strip()
        if category not in {"国产电视", "外国电视", "番剧"}:
            telegram.answer_callback_query(callback_id, "仅已完成的剧集任务支持追更", show_alert=True)
            return True
        updated, result = start_series_update_task(task, store, task_store, source="TG 按钮")
        if result == "not_eligible":
            telegram.answer_callback_query(callback_id, "原自分享任务记录不可用", show_alert=True)
            return True
        if result == "failed":
            telegram.answer_callback_query(callback_id, "追更准备失败", show_alert=True)
            return True
        telegram.answer_callback_query(callback_id, "已开始追更", show_alert=False)
        telegram.send_message(chat_id, f"已开始追更：{format_task_snapshot(updated)}\n将重新接收当前分享内容并合并新增剧集。")
        return True
    return False


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


def _start_status_poll_impl(
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


# Import after the implementation exists so app.legacy_polling can avoid
# importing bridge at module load time.
from app.legacy_polling import start_status_poll

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
    task_engine_enabled: bool = False,
) -> None:
    if update.get("callback_query"):
        handle_callback_query(update.get("callback_query") or {}, telegram, allowed_chat_id, store, emby=emby, task_store=task_store)
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
        if task_engine_enabled and task_store is not None:
            tasks = task_store.list_recent_tasks(limit=8)
            taskstore_status = format_taskstore_status(tasks)
            if taskstore_status:
                telegram.send_message(chat_id, taskstore_status, reply_markup=task_action_keyboard(tasks))
                return
        telegram.send_message(chat_id, format_status(store.recent(limit=8)))
        return
    if command == "/metrics":
        payload = build_metrics_snapshot(store)
        write_metrics_snapshot(store, metrics_path_for_store(store))
        telegram.send_message(chat_id, format_metrics(payload))
        return
    if command == "/history":
        if task_engine_enabled and task_store is not None:
            taskstore_history = format_taskstore_history(task_store.list_recent_tasks(limit=10))
            if taskstore_history:
                telegram.send_message(chat_id, taskstore_history)
                return
        telegram.send_message(chat_id, format_history(store.recent(limit=10)))
        return
    if command == "/quality":
        if task_engine_enabled and task_store is not None:
            issues = scan_task_quality(task_store)
            if issues:
                telegram.send_message(chat_id, format_task_quality_report(issues))
                return
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
                task_health=format_taskstore_health(
                    task_store,
                    enabled=bool(task_engine_enabled),
                ) if task_engine_enabled and task_store is not None else None,
            ),
        )
        return

    explicit_series_update = text.startswith("追更")
    if explicit_series_update:
        text = text.removeprefix("追更").lstrip(" ：:")
    sources = parse_media_sources(text)
    if not sources:
        return

    result_lines = [f"收到 {len(sources)} 个链接："]
    for index, source in enumerate(sources, 1):
        link = source.raw_url
        try:
            if source.source_type in {"magnet", "ed2k"}:
                if not (self_share_workflow and task_engine_enabled and task_store is not None):
                    result_lines.append(f"{index}. 云下载链接需要启用 TaskStore 自分享工作流")
                    continue
                task = task_store.upsert_cloud_task(
                    source.source_key,
                    source.raw_url,
                    chat_id=str(chat_id or ""),
                    title=source.display_name,
                )
                if not task_store.list_events(task.id):
                    task = task_store.enqueue_task(task.id, TaskStage.CLOUD_DOWNLOADING, message="等待 115 云下载", next_run_at=0)
                result_lines.append(f"{index}. {format_task_intake_reply(task)}")
                LOG.info("Enqueued cloud source in TaskStore: type=%s key=%s task_id=%s", source.source_type, source.source_key, task.id)
                continue
            key = normalize_share_link(link)
            if self_share_workflow and task_engine_enabled and task_store is None:
                raise RuntimeError("TaskStore is required when TASK_ENGINE_ENABLED=true for self_share_sync")
            if self_share_workflow and task_engine_enabled and task_store is not None:
                if explicit_series_update:
                    existing_task = task_store.find_task_by_share_key(key.share_code, key.receive_code)
                    updated_task, update_result = start_series_update_task(
                        existing_task,
                        store,
                        task_store,
                        source="文本追更",
                    )
                    if update_result == "started":
                        result_lines.append(f"{index}. 已开始追更：{format_task_snapshot(updated_task)}")
                        continue
                    if update_result == "failed":
                        result_lines.append(f"{index}. 追更失败：{format_task_snapshot(updated_task)}")
                        continue
                task = task_store.upsert_task(key.share_code, key.receive_code, link, chat_id=str(chat_id or ""))
                submission = None
                submission_id = task.submission_id or task.metadata.get("submission_id")
                if submission_id not in (None, ""):
                    try:
                        submission = store.find_by_id(int(submission_id))
                    except (TypeError, ValueError):
                        submission = None
                drift_stage = completion_drift_retry_stage(task, submission)
                if drift_stage:
                    task = task_store.record_event(
                        task.id,
                        drift_stage,
                        TaskStatus.RUNNING,
                        "已完成任务状态漂移，准备重新检查",
                        metadata_patch={"retry_from_stage": task.current_stage.value, "retry_stage": drift_stage.value},
                    )
                    task = task_store.enqueue_task(task.id, drift_stage, message="重新检查入库状态")
                elif task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {TaskStage.FAILED, TaskStage.NEEDS_ACTION}:
                    retry_stage = retry_stage_for_intake(task)
                    task = task_store.record_event(
                        task.id,
                        retry_stage,
                        task.status,
                        "准备重新入队",
                        metadata_patch={"retry_from_stage": task.current_stage.value, "retry_stage": retry_stage.value},
                    )
                    task = task_store.enqueue_task(task.id, retry_stage, message="重新入队")
                elif task.current_stage == TaskStage.RECEIVED and task.status == TaskStatus.PENDING and not task_store.list_events(task.id):
                    task = task_store.enqueue_task(task.id, TaskStage.RECEIVED, message="等待执行")
                result_lines.append(f"{index}. {format_task_intake_reply(task)}")
                LOG.info("Enqueued self-share link in TaskStore: share_code=%s task_id=%s stage=%s status=%s", key.share_code, task.id, task.current_stage.value, task.status.value)
                continue
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


def run_forever(config: Config, stop_event: threading.Event | None = None) -> None:
    stop_event = stop_event or threading.Event()
    cms = CmsClient(config)
    telegram = TelegramClient(config.tg_bot_token, timeout=config.http_timeout)
    emby = EmbyClient(config.emby_base_url, config.emby_api_key, config.emby_user_id, timeout=config.http_timeout)
    openai_classifier = OpenAIClassifier(config)
    tmdb_web_resolver = TmdbWebResolver(timeout=min(config.http_timeout, 20))
    tmdb_resolver = (
        TmdbApiResolver(config.tmdb_api_key, config.tmdb_bearer_token, timeout=min(config.http_timeout, 20), fallback=tmdb_web_resolver)
        if config.tmdb_api_key or config.tmdb_bearer_token
        else tmdb_web_resolver
    )
    store = SubmissionStore(config.db_path)
    task_store = create_task_store(config)
    web_server = call_maybe_start_web_server(config, task_store, submission_store=store)
    self_share_config = SelfShareConfig.from_config(config, cms)
    p115 = (
        P115WebClient(
            config.p115_cookie_path,
            timeout=config.http_timeout,
            min_interval_seconds=config.p115_min_request_interval_seconds,
        )
        if self_share_config.enabled
        else None
    )
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
    task_runner = None
    if config.task_engine_enabled and self_share_config.enabled and p115:
        task_workflow = BridgeSelfShareTaskWorkflow(
            cms=cms,
            telegram=telegram,
            chat_id=config.tg_allowed_chat_id,
            store=store,
            task_store=task_store,
            p115=p115,
            self_share_config=self_share_config,
            move_config=move_config,
            emby=emby,
            openai_classifier=openai_classifier,
            tmdb_resolver=tmdb_resolver,
            cleanup_client=p115 if self_share_config.cleanup_after_emby else None,
            receive_cid=config.self_share_receive_cid,
            cms_cloud_index=CmsCloudDataIndex(self_share_config.cms_state_db_path),
        )
        task_runner = TaskRunner(
            task_store,
            task_workflow,
            interval_seconds=config.task_worker_interval_seconds,
            risk_cooldown_seconds=config.p115_risk_cooldown_seconds,
            p115_client=p115,
        )
        task_runner.start()
        LOG.info("Task engine worker started interval_seconds=%s", config.task_worker_interval_seconds)
        if config.self_share_invalid_cleanup_enabled:
            start_invalid_self_share_probe_loop(
                store,
                task_store,
                p115,
                emby,
                telegram,
                config.tg_allowed_chat_id,
                move_config,
                interval_seconds=config.self_share_invalid_check_interval_seconds,
                limit=config.self_share_invalid_check_limit,
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
    if config.status_repair_enabled and not (config.task_engine_enabled and self_share_config.enabled):
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
    try:
        while not stop_event.is_set():
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
                        task_engine_enabled=config.task_engine_enabled,
                    )
            except Exception as exc:
                if stop_event.is_set():
                    break
                log_polling_error(telegram, exc)
                stop_event.wait(5)
    finally:
        if task_runner is not None:
            stop = getattr(task_runner, "stop", None)
            if callable(stop):
                try:
                    stop(join_timeout=5)
                except TypeError:
                    stop()
        stop_web_server(web_server)


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    stop_event = threading.Event()

    def request_stop(signum, _frame):
        LOG.info("Received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    run_forever(Config.from_env(), stop_event=stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
