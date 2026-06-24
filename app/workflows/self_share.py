from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.cms import CmsClient
from app.clients.p115 import P115WebClient, category_for_115_parent_id
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
    cleanup_direct_strm_for_organized_folder,
    find_self_share_strm_source_dir,
    has_strm_file,
    merge_self_share_strm_folder,
    move_config_for_workflow_source,
    plan_strm_move,
    restore_missing_self_share_library_folder,
    validate_self_share_strm_source,
)
from app.models import TaskStage
from app.task_runner import StageResult

LOG = logging.getLogger("cms-tg-ingest")
OPENAI_CATEGORY_LABELS = ["华语电影", "欧美电影", "亚洲电影", "动漫电影", "国产电视", "外国电视", "番剧", "纪录片"]


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
    text = str(exc or "")
    return any(token in text for token in ("限制接收", "被限制接收", "操作过于频繁", "风控"))


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
        return self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))

    def _stage_received(self, task):
        if not self.self_share_config.enabled:
            return StageResult.failed("自分享工作流未启用", error_type="self_share_disabled")
        if not self.receive_cid:
            return StageResult.failed("缺少 115 接收目录 ID", error_type="missing_receive_cid")

        existing = self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))
        if self._has_received_self_share_state(existing):
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
        find_kwargs = {
            "excluded_parent_ids": self.self_share_config.excluded_parent_ids or set(),
            "min_update_time": float(row.get("created_at") or 0),
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
        if folder is None:
            folder = self.p115.find_organized_folder(recognition, title, **find_kwargs)
        if not folder:
            tmdb_resolved, tmdb_should_prompt = apply_tmdb_search_resolution(recognition, title, self.tmdb_resolver)
            if not tmdb_should_prompt and str(tmdb_resolved.get("tmdb_id") or "").strip():
                recognition = dict(tmdb_resolved)
                folder = self.p115.find_organized_folder(recognition, title, **find_kwargs)
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
                {"submission_id": int(row["id"])},
            )
        existing_library_category = category_from_existing_library_folder(self.move_config, folder)
        if existing_library_category and not str(folder.get("category") or "").strip():
            folder = dict(folder)
            folder["category"] = existing_library_category
        direct_strm_removed = cleanup_direct_strm_for_organized_folder(self.move_config, folder)
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
                "share_name": recognition.get("share_name") or share_name,
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
            tmdb_resolved, tmdb_should_prompt = apply_tmdb_hint_resolution(recognition, share_name, self.tmdb_resolver)
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

    def _stage_own_share_created(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        file_id = str(task.metadata.get("own_share_file_id") or row.get("own_share_file_id") or "").strip()
        if not file_id:
            return StageResult.failed("缺少自有分享文件夹 ID", error_type="own_share_file_missing")
        created = False
        if not row.get("own_share_code"):
            share = self.p115.create_long_share(file_id)
            row = self.store.update_self_share(
                int(row["id"]),
                workflow_phase="own_share_created",
                own_share_code=share.get("share_code"),
                own_share_receive_code=share.get("receive_code"),
                own_share_url=share.get("share_url"),
            ) or row
            created = True
        if self.cleanup_client:
            row, cleanup_line = cleanup_own_share_source(self.store, row, self.cleanup_client)
            if str(row.get("cleanup_status") or "").lower() == "error":
                return StageResult.failed(
                    str(row.get("cleanup_error") or cleanup_line or "115 转存源删除失败"),
                    error_type="cleanup_failed",
                    metadata=self._own_share_metadata(row),
                )
        message = "已创建自有 115 分享" if created else "已存在自有 115 分享"
        return StageResult.complete(message, self._own_share_metadata(row))

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
                removed = cleanup_direct_strm_for_organized_folder(
                    self.move_config,
                    {"file_name": folder_name},
                )
                metadata["direct_strm_removed"] = removed
            return StageResult.defer(
                "等待自有分享 STRM 源目录生成",
                min(self.self_share_config.auto_organize_retry_seconds or 30, 5),
                metadata,
            )
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
        return StageResult.complete("已找到自有分享 STRM 源目录", metadata)

    def _stage_moved(self, task):
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if str(row.get("move_status") or "").lower() == "moved":
            metadata = self._move_metadata(row, task.metadata)
            dest_path = str(metadata.get("dest_path") or "").strip()
            if self._strm_destination_ready(dest_path):
                metadata.update(self._request_emby_refresh_once(task, dest_path))
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
        existing_category = "" if has_authoritative_category(row, recognition) else category_from_existing_library_match(self.move_config, row, recognition, share_name)
        if existing_category and existing_category != category:
            category = existing_category
            row = self.store.update_category(int(row["id"]), category, "selected") or row
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
            if dest_path and not self._strm_destination_ready(dest_path):
                restore_status, restore_metadata = restore_missing_self_share_library_folder(
                    self.store,
                    self.cms,
                    row,
                    self.self_share_config,
                    self.move_config,
                )
                if restore_status in {"restore_submitted", "waiting_source"}:
                    return StageResult.defer(
                        "目标 STRM 被 CMS 同步删除，等待自有分享 STRM 重新生成",
                        self.self_share_config.auto_organize_retry_seconds or 30,
                        restore_metadata,
                    )
                if restore_status == "restored":
                    return StageResult.defer(
                        "目标 STRM 被 CMS 同步删除，已用自有分享 STRM 恢复",
                        self.self_share_config.auto_organize_retry_seconds or 30,
                        restore_metadata,
                    )
                return StageResult.defer(
                    "等待已移动 STRM 目标目录恢复",
                    self.self_share_config.auto_organize_retry_seconds or 30,
                    metadata,
                )
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
        if str(row.get("move_status") or "").lower() == "moved":
            metadata = self._move_metadata(row, task.metadata)
            dest_path = str(metadata.get("dest_path") or "").strip()
            if dest_path and not self._strm_destination_ready(dest_path):
                restore_status, restore_metadata = restore_missing_self_share_library_folder(
                    self.store,
                    self.cms,
                    row,
                    self.self_share_config,
                    self.move_config,
                )
                if restore_status in {"restore_submitted", "waiting_source"}:
                    return StageResult.defer(
                        "目标 STRM 被 CMS 同步删除，等待自有分享 STRM 重新生成",
                        self.self_share_config.auto_organize_retry_seconds or 30,
                        restore_metadata,
                    )
                if restore_status == "restored":
                    return StageResult.defer(
                        "目标 STRM 被 CMS 同步删除，已用自有分享 STRM 恢复",
                        self.self_share_config.auto_organize_retry_seconds or 30,
                        restore_metadata,
                    )
                return StageResult.needs_action(
                    "任务状态已完成，但目标 STRM 不存在，请检查媒体库目录",
                    metadata,
                )
        if str(row.get("cleanup_status") or "").lower() == "deleted":
            return StageResult.complete("115 转存源已删除，自有分享保留", self._cleanup_metadata(row))
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
            "own_share_code": row.get("own_share_code"),
            "own_share_receive_code": row.get("own_share_receive_code"),
            "own_share_url": row.get("own_share_url"),
            "cleanup_status": row.get("cleanup_status"),
            "cleanup_file_id": row.get("cleanup_file_id"),
            "cleanup_error": row.get("cleanup_error"),
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

    def _request_emby_refresh_once(self, task, dest_path: str) -> dict[str, Any]:
        if not dest_path or task.metadata.get("emby_refresh_requested"):
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
