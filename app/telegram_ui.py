"""Telegram UI formatting and keyboard helpers."""

from __future__ import annotations

import re
import time
from typing import Any

from app.media.classify import expected_task_tmdb_id, extract_tmdb_id_from_name, normalize_text, parse_recognition_json
from app.models import TaskStage, TaskStatus
from app.task_diagnostics import describe_task_wait, format_task_observability
from app.task_engine import stage_display_name
from app.workflows.self_share import format_task_label


_SERIES_UPDATE_CATEGORIES = {"国产电视", "外国电视", "番剧"}


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


def format_taskstore_history(tasks: list[Any]) -> str:
    if not tasks:
        return ""
    lines = ["TaskStore 最近历史："]
    for idx, task in enumerate(tasks, 1):
        title = task.title or task.metadata.get("received_title") or task.share_code
        category = task.category or task.metadata.get("category") or task.metadata.get("category_final") or "-"
        dest = task.metadata.get("dest_path") or "-"
        emby_parent = task.metadata.get("emby_parent") or task.metadata.get("emby_refresh_library") or "-"
        lines.append(
            f"{idx}. #{task.id} {title} | 阶段:{stage_display_name(task.current_stage)} | "
            f"状态:{task.status.value} | 分类:{category} | 媒体库:{emby_parent} | 路径:{dest}"
        )
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


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


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


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    tail_len = min(80, max(0, limit // 3))
    head_len = max(0, limit - tail_len - 3)
    return f"{text[:head_len]}...{text[-tail_len:]}"


def format_taskstore_status(tasks: list[Any]) -> str:
    if not tasks:
        return ""
    lines = ["TaskStore 最近任务："]
    for idx, task in enumerate(tasks, 1):
        title = truncate_text(str(task.title or task.metadata.get("received_title") or task.share_code), 80)
        err = f"，{truncate_text(task.error_summary, 100)}" if task.error_summary else ""
        lines.append(
            f"{idx}. #{task.id} {title}：{stage_display_name(task.current_stage)} / {task.status.value}{err}"
        )
        if task.status in {TaskStatus.RUNNING, TaskStatus.PENDING}:
            lines.append(f"   等待：{truncate_text(describe_task_wait(task, now=time.time()), 200)}")
        for line in format_task_observability(task, now=time.time()):
            lines.append(f"   {truncate_text(line, 200)}")
    return "\n".join(lines)


def task_action_keyboard(tasks: list[Any], limit: int = 5) -> dict[str, Any] | None:
    buttons: list[list[dict[str, str]]] = []
    for task in tasks[:limit]:
        row = [
            {"text": f"详情 #{task.id}", "callback_data": f"task_detail:{task.id}"},
            {"text": f"查 Emby #{task.id}", "callback_data": f"task_emby:{task.id}"},
        ]
        if task.status in {TaskStatus.FAILED, TaskStatus.NEEDS_ACTION} or task.current_stage in {TaskStage.FAILED, TaskStage.NEEDS_ACTION}:
            row.append({"text": f"重试 #{task.id}", "callback_data": f"task_retry:{task.id}"})
        row.append({"text": f"恢复 STRM #{task.id}", "callback_data": f"task_restore:{task.id}"})
        row.append({"text": f"从头重跑 #{task.id}", "callback_data": f"task_reprocess:{task.id}"})
        category = str(task.category or task.metadata.get("category") or task.metadata.get("category_final") or "").strip()
        submission_id = task.submission_id or task.metadata.get("submission_id")
        if (
            task.status == TaskStatus.SUCCEEDED
            and task.current_stage == TaskStage.CLEANED
            and category in _SERIES_UPDATE_CATEGORIES
            and submission_id not in (None, "")
        ):
            row.append({"text": f"追更 #{task.id}", "callback_data": f"task_update:{task.id}"})
        buttons.append(row)
    return {"inline_keyboard": buttons} if buttons else None


def hdhive_candidate_keyboard(session_id: str, candidates: list[dict[str, str]]) -> dict[str, Any]:
    buttons = []
    for index, candidate in enumerate(candidates[:12]):
        title = truncate_text(candidate.get("title") or "未命名", 34)
        year = candidate.get("year") or "年未知"
        media_type = "电影" if candidate.get("media_type") == "movie" else "剧集"
        buttons.append([{"text": f"{index + 1}. {title} ({year}) [{media_type}]", "callback_data": f"hive:candidate:{session_id}:{index}"}])
    buttons.append([{"text": "取消搜索", "callback_data": f"hive:cancel:{session_id}"}])
    return {"inline_keyboard": buttons}


def hdhive_resource_keyboard(
    session_id: str,
    resources: list[Any],
    visible_indexes: list[int],
    selected_indexes: list[int],
    pan_types: list[str],
    current_pan_type: str,
) -> dict[str, Any]:
    buttons = []
    filter_buttons = [{"text": "全部" if current_pan_type == "all" else "全部资源", "callback_data": f"hive:filter:{session_id}:all"}]
    for index, pan_type in enumerate(pan_types):
        filter_buttons.append(
            {
                "text": f"[{pan_type}]" if pan_type == current_pan_type else pan_type,
                "callback_data": f"hive:filter:{session_id}:{index}",
            }
        )
    for start in range(0, len(filter_buttons), 4):
        buttons.append(filter_buttons[start : start + 4])
    for resource_index in visible_indexes:
        resource = resources[resource_index]
        title = truncate_text(resource.title or f"资源 {resource_index + 1}", 28)
        details = "/".join(resource.video_resolution) or "分辨率未知"
        cost = "已解锁" if resource.is_unlocked else f"{resource.unlock_points if resource.unlock_points is not None else '?'}分"
        if resource.validate_status.lower() == "invalid":
            text = f"不可用 {resource_index + 1}. {title} | {resource.pan_type} | {cost}"
        else:
            mark = "已选 " if resource_index in selected_indexes else ""
            text = f"{mark}{resource_index + 1}. {title} | {resource.pan_type} | {details} | {cost}"
        buttons.append(
            [
                {
                    "text": text,
                    "callback_data": f"hive:toggle:{session_id}:{resource_index}",
                },
                {
                    "text": "单独解锁",
                    "callback_data": f"hive:single:{session_id}:{resource_index}",
                },
            ]
        )
    selected_count = len(selected_indexes)
    buttons.append(
        [
            {"text": f"解锁选中 ({selected_count})", "callback_data": f"hive:unlock:{session_id}"},
            {"text": "取消", "callback_data": f"hive:cancel:{session_id}"},
        ]
    )
    return {"inline_keyboard": buttons}


def hdhive_confirmation_keyboard(session_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "确认解锁", "callback_data": f"hive:confirm:{session_id}"},
                {"text": "取消", "callback_data": f"hive:cancel:{session_id}"},
            ]
        ]
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


def menu_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📊 统计"}, {"text": "📋 最近任务"}],
            [{"text": "🕘 历史"}, {"text": "🧹 清理历史"}],
            [{"text": "HDHive 搜索"}],
            [{"text": "🩺 健康检查"}, {"text": "❓ 帮助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }
