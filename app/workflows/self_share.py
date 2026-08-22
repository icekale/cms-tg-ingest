from __future__ import annotations

import json
import logging
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.cms import CmsClient, CmsSharePlaybackUnavailableError
from app.cms_cloud_index import CmsCloudDataIndex
from app.clients.p115 import (
    P115CloudOutputPendingError,
    P115RiskControlError,
    P115SharePendingError,
    P115ShareUnavailableError,
    P115WebClient,
    category_for_115_parent_id,
    is_p115_risk_control_message,
    normalize_cloud_status,
    p115_file_name,
    p115_is_folder,
    p115_item_id,
    p115_item_parent_id,
)
from app.config import DEFAULT_OWN_SHARE_RECEIVE_CODE, MovePlan, SelfShareConfig, default_library_roots, is_relative_to, safe_resolve
from app.media.classify import (
    apply_tmdb_hint_resolution,
    apply_tmdb_search_resolution,
    explicit_task_tmdb_id,
    expected_task_tmdb_id,
    extract_tmdb_id_from_name,
    final_category_for_move,
    is_recognition_uncertain,
    item_tmdb_id,
    map_category_label,
    media_type_for_category,
    normalize_text,
    normalize_tmdb_hint_name,
    user_movie_category_bucket,
)
from app.media.intake_identity import CONFLICT, INCOMPLETE, dest_id_from_file_hits, snapshot_files
from app.media.strm import (
    category_from_existing_library_folder,
    category_from_existing_library_match,
    find_recent_direct_library_strm_source_dir,
    dest_missing_source_strms,
    find_self_share_strm_source_dir,
    has_strm_file,
    merge_self_share_strm_folder,
    move_config_for_workflow_source,
    plan_strm_move,
    restore_canonical_strm_paths,
    restore_missing_self_share_library_folder,
    restore_missing_self_share_library_folders,
    _single_relative_directory_name,
    validate_self_share_strm_destination,
    validate_self_share_strm_source,
)
from app.models import TaskStage, TaskStatus
from app.self_share_settings import resolve_own_share_receive_code, resolve_self_share_review_policy
from app.task_bridge import reset_self_share_submission_for_reprocess
from app.task_store import operation_scope
from app.strm_mode import effective_task_strm_mode
from app.task_runner import StageOutcome, StageResult

LOG = logging.getLogger("cms-tg-ingest")
OPENAI_CATEGORY_LABELS = ["华语电影", "欧美电影", "亚洲电影", "动漫电影", "国产电视", "外国电视", "番剧", "纪录片"]
_RECEIVE_RECOVERY_RETRY_SECONDS = 30
_RECEIVE_RECOVERY_WINDOW_SECONDS = 300
_DELETE_RECOVERY_RETRY_SECONDS = 30
_DELETE_RECOVERY_WINDOW_SECONDS = 300

_post_organize_guard_lock = threading.Lock()
_post_organize_guard_last_scheduled_at: float = 0.0


def schedule_post_organize_restore_guard(
    store: Any,
    cms: Any,
    self_share_config: SelfShareConfig,
    move_config: Any,
    emby: Any | None = None,
    delay_seconds: int = 30,
    limit: int = 50,
) -> threading.Thread | None:
    """Schedule a one-shot delayed self-share restore after CMS auto-organize.

    CMS consumes 115 life events inside its async auto_tidy job; a stale
    delete_file event can remove a self-share STRM directory. Running the
    existing restore path shortly after the trigger shrinks the deletion
    window from the maintenance-loop cadence to seconds.
    """
    global _post_organize_guard_last_scheduled_at
    delay = max(0, int(delay_seconds))
    with _post_organize_guard_lock:
        now = time.time()
        if delay and now - _post_organize_guard_last_scheduled_at < delay:
            return None
        _post_organize_guard_last_scheduled_at = now

    def run() -> None:
        try:
            if delay:
                time.sleep(delay)
            restored = restore_missing_self_share_library_folders(
                store,
                cms,
                self_share_config,
                move_config,
                emby=emby,
                limit=limit,
            )
            if restored:
                LOG.info("Post-auto-organize guard restored %s missing self-share folders", restored)
        except Exception:
            LOG.warning("Post-auto-organize guard failed", exc_info=True)

    thread = threading.Thread(target=run, name="post-auto-organize-restore", daemon=True)
    thread.start()
    return thread


@dataclass(frozen=True)
class _ShareKey:
    share_code: str
    receive_code: str


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_finite_timestamp(value: Any) -> float:
    timestamp = as_float(value, 0.0)
    return timestamp if math.isfinite(timestamp) else 0.0


def source_delete_parent_id(task: Any, row: dict[str, Any], file_id: str) -> str:
    folder = task.metadata.get("organized_folder")
    if isinstance(folder, dict):
        if str(folder.get("file_id") or "").strip() == file_id:
            parent_id = str(folder.get("parent_id") or "").strip()
            if parent_id:
                return parent_id
        if str(folder.get("direct_file_id") or "").strip() == file_id:
            return str(folder.get("direct_parent_id") or "").strip()
    if str(task.metadata.get("direct_file_share_file_id") or "").strip() == file_id:
        parent_id = str(task.metadata.get("direct_file_share_parent_id") or "").strip()
        if parent_id:
            return parent_id
    try:
        recognition = json.loads(row.get("recognition_json") or "{}")
    except (TypeError, ValueError):
        recognition = {}
    recognition = recognition if isinstance(recognition, dict) else {}
    return str(recognition.get("organized_parent_id") or recognition.get("parent_id") or "").strip()


def _is_missing_delete_target_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return any(
        token in message
        for token in (
            "file not found",
            "文件不存在",
            "文件已不存在",
            "不存在或已删除",
            "目标不存在",
        )
    )


def journaled_delete_file(
    task_store: Any,
    task: Any,
    cleanup_client: Any,
    file_id: str,
    parent_id: str,
    operation_type: str,
    *,
    now: float,
) -> StageResult | None:
    operation_key = f"{operation_scope(task)}:{operation_type}:{file_id}"
    operation = task_store.find_operation(int(task.id), operation_key)
    if operation is None:
        operation = task_store.prepare_operation(
            int(task.id),
            operation_key,
            operation_type,
            {"file_id": file_id, "parent_id": parent_id},
        )
    if operation.status == "prepared":
        started = task_store.start_operation(int(task.id), operation_key)
        operation = started or task_store.find_operation(int(task.id), operation_key)
        if started is not None:
            try:
                response = cleanup_client.delete_file(str(operation.request.get("file_id") or file_id))
            except Exception as exc:
                if not _is_missing_delete_target_error(exc):
                    raise
                response = {"state": True, "already_absent": True}
            completed = task_store.complete_operation(
                int(task.id),
                operation_key,
                response if isinstance(response, dict) else {"state": True},
            )
            operation = completed or task_store.find_operation(int(task.id), operation_key)
    if operation is None:
        raise RuntimeError("115 delete operation disappeared")
    metadata = {
        "delete_operation_key": operation_key,
        "delete_operation_type": operation_type,
        "delete_file_id": file_id,
        "delete_parent_id": parent_id,
    }
    if operation.status == "succeeded":
        return None
    if operation.status not in {"started", "uncertain"}:
        return StageResult.needs_action("115 删除操作状态无法安全恢复，请人工检查", metadata)
    try:
        exists = cleanup_client.file_exists_in_parent(
            str(operation.request.get("file_id") or file_id),
            str(operation.request.get("parent_id") or parent_id),
        )
    except Exception as exc:
        return StageResult.defer(
            f"115 删除结果暂时无法确认：{exc}",
            _DELETE_RECOVERY_RETRY_SECONDS,
            metadata,
        )
    if not exists:
        if operation.status == "started":
            task_store.complete_operation(
                int(task.id),
                operation_key,
                {"state": True, "reconciled_absent": True},
            )
        return None
    recovery_age = max(0.0, float(now) - float(operation.started_at or operation.created_at))
    if recovery_age >= _DELETE_RECOVERY_WINDOW_SECONDS:
        return StageResult.needs_action("115 删除结果仍不明确，文件仍存在且禁止自动重复删除", metadata)
    return StageResult.defer(
        "等待确认 115 删除结果，禁止自动重复删除",
        _DELETE_RECOVERY_RETRY_SECONDS,
        metadata,
    )



def is_115_receive_restricted_error(exc: Exception) -> bool:
    if isinstance(exc, P115RiskControlError):
        return True
    text = str(exc or "")
    return is_p115_risk_control_message(text)


def is_complete_share_receive_result(received: dict[str, Any] | None) -> bool:
    if not isinstance(received, dict) or not received.get("received_items_complete"):
        return False
    items = received.get("received_items")
    if not isinstance(items, list):
        return False
    try:
        expected_count = int(received.get("received_expected_item_count") or 0)
    except (TypeError, ValueError):
        return False
    return expected_count > 0 and len(items) == expected_count


def has_authoritative_category(row: dict[str, Any], recognition: dict[str, Any]) -> bool:
    if str(row.get("category_status") or "").strip() == "selected" and str(row.get("category_choice") or "").strip():
        return True
    status = str(recognition.get("category_status") or "").strip()
    category = str(recognition.get("category") or "").strip()
    if not category:
        return False
    if status in {"tmdb_resolved", "tmdb_search_resolved"}:
        return bool(str(recognition.get("tmdb_id") or "").strip())
    if status != "self_share_resolved":
        return False
    return bool(
        str(recognition.get("organized_parent_id") or "").strip() or str(recognition.get("parent_id") or "").strip()
    )


def is_unverified_received_source(folder: dict[str, Any], task_metadata: dict[str, Any], receive_cid: str) -> bool:
    file_id = str(folder.get("file_id") or "").strip()
    parent_id = str(folder.get("parent_id") or folder.get("pid") or "").strip()
    receive_cid = str(receive_cid or "").strip()
    if receive_cid and parent_id == receive_cid:
        return True
    identity = task_metadata.get("intake_identity")
    root_ids = identity.get("root_ids") if isinstance(identity, dict) else []
    return bool(
        receive_cid
        and file_id
        and file_id in {str(value) for value in (root_ids or []) if str(value)}
        and parent_id == receive_cid
    )


def has_tmdb_folder_mismatch(folder: dict[str, Any], recognition: dict[str, Any], row: dict[str, Any], share_name: str) -> bool:
    explicit = explicit_task_tmdb_id(recognition, row, share_name)
    actual = extract_tmdb_id_from_name(str(folder.get("file_name") or ""))
    if explicit:
        # An explicit source marker is an identity assertion. A missing
        # marker is unsafe too: title-only matching could select an old folder.
        return actual != explicit
    expected = expected_task_tmdb_id(recognition, row)
    if not actual:
        actual = extract_tmdb_id_from_name(share_name)
    return bool(expected and actual and expected != actual)


def task_tmdb_identity(task: Any) -> str:
    metadata = getattr(task, "metadata", {}) or {}
    recognition = metadata.get("recognition")
    if not isinstance(recognition, dict):
        recognition = {}
    return str(
        getattr(task, "tmdb_id", "")
        or metadata.get("tmdb_id")
        or recognition.get("tmdb_id")
        or extract_tmdb_id_from_name(str(metadata.get("own_share_file_name") or ""))
        or ""
    ).strip()


def has_explicit_task_tmdb_hint(recognition: dict[str, Any], row: dict[str, Any], share_name: str = "") -> bool:
    return bool(explicit_task_tmdb_id(recognition, row, share_name))


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
    category = user_movie_category_bucket(
        category,
        media_type,
        str(result.get("reason") or ""),
        str(result.get("title") or ""),
        share_name,
    )
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


def format_task_label(row: dict[str, Any]) -> str:
    task_id = row.get("cms_task_id")
    title = row.get("title") or row.get("share_code") or "任务"
    return f"{title} #{task_id}" if task_id else str(title)


def emby_parent_label(item: dict) -> str:
    return str(item.get("ParentId") or item.get("CollectionType") or item.get("Type") or "未知")


def send_move_result(telegram: Any, chat_id: int | str, move_plan: MovePlan, moved_row: dict[str, Any]) -> None:
    if str(moved_row.get("move_status") or "").lower() == "moved":
        telegram.send_message(chat_id, f"STRM 已移动：{moved_row.get('dest_path')}")
    elif move_plan.status in {"conflict", "error"}:
        telegram.send_message(chat_id, f"STRM 未移动：{move_plan.reason}\n源：{move_plan.source_path or '-'}\n目标：{move_plan.dest_path or '-'}")


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


class SelfShareWorkflow:
    def __init__(
        self,
        config: SelfShareConfig,
        cms: CmsClient,
        p115: P115WebClient,
        store: Any,
        settings_store: Any | None = None,
    ):
        self.config = config
        self.cms = cms
        self.p115 = p115
        self.store = store
        self.settings_store = settings_store or store

    def prepare(self, row: dict[str, Any], recognition: dict[str, Any], share_name: str) -> tuple[dict[str, Any], Path | None]:
        if not self.config.enabled:
            return row, None
        row_id = int(row["id"])
        explicit_tmdb = explicit_task_tmdb_id(recognition, row, share_name)
        if explicit_tmdb:
            # The legacy polling path may pass a confident but stale CMS
            # recognition. Anchor lookup to the source marker first.
            recognition = dict(recognition)
            recognition["tmdb_id"] = explicit_tmdb
            recognition["share_name"] = str(recognition.get("share_name") or share_name)
        if not row.get("workflow_mode"):
            row = self.store.update_self_share(row_id, workflow_mode="self_share_sync", workflow_phase="submitted") or row
        if row.get("own_share_file_id") and explicit_tmdb:
            persisted_folder = {
                "file_id": row.get("own_share_file_id"),
                "file_name": row.get("own_share_file_name"),
            }
            if has_tmdb_folder_mismatch(persisted_folder, recognition, row, share_name):
                LOG.warning(
                    "Rejecting persisted self-share state with mismatched TMDB task_id=%s folder=%s",
                    explicit_tmdb,
                    row.get("own_share_file_name"),
                )
                return row, None
        if not row.get("own_share_file_id"):
            self.cms.run_auto_organize()
            row = self.store.update_self_share(row_id, workflow_phase="auto_organize_submitted") or row
            find_kwargs = {
                "excluded_parent_ids": self.config.excluded_parent_ids or set(),
                "min_update_time": float(row.get("created_at") or 0),
            }
            if self.config.organized_scan_parent_ids:
                find_kwargs.update(
                    {
                        "scan_parent_ids": self.config.organized_scan_parent_ids,
                        "category_names": set(self.config.parent_cid_category_map.values())
                        if self.config.parent_cid_category_map
                        else set(default_library_roots()),
                    }
                )
            folder = self.p115.find_organized_folder(recognition, share_name, **find_kwargs)
            if not folder:
                return row, None
            if has_tmdb_folder_mismatch(folder, recognition, row, share_name):
                LOG.warning(
                    "Rejecting legacy self-share folder with mismatched TMDB task_id=%s folder=%s",
                    expected_task_tmdb_id(recognition, row),
                    folder.get("file_name"),
                )
                return row, None
            category = str(folder.get("category") or "").strip() or category_for_115_parent_id(
                str(folder.get("parent_id") or ""),
                self.config.parent_cid_category_map,
            )
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
            receive_code = resolve_own_share_receive_code(self.settings_store, self.config).value
            share = self.p115.create_long_share(
                str(row.get("own_share_file_id") or ""),
                preferred_receive_code=receive_code,
            )
            row = self.store.update_self_share(
                row_id,
                workflow_phase="own_share_created",
                own_share_code=share.get("share_code"),
                own_share_receive_code=share.get("receive_code"),
                own_share_url=share.get("share_url"),
            ) or row
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
        cms_cloud_index: CmsCloudDataIndex | None = None,
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
        self.cms_cloud_index = cms_cloud_index
        self._now = time.time

    def _configured_receive_cid(self) -> str:
        getter = getattr(self.task_store, "get_self_share_receive_cid_override", None)
        override = str(getter() or "").strip() if callable(getter) else ""
        return override or self.receive_cid

    def _task_receive_cid(self, task) -> str:
        if str(getattr(task, "source_type", "") or "").strip() == "cloud_download":
            return (
                str(task.metadata.get("cloud_target_cid") or "").strip()
                or str(task.metadata.get("receive_target_cid") or "").strip()
                or self._configured_receive_cid()
            )
        return (
            str(task.metadata.get("receive_target_cid") or "").strip()
            or str(task.metadata.get("cloud_target_cid") or "").strip()
            or self._configured_receive_cid()
        )

    @staticmethod
    def _shared_only_stage(stage: TaskStage) -> bool:
        return stage in {
            TaskStage.SHARE_ALIAS_PREPARED,
            TaskStage.OWN_SHARE_CREATED,
            TaskStage.SHARE_VALIDATED,
            TaskStage.SHARE_SYNC_SUBMITTED,
            TaskStage.CMS_DELETE_SETTLED,
            TaskStage.CLEANED,
        }

    def run_stage(self, task):
        if self._shared_only_stage(task.current_stage) and effective_task_strm_mode(task) != "shared":
            return StageResult.failed(
                "直链任务误入共享 STRM 阶段，已阻止 115 分享和清理操作",
                error_type="strm_mode_mismatch",
                metadata={"strm_mode": effective_task_strm_mode(task)},
            )
        if task.current_stage == TaskStage.RECEIVED:
            return self._stage_received(task)
        if task.current_stage == TaskStage.CLOUD_DOWNLOADING:
            return self._stage_cloud_downloading(task)
        if task.current_stage == TaskStage.ORGANIZING:
            return self._stage_organizing(task)
        if task.current_stage == TaskStage.RECOGNIZING:
            return self._stage_recognizing(task)
        if task.current_stage == TaskStage.SHARE_ALIAS_PREPARED:
            return self._stage_share_alias_prepared(task)
        if task.current_stage == TaskStage.OWN_SHARE_CREATED:
            return self._stage_own_share_created(task)
        if task.current_stage == TaskStage.SHARE_VALIDATED:
            return self._stage_share_validated(task)
        if task.current_stage == TaskStage.SHARE_SYNC_SUBMITTED:
            return self._stage_share_sync_submitted(task)
        if task.current_stage == TaskStage.STRM_READY:
            return self._stage_strm_ready(task)
        if task.current_stage == TaskStage.CMS_DELETE_SETTLED:
            return self._stage_cms_delete_settled(task)
        if task.current_stage == TaskStage.MOVED:
            return self._stage_moved(task)
        if task.current_stage == TaskStage.EMBY_CONFIRMED:
            return self._stage_emby_confirmed(task)
        if task.current_stage == TaskStage.CLEANED:
            return self._stage_cleaned(task)
        return StageResult.failed("阶段尚未实现", error_type="unsupported_stage")

    def _trigger_cloud_auto_organize(self, task, row_id: int, metadata: dict[str, Any]) -> StageResult:
        operation = None
        operation_key = ""
        attempt = max(0, int(metadata.get("auto_organize_attempt") or 0))
        if self.task_store is not None:
            while True:
                operation_key = f"{operation_scope(task)}:cloud_auto_organize:{attempt}"
                operation = self.task_store.find_operation(int(task.id), operation_key)
                if operation is None or operation.status != "failed":
                    break
                attempt += 1
                metadata["auto_organize_attempt"] = attempt
            if operation is None:
                operation = self.task_store.prepare_operation(
                    int(task.id),
                    operation_key,
                    "cloud_auto_organize",
                    {"submission_id": int(row_id)},
                )
        if operation is None:
            try:
                self.cms.run_auto_organize()
            except Exception as exc:
                metadata["auto_organize_pending"] = True
                metadata["auto_organize_last_error"] = str(exc)[:500]
                LOG.warning("CMS auto-organize trigger failed after cloud download row_id=%s", row_id, exc_info=True)
                return StageResult.defer(
                    "文件已移动到待整理目录，等待 CMS 自动整理触发成功",
                    self.self_share_config.auto_organize_retry_seconds or 30,
                    metadata,
                )
        elif operation.status == "prepared":
            started = self.task_store.start_operation(int(task.id), operation_key)
            operation = started or self.task_store.find_operation(int(task.id), operation_key)
            if started is not None:
                try:
                    response = self.cms.run_auto_organize()
                except Exception as exc:
                    self.task_store.mark_operation_failed(int(task.id), operation_key, str(exc))
                    metadata["auto_organize_attempt"] = attempt + 1
                    metadata["auto_organize_pending"] = True
                    metadata["auto_organize_last_error"] = str(exc)[:500]
                    LOG.warning(
                        "CMS auto-organize trigger failed after cloud download row_id=%s",
                        row_id,
                        exc_info=True,
                    )
                    return StageResult.defer(
                        "文件已移动到待整理目录，等待 CMS 自动整理触发成功",
                        self.self_share_config.auto_organize_retry_seconds or 30,
                        metadata,
                    )
                completed = self.task_store.complete_operation(
                    int(task.id),
                    operation_key,
                    response if isinstance(response, dict) else {"accepted": True},
                )
                operation = completed or self.task_store.find_operation(int(task.id), operation_key)
        elif operation.status in {"started", "uncertain"}:
            if operation.status == "started":
                uncertain = self.task_store.mark_operation_uncertain(
                    int(task.id),
                    operation_key,
                    "CMS auto-organize result was not persisted",
                )
                operation = uncertain or self.task_store.find_operation(int(task.id), operation_key)
            metadata.update(
                {
                    "auto_organize_pending": True,
                    "auto_organize_operation_key": operation_key,
                    "auto_organize_last_error": "CMS 自动整理触发结果无法确认",
                }
            )
            return StageResult(
                StageOutcome.NEEDS_ACTION,
                "CMS 自动整理触发结果无法确认，已禁止自动重复触发；请检查 CMS 后重新处理",
                metadata,
                error_type="cloud_auto_organize_uncertain",
            )
        if operation is not None and operation.status != "succeeded":
            raise RuntimeError("CMS auto-organize operation could not be persisted")
        self.store.update_self_share(row_id, workflow_phase="auto_organize_submitted")
        metadata["auto_organize_pending"] = False
        metadata["auto_organize_submitted_at"] = self._now()
        metadata.pop("auto_organize_last_error", None)
        guard_delay = min(60, max(15, int(self.self_share_config.auto_organize_retry_seconds or 90) // 2))
        schedule_post_organize_restore_guard(
            store=self.store,
            cms=self.cms,
            self_share_config=self.self_share_config,
            move_config=self.move_config,
            emby=self.emby,
            delay_seconds=guard_delay,
            limit=50,
        )
        return StageResult.complete("115 云下载完成，已移动到待整理目录并触发 CMS 整理", metadata)

    def _stage_cloud_downloading(self, task):
        if not self.self_share_config.enabled:
            return StageResult.failed("自分享工作流未启用", error_type="self_share_disabled")
        metadata = dict(task.metadata)
        receive_cid = str(metadata.get("cloud_target_cid") or "").strip() or self._configured_receive_cid()
        if not receive_cid:
            return StageResult.failed("缺少 115 接收目录 ID", error_type="missing_receive_cid")
        started_at = float(metadata.get("cloud_started_at") or 0)
        timeout_seconds = float(
            metadata.get("cloud_timeout_seconds") or self.self_share_config.cloud_timeout_seconds
        )
        timed_out = bool(started_at and self._now() - started_at >= timeout_seconds)
        if metadata.get("auto_organize_pending") and (
            metadata.get("cloud_output_items") or metadata.get("cloud_output_file_id")
        ):
            if timed_out:
                return StageResult(
                    StageOutcome.NEEDS_ACTION,
                    "云下载输出已移动，但 CMS 自动整理触发超时，请人工检查后重试",
                    metadata,
                    error_type="cloud_auto_organize_timeout",
                )
            row = self._submission_row(task)
            if not row:
                return StageResult.failed("找不到云下载提交记录", error_type="submission_missing")
            return self._trigger_cloud_auto_organize(task, int(row["id"]), metadata)
        info_hash = str(metadata.get("cloud_info_hash") or "").strip()
        task_id = str(metadata.get("cloud_task_id") or "").strip()
        if not info_hash and not task_id:
            submitted = None
            operation = None
            operation_key = f"{operation_scope(task)}:cloud_download_submit"
            if self.task_store is not None:
                operation = self.task_store.find_operation(int(task.id), operation_key)
                if operation is None:
                    operation = self.task_store.prepare_operation(
                        int(task.id),
                        operation_key,
                        "cloud_download_submit",
                        {"url": task.url, "target_cid": receive_cid},
                    )
            if operation is None:
                submitted = self.p115.cloud_download_add(task.url, receive_cid)
            elif operation.status == "prepared":
                started = self.task_store.start_operation(int(task.id), operation_key)
                operation = started or self.task_store.find_operation(int(task.id), operation_key)
                if started is not None:
                    submitted = self.p115.cloud_download_add(
                        str(operation.request.get("url") or task.url),
                        str(operation.request.get("target_cid") or receive_cid),
                    )
                    completed = self.task_store.complete_operation(
                        int(task.id),
                        operation_key,
                        submitted,
                    )
                    operation = completed or self.task_store.find_operation(int(task.id), operation_key)
            elif operation.status == "succeeded":
                submitted = operation.result
            elif operation.status in {"started", "uncertain"}:
                recover = getattr(self.p115, "find_cloud_download_by_source", None)
                if callable(recover):
                    submitted = recover(str(operation.request.get("url") or task.url)) or None
                if submitted and operation.status == "started":
                    completed = self.task_store.complete_operation(
                        int(task.id),
                        operation_key,
                        submitted,
                    )
                    operation = completed or self.task_store.find_operation(int(task.id), operation_key)
                if not submitted:
                    operation_started_at = float(operation.started_at or operation.created_at or self._now())
                    metadata.update(
                        {
                            "cloud_started_at": operation_started_at,
                            "cloud_target_cid": receive_cid,
                            "cloud_submit_recovery_pending": True,
                        }
                    )
                    if self._now() - operation_started_at >= timeout_seconds:
                        return StageResult(
                            StageOutcome.NEEDS_ACTION,
                            "115 云下载提交结果无法确认，已禁止自动重复提交",
                            metadata,
                            error_type="cloud_download_submit_uncertain",
                        )
                    return StageResult.defer(
                        "等待确认 115 云下载提交结果，禁止自动重复提交",
                        self.self_share_config.cloud_poll_seconds,
                        metadata,
                    )
            if operation is not None and operation.status == "failed":
                return StageResult(
                    StageOutcome.NEEDS_ACTION,
                    "115 云下载提交状态无法安全恢复，请人工检查",
                    metadata,
                    error_type="cloud_download_submit_failed",
                )
            if not submitted:
                raise RuntimeError("115 cloud download operation disappeared")
            info_hash = str(submitted.get("info_hash") or "").strip()
            task_id = str(submitted.get("task_id") or "").strip()
            started_at = float(
                (operation.started_at if operation is not None else 0)
                or metadata.get("cloud_started_at")
                or self._now()
            )
            metadata.update(
                {
                    "cloud_info_hash": info_hash,
                    "cloud_task_id": task_id,
                    "cloud_started_at": started_at,
                    "cloud_target_cid": receive_cid,
                    "cloud_status": normalize_cloud_status(submitted),
                }
            )
            metadata.pop("cloud_submit_recovery_pending", None)
            return StageResult.defer(
                "已提交 115 云下载，等待完成",
                self.self_share_config.cloud_poll_seconds,
                metadata,
            )

        if timed_out:
            return StageResult.failed(
                "115 云下载超时，未进入后续整理和清理阶段",
                error_type="cloud_download_timeout",
                metadata=metadata,
            )

        persisted_output_items = metadata.get("cloud_output_items")
        if isinstance(persisted_output_items, list) and persisted_output_items:
            status = {"status": "completed"}
            normalized = "completed"
        else:
            identity = {"info_hash": info_hash, "task_id": task_id}
            status = self.p115.cloud_download_status(identity)
            normalized = normalize_cloud_status(status)
            metadata["cloud_status"] = normalized
        if normalized == "running":
            return StageResult.defer(
                "等待 115 云下载完成",
                self.self_share_config.cloud_poll_seconds,
                metadata,
            )
        if normalized == "failed":
            return StageResult.failed(
                "115 云下载失败，未删除任何源文件",
                error_type="cloud_download_failed",
                metadata=metadata,
            )
        if normalized != "completed":
            return StageResult.defer(
                "等待 115 云下载状态确认",
                self.self_share_config.cloud_poll_seconds,
                metadata,
            )

        output_items = metadata.get("cloud_output_items")
        if not isinstance(output_items, list) or not output_items:
            discover = getattr(self.p115, "discover_cloud_download_outputs", None)
            if not callable(discover):
                return StageResult.failed(
                    "115 客户端不支持云下载输出发现",
                    error_type="cloud_output_discovery_unsupported",
                    metadata=metadata,
                )
            try:
                output_items = discover(status)
            except P115CloudOutputPendingError as exc:
                return StageResult.defer(
                    str(exc),
                    self.self_share_config.cloud_poll_seconds,
                    metadata,
                )
            metadata["cloud_output_items"] = output_items
            return StageResult.defer(
                "已识别云下载输出，等待移动到待整理目录",
                1,
                metadata,
            )

        ensure = getattr(self.p115, "ensure_cloud_outputs_in_target", None)
        if not callable(ensure):
            return StageResult.failed(
                "115 客户端不支持云下载输出移动",
                error_type="cloud_output_movement_unsupported",
                metadata=metadata,
            )
        output_items = ensure(output_items, receive_cid)
        if not output_items:
            return StageResult.defer(
                "115 云下载输出尚未可移动",
                self.self_share_config.cloud_poll_seconds,
                metadata,
            )
        metadata["cloud_output_items"] = output_items
        first_output = output_items[0]
        row = self.store.upsert_submission(
            _ShareKey(task.share_code, task.receive_code),
            task.url,
            "received",
            title=first_output.get("file_name") or task.title or task.share_code,
        )
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="cloud_downloaded_to_pending",
        ) or row
        metadata.update(
            {
                "submission_id": int(row["id"]),
                "received_title": first_output.get("file_name") or task.title or task.share_code,
                "received_file_ids": [item["file_id"] for item in output_items],
                "received_items": [
                    {
                        "file_id": item["file_id"],
                        "file_name": item.get("file_name") or task.title or task.share_code,
                        "is_folder": bool(item.get("is_folder")),
                        "parent_id": item.get("parent_id") or receive_cid,
                        "received_item_verified": True,
                    }
                    for item in output_items
                ],
                "received_items_complete": True,
                "received_expected_item_count": len(output_items),
                "received_existing_file_ids": [],
                "received_snapshot_complete": True,
                "cloud_output_file_id": first_output["file_id"],
                "cloud_output_parent_id": first_output["parent_id"],
                "cloud_output_name": first_output.get("file_name") or "",
                "auto_organize_pending": True,
            }
        )
        return self._trigger_cloud_auto_organize(task, int(row["id"]), metadata)

    def _submission_row(self, task) -> dict[str, Any] | None:
        submission_id = task.metadata.get("submission_id") or task.submission_id
        if submission_id not in (None, ""):
            return self.store.find_by_id(int(submission_id))
        return self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))

    def _find_organized_folder(self, recognition, title, find_kwargs, scan_cursor, max_requests=8):
        try:
            raw = self.p115.find_organized_folder(
                recognition,
                title,
                **find_kwargs,
                organized_scan_cursor=scan_cursor,
                max_requests=max(1, int(max_requests)),
                return_scan_state=True,
            )
        except TypeError as exc:
            # Keep older FakeP115/custom clients usable while the optional state API rolls out.
            if "unexpected keyword argument" not in str(exc):
                raise
            raw = self.p115.find_organized_folder(recognition, title, **find_kwargs)
            return raw, None, True, 0
        if isinstance(raw, dict) and "folder" in raw and "scan_complete" in raw:
            try:
                request_count = max(0, int(raw.get("request_count") or 0))
            except (TypeError, ValueError):
                request_count = 0
            return (
                raw.get("folder"),
                raw.get("organized_scan_cursor"),
                bool(raw.get("scan_complete")),
                request_count,
            )
        return raw, None, True, 0

    def _stage_received(self, task):
        if not self.self_share_config.enabled:
            return StageResult.failed("自分享工作流未启用", error_type="self_share_disabled")
        receive_cid = self._task_receive_cid(task)
        if not receive_cid:
            return StageResult.failed("缺少 115 接收目录 ID", error_type="missing_receive_cid")

        reprocess_started_at = as_float(task.metadata.get("reprocess_started_at"), 0)
        if task.metadata.get("force_reprocess") and not reprocess_started_at:
            reprocess_started_at = self._now()
        existing = self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))
        reprocess_reset = False
        if task.metadata.get("force_reprocess") and not task.metadata.get("self_share_reprocess_reset") and existing:
            has_self_share_state = str(existing.get("workflow_mode") or "") == "self_share_sync" or any(
                str(existing.get(key) or "").strip()
                for key in (
                    "own_share_file_id",
                    "own_share_file_name",
                    "own_share_code",
                    "own_share_url",
                    "share_sync_status",
                    "share_alias_name",
                )
            )
            if has_self_share_state:
                if not reset_self_share_submission_for_reprocess(self.store, task):
                    return StageResult.needs_action(
                        "无法清理旧的自有分享状态，已停止重跑以避免复用错误目录",
                        {"submission_id": int(existing["id"])},
                    )
                existing = self.store.find_by_id(int(existing["id"])) or existing
                reprocess_reset = True
        patch_claimed_metadata = getattr(self.task_store, "patch_claimed_metadata", None)
        if str(getattr(task, "claimed_by", "") or "").strip():
            if not callable(patch_claimed_metadata):
                return StageResult(
                    StageOutcome.NEEDS_ACTION,
                    "当前任务不支持原子持久化接收目录 CID，已在接收前停止",
                    {
                        "receive_target_cid": receive_cid,
                        "receive_cid_persist_status": "unsupported",
                    },
                    error_type="receive_cid_persistence_unsupported",
                )
            persisted = patch_claimed_metadata(
                int(task.id),
                expected_claimed_by=str(task.claimed_by),
                expected_claimed_at=float(task.claimed_at),
                expected_claim_token=str(task.claim_token),
                expected_updated_at=float(task.updated_at),
                patch={"receive_target_cid": receive_cid},
            )
            if persisted is None:
                return StageResult(
                    StageOutcome.NEEDS_ACTION,
                    "接收目录 CID 未能持久化，任务 claim 已失效；已在接收前停止",
                    {
                        "receive_target_cid": receive_cid,
                        "receive_cid_persist_status": "stale_claim",
                    },
                    error_type="receive_cid_persistence_stale_claim",
                )
            task = persisted

        operation_key = f"{operation_scope(task)}:receive_share:{task.share_code}:{receive_cid}"
        operation = self.task_store.find_operation(int(task.id), operation_key)
        if operation is None and self._should_reuse_received_self_share_state(existing, task.metadata):
            metadata = self._received_metadata(existing, task.metadata)
            if reprocess_reset:
                metadata["self_share_reprocess_reset"] = True
            if reprocess_started_at:
                metadata["reprocess_started_at"] = reprocess_started_at
            return StageResult.complete("已接收 115 分享到待整理", metadata)

        if operation is None:
            intent = self.p115.prepare_share_receive(task.share_code, task.receive_code, receive_cid)
            operation = self.task_store.prepare_operation(
                int(task.id),
                operation_key,
                "receive_share",
                intent,
            )

        execute_authorized = False
        if operation.status == "prepared":
            started = self.task_store.start_operation(int(task.id), operation_key)
            if started is not None:
                operation = started
                execute_authorized = True
            else:
                operation = self.task_store.find_operation(int(task.id), operation_key)

        execution_incomplete = False
        if execute_authorized:
            try:
                received = self.p115.execute_prepared_share_receive(operation.request)
            except RuntimeError as exc:
                if is_115_receive_restricted_error(exc):
                    self.task_store.mark_operation_failed(int(task.id), operation_key, str(exc))
                    return StageResult.needs_action(
                        "115 接收被限制，已停止自动重试；请稍后恢复后手动重试或先手动转存。",
                        {"share_code": task.share_code, "receive_target_cid": receive_cid},
                    )
                self.task_store.mark_operation_uncertain(int(task.id), operation_key, str(exc))
                raise
            except Exception as exc:
                self.task_store.mark_operation_uncertain(int(task.id), operation_key, str(exc))
                raise
            if is_complete_share_receive_result(received):
                completed = self.task_store.complete_operation(int(task.id), operation_key, received)
                if completed is None:
                    completed = self.task_store.find_operation(int(task.id), operation_key)
                operation = completed
            else:
                execution_incomplete = True

        if operation is None:
            return StageResult.needs_action(
                "115 接收操作记录丢失，请人工检查后重试",
                {"receive_target_cid": receive_cid, "receive_operation_key": operation_key},
            )
        if operation.status == "succeeded":
            received = operation.result
        elif operation.status in {"started", "uncertain"}:
            if not execution_incomplete:
                received = self.p115.reconcile_prepared_share_receive(operation.request)
            if is_complete_share_receive_result(received):
                if operation.status == "started":
                    completed = self.task_store.complete_operation(int(task.id), operation_key, received)
                    if completed is not None:
                        received = completed.result
            else:
                recovery_age = max(0.0, self._now() - float(operation.started_at or operation.created_at))
                recovery_metadata = {
                    "receive_target_cid": receive_cid,
                    "receive_operation_key": operation_key,
                    "receive_recovery_started_at": float(operation.started_at or operation.created_at),
                }
                if recovery_age >= _RECEIVE_RECOVERY_WINDOW_SECONDS:
                    return StageResult.needs_action(
                        "无法确认 115 分享接收结果，已停止自动处理以避免重复接收",
                        recovery_metadata,
                    )
                return StageResult.defer(
                    "等待确认 115 分享接收结果",
                    _RECEIVE_RECOVERY_RETRY_SECONDS,
                    recovery_metadata,
                )
        else:
            return StageResult.needs_action(
                "115 分享接收操作未成功，已停止自动重试",
                {
                    "receive_target_cid": receive_cid,
                    "receive_operation_key": operation_key,
                    "receive_operation_status": operation.status,
                },
            )

        title = str(received.get("title") or task.title or task.share_code).strip()
        row = self.store.upsert_submission(
            _ShareKey(task.share_code, task.receive_code),
            task.url,
            "received",
            title=title,
        )
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="received_to_pending",
        ) or row
        metadata = {
            "submission_id": int(row["id"]),
            "received_title": title,
            "received_file_ids": received.get("file_ids") or [],
            "received_items": received.get("received_items") or [],
            "received_items_complete": bool(received.get("received_items_complete", True)),
            "received_expected_item_count": int(received.get("received_expected_item_count") or 0),
            "received_existing_file_ids": received.get("received_existing_file_ids") or [],
            "received_snapshot_complete": bool(received.get("received_snapshot_complete", False)),
            "receive_target_cid": receive_cid,
            "tmdb_hint_normalized": False,
        }
        roots = received.get("received_items") or []
        list_files = self.p115.list_files if hasattr(self.p115, "list_files") else (lambda *_a, **_k: [])
        try:
            files = snapshot_files(roots, list_files)
        except Exception:
            files = []
        metadata["intake_identity"] = {
            "root_ids": [str(item.get("file_id") or "").strip() for item in roots if str(item.get("file_id") or "").strip()],
            "files": files,
        }
        if reprocess_reset:
            metadata["self_share_reprocess_reset"] = True
        if reprocess_started_at:
            metadata["reprocess_started_at"] = reprocess_started_at
        if self._is_pending_update_run(task.metadata):
            metadata["update_received_run"] = int(task.metadata.get("update_requested_run") or 0)
        return StageResult.complete(
            "已接收 115 分享到待整理",
            metadata,
        )

    @staticmethod
    def _received_item_candidates(task, row: dict[str, Any]) -> list[dict[str, Any]]:
        items = task.metadata.get("received_items")
        if isinstance(items, list):
            normalized = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                file_id = str(item.get("file_id") or "").strip()
                file_name = str(item.get("file_name") or "").strip()
                if file_id and file_name:
                    normalized.append(dict(item))
            if normalized:
                return normalized
        file_id = str(task.metadata.get("cloud_output_file_id") or "").strip()
        file_name = str(task.metadata.get("cloud_output_name") or "").strip()
        if file_id and file_name:
            return [{"file_id": file_id, "file_name": file_name, "is_folder": False}]
        return []

    def _hint_source_name(self, task, row: dict[str, Any]) -> str:
        subscription_hint = str(task.metadata.get("tmdb_hint_source_name") or "").strip()
        if subscription_hint and extract_tmdb_id_from_name(subscription_hint):
            return subscription_hint
        candidates = [
            str(item.get("file_name") or "").strip()
            for item in self._received_item_candidates(task, row)
            if isinstance(item, dict)
        ]
        candidates.extend(
            str(value or "").strip()
            for value in (
                task.metadata.get("cloud_output_name"),
                task.metadata.get("received_title"),
                row.get("title"),
                task.title,
            )
        )
        for candidate in candidates:
            if extract_tmdb_id_from_name(candidate):
                return candidate
        return next((candidate for candidate in candidates if candidate), "")

    def _recover_received_items_for_hint(
        self,
        task,
        hint_id: str,
        expected_count: int = 1,
    ) -> list[dict[str, Any]]:
        receive_cid = self._task_receive_cid(task)
        if (
            not hint_id
            or not receive_cid
            or not hasattr(self.p115, "list_files")
            or task.metadata.get("received_snapshot_complete") is not True
        ):
            return []
        existing_ids = {
            str(value).strip()
            for value in (task.metadata.get("received_existing_file_ids") or [])
            if str(value).strip()
        }
        if receive_cid in existing_ids:
            # Older snapshots stored a regular file's parent cid as its item
            # id. That baseline cannot safely distinguish old and new files.
            return []
        try:
            items = self.p115.list_files(receive_cid, limit=500)
        except Exception:
            LOG.debug("Failed to recover received item for explicit TMDB hint", exc_info=True)
            return []
        matches = []
        for item in items:
            file_id = p115_item_id(item)
            file_name = p115_file_name(item)
            parent_id = p115_item_parent_id(item)
            if not file_id or not file_name:
                continue
            if file_id in existing_ids:
                continue
            if parent_id and parent_id != receive_cid:
                continue
            if extract_tmdb_id_from_name(file_name) != hint_id:
                continue
            matches.append(
                {
                    "file_id": file_id,
                    "file_name": file_name,
                    "is_folder": p115_is_folder(item),
                    "parent_id": parent_id or receive_cid,
                    "received_item_verified": True,
                }
            )
        return matches if len(matches) == max(1, int(expected_count or 1)) else []

    def _prepare_received_tmdb_hint(
        self,
        task,
        row: dict[str, Any],
        recognition: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        """Resolve and normalize explicit TMDB names before invoking CMS."""
        metadata: dict[str, Any] = {}
        source_name = self._hint_source_name(task, row)
        hint_id = extract_tmdb_id_from_name(source_name)
        if not hint_id:
            return recognition, metadata, ""
        if task.metadata.get("tmdb_hint_normalized"):
            return recognition, metadata, ""

        resolved, should_prompt = apply_tmdb_hint_resolution(recognition, source_name, self.tmdb_resolver)
        resolved_id = hint_id
        resolved_valid = (
            not should_prompt
            and str(resolved.get("tmdb_id") or "").strip() == hint_id
            and str(resolved.get("category_status") or "").strip() == "tmdb_resolved"
        )
        resolved_title = str(resolved.get("title") or "").strip() if resolved_valid else ""
        items = self._received_item_candidates(task, row)
        if items and not all(item.get("received_item_verified") is True for item in items):
            items = []
        if not items:
            try:
                expected_count = int(task.metadata.get("received_expected_item_count") or 1)
            except (TypeError, ValueError):
                expected_count = 1
            items = self._recover_received_items_for_hint(task, hint_id, expected_count)
        if not items:
            metadata.update(
                {
                    "tmdb_hint_id": hint_id,
                    "tmdb_hint_source_name": source_name,
                    "tmdb_hint_normalized": False,
                }
            )
            return recognition, metadata, "等待确认 115 接收后的本地文件，暂不触发 CMS 整理"

        complete = task.metadata.get("received_items_complete", True)
        try:
            expected_count = int(task.metadata.get("received_expected_item_count") or 0)
        except (TypeError, ValueError):
            expected_count = 0
        if complete is False and (not items or not expected_count or len(items) != expected_count):
            metadata.update(
                {
                    "tmdb_hint_id": hint_id,
                    "tmdb_hint_source_name": source_name,
                    "tmdb_hint_normalized": False,
                }
            )
            return recognition, metadata, "115 接收项未完整确认，暂不触发 CMS 整理"

        if len(items) == 1:
            selected = items
        else:
            selected = [
                item
                for item in items
                if extract_tmdb_id_from_name(str(item.get("file_name") or "")) == hint_id
            ]
            if not selected:
                selected = [item for item in items if bool(item.get("is_folder"))]
                if len(selected) != 1:
                    metadata.update(
                        {
                            "tmdb_hint_id": hint_id,
                            "tmdb_hint_source_name": source_name,
                            "tmdb_hint_normalized": False,
                        }
                    )
                    return recognition, metadata, "无法安全确定 TMDB 提示对应的 115 根项目，暂不触发 CMS 整理"

        normalized_items = []
        for item in selected:
            file_id = str(item.get("file_id") or "").strip()
            old_name = str(item.get("file_name") or "").strip()
            desired_name = normalize_tmdb_hint_name(old_name, hint_id, resolved_title)
            if not file_id or not old_name or not desired_name:
                continue
            if desired_name != old_name:
                self.p115.rename_file(file_id, desired_name)
            normalized_items.append(
                {
                    "file_id": file_id,
                    "before": old_name,
                    "after": desired_name,
                }
            )
        if len(normalized_items) != len(selected):
            metadata.update(
                {
                    "tmdb_hint_id": hint_id,
                    "tmdb_hint_source_name": source_name,
                    "tmdb_hint_normalized": False,
                }
            )
            return recognition, metadata, "无法安全规范化全部 115 接收项目，暂不触发 CMS 整理"

        enriched = dict(resolved) if resolved_valid else dict(recognition)
        if not resolved_valid:
            for key in ("ok", "title", "type", "category", "category_status", "category_suggestion"):
                enriched.pop(key, None)
        enriched.update(
            {
                "tmdb_id": resolved_id,
                "share_name": str(enriched.get("share_name") or source_name),
            }
        )
        if resolved_title:
            enriched["title"] = resolved_title
        if not enriched.get("category_status"):
            enriched["category_status"] = "tmdb_hint_pending"
        metadata.update(
            {
                "tmdb_hint_id": hint_id,
                "tmdb_hint_title": resolved_title,
                "tmdb_hint_category": str(enriched.get("category") or ""),
                "tmdb_hint_source_name": source_name,
                "tmdb_hint_normalized": True,
                "tmdb_hint_normalized_items": normalized_items,
            }
        )
        if items:
            metadata.update(
                {
                    "received_items": items,
                    "received_items_complete": True,
                    "received_expected_item_count": expected_count or len(items),
                }
            )
        if hasattr(self.store, "update_recognition"):
            row = self.store.update_recognition(
                int(row["id"]),
                enriched,
                str(enriched.get("category_status") or "tmdb_hint_pending"),
            ) or row
        if enriched.get("category") and hasattr(self.store, "update_category"):
            self.store.update_category(int(row["id"]), str(enriched["category"]), "selected")
        return enriched, metadata, ""

    def _cloud_output_file_ids(self, task) -> list[str]:
        """115 file ids produced by this task's cloud download, if known."""
        items = task.metadata.get("cloud_output_items")
        if isinstance(items, list):
            ids = [
                str(item.get("file_id") or "").strip()
                for item in items
                if isinstance(item, dict) and str(item.get("file_id") or "").strip()
            ]
            if ids:
                return ids
        file_id = str(task.metadata.get("cloud_output_file_id") or "").strip()
        return [file_id] if file_id else []

    def _folder_record_for_dest(self, dest: str, folder_hits: list[dict[str, Any]]) -> dict[str, Any]:
        dest = str(dest or "").strip()
        candidates = [item for item in folder_hits if isinstance(item, dict)]
        if dest and hasattr(self.p115, "search_files"):
            try:
                candidates.extend(self.p115.search_files(dest) or [])
            except Exception:
                LOG.debug("Failed to search dest folder dest=%s", dest, exc_info=True)
        folder_hit = next((item for item in candidates if p115_item_id(item) == dest), None)
        if folder_hit:
            return {
                "file_id": dest,
                "file_name": p115_file_name(folder_hit) or dest,
                "parent_id": p115_item_parent_id(folder_hit),
            }
        return {"file_id": dest, "file_name": dest, "parent_id": ""}

    def _dest_is_receive_child(self, dest: str, receive_cid: str) -> bool:
        dest = str(dest or "").strip()
        receive_cid = str(receive_cid or "").strip()
        if not dest or not receive_cid or dest == receive_cid:
            return False
        if not hasattr(self.p115, "list_files"):
            return False
        try:
            items = self.p115.list_files(receive_cid, limit=500)
        except Exception:
            LOG.debug("Failed to list receive_cid children dest=%s", dest, exc_info=True)
            return False
        return any(p115_item_id(item) == dest for item in items if isinstance(item, dict))

    def _intake_dest_skips_tmdb_mismatch(self, folder_id: str, metadata: dict[str, Any] | None) -> bool:
        identity = metadata.get("intake_identity") if isinstance(metadata, dict) else None
        dest_id = str(identity.get("dest_id") or "").strip() if isinstance(identity, dict) else ""
        return bool(dest_id and str(folder_id or "").strip() == dest_id)

    def _resolve_intake_dest_folder(
        self,
        stage_metadata: dict[str, Any],
        recognition: dict[str, Any],
        own_share_file_id: str = "",
        receive_cid: str = "",
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
        identity = stage_metadata.get("intake_identity")
        if not isinstance(identity, dict):
            return "", None, None
        files = [item for item in (identity.get("files") or []) if isinstance(item, dict)]
        expected_ids = [str(item.get("id") or "").strip() for item in files if str(item.get("id") or "").strip()]
        if not expected_ids:
            return "empty_files", None, None
        file_hits: list[dict[str, Any]] = []
        folder_hits: list[dict[str, Any]] = []
        if hasattr(self.p115, "search_files"):
            for item in files:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                try:
                    file_hits.extend(self.p115.search_files(name) or [])
                except Exception:
                    LOG.debug("Failed to search intake file name=%s", name, exc_info=True)
            tmdb_id = str(stage_metadata.get("tmdb_hint_id") or recognition.get("tmdb_id") or "").strip()
            if tmdb_id:
                try:
                    folder_hits.extend(self.p115.search_files(tmdb_id) or [])
                except Exception:
                    LOG.debug("Failed to search intake tmdb_id=%s", tmdb_id, exc_info=True)
        dest = dest_id_from_file_hits(file_hits=file_hits, folder_hits=folder_hits, expected_ids=expected_ids)
        root_ids = {str(value) for value in (identity.get("root_ids") or []) if str(value)}
        receive_cid = str(receive_cid or "").strip()
        if dest == CONFLICT:
            return CONFLICT, None, None
        if dest == receive_cid or dest in root_ids or self._dest_is_receive_child(dest, receive_cid):
            return INCOMPLETE, None, None
        persisted = str(own_share_file_id or "").strip()
        if dest == INCOMPLETE:
            if (
                persisted
                and persisted not in root_ids
                and persisted != receive_cid
                and not self._dest_is_receive_child(persisted, receive_cid)
            ):
                folder = self._folder_record_for_dest(persisted, folder_hits)
                if receive_cid and str(folder.get("parent_id") or "").strip() == receive_cid:
                    return INCOMPLETE, None, None
                return persisted, folder, {**identity, "dest_id": persisted}
            return INCOMPLETE, None, None
        folder = self._folder_record_for_dest(dest, folder_hits)
        if receive_cid and str(folder.get("parent_id") or "").strip() == receive_cid:
            return INCOMPLETE, None, None
        return dest, folder, {**identity, "dest_id": dest}

    def _complete_organized_folder(
        self,
        task,
        row: dict[str, Any],
        folder: dict[str, Any],
        recognition: dict[str, Any],
        title: str,
        stage_metadata: dict[str, Any],
        hint_metadata: dict[str, Any],
    ) -> StageResult:
        existing_library_category = category_from_existing_library_folder(self.move_config, folder)
        if existing_library_category and not str(folder.get("category") or "").strip():
            folder = dict(folder)
            folder["category"] = existing_library_category
        if self._conflicting_folder_owner(task, folder, recognition, row, title):
            return StageResult.needs_action(
                "CMS 整理目录已被其他 TMDB 任务占用，已阻止创建自有分享",
                {"submission_id": int(row["id"]), "own_share_file_id": ""},
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
                "category": str(folder.get("category") or recognition.get("category") or ""),
            }
        )
        if hasattr(self.store, "update_recognition"):
            row = self.store.update_recognition(int(row["id"]), recognition, "organized_found") or row
        complete_metadata = {
            "submission_id": int(row["id"]),
            "organized_folder": folder,
            "organized_scan_cursor": {},
            **hint_metadata,
        }
        identity = stage_metadata.get("intake_identity")
        if isinstance(identity, dict):
            dest_id = str(folder.get("file_id") or identity.get("dest_id") or "").strip()
            if dest_id:
                complete_metadata["intake_identity"] = {**identity, "dest_id": dest_id}
        return StageResult.complete(
            "已找到 CMS 整理后的 115 文件夹",
            complete_metadata,
        )

    def _stage_organizing(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        workflow_phase = str(row.get("workflow_phase") or "")
        recognition = self._recognition_from_row(row)
        title = str(row.get("title") or task.title or task.share_code)
        hint_metadata: dict[str, Any] = {}
        hint_block_message = ""
        hint_normalized_now = False
        if not row.get("own_share_file_id") and workflow_phase in {
            "",
            "received",
            "received_to_pending",
            "auto_organize_submitted",
        }:
            recognition, hint_metadata, hint_block_message = self._prepare_received_tmdb_hint(
                task,
                row,
                recognition,
            )
            hint_normalized_now = bool(hint_metadata.get("tmdb_hint_normalized"))
            if hint_block_message:
                return StageResult.defer(
                    hint_block_message,
                    self.self_share_config.auto_organize_retry_seconds or 30,
                    {"submission_id": int(row["id"]), **hint_metadata},
                )
            if recognition.get("title"):
                title = str(recognition.get("title") or title)
        if not expected_task_tmdb_id(recognition, row) and not row.get("own_share_file_id"):
            identity_name = self._received_child_video_identity(task, row)
            if identity_name:
                resolved, should_prompt = apply_tmdb_search_resolution(
                    recognition,
                    identity_name,
                    self.tmdb_resolver,
                )
                if not should_prompt and str(resolved.get("tmdb_id") or "").strip():
                    recognition = dict(resolved)
                    title = str(recognition.get("title") or identity_name)
                    if recognition.get("category") and hasattr(self.store, "update_category"):
                        row = self.store.update_category(int(row["id"]), str(recognition["category"]), "selected") or row
                    if hasattr(self.store, "update_recognition"):
                        row = self.store.update_recognition(
                            int(row["id"]),
                            recognition,
                            str(recognition.get("category_status") or "tmdb_search_resolved"),
                        ) or row
        folder = None
        persisted_folder = None
        if row.get("own_share_file_id") and row.get("own_share_file_name"):
            folder = {
                "file_id": row.get("own_share_file_id"),
                "file_name": row.get("own_share_file_name"),
                "parent_id": self._organized_parent_id(task, self._recognition_from_row(row)),
            }
            persisted_folder = folder
        elif hint_normalized_now or workflow_phase not in {
            "auto_organize_submitted",
            "organized_found",
            "own_share_created",
            "share_sync_submitted",
        }:
            self.cms.run_auto_organize()
            row = self.store.update_self_share(int(row["id"]), workflow_phase="auto_organize_submitted") or row
            schedule_post_organize_restore_guard(
                store=self.store,
                cms=self.cms,
                self_share_config=self.self_share_config,
                move_config=self.move_config,
                emby=self.emby,
                delay_seconds=min(60, max(15, int(self.self_share_config.auto_organize_retry_seconds or 90) // 2)),
                limit=50,
            )
        excluded_parent_ids = set(self.self_share_config.excluded_parent_ids or set())
        receive_cid = self._task_receive_cid(task)
        if receive_cid:
            excluded_parent_ids.add(receive_cid)
        min_update_time = float(row.get("created_at") or 0)
        stage_metadata = dict(task.metadata)
        stage_metadata.update(hint_metadata)
        update_started_at = as_finite_timestamp(stage_metadata.get("update_started_at"))
        if update_started_at:
            min_update_time = max(min_update_time, update_started_at - 5)
        reprocess_started_at = as_finite_timestamp(stage_metadata.get("reprocess_started_at"))
        if reprocess_started_at:
            min_update_time = max(min_update_time, reprocess_started_at - 5)
        direct_min_update_time = max(
            update_started_at - 5 if update_started_at else 0,
            reprocess_started_at - 5 if reprocess_started_at else 0,
        )
        organized_scan_cursor = stage_metadata.get("organized_scan_cursor")
        if not isinstance(organized_scan_cursor, dict):
            organized_scan_cursor = None
        rejected_file_ids = {
            str(value).strip()
            for value in (stage_metadata.get("rejected_organized_file_ids") or [])
            if str(value).strip()
        }
        dest_status, dest_folder, dest_identity = self._resolve_intake_dest_folder(
            stage_metadata,
            recognition,
            own_share_file_id=str(row.get("own_share_file_id") or ""),
            receive_cid=receive_cid,
        )
        if dest_status == "empty_files":
            return StageResult.defer(
                "等待 CMS 整理完成",
                self.self_share_config.auto_organize_retry_seconds or 30,
                {
                    "submission_id": int(row["id"]),
                    "organized_scan_cursor": organized_scan_cursor or {},
                    "rejected_organized_file_ids": sorted(rejected_file_ids),
                    **hint_metadata,
                },
            )
        if dest_status == CONFLICT:
            return StageResult.needs_action(
                "接收文件落到多个片库目录，已停止自动绑定",
                {"submission_id": int(row["id"])},
            )
        if dest_folder:
            if dest_identity:
                stage_metadata["intake_identity"] = dest_identity
            return self._complete_organized_folder(
                task,
                row,
                dest_folder,
                recognition,
                title,
                stage_metadata,
                hint_metadata,
            )
        if dest_status == INCOMPLETE:
            return StageResult.defer(
                "等待 CMS 整理完成",
                self.self_share_config.auto_organize_retry_seconds or 30,
                {
                    "submission_id": int(row["id"]),
                    "organized_scan_cursor": organized_scan_cursor or {},
                    "rejected_organized_file_ids": sorted(rejected_file_ids),
                    **hint_metadata,
                },
            )
        find_kwargs = {
            "excluded_parent_ids": excluded_parent_ids,
            "min_update_time": min_update_time,
        }
        if rejected_file_ids:
            find_kwargs["excluded_file_ids"] = rejected_file_ids
        if self.self_share_config.organized_scan_parent_ids:
            find_kwargs.update(
                {
                    "scan_parent_ids": self.self_share_config.organized_scan_parent_ids,
                    "category_names": set(self.self_share_config.parent_cid_category_map.values())
                    if self.self_share_config.parent_cid_category_map
                    else set(default_library_roots()),
                }
            )
        lookup_budget = 8
        direct_signal = None
        cloud_output_name = str(stage_metadata.get("cloud_output_name") or "").strip()
        if self.cms_cloud_index and cloud_output_name:
            indexed_folder = self.cms_cloud_index.folder_for_cloud_output_name(
                cloud_output_name,
                started_at=as_float(stage_metadata.get("cloud_started_at"), 0),
            )
            if indexed_folder:
                folder = indexed_folder
        if folder and has_tmdb_folder_mismatch(folder, recognition, row, title):
            LOG.warning(
                "Rejecting organized folder with mismatched TMDB task_id=%s folder=%s",
                expected_task_tmdb_id(recognition, row),
                folder.get("file_name"),
            )
            folder = None
        if folder and self.cms_cloud_index and folder.get("direct_file_id") and not folder.get("direct_relative_path"):
            folder_tmdb = extract_tmdb_id_from_name(str(folder.get("file_name") or ""))
            if folder_tmdb:
                direct_signal = find_recent_direct_library_strm_source_dir(
                    self.move_config,
                    row,
                    {**recognition, "tmdb_id": folder_tmdb},
                    title,
                    min_update_time=direct_min_update_time,
                )
                if direct_signal:
                    direct_source, _direct_category = direct_signal
                    direct_tmdb = extract_tmdb_id_from_name(str(direct_source))
                    expected_tmdb = expected_task_tmdb_id(recognition, row)
                    if has_explicit_task_tmdb_hint(recognition, row, title) and expected_tmdb and direct_tmdb and direct_tmdb != expected_tmdb:
                        direct_signal = None
                    if direct_signal:
                        direct_folder = self.cms_cloud_index.folder_for_direct_strm(direct_source, folder_tmdb)
                        if direct_folder and str(direct_folder.get("direct_file_id") or "") == str(folder.get("direct_file_id") or ""):
                            relative_path = str(direct_folder.get("direct_relative_path") or "").strip()
                            if relative_path:
                                folder = dict(folder)
                                folder["direct_relative_path"] = relative_path
        if folder is None:
            direct_signal = find_recent_direct_library_strm_source_dir(
                self.move_config,
                row,
                recognition,
                title,
                min_update_time=direct_min_update_time,
            )
            if direct_signal:
                direct_source, direct_category = direct_signal
                direct_tmdb = extract_tmdb_id_from_name(str(direct_source))
                expected_tmdb = expected_task_tmdb_id(recognition, row)
                if has_explicit_task_tmdb_hint(recognition, row, title) and expected_tmdb and direct_tmdb and direct_tmdb != expected_tmdb:
                    direct_signal = None
                if direct_signal:
                    direct_recognition = dict(recognition)
                    direct_recognition.update(
                        {
                            "ok": True,
                            "title": direct_source.name,
                            "share_name": str(direct_recognition.get("share_name") or title),
                            "category": direct_category or str(direct_recognition.get("category") or ""),
                            "category_status": "cms_direct_strm_resolved",
                        }
                    )
                    if direct_tmdb:
                        direct_recognition["tmdb_id"] = direct_tmdb
                    if direct_category and hasattr(self.store, "update_category"):
                        row = self.store.update_category(int(row["id"]), direct_category, "selected") or row
                    if hasattr(self.store, "update_recognition"):
                        row = self.store.update_recognition(int(row["id"]), direct_recognition, "cms_direct_strm_resolved") or row
                    recognition = direct_recognition
                    if self.cms_cloud_index and direct_tmdb:
                        folder = self.cms_cloud_index.folder_for_direct_strm(direct_source, direct_tmdb)
                        if folder:
                            folder = dict(folder)
                            if direct_category:
                                folder["category"] = direct_category
        if folder is None:
            folder, organized_scan_cursor, _scan_complete, lookup_requests = self._find_organized_folder(
                recognition,
                title,
                find_kwargs,
                organized_scan_cursor,
                max_requests=lookup_budget,
            )
            lookup_budget = max(0, lookup_budget - lookup_requests)
            if folder and has_tmdb_folder_mismatch(folder, recognition, row, title):
                LOG.warning(
                    "Rejecting searched organized folder with mismatched TMDB task_id=%s folder=%s",
                    expected_task_tmdb_id(recognition, row),
                    folder.get("file_name"),
                )
                folder = None
        if folder and is_unverified_received_source(folder, stage_metadata, receive_cid):
            folder = None
        folder = self._reject_if_unrelated(folder, task, rejected_file_ids)
        if rejected_file_ids:
            find_kwargs["excluded_file_ids"] = rejected_file_ids
        if not folder:
            tmdb_resolved, tmdb_should_prompt = apply_tmdb_search_resolution(recognition, title, self.tmdb_resolver)
            if not tmdb_should_prompt and str(tmdb_resolved.get("tmdb_id") or "").strip() and lookup_budget > 0:
                recognition = dict(tmdb_resolved)
                folder, organized_scan_cursor, _scan_complete, lookup_requests = self._find_organized_folder(
                    recognition,
                    title,
                    find_kwargs,
                    organized_scan_cursor,
                    max_requests=lookup_budget,
                )
                lookup_budget = max(0, lookup_budget - lookup_requests)
                if folder and is_unverified_received_source(folder, stage_metadata, receive_cid):
                    folder = None
                if folder and has_tmdb_folder_mismatch(folder, recognition, row, title):
                    folder = None
                folder = self._reject_if_unrelated(folder, task, rejected_file_ids)
                category = str(recognition.get("category") or "").strip()
                preserve_authoritative_category = (
                    direct_min_update_time > 0
                    and not folder
                    and has_authoritative_category(row, recognition)
                )
                if category and hasattr(self.store, "update_category") and not preserve_authoritative_category:
                    row = self.store.update_category(int(row["id"]), category, "selected") or row
                if hasattr(self.store, "update_recognition") and not preserve_authoritative_category:
                    row = self.store.update_recognition(
                        int(row["id"]),
                        recognition,
                        str(recognition.get("category_status") or "tmdb_search_resolved"),
                    ) or row
        if folder is not persisted_folder and self.cms_cloud_index:
            cloud_output_file_ids = self._cloud_output_file_ids(task)
            if cloud_output_file_ids and not self.cms_cloud_index.folder_contains_cloud_output(
                folder,
                cloud_output_file_ids,
            ):
                LOG.warning(
                    "Rejecting organized folder that lacks this task's cloud output task_id=%s folder=%s",
                    int(row["id"]),
                    folder.get("file_name"),
                )
                folder = None
        if not folder:
            return StageResult.defer(
                "等待 CMS 整理完成",
                self.self_share_config.auto_organize_retry_seconds or 30,
                {
                    "submission_id": int(row["id"]),
                    "organized_scan_cursor": organized_scan_cursor or {},
                    "rejected_organized_file_ids": sorted(rejected_file_ids),
                    **hint_metadata,
                },
            )
        return self._complete_organized_folder(
            task,
            row,
            folder,
            recognition,
            title,
            stage_metadata,
            hint_metadata,
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
        if self._conflicting_folder_owner(
            task,
            folder,
            recognition,
            row,
            str(folder.get("file_name") or task.title or task.share_code),
        ):
            return StageResult.needs_action(
                "CMS 整理目录已被其他 TMDB 任务占用，已阻止创建自有分享",
                {"submission_id": int(row["id"]), "own_share_file_id": ""},
            )
        if not self._intake_dest_skips_tmdb_mismatch(folder.get("file_id"), task.metadata):
            if has_tmdb_folder_mismatch(
                folder,
                recognition,
                row,
                str(folder.get("file_name") or task.title or task.share_code),
            ):
                return StageResult.needs_action(
                    "CMS 整理目录与源任务 TMDB 不一致或无法确认，已阻止创建自有分享",
                    {"submission_id": int(row["id"]), "own_share_file_id": ""},
                )
        file_id = str(folder.get("file_id") or "").strip()
        folder_name = str(folder.get("file_name") or row.get("own_share_file_name") or task.title or "").strip()
        share_name = str(row.get("title") or task.title or folder_name or task.share_code).strip()
        if is_unverified_received_source(folder, task.metadata, self._task_receive_cid(task)):
            return StageResult.needs_action(
                "等待可验证的 CMS 整理后源目录，当前 115 ID 仍是接收/分享快照，拒绝继续创建自有分享",
                {"submission_id": int(row["id"]), "own_share_file_id": ""},
            )
        child_video_name = self._folder_child_video_name(file_id)
        recognition_share_name = child_video_name or share_name
        parent_id = self._organized_parent_id(task, recognition, folder)
        category = str(folder.get("category") or "").strip() or category_for_115_parent_id(
            parent_id,
            self.self_share_config.parent_cid_category_map,
        )
        if not category and hasattr(self.store, "category_for_parent_id"):
            category = self.store.category_for_parent_id(parent_id)
        manual_category = ""
        if str(row.get("category_status") or "").strip() == "selected":
            manual_category = str(row.get("category_choice") or "").strip()
        if manual_category:
            category = manual_category
        tmdb_id = str(
            extract_tmdb_id_from_name(folder_name)
            or extract_tmdb_id_from_name(share_name)
            or recognition.get("tmdb_id")
            or ""
        ).strip()
        recognition.update(
            {
                "title": recognition.get("title") or folder_name or share_name,
                "share_name": recognition.get("share_name") or recognition_share_name,
                "tmdb_id": tmdb_id,
                "category": category,
                "organized_parent_id": parent_id,
                "parent_id": parent_id,
            }
        )
        category = str(category or "").strip()
        if category:
            recognition = enrich_recognition_from_self_share_folder(recognition, folder, category, share_name)
            recognition["organized_parent_id"] = parent_id
            recognition["parent_id"] = parent_id
            tmdb_id = str(recognition.get("tmdb_id") or tmdb_id).strip()
        else:
            tmdb_resolved, tmdb_should_prompt = apply_tmdb_hint_resolution(recognition, recognition_share_name, self.tmdb_resolver)
            tmdb_category = str(tmdb_resolved.get("category") or "").strip()
            if tmdb_should_prompt and child_video_name:
                tmdb_resolved, tmdb_should_prompt = apply_tmdb_search_resolution(
                    recognition,
                    child_video_name,
                    self.tmdb_resolver,
                )
                tmdb_category = str(tmdb_resolved.get("category") or "").strip()
            if not tmdb_should_prompt and tmdb_category:
                category = tmdb_category
                recognition = dict(tmdb_resolved)
                recognition["organized_parent_id"] = parent_id
                recognition["parent_id"] = parent_id
                tmdb_id = str(recognition.get("tmdb_id") or tmdb_id).strip()
                if hasattr(self.store, "update_category"):
                    row = self.store.update_category(int(row["id"]), category, "selected") or row
                if hasattr(self.store, "update_recognition"):
                    row = self.store.update_recognition(int(row["id"]), recognition, str(recognition.get("category_status") or "tmdb_resolved")) or row
                return StageResult.complete(
                    "已通过 TMDB 识别分类",
                    {
                        "submission_id": int(row["id"]),
                        "recognition": recognition,
                        "category": category,
                        "tmdb_id": tmdb_id,
                        "own_share_file_id": file_id,
                    },
                )
            cms_category = category_from_existing_library_folder(self.move_config, {"file_name": folder_name})
            if cms_category:
                category = cms_category
                recognition = enrich_recognition_from_self_share_folder(recognition, folder, category, share_name)
                recognition["organized_parent_id"] = parent_id
                recognition["parent_id"] = parent_id
                tmdb_id = str(recognition.get("tmdb_id") or tmdb_id).strip()
                if hasattr(self.store, "update_category"):
                    row = self.store.update_category(int(row["id"]), category, "selected") or row
                if hasattr(self.store, "update_recognition"):
                    row = self.store.update_recognition(int(row["id"]), recognition, "self_share_resolved") or row
                return StageResult.complete(
                    "已通过 CMS 直链 STRM 媒体库识别分类",
                    {
                        "submission_id": int(row["id"]),
                        "recognition": recognition,
                        "category": category,
                        "tmdb_id": tmdb_id,
                        "own_share_file_id": file_id,
                    },
                )
            previous_count = 0
            if task.metadata.get("_defer_stage") == TaskStage.RECOGNIZING.value and task.metadata.get("_defer_message") == "等待 CMS 直链 STRM 分类":
                try:
                    previous_count = int(task.metadata.get("_defer_count") or 0)
                except (TypeError, ValueError):
                    previous_count = 0
            if self.move_config.library_roots and previous_count < 4:
                recognition["category"] = ""
                recognition["category_status"] = "waiting_cms_direct_strm"
                if hasattr(self.store, "update_recognition"):
                    row = self.store.update_recognition(int(row["id"]), recognition, "waiting_cms_direct_strm") or row
                return StageResult.defer(
                    "等待 CMS 直链 STRM 分类",
                    5,
                    {"submission_id": int(row["id"]), "recognition": recognition, "own_share_file_id": file_id},
                )
            recognition["category"] = ""
            recognition["category_status"] = "needs_action"
            recognition.pop("category_suggestion", None)
            recognition.pop("openai_confidence", None)
            recognition.pop("openai_reason", None)
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

    def _conflicting_folder_owner(self, task, folder, recognition, row, share_name):
        file_id = str(folder.get("file_id") or "").strip()
        if not file_id:
            return None
        expected = expected_task_tmdb_id(recognition, row) or task_tmdb_identity(task)
        owners = self.task_store.list_tasks_by_own_share_file_id(file_id, exclude_task_id=task.id)
        for owner in owners:
            owner_identity = task_tmdb_identity(owner)
            if not expected or not owner_identity or owner_identity != expected:
                return owner
        return None

    def _folder_child_video_name(self, file_id: str) -> str:
        if not file_id or not hasattr(self.p115, "list_files"):
            return ""
        try:
            items = self.p115.list_files(file_id, limit=20)
        except Exception:
            LOG.debug("Failed to list received folder children for recognition", exc_info=True)
            return ""
        for item in items:
            name = str(item.get("n") or item.get("file_name") or item.get("name") or "").strip()
            if name.lower().endswith((".mkv", ".mp4", ".ts", ".iso", ".avi", ".mov", ".wmv", ".m2ts")):
                return name
        return ""

    def _received_child_video_identity(self, task, row: dict[str, Any]) -> str:
        for item in self._received_item_candidates(task, row):
            name = str(item.get("file_name") or "").strip()
            if not bool(item.get("is_folder")):
                if name.lower().endswith((".mkv", ".mp4", ".ts", ".iso", ".avi", ".mov", ".wmv", ".m2ts")):
                    return name
                continue
            child = self._folder_child_video_name(str(item.get("file_id") or ""))
            if child:
                return child
        return ""

    def _received_file_ids(self, task) -> set[str]:
        ids = {
            str(value).strip()
            for value in (task.metadata.get("received_file_ids") or [])
            if str(value).strip()
        }
        for item in self._received_item_candidates(task, {}):
            file_id = str(item.get("file_id") or "").strip()
            if file_id:
                ids.add(file_id)
        return ids

    def _folder_child_ids(self, file_id: str) -> list[str]:
        if not file_id or not hasattr(self.p115, "list_files"):
            return []
        try:
            items = self.p115.list_files(file_id, limit=500)
        except Exception:
            LOG.debug("Failed to list folder children", exc_info=True)
            return []
        return sorted({p115_item_id(item) for item in items if p115_item_id(item)})

    def _folder_contains_received_items(self, folder, task) -> bool:
        ids = self._received_file_ids(task)
        if not ids:
            return True
        file_id = str((folder or {}).get("file_id") or "").strip()
        if not file_id or file_id in ids:
            return True
        children = self._folder_child_ids(file_id)
        if not children:
            return True
        return bool(set(children) & ids)

    def _reject_if_unrelated(self, folder, task, rejected_file_ids):
        if not folder or self._folder_contains_received_items(folder, task):
            return folder
        rejected_id = str(folder.get("file_id") or "").strip()
        if rejected_id:
            rejected_file_ids.add(rejected_id)
        return None

    def _is_library_dest_cleanup_target(self, task, row: dict[str, Any], file_id: str) -> bool:
        dest_id = str(row.get("own_share_file_id") or task.metadata.get("own_share_file_id") or "").strip()
        if not file_id or file_id != dest_id:
            return False
        parent_id = source_delete_parent_id(task, row, file_id)
        receive_cid = self._task_receive_cid(task)
        inbox = set(self.self_share_config.source_cleanup_parent_ids or set())
        if receive_cid:
            inbox.add(receive_cid)
        if parent_id and parent_id in inbox:
            return False
        recognition = self._recognition_from_row(row)
        organized_parent = str(recognition.get("organized_parent_id") or recognition.get("parent_id") or "").strip()
        dest_path = str(row.get("dest_path") or task.metadata.get("dest_path") or "").strip()
        return bool(dest_path and parent_id and organized_parent and parent_id == organized_parent)

    def _stage_share_alias_prepared(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        file_id = str(task.metadata.get("own_share_file_id") or row.get("own_share_file_id") or "").strip()
        canonical_name = str(row.get("own_share_file_name") or "").strip()
        if not file_id or not canonical_name:
            return StageResult.failed("缺少 CMS 整理后的文件夹", error_type="organized_folder_missing")
        recognition = self._recognition_from_row(row)
        if not self._intake_dest_skips_tmdb_mismatch(file_id, task.metadata) and has_tmdb_folder_mismatch(
            {"file_name": canonical_name}, recognition, row, canonical_name
        ):
            return StageResult.needs_action(
                "CMS 整理目录与源任务 TMDB 不一致或无法确认，已阻止创建自有分享",
                {"submission_id": int(row["id"]), "own_share_file_id": ""},
            )
        alias_name = str(row.get("share_alias_name") or "").strip()
        if alias_name:
            return StageResult.complete("分享目录别名已准备", self._own_share_metadata(row))
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="share_alias_prepared",
            share_validation_status="pending",
            share_validation_error="",
        ) or row
        return StageResult.complete("已保留 CMS 整理目录名", self._own_share_metadata(row))

    def _stage_own_share_created(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        file_id = str(task.metadata.get("own_share_file_id") or row.get("own_share_file_id") or "").strip()
        if not file_id:
            return StageResult.failed("缺少自有分享文件夹 ID", error_type="own_share_file_missing")
        recognition = self._recognition_from_row(row)
        if not self._intake_dest_skips_tmdb_mismatch(file_id, task.metadata) and has_tmdb_folder_mismatch(
            {"file_name": str(row.get("own_share_file_name") or "")},
            recognition,
            row,
            str(row.get("own_share_file_name") or task.title or task.share_code),
        ):
            return StageResult.needs_action(
                "CMS 整理目录与源任务 TMDB 不一致或无法确认，已阻止创建自有分享",
                self._own_share_metadata(row) | {"own_share_file_id": ""},
            )
        folder = task.metadata.get("organized_folder")
        if isinstance(folder, dict) and is_unverified_received_source(folder, task.metadata, self._task_receive_cid(task)):
            return StageResult.needs_action(
                "等待可验证的 CMS 整理后源目录，当前 115 ID 仍是接收/分享快照，拒绝创建自有分享",
                self._own_share_metadata(row) | {"own_share_file_id": ""},
            )
        if self._conflicting_folder_owner(
            task,
            {"file_id": file_id},
            recognition,
            row,
            str(row.get("own_share_file_name") or task.title or task.share_code),
        ):
            return StageResult.needs_action(
                "CMS 整理目录已被其他 TMDB 任务占用，已阻止创建自有分享",
                self._own_share_metadata(row) | {"own_share_file_id": ""},
            )
        child_ids = self._folder_child_ids(file_id)
        previous_child_ids = task.metadata.get("own_share_child_ids")
        dest_changed = (
            bool(row.get("own_share_code"))
            and previous_child_ids is not None
            and sorted(str(value) for value in previous_child_ids) != child_ids
        )
        if dest_changed:
            task.metadata["operation_generation"] = max(0, int(task.metadata.get("operation_generation") or 0)) + 1
            if hasattr(self.store, "update_self_share"):
                row = self.store.update_self_share(
                    int(row["id"]),
                    own_share_code="",
                    own_share_receive_code="",
                    own_share_url="",
                    share_sync_status="",
                ) or row
        created = False
        direct_file_share = False
        direct_relative_path = ""
        recovered_share_created_at = 0.0
        share_creation_pending = str(task.metadata.get("share_create_status") or "").strip().lower() == "pending"
        create_operation_key = f"{operation_scope(task)}:create_share:{file_id}"
        create_operation = self.task_store.find_operation(int(task.id), create_operation_key)
        legacy_share_creation_pending = share_creation_pending and create_operation is None
        if legacy_share_creation_pending and not row.get("own_share_code"):
            recover = getattr(self.p115, "find_own_share_by_title", None)
            if callable(recover):
                recovered = recover(
                    str(row.get("own_share_file_name") or task.title or "").strip(),
                    min_create_time=self._positive_timestamp(task.metadata.get("share_create_requested_at")),
                )
                if recovered:
                    if recovered.get("recovery_status") == "ambiguous":
                        return StageResult.needs_action(
                            "发现多个符合恢复条件的同名 115 分享，无法安全确认归属，请人工检查",
                            self._own_share_metadata(row)
                            | {
                                "share_recovery_status": "ambiguous",
                                "share_recovery_match_count": int(recovered.get("match_count") or 0),
                            },
                        )
                    receive_code = resolve_own_share_receive_code(self.task_store, self.self_share_config).value
                    settings = self.p115.ensure_share_settings(str(recovered.get("share_code") or ""), receive_code)
                    recovered = {**recovered, **settings}
                    recovered_share_created_at = self._positive_timestamp(recovered.get("create_time"))
                    row = self.store.update_self_share(
                        int(row["id"]),
                        workflow_phase="own_share_created",
                        own_share_code=recovered.get("share_code"),
                        own_share_receive_code=recovered.get("receive_code"),
                        own_share_url=recovered.get("share_url"),
                    ) or row
            if not row.get("own_share_code"):
                return StageResult.defer(
                    "等待 115 完成分享创建",
                    1800,
                    self._own_share_metadata(row)
                    | {
                        "share_create_status": "pending",
                        "share_create_requested_at": task.metadata.get("share_create_requested_at") or self._now(),
                    },
                )
        if not row.get("own_share_code"):
            receive_code = resolve_own_share_receive_code(self.task_store, self.self_share_config).value
            try:
                share = self._journaled_create_share(
                    task,
                    file_id,
                    str(row.get("own_share_file_name") or task.title or "").strip(),
                    receive_code,
                )
            except P115SharePendingError:
                return StageResult.defer(
                    "等待 115 完成分享创建",
                    1800,
                    self._own_share_metadata(row)
                    | {
                        "share_create_status": "pending",
                        "share_create_requested_at": self._now(),
                    },
                )
            except RuntimeError as exc:
                direct_file_id, direct_relative_path, direct_file_name, direct_parent_id = self._direct_file_share_details(task)
                if not direct_file_id or not self._is_gone_share_source_error(exc):
                    raise
                if self._conflicting_folder_owner(
                    task,
                    {"file_id": direct_file_id},
                    recognition,
                    row,
                    direct_file_name or Path(direct_relative_path).name,
                ):
                    return StageResult.needs_action(
                        "CMS 直链文件已被其他 TMDB 任务占用，已阻止创建自有分享",
                        self._own_share_metadata(row),
                    )
                if not hasattr(self.store, "replace_self_share_source_file_id"):
                    raise
                row = self.store.replace_self_share_source_file_id(int(row["id"]), direct_file_id) or row
                direct_title = str(
                    direct_file_name
                    or task.metadata.get("cloud_output_name")
                    or Path(direct_relative_path).name
                ).strip()
                direct_metadata = {
                    "direct_file_share": True,
                    "direct_file_share_file_id": direct_file_id,
                    "direct_file_share_file_name": direct_file_name,
                    "direct_file_share_parent_id": direct_parent_id,
                    "direct_file_share_relative_path": direct_relative_path,
                }
                share = self._journaled_create_share(
                    task,
                    direct_file_id,
                    direct_title,
                    receive_code,
                    recovery_metadata=direct_metadata,
                )
                direct_file_share = True
            if share.get("recovery_status") == "ambiguous":
                return StageResult.needs_action(
                    "发现多个符合恢复条件的同名 115 分享，无法安全确认归属，请人工检查",
                    self._own_share_metadata(row)
                    | {
                        "share_recovery_status": "ambiguous",
                        "share_recovery_match_count": int(share.get("match_count") or 0),
                    },
                )
            row = self.store.update_self_share(
                int(row["id"]),
                workflow_phase="own_share_created",
                own_share_code=share.get("share_code"),
                own_share_receive_code=share.get("receive_code"),
                own_share_url=share.get("share_url"),
            ) or row
            created = True
        message = "已创建自有 115 分享" if created else "已存在自有 115 分享"
        metadata = self._own_share_metadata(row)
        durable_file_id = str(row.get("own_share_file_id") or file_id).strip()
        durable_operation = self.task_store.find_operation(
            int(task.id),
            f"{operation_scope(task)}:create_share:{durable_file_id}",
        )
        metadata.update(self._create_share_operation_metadata(durable_operation))
        share_created_at = self._positive_timestamp(task.metadata.get("share_created_at")) or recovered_share_created_at
        share_created_at = self._positive_timestamp(metadata.get("share_created_at")) or share_created_at
        if created and not share_created_at:
            share_created_at = float(int(max(0.0, self._now())))
        if share_creation_pending and not share_created_at:
            share_created_at = self._now()
        if share_created_at:
            metadata["share_created_at"] = share_created_at
        if child_ids:
            metadata["own_share_child_ids"] = child_ids
        if dest_changed:
            metadata["operation_generation"] = task.metadata.get("operation_generation")
        if direct_file_share:
            metadata.update(direct_metadata)
        return StageResult.complete(message, metadata)

    def _journaled_create_share(
        self,
        task,
        file_id: str,
        share_title: str,
        receive_code: str,
        *,
        recovery_metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        operation_key = f"{operation_scope(task)}:create_share:{file_id}"
        operation = self.task_store.find_operation(int(task.id), operation_key)
        if operation is None:
            request = {
                "file_id": file_id,
                "share_title": share_title,
                "receive_code": receive_code,
                "requested_at": float(int(max(0.0, self._now()))),
            }
            request.update(recovery_metadata or {})
            operation = self.task_store.prepare_operation(
                int(task.id),
                operation_key,
                "create_share",
                request,
            )
        if operation.status == "prepared":
            started = self.task_store.start_operation(int(task.id), operation_key)
            operation = started or self.task_store.find_operation(int(task.id), operation_key)
            if started is not None:
                try:
                    created = self.p115.create_share(str(operation.request.get("file_id") or file_id))
                except P115SharePendingError:
                    raise
                except Exception as exc:
                    if self._is_gone_share_source_error(exc):
                        self.task_store.mark_operation_failed(int(task.id), operation_key, str(exc))
                    else:
                        self.task_store.mark_operation_uncertain(int(task.id), operation_key, str(exc))
                    raise
                completed = self.task_store.complete_operation(int(task.id), operation_key, created)
                operation = completed or self.task_store.find_operation(int(task.id), operation_key)
        if operation is None:
            raise RuntimeError("115 share creation operation disappeared")
        if operation.status == "succeeded":
            created = operation.result
        elif operation.status in {"started", "uncertain"}:
            recovered = self.p115.find_own_share_by_title(
                str(operation.request.get("share_title") or ""),
                min_create_time=self._positive_timestamp(operation.request.get("requested_at")),
            )
            if not recovered:
                raise P115SharePendingError("115 create share outcome is not visible yet")
            created = recovered
            if recovered.get("recovery_status") == "ambiguous":
                return recovered
            if operation.status == "started":
                completed = self.task_store.complete_operation(int(task.id), operation_key, recovered)
                operation = completed or operation
        elif operation.status == "failed":
            raise RuntimeError(operation.last_error or "115 create share failed")
        else:
            raise RuntimeError(f"unsupported create share operation status: {operation.status}")
        settings = self.p115.ensure_share_settings(
            str(created.get("share_code") or ""),
            str(operation.request.get("receive_code") or receive_code),
        )
        return {**created, **settings}

    def _create_share_operation_metadata(self, operation: Any | None) -> dict[str, Any]:
        if operation is None or operation.status not in {"started", "uncertain", "succeeded"}:
            return {}
        metadata: dict[str, Any] = {}
        created_at = self._positive_timestamp(operation.result.get("create_time"))
        if not created_at:
            created_at = self._positive_timestamp(operation.request.get("requested_at"))
        if created_at:
            metadata["share_created_at"] = created_at
        if operation.request.get("direct_file_share"):
            for key in (
                "direct_file_share",
                "direct_file_share_file_id",
                "direct_file_share_file_name",
                "direct_file_share_parent_id",
                "direct_file_share_relative_path",
            ):
                value = operation.request.get(key)
                if value not in (None, ""):
                    metadata[key] = value
        return metadata

    def _stage_share_validated(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        own_code = str(row.get("own_share_code") or "").strip()
        own_pwd = str(row.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        if not own_code:
            return StageResult.failed("缺少自有分享码", error_type="own_share_missing")
        try:
            status = self.p115.inspect_share(own_code, own_pwd)
        except P115ShareUnavailableError as exc:
            row = self.store.update_self_share(
                int(row["id"]),
                share_validation_status="invalid",
                share_validation_error=str(exc)[:200],
            ) or row
            metadata = self._own_share_metadata(row)
            metadata.update(self._share_review_metadata(task, row, "invalid", error=str(exc)))
            return StageResult.needs_action("自有分享已被 115 判定为不可用，源文件已保留，停止自动改名和重建", metadata)
        except RuntimeError as exc:
            metadata = self._own_share_metadata(row)
            metadata.update(self._share_review_metadata(task, row, "unknown", error=str(exc)))
            return StageResult.defer("115 分享状态暂时无法确认，源文件暂不清理", 60, metadata)
        have_vio_file = self._as_bool_flag(status.get("have_vio_file"))
        share_state = str(status.get("share_state") or "").strip().lower()
        if have_vio_file or (share_state and share_state not in {"0", "1", "true"}):
            reason = "115 标记 have_vio_file" if have_vio_file else f"115 分享状态不可用：{share_state or '未知'}"
            row = self.store.update_self_share(
                int(row["id"]),
                share_validation_status="invalid",
                share_validation_error=reason,
            ) or row
            metadata = self._own_share_metadata(row)
            metadata.update(self._share_review_metadata(task, row, "invalid", error=reason))
            return StageResult.needs_action("自有分享存在 115 风险标记或不可用状态，源文件已保留，停止自动改名和重建", metadata)
        if not share_state:
            metadata = self._own_share_metadata(row)
            metadata.update(self._share_review_metadata(task, row, "unknown", error="115 未返回明确分享状态"))
            return StageResult.defer("115 未返回明确分享状态，源文件暂不清理", 60, metadata)
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="share_validated",
            share_validation_status="valid",
            share_validation_error="",
        ) or row
        metadata = self._own_share_metadata(row)
        metadata.update(self._share_review_metadata(task, row, "pending", error=""))
        return StageResult.complete("自有分享即时验证通过，进入 115 异步审核观察期；源文件暂不清理", metadata)

    @staticmethod
    def _canonical_manifest(row: dict[str, Any]) -> dict[str, Any]:
        try:
            manifest = json.loads(row.get("canonical_manifest_json") or "{}")
        except (TypeError, ValueError):
            manifest = {}
        return manifest if isinstance(manifest, dict) else {}

    def _stage_share_sync_submitted(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        own_code = str(task.metadata.get("own_share_code") or row.get("own_share_code") or "").strip()
        own_pwd = str(task.metadata.get("own_share_receive_code") or row.get("own_share_receive_code") or "").strip()
        if not own_code:
            return StageResult.failed("缺少自有分享码", error_type="own_share_missing")
        operation_key = f"{operation_scope(task)}:cms_share_sync:{own_code}"
        operation = self.task_store.find_operation(int(task.id), operation_key)
        if row.get("share_sync_status") != "submitted" or operation is not None:
            if operation is None:
                waiting_task = self._pending_cms_share_sync_task(task)
                if waiting_task:
                    return StageResult.defer(
                        "等待上一条 CMS 分享同步完成",
                        5,
                        {
                            "submission_id": int(row["id"]),
                            "share_sync_wait_task_id": waiting_task.id,
                        },
                    )
                operation = self.task_store.prepare_operation(
                    int(task.id),
                    operation_key,
                    "cms_share_sync",
                    {
                        "share_code": own_code,
                        "receive_code": own_pwd,
                        "cid": self.self_share_config.cms_cid,
                        "local_path": self.self_share_config.cms_local_path,
                    },
                )
            if operation.status == "prepared":
                started = self.task_store.start_operation(int(task.id), operation_key)
                operation = started or self.task_store.find_operation(int(task.id), operation_key)
                if started is not None:
                    response = self.cms.add_share115_sync_task(
                        str(operation.request.get("share_code") or own_code),
                        str(operation.request.get("receive_code") or own_pwd),
                        cid=str(operation.request.get("cid") or self.self_share_config.cms_cid),
                        local_path=str(operation.request.get("local_path") or self.self_share_config.cms_local_path),
                    )
                    completed = self.task_store.complete_operation(
                        int(task.id),
                        operation_key,
                        response if isinstance(response, dict) else {},
                    )
                    operation = completed or self.task_store.find_operation(int(task.id), operation_key)
            if operation is None:
                raise RuntimeError("CMS share sync operation disappeared")
            if operation.status == "started":
                uncertain = self.task_store.mark_operation_uncertain(
                    int(task.id),
                    operation_key,
                    "CMS share sync result was not persisted",
                )
                operation = uncertain or self.task_store.find_operation(int(task.id), operation_key)
            if operation.status not in {"succeeded", "uncertain"}:
                return StageResult.needs_action(
                    "CMS 分享同步结果无法安全重试，请人工检查",
                    {
                        "submission_id": int(row["id"]),
                        "cms_share_sync_outcome": "unknown",
                    },
                )
            sync_outcome = "submitted" if operation.status == "succeeded" else "unknown"
            row = self.store.update_self_share(
                int(row["id"]),
                workflow_phase="share_sync_submitted",
                share_sync_status="submitted",
            ) or row
        else:
            sync_outcome = str(task.metadata.get("cms_share_sync_outcome") or "submitted")
        return StageResult.complete(
            "已提交 CMS 分享同步" if sync_outcome == "submitted" else "CMS 分享同步结果未知，转入 STRM 结果核对",
            {
                "submission_id": int(row["id"]),
                "share_sync_status": row.get("share_sync_status") or "submitted",
                "share_sync_wait_task_id": "",
                "cms_share_sync_outcome": sync_outcome,
            },
        )

    def _pending_cms_share_sync_task(self, task):
        return self.task_store.find_pending_stage(TaskStage.STRM_READY, exclude_task_id=task.id)

    def _stage_strm_ready(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        recognition = self._recognition_from_row(row)
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        source = find_self_share_strm_source_dir(self.self_share_config, row, recognition, share_name)
        if not source and task.metadata.get("direct_file_share"):
            source = self._prepare_direct_file_share_strm(task, row)
        metadata = {
            "submission_id": int(row["id"]),
            "category": final_category_for_move(row, recognition),
            "recognition": recognition,
        }
        if not source:
            folder_name = str(row.get("own_share_file_name") or "").strip()
            if folder_name:
                cms_category = category_from_existing_library_folder(
                    self.move_config,
                    {"file_name": folder_name},
                )
                if cms_category:
                    recognition["category"] = cms_category
                    recognition["category_status"] = "self_share_resolved"
                    if hasattr(self.store, "update_category"):
                        row = self.store.update_category(int(row["id"]), cms_category, "selected") or row
                    if hasattr(self.store, "update_recognition"):
                        row = self.store.update_recognition(int(row["id"]), recognition, "self_share_resolved") or row
                    metadata["category"] = cms_category
                    metadata["recognition"] = recognition
            return StageResult.defer(
                "等待自有分享 STRM 源目录生成",
                min(self.self_share_config.auto_organize_retry_seconds or 30, 5),
                metadata,
            )
        restored = restore_canonical_strm_paths(source, row)
        if restored:
            metadata["canonical_strm_paths_restored"] = restored
        metadata["source_path"] = str(source)
        issue = validate_self_share_strm_source(source, row)
        if issue:
            share_sync_status = str(
                row.get("share_sync_status") or task.metadata.get("share_sync_status") or ""
            ).strip().lower()
            if (
                share_sync_status in {"submitted", "restore_submitted"}
                and "直链" not in issue
            ):
                return StageResult.defer(
                    "等待自有分享 STRM 生成",
                    min(self.self_share_config.auto_organize_retry_seconds or 30, 5),
                    metadata,
                )
            if hasattr(self.store, "update_move"):
                self.store.update_move(
                    int(row["id"]),
                    "error",
                    source_path=str(source),
                    category_final=str(metadata.get("category") or ""),
                    error=issue,
                )
            return StageResult.failed(issue, error_type="invalid_strm_source", metadata=metadata)
        if not task.metadata.get("share_playback_validated") and hasattr(self.cms, "probe_strm_url"):
            # os.walk(followlinks=False) so a directory symlink cannot pull a
            # playback URL from outside the source folder.
            strm_files = sorted(
                base_path / name
                for base, _dirnames, filenames in os.walk(source, followlinks=False)
                for base_path in [Path(base)]
                for name in filenames
                if name.lower().endswith(".strm")
            )
            try:
                strm_url = strm_files[0].read_text(encoding="utf-8", errors="replace").strip() if strm_files else ""
                playback_ok = bool(strm_url and self.cms.probe_strm_url(strm_url))
            except CmsSharePlaybackUnavailableError as exc:
                metadata["share_playback_error"] = str(exc)
                return StageResult.needs_action(
                    "CMS 获取分享直连失败，可能处于 115 风控；已停止自动探测，请稍后重试当前阶段",
                    metadata,
                )
            except Exception:
                LOG.debug("Self-share STRM playback probe failed", exc_info=True)
                playback_ok = False
            if not playback_ok:
                return StageResult.defer("等待自有分享 STRM 播放验证", 15, metadata)
            metadata["share_playback_validated"] = True
        return StageResult.complete("已找到并验证自有分享 STRM 源目录", metadata)

    def _stage_cms_delete_settled(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        cleanup_file_id = str(row.get("cleanup_file_id") or "").strip()
        dest_id = str(row.get("own_share_file_id") or task.metadata.get("own_share_file_id") or "").strip()
        if (
            str(row.get("cleanup_status") or "").lower() == "deleted"
            and cleanup_file_id
            and cleanup_file_id != dest_id
            and self.cms_cloud_index
            and self.cms_cloud_index.has_file_id(cleanup_file_id)
        ):
            return StageResult.defer(
                "等待 CMS 清理源目录同步完成",
                min(self.self_share_config.auto_organize_retry_seconds or 30, 5),
                {"submission_id": int(row["id"]), "cleanup_file_id": cleanup_file_id},
            )
        return StageResult.complete(
            "CMS 源目录清理状态已稳定",
            {"submission_id": int(row["id"]), "cleanup_file_id": cleanup_file_id, "cms_delete_settled": True},
        )

    def _stage_moved(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        recognition = self._recognition_from_row(row)
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        source = find_self_share_strm_source_dir(self.self_share_config, row, recognition, share_name)
        if str(row.get("move_status") or "").lower() == "moved":
            metadata = self._move_metadata(row, task.metadata)
            dest_path = str(metadata.get("dest_path") or "").strip()
            if not (source and dest_path and dest_missing_source_strms(source, Path(dest_path))):
                if self._strm_destination_ready(dest_path, row, task.metadata):
                    metadata.update(self._request_emby_refresh_once(task, dest_path))
                    return StageResult.complete("STRM 已移动到媒体库", metadata)
                return self._restore_missing_moved_destination(task, row, metadata)
        category = final_category_for_move(row, recognition)
        existing_category = "" if has_authoritative_category(row, recognition) else category_from_existing_library_match(self.move_config, row, recognition, share_name)
        if existing_category and existing_category != category:
            category = existing_category
            row = self.store.update_category(int(row["id"]), category, "selected") or row
        move_config = move_config_for_workflow_source(self.move_config, source, self.self_share_config)
        canonical_name = str(self._canonical_manifest(row).get("root_name") or row.get("own_share_file_name") or "").strip()
        plan = plan_strm_move(source, category, move_config, destination_name=canonical_name)
        metadata = {
            "submission_id": int(row["id"]),
            "source_path": str(plan.source_path) if plan.source_path else "",
            "dest_path": str(plan.dest_path) if plan.dest_path else "",
            "category": category,
        }
        if plan.metadata:
            metadata.update(plan.metadata)
        if is_move_plan_retryable(plan):
            return StageResult.defer(
                plan.reason,
                self.self_share_config.auto_organize_retry_seconds or 30,
                metadata,
            )
        moved_row = merge_self_share_strm_folder(plan, self.store, row, move_config)
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
            metadata.update(self._request_emby_refresh_once(task, str(metadata.get("dest_path") or "")))
            return StageResult.complete("STRM 已移动到媒体库", metadata)
        error = str(moved_row.get("move_error") or plan.reason or "STRM 移动失败")
        return StageResult.failed(error, error_type="strm_move_failed", metadata=metadata)

    def _stage_emby_confirmed(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if str(row.get("emby_status") or "").lower() == "confirmed":
            if not self.emby or not getattr(self.emby, "enabled", False):
                return StageResult.complete("Emby 已确认入库", self._emby_metadata(row))
            recognition = self._recognition_from_row(row)
            share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
            recognition.setdefault("share_name", share_name)
            match = self._find_emby_match_for_moved_dest(recognition, row, task.metadata)
            if match:
                return StageResult.complete("Emby 已确认入库", self._emby_metadata(row))
            updated = self.store.update_emby(int(row["id"]), "pending") or row
            return StageResult.defer(
                "等待 Emby 确认入库",
                self._emby_confirmation_retry_seconds(task),
                {"submission_id": int(row["id"]), "recognition": recognition, "emby_status": updated.get("emby_status")},
            )
        if not self.emby or not getattr(self.emby, "enabled", False):
            return StageResult.needs_action("Emby 确认未启用", {"submission_id": int(row["id"])})
        if str(row.get("move_status") or "").lower() == "moved":
            metadata = self._move_metadata(row, task.metadata)
            dest_path = str(metadata.get("dest_path") or "").strip()
            if dest_path and not self._strm_destination_ready(dest_path, row, task.metadata):
                return self._restore_missing_moved_destination(task, row, metadata)
        recognition = self._recognition_from_row(row)
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        recognition.setdefault("share_name", share_name)
        match = self._find_emby_match_for_moved_dest(recognition, row, task.metadata)
        if not match:
            return StageResult.defer(
                "等待 Emby 确认入库",
                self._emby_confirmation_retry_seconds(task),
                {"submission_id": int(row["id"]), "recognition": recognition},
            )
        send_emby_confirmed(self.telegram, self.chat_id, self.store, row, match, self.emby, cleanup_client=None)
        updated = self.store.find_by_id(int(row["id"])) or row
        return StageResult.complete("Emby 已确认入库", self._emby_metadata(updated))

    def _stage_cleaned(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if str(row.get("move_status") or "").lower() != "moved":
            return StageResult.needs_action("等待 STRM 移动确认后再清理", {"submission_id": int(row["id"])})
        if str(row.get("emby_status") or "").lower() != "confirmed":
            return StageResult.needs_action("等待 Emby 确认后再清理", {"submission_id": int(row["id"])})
        if not self.cleanup_client:
            updated = row
            if hasattr(self.store, "update_cleanup"):
                updated = self.store.update_cleanup(int(row["id"]), "skipped", error="disabled") or row
            metadata = self._cleanup_metadata(updated)
            metadata["cleanup_status"] = "skipped"
            metadata["cleanup_error"] = "disabled"
            return StageResult.complete("清理已跳过（未启用）", metadata)
        if not str(row.get("own_share_code") or "").strip():
            return StageResult.failed("缺少自有分享码，拒绝清理 115 转存源", error_type="own_share_missing")
        if not str(row.get("own_share_file_id") or "").strip():
            return StageResult.failed("缺少自有分享文件夹 ID", error_type="own_share_file_missing")
        if str(row.get("move_status") or "").lower() == "moved":
            metadata = self._move_metadata(row, task.metadata)
            dest_path = str(metadata.get("dest_path") or "").strip()
            if dest_path and not self._strm_destination_ready(dest_path, row, task.metadata):
                return self._restore_missing_moved_destination(task, row, metadata, terminal=True)
        review_metadata: dict[str, Any] = {}
        if str(row.get("cleanup_status") or "").lower() != "deleted":
            review_status, review_metadata, review_message, review_delay = self._advance_share_review(task, row)
            if review_status == "invalid":
                updated = self.store.update_self_share(
                    int(row["id"]),
                    share_validation_status="invalid",
                    share_validation_error=str(review_metadata.get("share_review_error") or review_message),
                ) or row
                metadata = self._cleanup_metadata(updated)
                metadata.update(review_metadata)
                return StageResult.needs_action(
                    "自有分享在异步审核中已变为不可用，源文件已保留，停止自动改名和重建",
                    metadata,
                )
            if review_status != "passed":
                metadata = self._cleanup_metadata(row)
                metadata.update(review_metadata)
                return StageResult.defer(review_message, review_delay, metadata)
            file_id = str(row.get("own_share_file_id") or "").strip()
            if self._is_library_dest_cleanup_target(task, row, file_id):
                row = self.store.update_cleanup(int(row["id"]), "deleted", file_id="") or row
            else:
                parent_id = self._delete_source_parent_id(task, row, file_id)
                if not parent_id:
                    metadata = self._cleanup_metadata(row)
                    metadata.update(review_metadata)
                    return StageResult.needs_action("缺少 115 转存源父目录，无法安全核对删除结果", metadata)
                recovery = self._journaled_delete(task, file_id, parent_id, "delete_source")
                if recovery is not None:
                    return recovery
                row = self.store.update_cleanup(int(row["id"]), "deleted", file_id=file_id) or row
        residue_result, residue_count = self._cleanup_residue_operations(task, row)
        if residue_result is not None:
            return residue_result
        metadata = self._cleanup_metadata(row)
        metadata.update(review_metadata)
        if residue_count:
            metadata["residue_deleted_count"] = residue_count
        return StageResult.complete("115 转存源已删除，自有分享保留", metadata)

    def _delete_source_parent_id(self, task, row: dict[str, Any], file_id: str) -> str:
        return source_delete_parent_id(task, row, file_id)

    @staticmethod
    def _is_missing_delete_target_error(exc: Exception) -> bool:
        return _is_missing_delete_target_error(exc)

    def _journaled_delete(
        self,
        task,
        file_id: str,
        parent_id: str,
        operation_type: str,
    ) -> StageResult | None:
        return journaled_delete_file(
            self.task_store,
            task,
            self.cleanup_client,
            file_id,
            parent_id,
            operation_type,
            now=self._now(),
        )

    def _cleanup_residue_operations(self, task, row: dict[str, Any]) -> tuple[StageResult | None, int]:
        parent_ids = self.self_share_config.source_cleanup_parent_ids or set()
        if not parent_ids or not hasattr(self.cleanup_client, "find_source_residue_files"):
            return None, 0
        manifest_key = f"{operation_scope(task)}:delete_residue:manifest"
        manifest = self.task_store.find_operation(int(task.id), manifest_key)
        if manifest is None:
            recognition = self._recognition_from_row(row)
            share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
            try:
                discovered = self.cleanup_client.find_source_residue_files(
                    recognition,
                    share_name,
                    parent_ids,
                    excluded_file_ids={str(row.get("own_share_file_id") or "").strip()},
                    min_update_time=float(row.get("created_at") or 0),
                )
            except Exception as exc:
                return StageResult.defer(f"115 接收残留暂时无法扫描：{exc}", _DELETE_RECOVERY_RETRY_SECONDS), 0
            files = sorted(
                (
                    {
                        "file_id": str(item.get("file_id") or "").strip(),
                        "parent_id": str(item.get("parent_id") or "").strip(),
                    }
                    for item in discovered
                    if str(item.get("file_id") or "").strip() and str(item.get("parent_id") or "").strip()
                ),
                key=lambda item: (item["parent_id"], item["file_id"]),
            )
            if not files:
                return None, 0
            manifest = self.task_store.prepare_operation(
                int(task.id),
                manifest_key,
                "delete_residue",
                {"files": files},
            )
        files = manifest.request.get("files")
        files = files if isinstance(files, list) else []
        deleted = 0
        for item in files:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("file_id") or "").strip()
            parent_id = str(item.get("parent_id") or "").strip()
            if not file_id or not parent_id:
                continue
            recovery = self._journaled_delete(task, file_id, parent_id, "delete_residue")
            if recovery is not None:
                return recovery, deleted
            deleted += 1
        if manifest.status == "prepared":
            started = self.task_store.start_operation(int(task.id), manifest_key)
            manifest = started or self.task_store.find_operation(int(task.id), manifest_key)
        if manifest is not None and manifest.status == "started":
            self.task_store.complete_operation(
                int(task.id),
                manifest_key,
                {"deleted_count": deleted},
            )
        return None, deleted

    @staticmethod
    def _positive_timestamp(value: Any) -> float:
        try:
            timestamp = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return timestamp if timestamp > 0 else 0.0

    @staticmethod
    def _review_checkpoints(value: Any) -> list[int]:
        if not isinstance(value, (list, tuple)):
            return []
        checkpoints: list[int] = []
        for item in value:
            try:
                checkpoint = int(item)
            except (TypeError, ValueError):
                continue
            if checkpoint > 0 and checkpoint not in checkpoints:
                checkpoints.append(checkpoint)
        return checkpoints

    @staticmethod
    def _as_bool_flag(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes"}

    def _share_review_metadata(
        self,
        task,
        row: dict[str, Any],
        status: str,
        *,
        error: str | None = None,
        checks: list[int] | None = None,
        last_at: float | None = None,
        next_at: float | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        created_at = self._positive_timestamp(task.metadata.get("share_created_at"))
        if created_at:
            metadata["share_created_at"] = created_at
        existing_checks = self._review_checkpoints(task.metadata.get("share_review_checks"))
        metadata["share_review_status"] = status
        metadata["share_review_checks"] = checks if checks is not None else existing_checks
        metadata["share_review_last_at"] = (
            last_at if last_at is not None else task.metadata.get("share_review_last_at") or 0
        )
        metadata["share_review_next_at"] = (
            next_at if next_at is not None else task.metadata.get("share_review_next_at") or 0
        )
        metadata["share_review_error"] = (
            str(task.metadata.get("share_review_error") or "") if error is None else str(error)
        )
        return metadata

    def _read_share_review_state(self, own_code: str, own_pwd: str) -> dict[str, Any]:
        if hasattr(self.p115, "list_own_share_states"):
            states = self.p115.list_own_share_states(limit=100)
            if isinstance(states, dict) and own_code in states:
                return dict(states[own_code])
        return self.p115.inspect_share(own_code, own_pwd)

    def _advance_share_review(self, task, row: dict[str, Any]) -> tuple[str, dict[str, Any], str, float]:
        policy = resolve_self_share_review_policy(self.task_store, self.self_share_config)
        if policy.mode == "off":
            metadata = self._share_review_metadata(
                task,
                row,
                "passed",
                checks=[],
                next_at=0,
                error="",
            )
            return "passed", metadata, "115 异步审核观察已关闭", 0

        created_at = self._positive_timestamp(task.metadata.get("share_created_at"))
        if not created_at:
            metadata = self._share_review_metadata(
                task,
                row,
                "unknown",
                error="缺少自有分享创建时间，拒绝自动清理历史源文件",
            )
            return "invalid", metadata, "缺少自有分享创建时间，拒绝自动清理历史源文件", 0

        configured = policy.checkpoints
        checks = self._review_checkpoints(task.metadata.get("share_review_checks"))
        checks = [checkpoint for checkpoint in configured if checkpoint in checks]
        next_checkpoint = next((checkpoint for checkpoint in configured if checkpoint not in checks), None)
        now = self._now()
        if next_checkpoint is None:
            metadata = self._share_review_metadata(
                task,
                row,
                "passed",
                checks=checks,
                last_at=self._positive_timestamp(task.metadata.get("share_review_last_at")),
                next_at=0,
                error="",
            )
            return "passed", metadata, "115 异步审核观察期已通过", 0

        checkpoint_at = created_at + next_checkpoint
        if now < checkpoint_at:
            delay = max(1.0, checkpoint_at - now)
            metadata = self._share_review_metadata(
                task,
                row,
                "pending",
                checks=checks,
                next_at=checkpoint_at,
                error="",
            )
            remaining = max(1, int(delay))
            return "pending", metadata, f"等待 115 异步审核检查点（还需约 {remaining} 秒），源文件暂不清理", delay

        own_code = str(row.get("own_share_code") or "").strip()
        own_pwd = str(row.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        try:
            status = self._read_share_review_state(own_code, own_pwd)
        except P115ShareUnavailableError as exc:
            metadata = self._share_review_metadata(task, row, "invalid", error=str(exc), checks=checks, next_at=0)
            return "invalid", metadata, str(exc), 0
        except RuntimeError as exc:
            retry_after = max(60, min(300, int(self.self_share_config.review_list_cache_seconds)))
            metadata = self._share_review_metadata(
                task,
                row,
                "unknown",
                error=str(exc),
                checks=checks,
                next_at=now + retry_after,
            )
            return "unknown", metadata, "115 分享状态暂时无法确认，等待风控冷却或网络恢复，源文件暂不清理", retry_after

        share_state = str(status.get("share_state") or "").strip().lower()
        have_vio_file = self._as_bool_flag(status.get("have_vio_file"))
        if status.get("available") is False:
            metadata = self._share_review_metadata(task, row, "invalid", error="115 分享不可用", checks=checks, next_at=0)
            return "invalid", metadata, "115 分享不可用", 0
        if have_vio_file or (share_state and share_state not in {"0", "1", "true"}):
            reason = "115 标记 have_vio_file" if have_vio_file else f"115 分享状态不可用：{share_state or '未知'}"
            metadata = self._share_review_metadata(task, row, "invalid", error=reason, checks=checks, next_at=0)
            return "invalid", metadata, reason, 0
        if not share_state:
            retry_after = max(60, min(300, int(self.self_share_config.review_list_cache_seconds)))
            metadata = self._share_review_metadata(
                task,
                row,
                "unknown",
                error="115 未返回明确分享状态",
                checks=checks,
                next_at=now + retry_after,
            )
            return "unknown", metadata, "115 未返回明确分享状态，源文件暂不清理", retry_after

        checks = [*checks, next_checkpoint]
        next_remaining = next((checkpoint for checkpoint in configured if checkpoint not in checks), None)
        if next_remaining is None:
            metadata = self._share_review_metadata(
                task,
                row,
                "passed",
                checks=checks,
                last_at=now,
                next_at=0,
                error="",
            )
            return "passed", metadata, "115 异步审核观察期已通过", 0
        next_at = created_at + next_remaining
        metadata = self._share_review_metadata(
            task,
            row,
            "pending",
            checks=checks,
            last_at=now,
            next_at=next_at,
            error="",
        )
        delay = max(1.0, next_at - now)
        return "pending", metadata, f"115 异步审核检查通过，等待下一个检查点（还需约 {int(delay)} 秒）", delay

    def _recognition_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            recognition = json.loads(row.get("recognition_json") or "{}")
        except Exception:
            recognition = {}
        return recognition if isinstance(recognition, dict) else {}

    @staticmethod
    def _is_gone_share_source_error(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return (
            "已被移动或删除" in str(exc or "")
            or "不存在或已转移" in str(exc or "")
            or "moved or deleted" in message
        )

    @staticmethod
    def _direct_file_share_details(task) -> tuple[str, str, str, str]:
        folder = task.metadata.get("organized_folder")
        folder = folder if isinstance(folder, dict) else {}
        file_id = str(folder.get("direct_file_id") or task.metadata.get("direct_file_share_file_id") or "").strip()
        relative_path = str(folder.get("direct_relative_path") or task.metadata.get("direct_file_share_relative_path") or "").strip()
        file_name = str(folder.get("direct_file_name") or task.metadata.get("direct_file_share_file_name") or "").strip()
        parent_id = str(folder.get("direct_parent_id") or task.metadata.get("direct_file_share_parent_id") or "").strip()
        relative = Path(relative_path)
        if not file_id or not relative_path or relative.is_absolute() or ".." in relative.parts:
            return "", "", "", ""
        return file_id, relative_path, file_name, parent_id

    def _prepare_direct_file_share_strm(self, task, row: dict[str, Any]) -> Path | None:
        _file_id, relative_path, _file_name, _parent_id = self._direct_file_share_details(task)
        folder_name = str(row.get("own_share_file_name") or "").strip()
        own_share_code = str(row.get("own_share_code") or "").strip()
        receive_code = str(row.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE).strip() or DEFAULT_OWN_SHARE_RECEIVE_CODE
        if not relative_path or not folder_name or not own_share_code:
            return None
        trusted_root = safe_resolve(self.self_share_config.strm_root)
        folder_name = _single_relative_directory_name(folder_name)
        if not folder_name:
            return None
        source_root = safe_resolve(trusted_root / folder_name)
        relative = Path(relative_path)
        target = safe_resolve(source_root / relative)
        if not is_relative_to(source_root, trusted_root) or not is_relative_to(target, trusted_root):
            return None
        if target.exists():
            return source_root
        marker = f"/s/{own_share_code}_{receive_code}_"
        candidates: list[Path] = []
        if self.self_share_config.strm_root.exists():
            # os.walk(followlinks=False): symlink loops / links outside the
            # trusted root must not expand the candidate scan.
            for base, _dirnames, filenames in os.walk(self.self_share_config.strm_root, followlinks=False):
                base_path = Path(base)
                for name in filenames:
                    if not name.lower().endswith(".strm"):
                        continue
                    candidate = safe_resolve(base_path / name)
                    if not is_relative_to(candidate, trusted_root):
                        continue
                    try:
                        text = candidate.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if marker in text:
                        candidates.append(candidate)
        if not candidates:
            return None
        source_file = max(candidates, key=lambda path: path.stat().st_mtime)
        if not is_relative_to(source_file, trusted_root) or not is_relative_to(target, trusted_root):
            return None
        if safe_resolve(source_file) == target:
            return source_root
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
            source_file.unlink()
        except OSError:
            return None
        return source_root

    def _should_reuse_received_self_share_state(
        self,
        row: dict[str, Any] | None,
        task_metadata: dict[str, Any] | None = None,
    ) -> bool:
        if self._is_pending_update_run(task_metadata):
            return False
        if not self._has_received_self_share_state(row):
            return False
        if not (task_metadata or {}).get("force_reprocess"):
            return True
        return self._has_downstream_self_share_state(row)

    @staticmethod
    def _is_pending_update_run(task_metadata: dict[str, Any] | None = None) -> bool:
        metadata = task_metadata or {}
        try:
            requested = int(metadata.get("update_requested_run") or 0)
            received = int(metadata.get("update_received_run") or 0)
        except (TypeError, ValueError):
            return False
        return requested > received

    def _has_received_self_share_state(self, row: dict[str, Any] | None) -> bool:
        if not row or row.get("workflow_mode") != "self_share_sync":
            return False
        phase = str(row.get("workflow_phase") or "").strip()
        if phase in {
            "received",
            "received_to_pending",
            "auto_organize_submitted",
            "organized_found",
            "share_alias_prepared",
            "own_share_created",
            "share_validated",
            "share_sync_submitted",
        }:
            return True
        return self._has_downstream_self_share_state(row)

    def _has_downstream_self_share_state(self, row: dict[str, Any] | None) -> bool:
        if not row:
            return False
        phase = str(row.get("workflow_phase") or "").strip()
        if phase in {"organized_found", "share_alias_prepared", "own_share_created", "share_validated", "share_sync_submitted"}:
            return True
        return any(
            row.get(key)
            for key in (
                "own_share_file_id",
                "own_share_code",
                "share_alias_name",
                "share_validation_status",
                "share_sync_status",
                "source_path",
                "dest_path",
                "move_status",
                "emby_status",
                "cleanup_status",
            )
        )

    def _received_metadata(self, row: dict[str, Any], task_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = {
            "submission_id": int(row["id"]),
            "received_title": str(row.get("title") or ""),
            "received_file_ids": [],
        }
        receive_target_cid = str((task_metadata or {}).get("receive_target_cid") or "").strip()
        if receive_target_cid:
            metadata["receive_target_cid"] = receive_target_cid
        return metadata

    def _has_persisted_category_suggestion(self, recognition: dict[str, Any]) -> bool:
        status = str(recognition.get("category_status") or "").strip()
        if status == "openai_suggested":
            return True
        return bool(recognition.get("category_suggestion") and status not in {"selected", "self_share_resolved", "tmdb_resolved", "tmdb_search_resolved", "openai_confident"})

    def _needs_action_recognition_result(self, row: dict[str, Any], recognition: dict[str, Any]):
        status = str(recognition.get("category_status") or "needs_action").strip()
        if hasattr(self.store, "update_recognition"):
            row = self.store.update_recognition(int(row["id"]), recognition, status) or row
        message = f"CMS 未能确定分类：{format_task_label(row)}\n"
        suggestion = str(recognition.get("category_suggestion") or "").strip()
        if suggestion:
            confidence = as_float(recognition.get("openai_confidence"), 0.0)
            message += f"OpenAI建议：{suggestion}（置信度 {confidence:.2f}）\n"
        reason = str(recognition.get("openai_reason") or "").strip()
        if reason:
            message += f"理由：{reason[:80]}\n"
        message += "请选择分类："
        self.telegram.send_message(
            self.chat_id,
            message,
            reply_markup=category_keyboard(int(row["id"])),
        )
        return StageResult.needs_action(
            "等待人工确认分类",
            {"submission_id": int(row["id"]), "recognition": recognition},
        )

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
            "share_alias_name": row.get("share_alias_name"),
            "share_alias_level": row.get("share_alias_level"),
            "own_share_code": row.get("own_share_code"),
            "own_share_receive_code": row.get("own_share_receive_code"),
            "own_share_url": row.get("own_share_url"),
            "cleanup_status": row.get("cleanup_status"),
            "cleanup_file_id": row.get("cleanup_file_id"),
            "cleanup_error": row.get("cleanup_error"),
            "share_validation_status": row.get("share_validation_status"),
            "share_validation_error": row.get("share_validation_error"),
        }

    def _move_metadata(self, row: dict[str, Any], task_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        task_metadata = task_metadata or {}
        source_path = str(row.get("source_path") or task_metadata.get("source_path") or "")
        dest_path = str(row.get("dest_path") or task_metadata.get("dest_path") or "")
        metadata = {
            "submission_id": int(row["id"]),
            "source_path": str(safe_resolve(Path(source_path))) if source_path else "",
            "dest_path": str(safe_resolve(Path(dest_path))) if dest_path else "",
            "category": str(row.get("category_final") or task_metadata.get("category") or ""),
        }
        if task_metadata.get("emby_refresh_requested") is not None:
            metadata["emby_refresh_requested"] = bool(task_metadata.get("emby_refresh_requested"))
        if task_metadata.get("emby_refresh_library"):
            metadata["emby_refresh_library"] = str(task_metadata.get("emby_refresh_library") or "")
        if task_metadata.get("emby_refresh_error"):
            metadata["emby_refresh_error"] = str(task_metadata.get("emby_refresh_error") or "")
        return metadata

    @staticmethod
    def _required_direct_file_share_relative_path(task_metadata: dict[str, Any] | None = None) -> str:
        metadata = task_metadata or {}
        if not metadata.get("direct_file_share"):
            return ""
        return str(metadata.get("direct_file_share_relative_path") or "").strip()

    def _restore_missing_moved_destination(self, task, row: dict[str, Any], metadata: dict[str, Any], terminal: bool = False):
        required_relative_path = self._required_direct_file_share_relative_path(task.metadata)
        if required_relative_path:
            self._prepare_direct_file_share_strm(task, row)
        restore_status, restore_metadata = restore_missing_self_share_library_folder(
            self.store,
            self.cms,
            row,
            self.self_share_config,
            self.move_config,
            required_relative_path=required_relative_path,
        )
        delay = self.self_share_config.auto_organize_retry_seconds or 30
        if restore_status in {"restore_submitted", "waiting_source"}:
            return StageResult.defer(
                "目标 STRM 被 CMS 同步删除或不是当前自有分享，等待自有分享 STRM 重新生成",
                delay,
                restore_metadata,
            )
        if restore_status == "restored":
            restored_dest = str(restore_metadata.get("dest_path") or metadata.get("dest_path") or "")
            restore_metadata.update(self._request_emby_refresh_once(task, restored_dest, force=True))
            return StageResult.defer(
                "目标 STRM 被 CMS 同步删除，已用自有分享 STRM 恢复",
                delay,
                restore_metadata,
            )
        if restore_status == "move_failed":
            return StageResult.defer(
                "等待已移动 STRM 目标目录恢复",
                delay,
                restore_metadata or metadata,
            )
        if terminal:
            return StageResult.needs_action(
                "任务状态已完成，但目标 STRM 未通过自有分享校验，请检查媒体库目录",
                metadata,
            )
        if restore_status not in {"skipped", "error"}:
            return StageResult.defer(
                "等待已移动 STRM 目标目录恢复",
                delay,
                metadata,
            )
        restore_metadata = restore_metadata or metadata
        return StageResult.failed(
            "自有分享 STRM 目标恢复失败，请检查源目录和媒体库目录",
            error_type="self_share_restore_failed",
            metadata=restore_metadata,
        )

    def _strm_destination_ready(
        self,
        dest_path: str,
        row: dict[str, Any],
        task_metadata: dict[str, Any] | None = None,
    ) -> bool:
        if not dest_path:
            return False
        dest = safe_resolve(Path(dest_path))
        return not validate_self_share_strm_destination(
            dest,
            row,
            self._required_direct_file_share_relative_path(task_metadata),
        )

    def _request_emby_refresh_once(self, task, dest_path: str, force: bool = False) -> dict[str, Any]:
        if not dest_path or (task.metadata.get("emby_refresh_requested") and not force):
            return {}
        if not self.emby or not getattr(self.emby, "enabled", False):
            return {}
        if not hasattr(self.emby, "refresh_library_for_path"):
            return {}
        try:
            library_name = self.emby.refresh_library_for_path(dest_path)
        except Exception as exc:
            LOG.warning("Failed to request Emby library refresh for %s: %s", dest_path, exc)
            return {"emby_refresh_error": str(exc)[:200]}
        metadata = {"emby_refresh_requested": True}
        if library_name:
            metadata["emby_refresh_library"] = str(library_name)
        return metadata

    def _emby_confirmation_retry_seconds(self, task) -> int:
        message = "等待 Emby 确认入库"
        previous_count = 0
        if task.metadata.get("_defer_stage") == TaskStage.EMBY_CONFIRMED.value and task.metadata.get("_defer_message") == message:
            try:
                previous_count = int(task.metadata.get("_defer_count") or 0)
            except (TypeError, ValueError):
                previous_count = 0
        if previous_count < 4:
            return 5
        return self.self_share_config.auto_organize_retry_seconds or 30

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
        already_confirmed = str(
            row.get("emby_status") or (task_metadata or {}).get("emby_status") or ""
        ).strip().lower() == "confirmed"
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
            if self._emby_match_in_moved_dest(item, row, task_metadata) or already_confirmed:
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


def cleanup_own_share_source(store: Any, row: dict[str, Any], cleanup_client: Any | None) -> tuple[dict[str, Any], str]:
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


def send_emby_confirmed(
    telegram: Any,
    chat_id: int | str,
    store: Any,
    row: dict[str, Any],
    item: dict,
    emby: Any | None = None,
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
        lines.append("115 转存源未自动清理：旧兼容路径不执行异步审核后的删除，请启用 TaskRunner")
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
