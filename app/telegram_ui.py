"""Telegram UI formatting and keyboard helpers."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from app.logging_system import redact_text
from app.hdhive_subscriptions import diagnose_subscription_check
from app.media.classify import expected_task_tmdb_id, extract_tmdb_id_from_name, normalize_text, parse_recognition_json
from app.models import TaskStage, TaskStatus
from app.task_diagnostics import describe_task_wait, format_task_observability
from app.task_actions import available_task_actions
from app.task_engine import stage_display_name
from app.telegram_rich import RichDocument, details, document, heading, paragraph, table
from app.workflows.self_share import format_task_label


_SERIES_UPDATE_CATEGORIES = {"国产电视", "外国电视", "番剧"}
_TELEGRAM_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def safe_telegram_text(value: object, limit: int = 200) -> str:
    text = redact_text("" if value is None else value)
    return truncate_text(_TELEGRAM_URL_RE.sub("<redacted-url>", text), limit)


def format_history(rows: list[dict[str, Any]]) -> RichDocument:
    if not rows:
        return document(paragraph("暂无历史记录。"))
    table_rows = []
    for idx, row in enumerate(rows, 1):
        table_rows.append(
            (
                str(idx),
                safe_telegram_text(format_task_label(row), 120),
                safe_telegram_text(row.get("category_final") or row.get("category_choice") or row.get("category_status") or "-", 120),
                safe_telegram_text(row.get("move_status") or "-", 120),
                safe_telegram_text(row.get("emby_status") or "-", 120),
            )
        )
    blocks: list = [heading("最近历史"), table(("#", "任务", "分类", "移动", "Emby"), table_rows)]
    failure_summary = format_failure_summary(rows)
    if failure_summary:
        blocks.append(paragraph(failure_summary))
    library_summary = format_library_summary(rows)
    if library_summary:
        blocks.append(paragraph(library_summary))
    return RichDocument(tuple(blocks))


def format_taskstore_history(tasks: list[Any]) -> RichDocument:
    if not tasks:
        return RichDocument()
    table_rows = []
    for idx, task in enumerate(tasks, 1):
        title = safe_telegram_text(task.title or task.metadata.get("received_title") or f"任务 #{task.id}", 80)
        category = safe_telegram_text(task.category or task.metadata.get("category") or task.metadata.get("category_final") or "-", 120)
        dest = safe_telegram_text(task.metadata.get("dest_path") or "-", 240)
        emby_parent = safe_telegram_text(task.metadata.get("emby_parent") or task.metadata.get("emby_refresh_library") or "-", 160)
        table_rows.append(
            (
                f"#{task.id}",
                title,
                stage_display_name(task.current_stage),
                task.status.value,
                str(category),
                str(emby_parent),
                dest,
            )
        )
    return document(heading("TaskStore 最近历史"), table(("#", "任务", "阶段", "状态", "分类", "媒体库", "路径"), table_rows))


def format_failure_summary(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        if str(row.get("status") or "").lower() != "failed":
            continue
        reason = safe_telegram_text(row.get("last_error"), 160).strip()
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
        parent = safe_telegram_text(row.get("emby_parent") or "", 160).strip()
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


def format_quality_report(rows: list[dict[str, Any]]) -> RichDocument:
    table_rows = []
    for row in rows:
        issue = quality_issue_for_row(row)
        if not issue:
            continue
        table_rows.append((str(len(table_rows) + 1), format_task_label(row), str(row.get("emby_title") or "-"), issue))
    if not table_rows:
        return document(paragraph("最近任务未发现明显错配。"))
    return document(heading("质量巡检：发现疑似错配"), table(("#", "任务", "Emby", "问题"), table_rows))


def quality_issue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if quality_issue_for_row(row)]


def quality_keyboard(rows: list[dict[str, Any]], limit: int = 8) -> dict[str, Any] | None:
    buttons = []
    for row in quality_issue_rows(rows)[:limit]:
        row_id = int(row["id"])
        buttons.append([{"text": f"重新确认：{row_id}", "callback_data": f"emby_recheck:{row_id}"}])
    return {"inline_keyboard": buttons} if buttons else None


_QUALITY_ACTION_LABELS = {
    "execute": "执行重跑",
    "reprocess": "人工重跑",
    "snooze": "暂缓 24 小时",
    "ignore": "忽略",
    "resume": "恢复评估",
}


def quality_manual_rows(rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """Keep the Telegram queue focused on actionable quality decisions."""
    selected: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        try:
            task_id = int(row.get("task_id") or 0)
        except (TypeError, ValueError):
            continue
        rule_id = str(row.get("rule_id") or "").strip()
        status = str(row.get("manual_status") or "open").strip().lower()
        if task_id <= 0 or not rule_id or (not row.get("auto_allowed") and status not in {"manual_required", "snoozed", "ignored"}):
            continue
        key = (task_id, rule_id)
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def format_quality_scan_summary(rows: list[dict[str, Any]]) -> str:
    """One-line Telegram report; no action buttons."""
    count = sum(1 for row in rows if str(row.get("rule_id") or "") not in {"", "no_issue"})
    if count <= 0:
        return "质量巡检：未发现需要关注的本地 STRM 问题。"
    return f"质量巡检：发现 {count} 个问题，请到 Web 质量页查看。"


def format_quality_manual_report(rows: list[dict[str, Any]]) -> RichDocument:
    rows = quality_manual_rows(rows)
    if not rows:
        return document(paragraph("质量巡检：当前没有需要人工处理的问题。"))
    table_rows = []
    for row in rows:
        title = truncate_text(str(row.get("title") or f"任务 #{row.get('task_id')}"), 70)
        reason = truncate_text(str(row.get("rule_reason") or row.get("message") or "需要人工确认"), 120)
        table_rows.append(
            (
                f"#{row.get('task_id')}",
                title,
                str(row.get("rule_id") or "-"),
                str(row.get("risk_level") or "-"),
                str(row.get("manual_status") or "open"),
                reason,
                str(row.get("attempts", 0)),
            )
        )
    return document(
        heading(f"质量巡检：{len(rows)} 项需要关注"),
        table(("#", "任务", "规则", "风险", "状态", "原因", "尝试"), table_rows),
    )


def quality_manual_keyboard(rows: list[dict[str, Any]], limit: int = 8) -> dict[str, Any] | None:
    buttons: list[list[dict[str, str]]] = []
    for row in quality_manual_rows(rows, limit=limit):
        task_id = int(row["task_id"])
        rule_id = str(row["rule_id"])
        version = str(row.get("rule_version") or "1")
        actions = [str(action).strip().lower() for action in row.get("available_actions", [])]
        visible = [action for action in ("execute", "reprocess", "snooze", "ignore", "resume") if action in actions]
        row_buttons = [
            {
                "text": _QUALITY_ACTION_LABELS[action],
                "callback_data": f"quality:{action}:{task_id}:{rule_id}:{version}",
            }
            for action in visible
        ]
        if row_buttons:
            buttons.append(row_buttons)
    return {"inline_keyboard": buttons} if buttons else None


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def format_metrics(payload: dict[str, Any]) -> RichDocument:
    rows = (
        ("生成时间", payload.get("generated_at") or "-"),
        ("总数", payload.get("total", 0)),
        ("任务", format_counts(payload.get("status_counts") or {})),
        ("Emby", format_counts(payload.get("emby_status_counts") or {})),
        ("移动", format_counts(payload.get("move_status_counts") or {})),
        ("失败", payload.get("failure_summary") or "-"),
        ("媒体库", payload.get("library_summary") or "-"),
        ("Telegram瞬时错误", payload.get("telegram_last_transient_error_at") or "-"),
    )
    return document(heading("任务统计"), table(("项", "值"), rows))


def format_status(rows: list[dict[str, Any]]) -> RichDocument:
    if not rows:
        return document(paragraph("暂无记录。直接发送 115 分享链接即可创建任务。"))
    table_rows = []
    for row in rows:
        table_rows.append((safe_telegram_text(format_task_label(row), 120), str(row.get("status") or "unknown"), safe_telegram_text(row.get("last_error"), 160)))
    blocks: list = [heading("最近任务"), table(("任务", "状态", "错误"), table_rows)]
    failure_summary = format_failure_summary(rows)
    if failure_summary:
        blocks.append(paragraph(failure_summary))
    return RichDocument(tuple(blocks))


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    tail_len = min(80, max(0, limit // 3))
    head_len = max(0, limit - tail_len - 3)
    return f"{text[:head_len]}...{text[-tail_len:]}"


def truncate_end(text: str, limit: int) -> str:
    value = str(text or "")
    width = max(1, int(limit))
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


def format_hdhive_candidate_label(candidate: dict[str, str] | None) -> str:
    item = candidate if isinstance(candidate, dict) else {}
    title = str(item.get("title") or "未命名").strip() or "未命名"
    year = str(item.get("year") or "").strip() or "年份未知"
    media_type = "电影" if item.get("media_type") == "movie" else "剧集"
    tmdb_id = str(item.get("tmdb_id") or "").strip() or "-"
    return f"{title} ({year}) · {media_type} · TMDB {tmdb_id}"


def _safe_hdhive_failure_reason(value: object) -> str:
    return safe_telegram_text(value, 160)


def format_hdhive_candidates(candidates: list[dict[str, str]]) -> RichDocument:
    if not candidates:
        return document(paragraph("没有找到匹配的 TMDB 媒体。"))
    rows = []
    for index, candidate in enumerate(candidates[:12], 1):
        rows.append(
            (
                str(index),
                truncate_end(str(candidate.get("title") or "未命名"), 80),
                "电影" if candidate.get("media_type") == "movie" else "剧集",
                str(candidate.get("year") or "年份未知"),
                str(candidate.get("tmdb_id") or "-"),
            )
        )
    return document(
        heading("HDHive 候选媒体"),
        table(("#", "标题", "类型", "年份", "TMDB"), rows),
        paragraph(f"请选择要查询的媒体，共 {len(rows)} 个候选。"),
    )


def format_hdhive_unlock_result(
    results: list[Any],
    selected_pan_types: dict[str, str],
    *,
    enqueued_count: int = 0,
    enqueue_error: str = "",
) -> RichDocument:
    rows = []
    success_count = 0
    failed_count = 0
    non_115_count = 0
    for item in results:
        if item.success:
            success_count += 1
            status = "成功" + ("（已拥有）" if item.already_owned else "")
            if item.full_url and str(selected_pan_types.get(item.slug) or "").lower() != "115":
                non_115_count += 1
        else:
            failed_count += 1
            status = "失败"
        reason = _safe_hdhive_failure_reason(item.message or item.error_code or "未知原因") if not item.success else "-"
        rows.append((safe_telegram_text(str(item.slug), 80), status, reason))
    blocks: list = [
        heading("HDHive 解锁结果"),
        table(("资源", "状态", "原因"), rows),
        paragraph(f"成功 {success_count} 个，失败 {failed_count} 个。"),
    ]
    if enqueue_error:
        blocks.append(paragraph(f"115 入库提交失败：{safe_telegram_text(enqueue_error, 160)}。解锁链接未丢失，请稍后重试。"))
    elif enqueued_count:
        blocks.append(paragraph(f"115 入库：已入队 {enqueued_count} 个。"))
    else:
        blocks.append(paragraph("115 入库：无可入队链接。"))
    blocks.append(paragraph(f"非 115 资源：{non_115_count} 个。"))
    return RichDocument(tuple(blocks))


def format_taskstore_status(tasks: list[Any]) -> RichDocument:
    if not tasks:
        return RichDocument()
    table_rows = []
    extra = []
    for task in tasks:
        title = safe_telegram_text(str(task.title or task.metadata.get("received_title") or f"任务 #{task.id}"), 80)
        table_rows.append(
            (
                f"#{task.id}",
                title,
                stage_display_name(task.current_stage),
                task.status.value,
                safe_telegram_text(task.error_summary, 100),
            )
        )
        detail_lines = []
        if task.status in {TaskStatus.RUNNING, TaskStatus.PENDING}:
            detail_lines.append(paragraph(f"等待：{safe_telegram_text(describe_task_wait(task, now=time.time()), 200)}"))
        for line in format_task_observability(task, now=time.time()):
            detail_lines.append(paragraph(safe_telegram_text(line, 200)))
        if detail_lines:
            extra.append(details(f"#{task.id} {title}", detail_lines))
    return RichDocument((heading("TaskStore 最近任务"), table(("#", "任务", "阶段", "状态", "错误"), table_rows), *extra))


def task_action_keyboard(
    tasks: list[Any],
    limit: int = 5,
    max_retries: int = 3,
    task_store: Any | None = None,
) -> dict[str, Any] | None:
    buttons: list[list[dict[str, str]]] = []
    for task in tasks[:limit]:
        actions = available_task_actions(task, max_retries=max_retries, store=task_store)
        row = [
            {"text": f"详情 #{task.id}", "callback_data": f"task_detail:{task.id}"},
        ]
        if "emby" in actions:
            row.append({"text": f"查 Emby #{task.id}", "callback_data": f"task_emby:{task.id}"})
        if "retry" in actions:
            row.append({"text": f"重试 #{task.id}", "callback_data": f"task_retry:{task.id}"})
        if "restore" in actions:
            row.append({"text": f"恢复 STRM #{task.id}", "callback_data": f"task_restore:{task.id}"})
        if "reprocess" in actions:
            row.append({"text": f"从头重跑 #{task.id}", "callback_data": f"task_reprocess:{task.id}"})
        if "resume_organizing" in actions:
            row.append({"text": f"继续整理 #{task.id}", "callback_data": f"task_resume_organizing:{task.id}"})
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
        title = str(candidate.get("title") or "未命名").strip() or "未命名"
        label = truncate_end(f"{index + 1}. {title}", 64)
        row = [{"text": label, "callback_data": f"hive:candidate:{session_id}:{index}"}]
        if candidate.get("media_type") == "tv":
            row.append({"text": "订阅此剧", "callback_data": f"hive:subscribe:{session_id}:{index}"})
        buttons.append(row)
    buttons.append([{"text": "取消搜索", "callback_data": f"hive:cancel:{session_id}"}])
    return {"inline_keyboard": buttons}


def format_hdhive_subscriptions(
    subscriptions: list[Any],
    scheduler_snapshot: dict[str, Any] | None = None,
    pending_items: list[Any] | None = None,
    items_by_subscription_id: dict[int, list[Any]] | None = None,
) -> RichDocument:
    if not subscriptions:
        return document(paragraph("暂无 HDHive 剧集订阅。"))
    blocks: list = [heading("HDHive 剧集订阅")]
    if scheduler_snapshot:
        blocks.append(
            paragraph(
                f"自动检查：{'开启' if scheduler_snapshot.get('enabled') else '关闭'}，"
                f"每天 {scheduler_snapshot.get('time') or '01:30'}，下次：{scheduler_snapshot.get('next_run_at') or '-'}"
            )
        )
    table_rows = []
    extras = []
    status_map = {"active": "运行中", "paused": "已暂停", "error": "异常", "completed": "已完结"}
    for subscription in subscriptions:
        status = status_map.get(subscription.status, subscription.status)
        source = subscription.source_url or f"TMDB:{subscription.tmdb_id}"
        title = subscription.title or subscription.tmdb_id
        table_rows.append((f"#{subscription.id}", str(title), str(status), str(source)))
        detail_blocks = []
        episode_filter = str(getattr(subscription, "episode_filter", "") or "").strip()
        if episode_filter:
            detail_blocks.append(paragraph(f"集数过滤：{episode_filter}"))
        try:
            summary = json.loads(str(getattr(subscription, "last_summary_json", "{}") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        items = (items_by_subscription_id or {}).get(int(getattr(subscription, "id", 0) or 0), ())
        diagnosis = diagnose_subscription_check(summary if isinstance(summary, dict) else {}, items)
        if isinstance(summary, dict) and summary:
            counters = []
            for key, label in (
                ("discovered", "发现"),
                ("enqueued", "入队"),
                ("emby_exists", "Emby已有"),
                ("filtered", "过滤"),
                ("pending_confirmation", "待确认"),
                ("failed", "失败"),
                ("unparsed", "无法识别"),
                ("blocked", "阻塞"),
            ):
                if key in summary:
                    counters.append(f"{label} {summary[key]}")
            if counters:
                detail_blocks.append(paragraph("最近检查：" + "，".join(counters)))
            if diagnosis.conclusion:
                detail_blocks.append(paragraph(diagnosis.conclusion))
            if diagnosis.reasons:
                detail_blocks.append(paragraph("原因：" + "；".join(diagnosis.reasons)))
        if subscription.last_error:
            detail_blocks.append(paragraph(f"最近错误：{truncate_text(subscription.last_error, 120)}"))
        if detail_blocks:
            extras.append(details(f"#{subscription.id} {title}", detail_blocks))
    blocks.append(table(("#", "剧名", "状态", "来源"), table_rows))
    blocks.extend(extras)
    if pending_items:
        blocks.append(paragraph(f"待确认高费用资源：{len(pending_items)} 个，请点击按钮确认。"))
    return RichDocument(tuple(blocks))


def hdhive_subscriptions_keyboard(
    subscriptions: list[Any],
    pending_items: list[Any] | None = None,
) -> dict[str, Any] | None:
    buttons: list[list[dict[str, str]]] = []
    for subscription in subscriptions:
        toggle = "暂停" if subscription.status == "active" else "恢复"
        action = "pause" if subscription.status == "active" else "resume"
        buttons.append([{"text": f"{toggle} #{subscription.id}", "callback_data": f"hsub:{action}:{subscription.id}"}])
        buttons.append([{"text": f"设置集数过滤 #{subscription.id}", "callback_data": f"hsub:filter:{subscription.id}"}])
        buttons.append(
            [
                {"text": f"立即检查 #{subscription.id}", "callback_data": f"hsub:check:{subscription.id}"},
                {"text": f"删除 #{subscription.id}", "callback_data": f"hsub:delete:{subscription.id}"},
            ]
        )
    for item in pending_items or []:
        buttons.append([{"text": f"确认解锁资源 #{item.id}", "callback_data": f"hsub:confirm:{item.id}"}])
    return {"inline_keyboard": buttons} if buttons else None


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
            [{"text": "HDHive 订阅"}],
            [{"text": "🩺 健康检查"}, {"text": "❓ 帮助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }
