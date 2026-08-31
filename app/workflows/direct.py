from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import MoveConfig, SelfShareConfig, is_relative_to, is_under_any_root, safe_resolve
from app.media.classify import (
    apply_tmdb_hint_resolution,
    apply_tmdb_search_resolution,
    extract_tmdb_id_from_name,
    item_tmdb_id,
    media_type_for_category,
    parse_recognition_json,
)
from app.media.strm import (
    execute_strm_move,
    find_recent_direct_library_strm_source_dir,
    find_strm_source_dir,
    is_directory_stable,
    iter_strm_files,
    plan_strm_move,
    validate_direct_strm_source,
    validate_self_share_strm_file,
)
from app.models import TaskSnapshot, TaskStage
from app.strm_mode import effective_task_strm_mode
from app.task_runner import StageResult
from app.task_store import operation_scope
from app.workflows.self_share import (
    BridgeSelfShareTaskWorkflow,
    attach_row_checkpoint,
    emby_parent_label,
    match_emby_item,
)


_CMS_FAILURE_MARKERS = ("failed", "error", "失败", "timeout", "超时", "cancel")
_CMS_SUCCESS_MARKERS = ("done", "finish", "success", "complete", "完成", "成功")
_CMS_NO_TASK_ID_STATUSES = {"submitted_no_task_id", "accepted_without_task_id"}
_CMS_ACCEPTED_NUMERIC_STATUSES = {"1"}
_CMS_FAILED_NUMERIC_STATUSES = {"2"}


@dataclass(frozen=True)
class _ShareKey:
    share_code: str
    receive_code: str


def _detail_data(detail: dict[str, Any]) -> dict[str, Any]:
    data = detail.get("data")
    return data if isinstance(data, dict) else {}


def _detail_value(detail: dict[str, Any], *keys: str) -> Any:
    data = _detail_data(detail)
    for item in (detail, data):
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
    return None


def _extract_cms_task_info(response: dict[str, Any]) -> tuple[str, str]:
    data = _detail_data(response)
    task_id = _detail_value(response, "id", "task_id", "taskId")
    title = _detail_value(response, "name", "title", "share_name", "file_name")
    return str(task_id or "").strip(), str(title or "").strip()


def _cms_status(detail: dict[str, Any]) -> str:
    return str(_detail_value(detail, "status", "state", "task_status") or "").strip().lower()


def _cms_status_outcome(detail: dict[str, Any]) -> str:
    status = _cms_status(detail)
    if status in _CMS_ACCEPTED_NUMERIC_STATUSES:
        return "success"
    if status in _CMS_FAILED_NUMERIC_STATUSES:
        return "failed"
    if any(marker in status for marker in _CMS_FAILURE_MARKERS):
        return "failed"
    if any(marker in status for marker in _CMS_SUCCESS_MARKERS):
        return "success"
    return "pending"


def _cms_recognition(detail: dict[str, Any], existing: dict[str, Any], title: str) -> dict[str, Any]:
    data = _detail_data(detail)
    tmdb_info = data.get("tmdb_info") if isinstance(data.get("tmdb_info"), dict) else {}
    video_info = data.get("video_info") if isinstance(data.get("video_info"), dict) else {}
    recognition = dict(existing)
    category = _detail_value(detail, "category", "category_final", "category_choice")
    tmdb_id = _detail_value(detail, "tmdb_id") or tmdb_info.get("tmdb_id") or tmdb_info.get("id")
    media_type = _detail_value(detail, "type") or tmdb_info.get("type") or video_info.get("type")
    if str(media_type or "").strip().lower() not in {"movie", "tv"}:
        media_type = ""
    resolved_title = _detail_value(detail, "name", "title", "share_name", "file_name") or title
    if not tmdb_id:
        tmdb_id = extract_tmdb_id_from_name(str(resolved_title or ""))
    if category:
        recognition["category"] = str(category).strip()
    if tmdb_id:
        recognition["tmdb_id"] = str(tmdb_id).strip()
    if media_type:
        recognition["type"] = str(media_type).strip()
    if resolved_title:
        recognition["title"] = str(resolved_title).strip()
        recognition["share_name"] = str(resolved_title).strip()
    if recognition.get("category"):
        recognition["category_status"] = "cms_resolved"
        recognition["ok"] = True
    return recognition


class DirectTaskWorkflow:
    """TaskRunner workflow for CMS submissions that produce direct-link STRM files."""

    def __init__(
        self,
        cms: Any,
        store: Any,
        move_config: MoveConfig,
        *,
        task_store: Any,
        emby: Any | None = None,
        now: Callable[[], float] | None = None,
        emby_retry_seconds: int = 15,
    ):
        self.cms = cms
        self.store = store
        self.task_store = task_store
        self.move_config = move_config
        self.emby = emby
        self.now = now or time.time
        self.emby_retry_seconds = max(1, int(emby_retry_seconds))

    def run_stage(self, task: TaskSnapshot) -> StageResult:
        if effective_task_strm_mode(task) != "direct":
            return StageResult.failed(
                "共享任务不能由直链工作流处理",
                error_type="strm_mode_mismatch",
                metadata={"strm_mode": effective_task_strm_mode(task)},
            )
        if task.current_stage == TaskStage.RECEIVED:
            result = self._stage_received(task)
        elif task.current_stage == TaskStage.ORGANIZING:
            result = self._stage_organizing(task)
        elif task.current_stage == TaskStage.RECOGNIZING:
            result = self._stage_recognizing(task)
        elif task.current_stage == TaskStage.STRM_READY:
            result = self._stage_strm_ready(task)
        elif task.current_stage == TaskStage.MOVED:
            result = self._stage_moved(task)
        elif task.current_stage == TaskStage.EMBY_CONFIRMED:
            result = self._stage_emby_confirmed(task)
        else:
            result = StageResult.failed("直链工作流不支持此阶段", error_type="unsupported_stage")
        return attach_row_checkpoint(result, self._submission_row(task))

    def _submission_row(self, task: TaskSnapshot) -> dict[str, Any] | None:
        submission_id = task.metadata.get("submission_id") or task.submission_id
        row = None
        if submission_id not in (None, ""):
            row = self.store.find_by_id(int(submission_id))
        if row is None:
            row = self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))
        facts = {}
        if self.task_store is not None and hasattr(self.task_store, "workflow_facts"):
            facts = self.task_store.workflow_facts(int(task.id))
        if not facts:
            return row
        if row is None:
            if facts.get("dest_path") or facts.get("move_status") or facts.get("cms_task_id"):
                return facts
            return None
        merged = dict(row)
        for key, value in facts.items():
            if key == "id" or value in (None, "", [], {}, "{}", "[]"):
                continue
            merged[key] = value
        return merged

    @staticmethod
    def _recognition(row: dict[str, Any]) -> dict[str, Any]:
        return parse_recognition_json(row)

    def _submission_metadata(self, row: dict[str, Any], **extra: Any) -> dict[str, Any]:
        metadata = {
            "submission_id": int(row["id"]),
            "strm_mode": "direct",
            "direct_strm": True,
        }
        metadata.update({key: value for key, value in extra.items() if value is not None})
        return metadata

    @staticmethod
    def _accepted_without_task_id(task: TaskSnapshot, row: dict[str, Any]) -> bool:
        if task.metadata.get("cms_submission_accepted") is True:
            return True
        return str(row.get("status") or "").strip().lower() in _CMS_NO_TASK_ID_STATUSES

    @staticmethod
    def _cms_task_id(detail: dict[str, Any]) -> str:
        value = _detail_value(detail, "id", "task_id", "taskId")
        return str(value or "").strip()

    def _lookup_cms_task(self, task: TaskSnapshot) -> dict[str, Any]:
        lookup = getattr(self.cms, "get_share_down_by_key", None)
        if not callable(lookup):
            return {}
        try:
            result = lookup(_ShareKey(task.share_code, task.receive_code))
        except Exception:
            return {}
        return result if isinstance(result, dict) else {}

    def _stage_received(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        key = _ShareKey(task.share_code, task.receive_code)
        operation_key = f"{operation_scope(task)}:cms_direct_submit:direct:{task.share_code}"
        operation_request = {
            "strm_mode": "direct",
            "share_code": task.share_code,
            "receive_code": task.receive_code,
            "url": task.url,
        }
        operation = self.task_store.find_operation(int(task.id), operation_key)
        if operation is not None:
            operation = self.task_store.prepare_operation(
                int(task.id),
                operation_key,
                "cms_direct_submit",
                operation_request,
            )
        cms_task_id = str(
            (row or {}).get("cms_task_id") or task.metadata.get("cms_task_id") or ""
        ).strip()
        title = str((row or {}).get("title") or task.title or task.share_code).strip()
        recovered_existing = False
        if (
            not cms_task_id
            and not (row and self._accepted_without_task_id(task, row))
            and (operation is None or operation.status != "succeeded")
        ):
            existing = self._lookup_cms_task(task)
            existing_status = _cms_status(existing)
            if (
                existing
                and not any(marker in existing_status for marker in _CMS_FAILURE_MARKERS)
                and existing_status != "2"
            ):
                cms_task_id = self._cms_task_id(existing)
                title = str(_detail_value(existing, "name", "title", "share_name", "file_name") or title).strip()
                recovered_existing = bool(cms_task_id)
                if recovered_existing and operation is not None and operation.status == "started":
                    completed = self.task_store.complete_operation(int(task.id), operation_key, existing)
                    operation = completed or self.task_store.find_operation(int(task.id), operation_key)
        if (
            row
            and not cms_task_id
            and not self._accepted_without_task_id(task, row)
            and str(row.get("status") or "").lower() not in {"failed", "error"}
            and (operation is None or operation.status == "prepared")
        ):
            return StageResult.failed(
                "已有 CMS 提交记录但缺少任务 ID",
                error_type="cms_task_id_missing",
                metadata=self._submission_metadata(row),
            )
        if not cms_task_id:
            if operation is not None and operation.status in {"started", "uncertain"}:
                if operation.status == "started":
                    uncertain = self.task_store.mark_operation_uncertain(
                        int(task.id),
                        operation_key,
                        "CMS direct submission result was not persisted",
                    )
                    operation = uncertain or operation
                return StageResult.needs_action(
                    "CMS 直链提交结果未持久化，禁止自动重复提交，请人工检查",
                    {
                        "strm_mode": "direct",
                        "cms_operation_key": operation_key,
                        "cms_submission_outcome": "unknown",
                    },
                )
            if row and self._accepted_without_task_id(task, row):
                return StageResult.complete(
                    "CMS 已接受（同步响应未提供任务 ID）",
                    self._submission_metadata(
                        row,
                        title=title,
                        cms_submission_accepted=True,
                        cms_task_id_optional=True,
                    ),
                )
            operation = self.task_store.prepare_operation(
                int(task.id),
                operation_key,
                "cms_direct_submit",
                operation_request,
            )
            if operation.status == "prepared":
                started = self.task_store.start_operation(int(task.id), operation_key)
                operation = started or self.task_store.find_operation(int(task.id), operation_key)
                if started is not None:
                    try:
                        response = self.cms.add_share_down(str(started.request.get("url") or task.url))
                    except Exception as exc:
                        uncertain = self.task_store.mark_operation_uncertain(
                            int(task.id),
                            operation_key,
                            str(exc),
                        )
                        operation = uncertain or self.task_store.find_operation(int(task.id), operation_key)
                        return StageResult.needs_action(
                            "CMS 直链提交结果无法确认，禁止自动重复提交，请人工检查",
                            {
                                "strm_mode": "direct",
                                "cms_operation_key": operation_key,
                                "cms_submission_outcome": "unknown",
                            },
                        )
                    completed = self.task_store.complete_operation(
                        int(task.id),
                        operation_key,
                        response if isinstance(response, dict) else {},
                    )
                    operation = completed or self.task_store.find_operation(int(task.id), operation_key)
            if operation is None:
                raise RuntimeError("CMS direct submission operation disappeared")
            if operation.status == "started":
                uncertain = self.task_store.mark_operation_uncertain(
                    int(task.id),
                    operation_key,
                    "CMS direct submission result was not persisted",
                )
                operation = uncertain or operation
            if operation.status != "succeeded":
                return StageResult.needs_action(
                    "CMS 直链提交结果无法安全恢复，禁止自动重复提交，请人工检查",
                    {
                        "strm_mode": "direct",
                        "cms_operation_key": operation_key,
                        "cms_submission_outcome": "unknown",
                    },
                )
            response = operation.result
            cms_task_id, response_title = _extract_cms_task_info(response)
            title = response_title or title
            if not cms_task_id:
                row = self.store.upsert_submission(
                    key,
                    task.url,
                    "submitted_no_task_id",
                    title=title,
                )
                return StageResult.complete(
                    "CMS 已接受（同步响应未提供任务 ID）",
                    self._submission_metadata(
                        row,
                        title=title,
                        cms_submission_accepted=True,
                        cms_task_id_optional=True,
                    ),
                )
            row = self.store.upsert_submission(
                key,
                task.url,
                "submitted",
                cms_task_id=cms_task_id,
                title=title,
            )
        elif recovered_existing or not row:
            row = self.store.upsert_submission(
                key,
                task.url,
                "submitted",
                cms_task_id=cms_task_id,
                title=title,
            )
        else:
            row = self.store.update_status(int(row["id"]), "submitted", title=title) or row
        return StageResult.complete(
            "已提交 CMS 普通同步",
            self._submission_metadata(row, cms_task_id=cms_task_id, title=title),
        )

    def _stage_organizing(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        cms_task_id = str(row.get("cms_task_id") or task.metadata.get("cms_task_id") or "").strip()
        if not cms_task_id:
            if not self._accepted_without_task_id(task, row):
                return StageResult.failed("缺少 CMS 任务 ID", error_type="cms_task_id_missing")
            detail = self._lookup_cms_task(task)
            recovered_task_id = self._cms_task_id(detail)
            if recovered_task_id:
                cms_task_id = recovered_task_id
                title = str(_detail_value(detail, "name", "title", "share_name", "file_name") or row.get("title") or task.title or "").strip()
                row = self.store.upsert_submission(
                    _ShareKey(task.share_code, task.receive_code),
                    task.url,
                    str(row.get("status") or "submitted"),
                    cms_task_id=cms_task_id,
                    title=title,
                )
            else:
                title = str(row.get("title") or task.title or task.share_code).strip()
                try:
                    detail = self.cms.recognize_media(title)
                except Exception:
                    detail = {}
                existing = self._recognition(row)
                recognition = _cms_recognition(detail, existing, title)
                if recognition.get("category") or recognition.get("tmdb_id"):
                    updated = self.store.update_recognition(
                        int(row["id"]),
                        recognition,
                        "cms_resolved",
                    ) or row
                    return StageResult.complete(
                        "CMS 整理完成（同步响应未提供任务 ID）",
                        self._submission_metadata(
                            updated,
                            title=title,
                            cms_submission_accepted=True,
                            cms_task_id_optional=True,
                            recognition=recognition,
                            category=recognition.get("category") or "",
                            tmdb_id=recognition.get("tmdb_id") or "",
                        ),
                    )
                return StageResult.defer(
                    "等待 CMS 整理完成（同步响应未提供任务 ID）",
                    15,
                    self._submission_metadata(
                        row,
                        title=title,
                        cms_submission_accepted=True,
                        cms_task_id_optional=True,
                    ),
                )
        detail = self.cms.get_share_down_detail(cms_task_id)
        status = _cms_status(detail)
        outcome = _cms_status_outcome(detail)
        title = str(_detail_value(detail, "name", "title", "share_name", "file_name") or row.get("title") or task.title or "").strip()
        updated = self.store.update_status(int(row["id"]), status or "unknown", title=title) or row
        if outcome == "failed":
            reason = str(_detail_value(detail, "msg", "message", "error", "last_error", "remark") or "CMS 整理失败")
            return StageResult.failed(
                reason,
                error_type="cms_organize_failed",
                metadata=self._submission_metadata(updated, cms_task_id=cms_task_id, title=title),
            )
        if outcome != "success":
            return StageResult.defer("等待 CMS 整理完成", 15, self._submission_metadata(updated, cms_task_id=cms_task_id, title=title))
        existing = self._recognition(updated)
        recognition = _cms_recognition(detail, existing, title)
        if recognition.get("category") or recognition.get("tmdb_id"):
            updated = self.store.update_recognition(
                int(updated["id"]),
                recognition,
                str(recognition.get("category_status") or "cms_resolved"),
            ) or updated
        return StageResult.complete(
            "CMS 整理完成",
            self._submission_metadata(
                updated,
                cms_task_id=cms_task_id,
                title=title,
                recognition=recognition,
                category=recognition.get("category") or updated.get("category_final") or "",
                tmdb_id=recognition.get("tmdb_id") or "",
            ),
        )

    def _stage_recognizing(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        recognition = self._recognition(row)
        category = str(
            row.get("category_final") or row.get("category_choice") or recognition.get("category") or ""
        ).strip()
        if not category:
            return StageResult.needs_action("CMS 尚未给出媒体分类", self._submission_metadata(row))
        recognition = dict(recognition)
        recognition["category"] = category
        if not recognition.get("type"):
            recognition["type"] = media_type_for_category(category)
        tmdb_id = str(recognition.get("tmdb_id") or row.get("tmdb_id") or task.tmdb_id or "").strip()
        if tmdb_id:
            recognition["tmdb_id"] = tmdb_id
        updated = self.store.update_recognition(int(row["id"]), recognition, "cms_resolved") or row
        return StageResult.complete(
            "已使用 CMS 媒体分类",
            self._submission_metadata(
                updated,
                recognition=recognition,
                category=category,
                tmdb_id=tmdb_id,
            ),
        )

    def _allowed_source(self, source: Path) -> bool:
        roots = [*self.move_config.source_roots, *self.move_config.library_roots.values()]
        return bool(roots) and is_under_any_root(source, roots)

    def _find_source(self, task: TaskSnapshot, row: dict[str, Any], recognition: dict[str, Any]) -> Path | None:
        persisted = str(row.get("source_path") or task.metadata.get("source_path") or "").strip()
        if persisted:
            source = safe_resolve(Path(persisted))
            if not self._allowed_source(source):
                return None
            if source.exists():
                return source
        share_name = str(row.get("title") or recognition.get("share_name") or task.title or task.share_code).strip()
        source = find_strm_source_dir(self.move_config, recognition, share_name=share_name)
        if source:
            return source
        recent = find_recent_direct_library_strm_source_dir(self.move_config, row, recognition, share_name=share_name)
        return recent[0] if recent else None

    def _source_issue(self, source: Path, recognition: dict[str, Any], row: dict[str, Any]) -> str:
        if not self._allowed_source(source):
            return "源目录不在允许范围内"
        expected_tmdb = str(recognition.get("tmdb_id") or row.get("tmdb_id") or "").strip()
        folder_tmdb = extract_tmdb_id_from_name(source.name)
        if expected_tmdb and folder_tmdb and expected_tmdb != folder_tmdb:
            return f"任务 TMDB {expected_tmdb} 与文件夹 TMDB {folder_tmdb} 不一致，阻止移动 STRM"
        return validate_direct_strm_source(source)

    def _strm_metadata(
        self,
        row: dict[str, Any],
        source: Path,
        category: str,
        *,
        locked: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        metadata = self._submission_metadata(
            row,
            source_path=str(source),
            category=category,
            **extra,
        )
        if locked:
            metadata.update(
                {
                    "strm_mode_locked": True,
                    "strm_mode_locked_at": float(self.now()),
                }
            )
        return metadata

    def _stage_strm_ready(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        recognition = self._recognition(row)
        category = str(row.get("category_final") or row.get("category_choice") or recognition.get("category") or "").strip()
        if not category:
            return StageResult.needs_action("CMS 尚未给出媒体分类", self._submission_metadata(row))
        source = self._find_source(task, row, recognition)
        if not source:
            return StageResult.defer("等待 STRM 源目录生成", 15, self._submission_metadata(row, category=category))
        source = safe_resolve(source)
        issue = self._source_issue(source, recognition, row)
        metadata = self._strm_metadata(row, source, category, locked=True)
        if issue:
            return StageResult.failed(issue, error_type="invalid_strm_source", metadata=metadata)
        if not is_directory_stable(source, self.move_config.stable_seconds):
            return StageResult.defer("STRM 源目录仍在更新", 15, metadata)
        self.store.update_move(int(row["id"]), "pending", source_path=str(source), category_final=category)
        return StageResult.complete("已找到并验证直链 STRM 源目录", metadata)

    def _stage_moved(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        recognition = self._recognition(row)
        category = str(row.get("category_final") or row.get("category_choice") or recognition.get("category") or "").strip()
        if not category:
            return StageResult.needs_action("CMS 尚未给出媒体分类", self._submission_metadata(row))
        source = self._find_source(task, row, recognition)
        if not source:
            return StageResult.defer("等待 STRM 源目录生成", 15, self._submission_metadata(row, category=category))
        source = safe_resolve(source)
        metadata = self._strm_metadata(row, source, category, locked=True)
        issue = self._source_issue(source, recognition, row)
        if issue:
            return StageResult.failed(issue, error_type="invalid_strm_source", metadata=metadata)
        plan = plan_strm_move(source, category, self.move_config, destination_name=source.name)
        if plan.metadata:
            metadata.update(plan.metadata)
        metadata.update(
            {
                "source_path": str(plan.source_path) if plan.source_path else str(source),
                "dest_path": str(plan.dest_path) if plan.dest_path else "",
            }
        )
        if plan.status == "skipped" and plan.reason in {
            "未找到 STRM 源目录",
            "STRM 源目录不存在",
            "源目录不包含 STRM 文件",
            "STRM 源目录仍在更新",
        }:
            return StageResult.defer(plan.reason, 15, metadata)
        if plan.status == "skipped" and plan.reason == "已在目标媒体库，无需移动" and plan.dest_path:
            moved_row = self.store.update_move(
                int(row["id"]),
                "moved",
                source_path=str(plan.source_path or source),
                dest_path=str(plan.dest_path),
                category_final=category,
                error="已恢复移动完成状态",
            ) or row
        else:
            moved_row = execute_strm_move(plan, self.store, row)
        move_status = str(moved_row.get("move_status") or "").lower()
        metadata.update(
            {
                "source_path": str(moved_row.get("source_path") or metadata["source_path"]),
                "dest_path": str(moved_row.get("dest_path") or metadata["dest_path"]),
                "category": str(moved_row.get("category_final") or category),
                "move_status": move_status,
            }
        )
        if move_status != "moved":
            return StageResult.failed(
                str(moved_row.get("move_error") or plan.reason or "STRM 移动失败"),
                error_type="strm_move_failed",
                metadata=metadata,
            )
        metadata.update(self._request_emby_refresh(task, metadata["dest_path"]))
        return StageResult.complete("直链 STRM 已移动到媒体库", metadata)

    def _request_emby_refresh(self, task: TaskSnapshot, destination: str) -> dict[str, Any]:
        if not destination or task.metadata.get("emby_refresh_requested"):
            return {}
        if not self.emby or not getattr(self.emby, "enabled", False):
            return {}
        refresh = getattr(self.emby, "refresh_library_for_path", None)
        if not callable(refresh):
            return {}
        try:
            library = refresh(destination)
        except Exception as exc:
            return {"emby_refresh_error": str(exc)[:200]}
        result = {"emby_refresh_requested": True}
        if library:
            result["emby_refresh_library"] = str(library)
        return result

    @staticmethod
    def _emby_path_matches(item: dict[str, Any], destination: str) -> bool:
        if not destination:
            return True
        actual = str(item.get("Path") or "").strip()
        if not actual:
            return False
        expected_path = safe_resolve(Path(destination))
        actual_path = safe_resolve(Path(actual))
        return actual_path == expected_path or is_relative_to(actual_path, expected_path)

    def _find_emby_item(self, recognition: dict[str, Any], row: dict[str, Any], destination: str) -> dict[str, Any] | None:
        tmdb_id = str(recognition.get("tmdb_id") or row.get("tmdb_id") or "").strip()
        candidates: list[dict[str, Any]] = []
        if tmdb_id and hasattr(self.emby, "find_items_by_tmdb"):
            try:
                items = self.emby.find_items_by_tmdb(tmdb_id)
            except Exception:
                items = []
            if isinstance(items, list):
                candidates.extend(item for item in items if isinstance(item, dict))
        if tmdb_id and hasattr(self.emby, "find_item_by_tmdb"):
            try:
                item = self.emby.find_item_by_tmdb(tmdb_id)
            except Exception:
                item = None
            if isinstance(item, dict):
                candidates.append(item)
        if hasattr(self.emby, "recent_items"):
            try:
                candidates.extend(item for item in self.emby.recent_items(limit=100) if isinstance(item, dict))
            except Exception:
                pass
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
            if self._emby_path_matches(item, destination):
                return item
        return None

    def _stage_emby_confirmed(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        if not self.emby or not getattr(self.emby, "enabled", False):
            return StageResult.needs_action("Emby 确认未启用", self._submission_metadata(row))
        recognition = self._recognition(row)
        destination = str(row.get("dest_path") or task.metadata.get("dest_path") or "").strip()
        item = self._find_emby_item(recognition, row, destination)
        if not item:
            return StageResult.defer("等待 Emby 确认入库", self.emby_retry_seconds, self._submission_metadata(row, dest_path=destination))
        parent = ""
        try:
            parent = str(self.emby.library_name_for_item(item) or "")
        except Exception:
            parent = ""
        updated = self.store.update_emby(
            int(row["id"]),
            "confirmed",
            item_id=str(item.get("Id") or ""),
            title=str(item.get("Name") or ""),
            path=str(item.get("Path") or ""),
            parent=parent or emby_parent_label(item),
        ) or row
        return StageResult.complete(
            "Emby 已确认入库",
            self._submission_metadata(
                updated,
                emby_status="confirmed",
                emby_item_id=updated.get("emby_item_id"),
                emby_parent=updated.get("emby_parent"),
                dest_path=destination,
            ),
        )


class SourceShareTaskWorkflow(DirectTaskWorkflow):
    """Generate STRM directly from the incoming 115 share without receiving it."""

    def __init__(
        self,
        cms: Any,
        store: Any,
        move_config: MoveConfig,
        self_share_config: SelfShareConfig,
        *,
        task_store: Any,
        emby: Any | None = None,
        tmdb_resolver: Any | None = None,
        now: Callable[[], float] | None = None,
        emby_retry_seconds: int = 15,
    ):
        share_root = safe_resolve(
            Path(getattr(self_share_config, "strm_root", "/mnt/user/Unraid/strm/share"))
        )
        source_roots = [share_root]
        source_roots.extend(
            root for root in move_config.source_roots if safe_resolve(root) != share_root
        )
        super().__init__(
            cms,
            store,
            MoveConfig(
                source_roots=source_roots,
                library_roots=move_config.library_roots,
                conflict_policy=move_config.conflict_policy,
                stable_seconds=move_config.stable_seconds,
            ),
            task_store=task_store,
            emby=emby,
            now=now,
            emby_retry_seconds=emby_retry_seconds,
        )
        self.self_share_config = self_share_config
        self.tmdb_resolver = tmdb_resolver

    def run_stage(self, task: TaskSnapshot) -> StageResult:
        if effective_task_strm_mode(task) != "source_shared":
            return StageResult.failed(
                "原始分享任务模式不匹配",
                error_type="strm_mode_mismatch",
                metadata={"strm_mode": effective_task_strm_mode(task)},
            )
        if task.current_stage == TaskStage.RECEIVED:
            result = self._stage_received(task)
        elif task.current_stage == TaskStage.SHARE_SYNC_SUBMITTED:
            result = self._stage_share_sync_submitted(task)
        elif task.current_stage == TaskStage.RECOGNIZING:
            result = self._stage_recognizing(task)
        elif task.current_stage == TaskStage.STRM_READY:
            result = self._stage_strm_ready(task)
        elif task.current_stage == TaskStage.MOVED:
            result = self._stage_moved(task)
        elif task.current_stage == TaskStage.EMBY_CONFIRMED:
            result = self._stage_emby_confirmed(task)
        else:
            result = StageResult.failed("原始分享工作流不支持此阶段", error_type="unsupported_stage")
        return attach_row_checkpoint(result, self._submission_row(task))

    def _submission_metadata(self, row: dict[str, Any], **extra: Any) -> dict[str, Any]:
        metadata = {
            "submission_id": int(row["id"]),
            "strm_mode": "source_shared",
            "source_share": True,
        }
        metadata.update({key: value for key, value in extra.items() if value is not None})
        return metadata

    def _stage_received(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if row is None:
            row = self.store.upsert_submission(
                _ShareKey(task.share_code, task.receive_code),
                task.url,
                "received",
                title=task.title or task.share_code,
            )
        row = self.store.update_self_share(
            int(row["id"]),
            workflow_mode="source_share_sync",
            workflow_phase="source_share_received",
            own_share_code=task.share_code,
            own_share_receive_code=task.receive_code,
            own_share_url=task.url,
        ) or row
        return StageResult.complete("已接收原始 115 分享链接", self._submission_metadata(row))

    @staticmethod
    def _share_marker(task: TaskSnapshot) -> str:
        return f"/s/{task.share_code}_{task.receive_code}_"

    def _find_source_share_dir(self, task: TaskSnapshot, row: dict[str, Any]) -> Path | None:
        persisted = str(row.get("source_path") or task.metadata.get("source_path") or "").strip()
        if persisted:
            source = safe_resolve(Path(persisted))
            if self._allowed_source(source) and source.is_dir():
                return source
        root = safe_resolve(self.self_share_config.strm_root)
        if not root.is_dir():
            return None
        marker = self._share_marker(task)
        try:
            # os.walk(followlinks=False): a directory symlink in the strm root
            # must not pull candidate files from outside the root.
            for base, _dirnames, filenames in os.walk(root, followlinks=False):
                base_path = Path(base)
                for name in filenames:
                    if not name.lower().endswith(".strm"):
                        continue
                    path = base_path / name
                    try:
                        content = path.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        continue
                    if marker not in content:
                        continue
                    relative = path.relative_to(root)
                    source = safe_resolve(root / relative.parts[0])
                    if source.is_dir():
                        return source
        except OSError:
            return None
        return None

    def _stage_share_sync_submitted(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        operation_key = f"{operation_scope(task)}:cms_source_share_sync:source_shared:{task.share_code}"
        operation_request = {
            "strm_mode": "source_shared",
            "share_code": task.share_code,
            "receive_code": task.receive_code,
            "cid": self.self_share_config.cms_cid,
            "local_path": self.self_share_config.cms_local_path,
        }
        operation = self.task_store.find_operation(int(task.id), operation_key)
        if str(row.get("share_sync_status") or "") != "submitted" or operation is not None:
            operation = self.task_store.prepare_operation(
                int(task.id),
                operation_key,
                "cms_source_share_sync",
                operation_request,
            )
            if operation.status == "prepared":
                started = self.task_store.start_operation(int(task.id), operation_key)
                operation = started or self.task_store.find_operation(int(task.id), operation_key)
                if started is not None:
                    try:
                        response = self.cms.add_share115_sync_task(
                            str(started.request.get("share_code") or task.share_code),
                            str(started.request.get("receive_code") or task.receive_code),
                            str(started.request.get("cid") or self.self_share_config.cms_cid),
                            str(started.request.get("local_path") or self.self_share_config.cms_local_path),
                        )
                    except Exception as exc:
                        uncertain = self.task_store.mark_operation_uncertain(
                            int(task.id),
                            operation_key,
                            str(exc),
                        )
                        operation = uncertain or self.task_store.find_operation(int(task.id), operation_key)
                        return StageResult.needs_action(
                            "CMS 原始分享同步结果无法确认，禁止自动重复提交，请人工检查",
                            self._submission_metadata(
                                row,
                                cms_operation_key=operation_key,
                                cms_share_sync_outcome="unknown",
                            ),
                        )
                    completed = self.task_store.complete_operation(
                        int(task.id),
                        operation_key,
                        response if isinstance(response, dict) else {},
                    )
                    operation = completed or self.task_store.find_operation(int(task.id), operation_key)
            if operation is None:
                raise RuntimeError("CMS source-share sync operation disappeared")
            if operation.status == "started":
                uncertain = self.task_store.mark_operation_uncertain(
                    int(task.id),
                    operation_key,
                    "CMS source-share sync result was not persisted",
                )
                operation = uncertain or operation
            if operation.status != "succeeded":
                return StageResult.needs_action(
                    "CMS 原始分享同步结果无法安全恢复，禁止自动重复提交，请人工检查",
                    self._submission_metadata(
                        row,
                        cms_operation_key=operation_key,
                        cms_share_sync_outcome="unknown",
                    ),
                )
            row = self.store.update_self_share(
                int(row["id"]),
                workflow_phase="source_share_sync_submitted",
                share_sync_status="submitted",
            ) or row
        source = self._find_source_share_dir(task, row)
        if source:
            row = self.store.update_self_share(
                int(row["id"]),
                own_share_file_name=source.name,
            ) or row
            row = self.store.update_move(int(row["id"]), "pending", source_path=str(source)) or row
        return StageResult.complete(
            "已提交 CMS 原始分享同步",
            self._submission_metadata(row, source_path=str(source) if source else ""),
        )

    def _stage_recognizing(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        if not row:
            return StageResult.failed("找不到提交记录", error_type="submission_missing")
        source = self._find_source_share_dir(task, row)
        if not source:
            return StageResult.defer("等待原始分享 STRM 源目录生成", 15, self._submission_metadata(row))
        share_name = source.name
        recognition = self._recognition(row)
        tmdb_id = extract_tmdb_id_from_name(share_name)
        if tmdb_id:
            recognition["tmdb_id"] = tmdb_id
        recognition, should_prompt = apply_tmdb_hint_resolution(recognition, share_name, self.tmdb_resolver)
        if should_prompt:
            recognition, should_prompt = apply_tmdb_search_resolution(recognition, share_name, self.tmdb_resolver)
        category = str(recognition.get("category") or "").strip()
        if not category:
            return StageResult.needs_action(
                "无法识别原始分享 STRM 分类",
                self._submission_metadata(row, source_path=str(source), recognition=recognition),
            )
        recognition["category"] = category
        if not recognition.get("type"):
            recognition["type"] = media_type_for_category(category)
        recognition["share_name"] = share_name
        row = self.store.update_status(int(row["id"]), "source_share_ready", title=share_name) or row
        row = self.store.update_self_share(int(row["id"]), own_share_file_name=share_name) or row
        row = self.store.update_recognition(int(row["id"]), recognition, "tmdb_resolved") or row
        row = self.store.update_move(
            int(row["id"]),
            "pending",
            source_path=str(source),
            category_final=category,
        ) or row
        return StageResult.complete(
            "已识别原始分享 STRM 分类",
            self._submission_metadata(
                row,
                source_path=str(source),
                recognition=recognition,
                category=category,
                tmdb_id=str(recognition.get("tmdb_id") or ""),
            ),
        )

    def _find_source(self, task: TaskSnapshot, row: dict[str, Any], recognition: dict[str, Any]) -> Path | None:
        return self._find_source_share_dir(task, row)

    def _source_issue(self, source: Path, recognition: dict[str, Any], row: dict[str, Any]) -> str:
        if not self._allowed_source(source):
            return "源目录不在允许范围内"
        expected_tmdb = str(recognition.get("tmdb_id") or row.get("tmdb_id") or "").strip()
        folder_tmdb = extract_tmdb_id_from_name(source.name)
        if expected_tmdb and folder_tmdb and expected_tmdb != folder_tmdb:
            return f"任务 TMDB {expected_tmdb} 与文件夹 TMDB {folder_tmdb} 不一致，阻止移动 STRM"
        marker = f"/s/{str(row.get('own_share_code') or '')}_{str(row.get('own_share_receive_code') or '')}_"
        files = sorted(iter_strm_files(source))
        if not files:
            return "源目录不包含 STRM 文件"
        for path in files:
            issue = validate_self_share_strm_file(path, marker)
            if issue:
                return issue
        return ""


class ModeRoutingWorkflow(BridgeSelfShareTaskWorkflow):
    def __init__(
        self,
        direct: DirectTaskWorkflow,
        shared: Any | None,
        source_shared: SourceShareTaskWorkflow | None = None,
        default_mode: str = "shared",
    ):
        self.direct = direct
        self.shared = shared
        self.source_shared = source_shared
        self.default_mode = default_mode

    def run_stage(self, task: TaskSnapshot) -> StageResult:
        mode = effective_task_strm_mode(task, default_mode=self.default_mode)
        if mode == "direct":
            return self.direct.run_stage(task)
        if mode == "source_shared":
            if self.source_shared is None:
                return StageResult.failed("原始分享 STRM 工作流未配置", error_type="source_share_unavailable")
            return self.source_shared.run_stage(task)
        if self.shared is None:
            return StageResult.failed("共享 STRM 工作流需要 P115", error_type="p115_required")
        return self.shared.run_stage(task)

    def __getattr__(self, name: str) -> Any:
        # Keep integrations that inspect the historical shared workflow surface working.
        for workflow in (self.shared, self.source_shared, self.direct):
            if workflow is not None and hasattr(workflow, name):
                return getattr(workflow, name)
        raise AttributeError(name)
