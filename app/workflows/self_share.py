from __future__ import annotations

import json
import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.cms import CmsClient, CmsSharePlaybackUnavailableError
from app.cms_cloud_index import CmsCloudDataIndex
from app.clients.p115 import (
    P115RiskControlError,
    P115ShareUnavailableError,
    P115WebClient,
    category_for_115_parent_id,
    is_p115_risk_control_message,
    normalize_cloud_status,
    p115_file_id,
    p115_file_name,
    p115_is_folder,
    validate_cloud_output,
)
from app.config import MovePlan, SelfShareConfig, default_library_roots, is_relative_to, safe_resolve
from app.media.classify import (
    apply_tmdb_hint_resolution,
    apply_tmdb_search_resolution,
    expected_task_tmdb_id,
    extract_tmdb_id_from_name,
    final_category_for_move,
    is_recognition_uncertain,
    item_tmdb_id,
    map_category_label,
    media_type_for_category,
    normalize_text,
    user_movie_category_bucket,
)
from app.media.strm import (
    category_from_existing_library_folder,
    category_from_existing_library_match,
    find_recent_direct_library_strm_source_dir,
    find_self_share_strm_source_dir,
    has_strm_file,
    merge_self_share_strm_folder,
    move_config_for_workflow_source,
    plan_strm_move,
    restore_canonical_strm_paths,
    restore_missing_self_share_library_folder,
    validate_self_share_strm_destination,
    validate_self_share_strm_source,
)
from app.models import TaskStage, TaskStatus
from app.task_runner import StageResult

LOG = logging.getLogger("cms-tg-ingest")
OPENAI_CATEGORY_LABELS = ["华语电影", "欧美电影", "亚洲电影", "动漫电影", "国产电视", "外国电视", "番剧", "纪录片"]
VIDEO_SUFFIXES = {".mkv", ".mp4", ".ts", ".iso", ".avi", ".mov", ".wmv", ".m2ts"}


@dataclass(frozen=True)
class _ShareKey:
    share_code: str
    receive_code: str


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default



