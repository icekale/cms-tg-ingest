from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import MoveConfig, is_relative_to, is_under_any_root, safe_resolve
from app.media.classify import (
    expected_task_tmdb_id,
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
    plan_strm_move,
    validate_direct_strm_source,
)
from app.models import TaskSnapshot, TaskStage
from app.strm_mode import effective_task_strm_mode
from app.task_runner import StageResult
from app.workflows.self_share import emby_parent_label, match_emby_item


_CMS_FAILURE_MARKERS = ("failed", "error", "失败", "timeout", "超时", "cancel")
_CMS_SUCCESS_MARKERS = ("done", "finish", "success", "complete", "完成", "成功")
_DIRECT_STAGES = {
    TaskStage.RECEIVED,
    TaskStage.ORGANIZING,
    TaskStage.RECOGNIZING,
    TaskStage.STRM_READY,
    TaskStage.MOVED,
    TaskStage.EMBY_CONFIRMED,
}


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


def _cms_recognition(detail: dict[str, Any], existing: dict[str, Any], title: str) -> dict[str, Any]:
    data = _detail_data(detail)
    tmdb_info = data.get("tmdb_info") if isinstance(data.get("tmdb_info"), dict) else {}
    video_info = data.get("video_info") if isinstance(data.get("video_info"), dict) else {}
    recognition = dict(existing)
    category = _detail_value(detail, "category", "category_final", "category_choice")
    tmdb_id = _detail_value(detail, "tmdb_id") or tmdb_info.get("tmdb_id") or tmdb_info.get("id")
    media_type = _detail_value(detail, "type") or tmdb_info.get("type") or video_info.get("type")
    resolved_title = _detail_value(detail, "name", "title", "share_name", "file_name") or title
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
        emby: Any | None = None,
        now: Callable[[], float] | None = None,
        emby_retry_seconds: int = 15,
    ):
        self.cms = cms
        self.store = store
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
            return self._stage_received(task)
        if task.current_stage == TaskStage.ORGANIZING:
            return self._stage_organizing(task)
        if task.current_stage == TaskStage.RECOGNIZING:
            return self._stage_recognizing(task)
        if task.current_stage == TaskStage.STRM_READY:
            return self._stage_strm_ready(task)
        if task.current_stage == TaskStage.MOVED:
            return self._stage_moved(task)
        if task.current_stage == TaskStage.EMBY_CONFIRMED:
            return self._stage_emby_confirmed(task)
        return StageResult.failed("直链工作流不支持此阶段", error_type="unsupported_stage")

    def _submission_row(self, task: TaskSnapshot) -> dict[str, Any] | None:
        submission_id = task.metadata.get("submission_id") or task.submission_id
        if submission_id not in (None, ""):
            return self.store.find_by_id(int(submission_id))
        return self.store.find_by_key(_ShareKey(task.share_code, task.receive_code))

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

    def _stage_received(self, task: TaskSnapshot) -> StageResult:
        row = self._submission_row(task)
        cms_task_id = str(
            (row or {}).get("cms_task_id") or task.metadata.get("cms_task_id") or ""
        ).strip()
        if row and not cms_task_id and str(row.get("status") or "").lower() not in {"failed", "error"}:
            return StageResult.failed(
                "已有 CMS 提交记录但缺少任务 ID",
                error_type="cms_task_id_missing",
                metadata=self._submission_metadata(row),
            )
        title = str((row or {}).get("title") or task.title or task.share_code).strip()
        if not cms_task_id:
            response = self.cms.add_share_down(task.url)
            cms_task_id, response_title = _extract_cms_task_info(response)
            if not cms_task_id:
                return StageResult.failed("CMS 未返回任务 ID", error_type="cms_task_id_missing")
            title = response_title or title
            row = self.store.upsert_submission(
                _ShareKey(task.share_code, task.receive_code),
                task.url,
                "submitted",
                cms_task_id=cms_task_id,
                title=title,
            )
        elif not row:
            row = self.store.upsert_submission(
                _ShareKey(task.share_code, task.receive_code),
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
            return StageResult.failed("缺少 CMS 任务 ID", error_type="cms_task_id_missing")
        detail = self.cms.get_share_down_detail(cms_task_id)
        status = _cms_status(detail)
        title = str(_detail_value(detail, "name", "title", "share_name", "file_name") or row.get("title") or task.title or "").strip()
        updated = self.store.update_status(int(row["id"]), status or "unknown", title=title) or row
        if any(marker in status for marker in _CMS_FAILURE_MARKERS):
            reason = str(_detail_value(detail, "msg", "message", "error", "last_error") or "CMS 整理失败")
            return StageResult.failed(
                reason,
                error_type="cms_organize_failed",
                metadata=self._submission_metadata(updated, cms_task_id=cms_task_id, title=title),
            )
        if not any(marker in status for marker in _CMS_SUCCESS_MARKERS):
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


class ModeRoutingWorkflow:
    def __init__(self, direct: DirectTaskWorkflow, shared: Any | None, default_mode: str = "shared"):
        self.direct = direct
        self.shared = shared
        self.default_mode = default_mode

    def run_stage(self, task: TaskSnapshot) -> StageResult:
        mode = effective_task_strm_mode(task, default_mode=self.default_mode)
        if mode == "direct":
            return self.direct.run_stage(task)
        if self.shared is None:
            return StageResult.failed("共享 STRM 工作流需要 P115", error_type="p115_required")
        return self.shared.run_stage(task)