def is_115_receive_restricted_error(exc: Exception) -> bool:
    if isinstance(exc, P115RiskControlError):
        return True
    text = str(exc or "")
    return is_p115_risk_control_message(text)


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
    if file_id and file_id in {str(value) for value in (task_metadata.get("received_file_ids") or []) if str(value)}:
        return True
    return bool(receive_cid and parent_id == str(receive_cid).strip())


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
    def __init__(self, config: SelfShareConfig, cms: CmsClient, p115: P115WebClient, store: Any):
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

    def run_stage(self, task):
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

    def _stage_cloud_downloading(self, task):
        if not self.self_share_config.enabled:
            return StageResult.failed("自分享工作流未启用", error_type="self_share_disabled")
        if not self.receive_cid:
            return StageResult.failed("缺少 115 接收目录 ID", error_type="missing_receive_cid")

        metadata = dict(task.metadata)
        info_hash = str(metadata.get("cloud_info_hash") or "").strip()
        task_id = str(metadata.get("cloud_task_id") or "").strip()
        started_at = float(metadata.get("cloud_started_at") or 0)
        if not info_hash and not task_id:
            submitted = self.p115.cloud_download_add(task.url, self.receive_cid)
            info_hash = str(submitted.get("info_hash") or "").strip()
            task_id = str(submitted.get("task_id") or "").strip()
            started_at = self._now()
            metadata.update(
                {
                    "cloud_info_hash": info_hash,
                    "cloud_task_id": task_id,
                    "cloud_started_at": started_at,
                    "cloud_target_cid": self.receive_cid,
                    "cloud_status": normalize_cloud_status(submitted),
                }
            )
            return StageResult.defer(
                "已提交 115 云下载，等待完成",
                self.self_share_config.cloud_poll_seconds,
                metadata,
            )

        if started_at and self._now() - started_at >= self.self_share_config.cloud_timeout_seconds:
            return StageResult.failed(
                "115 云下载超时，未进入后续整理和清理阶段",
                error_type="cloud_download_timeout",
                metadata=metadata,
            )

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

        output = validate_cloud_output(status, self.receive_cid)
        row = self.store.upsert_submission(
            _ShareKey(task.share_code, task.receive_code),
            task.url,
            "received",
            title=output.get("file_name") or task.title or task.share_code,
        )
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            workflow_phase="cloud_downloaded_to_pending",
        ) or row
        metadata.update(
            {
                "submission_id": int(row["id"]),
                "received_title": output.get("file_name") or task.title or task.share_code,
                "received_file_ids": [output["file_id"]],
                "cloud_output_file_id": output["file_id"],
                "cloud_output_parent_id": output["parent_id"],
                "cloud_output_name": output.get("file_name") or "",
            }
        )
        return StageResult.complete("115 云下载完成，已进入 CMS 整理", metadata)

    def _submission_row(self, task) -> dict[str, Any] | None:
        submission_id = task.metadata.get("submission_id") or task.submission_id
        if submission_id not in (None, ""):
            return self.store.find_by_id(int(submission_id))
        return self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))

    def _stage_received(self, task):
        if not self.self_share_config.enabled:
            return StageResult.failed("自分享工作流未启用", error_type="self_share_disabled")
        if not self.receive_cid:
            return StageResult.failed("缺少 115 接收目录 ID", error_type="missing_receive_cid")

        existing = self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))
        if self._should_reuse_received_self_share_state(existing, task.metadata):
            return StageResult.complete("已接收 115 分享到待整理", self._received_metadata(existing))

        try:
            received = self.p115.receive_share_to_cid(task.share_code, task.receive_code, self.receive_cid)
        except RuntimeError as exc:
            if is_115_receive_restricted_error(exc):
                return StageResult.needs_action(
                    "115 接收被限制，已停止自动重试；请稍后恢复后手动重试或先手动转存。",
                    {"share_code": task.share_code},
                )
            raise
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
        }
        if self._is_pending_update_run(task.metadata):
            metadata["update_received_run"] = int(task.metadata.get("update_requested_run") or 0)
        return StageResult.complete(
            "已接收 115 分享到待整理",
            metadata,
        )

    def _stage_organizing(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        workflow_phase = str(row.get("workflow_phase") or "")
        folder = None
        if row.get("own_share_file_id") and row.get("own_share_file_name"):
            folder = {
                "file_id": row.get("own_share_file_id"),
                "file_name": row.get("own_share_file_name"),
                "parent_id": self._organized_parent_id(task, self._recognition_from_row(row)),
            }
        elif workflow_phase not in {"auto_organize_submitted", "organized_found", "own_share_created", "share_sync_submitted"}:
            self.cms.run_auto_organize()
            row = self.store.update_self_share(int(row["id"]), workflow_phase="auto_organize_submitted") or row
        recognition = self._recognition_from_row(row)
        title = str(row.get("title") or task.title or task.share_code)
        excluded_parent_ids = set(self.self_share_config.excluded_parent_ids or set())
        if self.receive_cid:
            excluded_parent_ids.add(self.receive_cid)
        min_update_time = float(row.get("created_at") or 0)
        try:
            update_started_at = float(task.metadata.get("update_started_at") or 0)
        except (TypeError, ValueError):
            update_started_at = 0
        if update_started_at:
            min_update_time = max(min_update_time, update_started_at - 5)
        find_kwargs = {
            "excluded_parent_ids": excluded_parent_ids,
            "min_update_time": min_update_time,
        }
        if self.self_share_config.organized_scan_parent_ids:
            find_kwargs.update(
                {
                    "scan_parent_ids": self.self_share_config.organized_scan_parent_ids,
                    "category_names": set(self.self_share_config.parent_cid_category_map.values())
                    if self.self_share_config.parent_cid_category_map
                    else set(default_library_roots()),
                }
            )
        direct_strm_removed = 0
        direct_signal = None
        cloud_output_name = str(task.metadata.get("cloud_output_name") or "").strip()
        if self.cms_cloud_index and cloud_output_name:
            indexed_folder = self.cms_cloud_index.folder_for_cloud_output_name(
                cloud_output_name,
                started_at=as_float(task.metadata.get("cloud_started_at"), 0),
            )
            if indexed_folder:
                folder = indexed_folder
        if folder and self.cms_cloud_index and folder.get("direct_file_id") and not folder.get("direct_relative_path"):
            folder_tmdb = extract_tmdb_id_from_name(str(folder.get("file_name") or ""))
            if folder_tmdb:
                direct_signal = find_recent_direct_library_strm_source_dir(
                    self.move_config,
                    row,
                    {**recognition, "tmdb_id": folder_tmdb},
                    title,
                )
                if direct_signal:
                    direct_source, _direct_category = direct_signal
                    direct_folder = self.cms_cloud_index.folder_for_direct_strm(direct_source, folder_tmdb)
                    if direct_folder and str(direct_folder.get("direct_file_id") or "") == str(folder.get("direct_file_id") or ""):
                        relative_path = str(direct_folder.get("direct_relative_path") or "").strip()
                        if relative_path:
                            folder = dict(folder)
                            folder["direct_relative_path"] = relative_path
        if folder is None:
            direct_signal = find_recent_direct_library_strm_source_dir(self.move_config, row, recognition, title)
            if direct_signal:
                direct_source, direct_category = direct_signal
                direct_recognition = dict(recognition)
                direct_tmdb = extract_tmdb_id_from_name(str(direct_source))
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
            folder = self.p115.find_organized_folder(recognition, title, **find_kwargs)
        if folder and is_unverified_received_source(folder, task.metadata, self.receive_cid):
            folder = None
        if not folder:
            tmdb_resolved, tmdb_should_prompt = apply_tmdb_search_resolution(recognition, title, self.tmdb_resolver)
            if not tmdb_should_prompt and str(tmdb_resolved.get("tmdb_id") or "").strip():
                recognition = dict(tmdb_resolved)
                folder = self.p115.find_organized_folder(recognition, title, **find_kwargs)
                if folder and is_unverified_received_source(folder, task.metadata, self.receive_cid):
                    folder = None
                category = str(recognition.get("category") or "").strip()
                if category and hasattr(self.store, "update_category"):
                    row = self.store.update_category(int(row["id"]), category, "selected") or row
                if hasattr(self.store, "update_recognition"):
                    row = self.store.update_recognition(
                        int(row["id"]),
                        recognition,
                        str(recognition.get("category_status") or "tmdb_search_resolved"),
                    ) or row
        if not folder:
            return StageResult.defer(
                "等待 CMS 整理完成",
                self.self_share_config.auto_organize_retry_seconds or 30,
                {"submission_id": int(row["id"]), "direct_strm_removed": direct_strm_removed},
            )
        existing_library_category = category_from_existing_library_folder(self.move_config, folder)
        if existing_library_category and not str(folder.get("category") or "").strip():
            folder = dict(folder)
            folder["category"] = existing_library_category
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
        return StageResult.complete(
            "已找到 CMS 整理后的 115 文件夹",
            {"submission_id": int(row["id"]), "organized_folder": folder, "direct_strm_removed": direct_strm_removed},
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
        if is_unverified_received_source(folder, task.metadata, self.receive_cid):
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

    def _stage_share_alias_prepared(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        file_id = str(task.metadata.get("own_share_file_id") or row.get("own_share_file_id") or "").strip()
        canonical_name = str(row.get("own_share_file_name") or "").strip()
        if not file_id or not canonical_name:
            return StageResult.failed("缺少 CMS 整理后的文件夹", error_type="organized_folder_missing")
        alias_name = str(row.get("share_alias_name") or "").strip()
        if alias_name:
            return StageResult.complete("分享目录别名已准备", self._own_share_metadata(row))
        short_id = hashlib.sha1(f"{task.id}:{file_id}".encode("utf-8")).hexdigest()[:8]
        alias_name = f"asset-{task.id}-{short_id}"
        recognition = self._recognition_from_row(row)
        manifest = {
            "version": 1,
            "root_name": canonical_name,
            "alias_name": alias_name,
            "category": final_category_for_move(row, recognition),
            "tmdb_id": expected_task_tmdb_id(recognition, row),
            "entries": [],
        }
        try:
            self.p115.rename_file(file_id, alias_name)
        except RuntimeError as exc:
            direct_file_id, direct_relative_path = self._direct_file_share_details(task)
            if not direct_file_id or not self._is_gone_share_source_error(exc):
                raise
            metadata = self._own_share_metadata(row)
            metadata.update(
                {
                    "direct_file_share_fallback": True,
                    "direct_file_share_file_id": direct_file_id,
                    "direct_file_share_relative_path": direct_relative_path,
                }
            )
            return StageResult.complete("CMS 整理目录已移动，保留单集文件分享兜底", metadata)
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="share_alias_prepared",
            canonical_manifest_json=json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            share_alias_name=alias_name,
            share_alias_level=1,
            share_validation_status="pending",
            share_validation_error="",
        ) or row
        return StageResult.complete("已准备中性分享目录名", self._own_share_metadata(row))

    def _stage_own_share_created(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        file_id = str(task.metadata.get("own_share_file_id") or row.get("own_share_file_id") or "").strip()
        if not file_id:
            return StageResult.failed("缺少自有分享文件夹 ID", error_type="own_share_file_missing")
        folder = task.metadata.get("organized_folder")
        if isinstance(folder, dict) and is_unverified_received_source(folder, task.metadata, self.receive_cid):
            return StageResult.needs_action(
                "等待可验证的 CMS 整理后源目录，当前 115 ID 仍是接收/分享快照，拒绝创建自有分享",
                self._own_share_metadata(row) | {"own_share_file_id": ""},
            )
        if file_id in {str(value) for value in (task.metadata.get("received_file_ids") or []) if str(value)}:
            return StageResult.needs_action(
                "等待可验证的 CMS 整理后源目录，当前 115 ID 仍是接收/分享快照，拒绝创建自有分享",
                self._own_share_metadata(row) | {"own_share_file_id": ""},
            )
        created = False
        direct_file_share = False
        direct_relative_path = ""
        if not row.get("own_share_code"):
            try:
                share = self.p115.create_long_share(file_id)
            except RuntimeError as exc:
                direct_file_id, direct_relative_path = self._direct_file_share_details(task)
                if not direct_file_id or not self._is_gone_share_source_error(exc):
                    raise
                if not hasattr(self.store, "replace_self_share_source_file_id"):
                    raise
                row = self.store.replace_self_share_source_file_id(int(row["id"]), direct_file_id) or row
                share = self.p115.create_long_share(direct_file_id)
                direct_file_share = True
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
        if direct_file_share:
            metadata.update(
                {
                    "direct_file_share": True,
                    "direct_file_share_file_id": direct_file_id,
                    "direct_file_share_relative_path": direct_relative_path,
                }
            )
        return StageResult.complete(message, metadata)

    def _stage_share_validated(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if int(row.get("share_alias_level") or 0) >= 2 and str(row.get("share_validation_status") or "") == "aliasing_files":
            row = self._complete_level_two_alias(task, row)
            return StageResult.defer("已完成文件别名并重建分享，等待 115 状态稳定", 5, self._own_share_metadata(row))
        own_code = str(row.get("own_share_code") or "").strip()
        own_pwd = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
        if not own_code:
            return StageResult.failed("缺少自有分享码", error_type="own_share_missing")
        try:
            status = self.p115.inspect_share(own_code, own_pwd)
        except P115ShareUnavailableError as exc:
            if int(row.get("share_alias_level") or 0) < 2:
                upgraded = self._prepare_level_two_alias(task, row)
                return StageResult.defer("分享被 115 拒绝，已升级文件别名并重建分享", 5, self._own_share_metadata(upgraded))
            row = self.store.update_self_share(
                int(row["id"]),
                share_validation_status="invalid",
                share_validation_error=str(exc)[:200],
            ) or row
            return StageResult.needs_action("中性文件名分享仍被 115 拒绝，已保留现有 STRM", self._own_share_metadata(row))
        have_vio_file = bool(status.get("have_vio_file"))
        if have_vio_file and int(row.get("share_alias_level") or 0) < 2:
            upgraded = self._prepare_level_two_alias(task, row)
            return StageResult.defer("分享含风险标记，已升级文件别名并重建分享", 5, self._own_share_metadata(upgraded))
        validation_status = "warning" if have_vio_file else "valid"
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_phase="share_validated",
            share_validation_status=validation_status,
            share_validation_error="" if not have_vio_file else "115 标记 have_vio_file，实际播放仍需验证",
        ) or row
        if self.cleanup_client:
            row, cleanup_line = cleanup_own_share_source(self.store, row, self.cleanup_client)
            if str(row.get("cleanup_status") or "").lower() == "error":
                return StageResult.failed(
                    str(row.get("cleanup_error") or cleanup_line or "115 转存源删除失败"),
                    error_type="cleanup_failed",
                    metadata=self._own_share_metadata(row),
                )
        metadata = self._own_share_metadata(row)
        metadata["share_have_vio_file"] = have_vio_file
        if str(row.get("cleanup_status") or "").lower() == "deleted" and not task.metadata.get("cleanup_sync_requested"):
            self.cms.run_auto_organize()
            metadata["cleanup_sync_requested"] = True
        message = "自有分享验证通过" if not have_vio_file else "自有分享当前有效，存在 115 风险标记"
        return StageResult.complete(message, metadata)

    def _prepare_level_two_alias(self, task, row: dict[str, Any]) -> dict[str, Any]:
        manifest = self._canonical_manifest(row)
        if not manifest.get("entries"):
            entries = []
            queue: list[tuple[str, Path]] = [(str(row.get("own_share_file_id") or ""), Path())]
            list_calls = 0
            file_index = 0
            while queue:
                parent_id, relative_parent = queue.pop(0)
                list_calls += 1
                if list_calls > 25:
                    raise RuntimeError("115 分享目录层级过多，停止文件别名扫描")
                for item in sorted(self.p115.list_files(parent_id, limit=500), key=p115_file_name):
                    item_id = p115_file_id(item)
                    name = p115_file_name(item)
                    if not item_id or not name:
                        continue
                    canonical_path = relative_parent / name
                    if p115_is_folder(item):
                        queue.append((item_id, canonical_path))
                        continue
                    if Path(name).suffix.lower() not in VIDEO_SUFFIXES:
                        continue
                    file_index += 1
                    episode = re.search(r"(?i)(S\d{1,2}E\d{1,3})", name)
                    episode_part = f"-{episode.group(1).upper()}" if episode else ""
                    alias_name = f"asset-{task.id}-{file_index:03d}{episode_part}{Path(name).suffix.lower()}"
                    entries.append(
                        {
                            "file_id": item_id,
                            "canonical_path": canonical_path.as_posix(),
                            "alias_path": (relative_parent / alias_name).as_posix(),
                        }
                    )
            manifest["entries"] = entries
        row = self.store.update_self_share(
            int(row["id"]),
            canonical_manifest_json=json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            share_alias_level=2,
            share_validation_status="aliasing_files",
        ) or row
        return self._complete_level_two_alias(task, row)

    def _complete_level_two_alias(self, task, row: dict[str, Any]) -> dict[str, Any]:
        manifest = self._canonical_manifest(row)
        for entry in manifest.get("entries") or []:
            file_id = str(entry.get("file_id") or "").strip()
            alias_name = Path(str(entry.get("alias_path") or "")).name
            if file_id and alias_name:
                self.p115.rename_file(file_id, alias_name)
        share = self.p115.create_long_share(str(row.get("own_share_file_id") or ""))
        return self.store.update_self_share(
            int(row["id"]),
            workflow_phase="own_share_created",
            own_share_code=share.get("share_code"),
            own_share_receive_code=share.get("receive_code"),
            own_share_url=share.get("share_url"),
            share_sync_status="",
            share_validation_status="pending",
            share_validation_error="",
        ) or row

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
        if row.get("share_sync_status") != "submitted":
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
            {
                "submission_id": int(row["id"]),
                "share_sync_status": row.get("share_sync_status") or "submitted",
                "share_sync_wait_task_id": "",
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
            strm_files = sorted(source.rglob("*.strm"))
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
        cleanup_file_id = str(row.get("cleanup_file_id") or row.get("own_share_file_id") or "").strip()
        if (
            str(row.get("cleanup_status") or "").lower() == "deleted"
            and cleanup_file_id
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
        if str(row.get("move_status") or "").lower() == "moved":
            metadata = self._move_metadata(row, task.metadata)
            dest_path = str(metadata.get("dest_path") or "").strip()
            if self._strm_destination_ready(dest_path, row, task.metadata):
                metadata.update(self._request_emby_refresh_once(task, dest_path))
                return StageResult.complete("STRM 已移动到媒体库", metadata)
            return self._restore_missing_moved_destination(task, row, metadata)
        recognition = self._recognition_from_row(row)
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        source = find_self_share_strm_source_dir(self.self_share_config, row, recognition, share_name)
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
        if str(row.get("cleanup_status") or "").lower() == "deleted":
            return StageResult.complete("115 转存源已删除，自有分享保留", self._cleanup_metadata(row))
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

    @staticmethod
    def _is_gone_share_source_error(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return (
            "已被移动或删除" in str(exc or "")
            or "不存在或已转移" in str(exc or "")
            or "moved or deleted" in message
        )

    @staticmethod
    def _direct_file_share_details(task) -> tuple[str, str]:
        folder = task.metadata.get("organized_folder")
        folder = folder if isinstance(folder, dict) else {}
        file_id = str(folder.get("direct_file_id") or task.metadata.get("direct_file_share_file_id") or "").strip()
        relative_path = str(folder.get("direct_relative_path") or task.metadata.get("direct_file_share_relative_path") or "").strip()
        relative = Path(relative_path)
        if not file_id or not relative_path or relative.is_absolute() or ".." in relative.parts:
            return "", ""
        return file_id, relative_path

    def _prepare_direct_file_share_strm(self, task, row: dict[str, Any]) -> Path | None:
        _file_id, relative_path = self._direct_file_share_details(task)
        folder_name = str(row.get("own_share_file_name") or "").strip()
        own_share_code = str(row.get("own_share_code") or "").strip()
        receive_code = str(row.get("own_share_receive_code") or "1212").strip() or "1212"
        if not relative_path or not folder_name or not own_share_code:
            return None
        source_root = safe_resolve(self.self_share_config.strm_root / folder_name)
        relative = Path(relative_path)
        target = safe_resolve(source_root / relative)
        if target.exists():
            return source_root
        marker = f"/s/{own_share_code}_{receive_code}_"
        candidates: list[Path] = []
        if self.self_share_config.strm_root.exists():
            for strm_path in self.self_share_config.strm_root.rglob("*.strm"):
                try:
                    text = strm_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if marker in text:
                    candidates.append(strm_path)
        if not candidates:
            return None
        source_file = max(candidates, key=lambda path: path.stat().st_mtime)
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
        if terminal:
            return StageResult.needs_action(
                "任务状态已完成，但目标 STRM 未通过自有分享校验，请检查媒体库目录",
                metadata,
            )
        return StageResult.defer(
            "等待已移动 STRM 目标目录恢复",
            delay,
            metadata,
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
