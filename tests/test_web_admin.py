import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from app.models import TaskStage, TaskStatus
from app.task_health import format_taskstore_health
from app.task_store import TaskStore
from app.web import (
    WebApp,
    _event_stage,
    _render_phase_track,
    _task_phase_index,
    render_health_page,
    render_quality_page,
    render_task_detail,
    render_task_list,
)


class WebAdminTests(unittest.TestCase):
    def test_pages_share_product_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("organizing", "", "https://115cdn.com/s/organizing")
            store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.RUNNING, "organizing")

            pages = {
                "运行概览": render_task_list(store),
                "质量巡检": render_quality_page(store),
                "本地健康": render_health_page(store),
                "任务详情": render_task_detail(store, task.id),
            }

            for page_name, page_html in pages.items():
                with self.subTest(page=page_name):
                    self.assertIn("CMS 入库助手", page_html)
                    self.assertIn('href="/"', page_html)
                    self.assertIn('href="/quality"', page_html)
                    self.assertIn('href="/health"', page_html)
                    self.assertIn('class="app-nav"', page_html)

            for active_label in ("运行概览", "质量巡检", "本地健康"):
                self.assertIn(f'aria-current="page">{active_label}</a>', pages[active_label])

    def test_task_detail_renders_eight_user_facing_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("recognizing", "", "https://115cdn.com/s/recognizing")
            store.record_event(task.id, TaskStage.RECEIVED, TaskStatus.SUCCEEDED, "received")
            store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.SUCCEEDED, "organized")
            store.record_event(task.id, TaskStage.RECOGNIZING, TaskStatus.RUNNING, "recognizing")

            page_html = render_task_detail(store, task.id)
            phase_html = _render_phase_track(store.find_task(task.id), store.list_events(task.id))
            labels = ("接收", "CMS 整理", "分类识别", "建分享", "分享 STRM", "移动入库", "Emby 确认", "清理完成")

            positions = [phase_html.index(f"<span>{label}</span>") for label in labels]
            self.assertEqual(positions, sorted(positions))
            self.assertIn(phase_html, page_html)
            self.assertEqual(page_html.count('class="phase-step'), 8)
            self.assertIn('class="phase-step is-current"', page_html)
            self.assertIn('role="list"', phase_html)
            self.assertEqual(phase_html.count('role="listitem"'), 8)
            self.assertIn('aria-current="step"', phase_html)
            self.assertIn('aria-label="接收，已完成"', phase_html)
            self.assertIn('aria-label="CMS 整理，已完成"', phase_html)

    def test_event_stage_accepts_task_stage_enum(self):
        self.assertIs(_event_stage(TaskStage.RECEIVED), TaskStage.RECEIVED)

    def test_result_stage_falls_back_to_latest_enum_valued_flow_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            cases = (
                (TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION),
                (TaskStage.FAILED, TaskStatus.FAILED),
            )

            for index, (result_stage, result_status) in enumerate(cases):
                with self.subTest(stage=result_stage):
                    task = store.upsert_task(f"result-{index}", "", f"https://115cdn.com/s/result-{index}")
                    task = store.record_event(task.id, result_stage, result_status, "result state")

                    phase_index = _task_phase_index(task, [{"stage": TaskStage.STRM_READY}])

                    self.assertEqual(phase_index, 4)

    def test_shared_focus_ring_uses_contrast_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")

            page_html = render_task_list(store)

            self.assertIn(":focus-visible { outline: 3px solid var(--primary-dark); outline-offset: 2px; }", page_html)

    def test_render_task_list_folds_completed_history_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            done = store.upsert_task("done", "", "https://115cdn.com/s/done")
            store.record_event(done.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done", title="已完成电影")
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.record_event(running.id, TaskStage.MOVED, TaskStatus.RUNNING, "moving", title="运行中电影", next_run_at=0)
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed", title="失败电影", error_summary="失败原因")

            html = render_task_list(store)

            self.assertIn("运行概览", html)
            self.assertIn("需要关注", html)
            self.assertIn("当前队列", html)
            self.assertIn("workspace-grid", html)
            self.assertIn("运行中", html)
            self.assertIn("需处理/失败", html)
            self.assertIn("等待资源", html)
            self.assertIn("已完成历史", html)
            self.assertIn("1 个活跃任务，1 个需关注", html)
            self.assertIn("运行中电影", html)
            self.assertIn("失败电影", html)
            self.assertNotIn("已完成电影", html)

    def test_overview_deduplicates_attention_and_active_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            failed = store.upsert_task("failed-only", "", "https://115cdn.com/s/failed-only")
            store.record_event(
                failed.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "failed",
                title="只在关注栏",
                error_summary="需要处理",
            )
            pending = store.upsert_task("queue-only", "", "https://115cdn.com/s/queue-only")
            store.record_event(
                pending.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "waiting",
                title="只在队列栏",
                metadata_patch={"_defer_message": "等待 CMS 整理完成"},
                next_run_at=9999999999.0,
            )

            page_html = render_task_list(store)

            self.assertEqual(page_html.count("只在关注栏"), 1)
            self.assertEqual(page_html.count("只在队列栏"), 1)
            attention_html = page_html.split('data-section="attention"', 1)[1].split('data-section="queue"', 1)[0]
            queue_html = page_html.split('data-section="queue"', 1)[1].split('data-section="maintenance"', 1)[0]
            self.assertIn("只在关注栏", attention_html)
            self.assertNotIn("只在队列栏", attention_html)
            self.assertIn("只在队列栏", queue_html)
            self.assertNotIn("只在关注栏", queue_html)

    def test_overview_keeps_attention_overflow_accessible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            titles = [f"关注任务 {index}" for index in range(10)]
            for index, title in enumerate(titles):
                task = store.upsert_task(f"failed-{index}", "", f"https://115cdn.com/s/failed-{index}")
                store.record_event(
                    task.id,
                    TaskStage.STRM_READY,
                    TaskStatus.FAILED,
                    "failed",
                    title=title,
                    error_summary="需要处理",
                )

            page_html = render_task_list(store)

            for title in titles:
                self.assertIn(title, page_html)
            self.assertIn('data-section="attention"', page_html)
            self.assertIn('data-section="queue"', page_html)
            attention_html = page_html.split('data-section="attention"', 1)[1].split('data-section="queue"', 1)[0]
            visible_html, overflow_html = attention_html.split('<details class="overflow-tasks">', 1)
            recent_titles = list(reversed(titles))
            for title in recent_titles[:8]:
                self.assertIn(title, visible_html)
                self.assertNotIn(title, overflow_html)
            for title in recent_titles[8:]:
                self.assertNotIn(title, visible_html)
                self.assertIn(title, overflow_html)
            self.assertIn("<summary>查看其余 2 项</summary>", overflow_html)

    def test_overview_phase_track_spans_task_row_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("phase-row", "", "https://115cdn.com/s/phase-row")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "waiting",
                title="阶段布局任务",
                next_run_at=0,
            )

            page_html = render_task_list(store)
            queue_html = page_html.split('data-section="queue"', 1)[1].split('data-section="maintenance"', 1)[0]
            phase_rules = re.findall(r"\.phase-track\s*\{([^}]*)\}", page_html)

            self.assertIn('</a><div class="phase-track"', queue_html)
            self.assertIn('.task-row > .phase-track { grid-column: 1 / -1;', page_html)
            self.assertTrue(any("overflow-x: auto" in declarations for declarations in phase_rules))
            self.assertTrue(all("overflow-x: visible" not in declarations for declarations in phase_rules))

    def test_overview_does_not_load_queue_event_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("no-events", "", "https://115cdn.com/s/no-events")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "running",
                title="无需事件历史",
                next_run_at=0,
            )

            with patch.object(store, "list_events", wraps=store.list_events) as list_events:
                render_task_list(store)

            list_events.assert_not_called()

    def test_overview_shows_active_cooldown_remaining_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cooldown-active", "", "https://115cdn.com/s/cooldown-active")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "waiting",
                metadata_patch={"p115_risk_cooldown_until": 1125.0},
                next_run_at=1125.0,
            )

            with patch("app.web.time.time", return_value=1000.0):
                page_html = render_task_list(store)

            self.assertIn("115 风控冷却中，剩余 2 分钟", page_html)
            self.assertNotIn("115 未冷却", page_html)

    def test_overview_ignores_expired_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cooldown-expired", "", "https://115cdn.com/s/cooldown-expired")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.PENDING,
                "waiting",
                metadata_patch={"p115_risk_cooldown_until": 999.0},
                next_run_at=1001.0,
            )

            with patch("app.web.time.time", return_value=1000.0):
                page_html = render_task_list(store)

            self.assertIn("115 未冷却", page_html)
            self.assertNotIn("115 风控冷却中", page_html)

    def test_overview_limits_queue_rows_but_counts_all_active_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            titles = [f"队列任务-{index:02d}" for index in range(26)]
            for index, title in enumerate(titles):
                task = store.upsert_task(f"queue-{index}", "", f"https://115cdn.com/s/queue-{index}")
                store.record_event(
                    task.id,
                    TaskStage.ORGANIZING,
                    TaskStatus.PENDING,
                    "waiting",
                    title=title,
                    next_run_at=0,
                )

            page_html = render_task_list(store)
            queue_html = page_html.split('data-section="queue"', 1)[1].split('data-section="maintenance"', 1)[0]

            self.assertEqual(queue_html.count('class="task-row"'), 25)
            for title in reversed(titles[1:]):
                self.assertIn(title, queue_html)
            self.assertNotIn(titles[0], page_html)
            self.assertIn('<div class="stat-label">运行中</div><div class="stat-value">26</div>', page_html)
            self.assertIn("26 个活跃任务，0 个需关注", page_html)

    def test_render_task_list_contains_task_stage_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM missing",
                title="示例电影",
                error_type="strm_missing",
                error_summary="未找到 STRM",
            )

            html = render_task_list(store)

            self.assertIn("示例电影", html)
            self.assertIn("STRM 生成", html)
            self.assertIn("未找到 STRM", html)
            self.assertIn(f"/task/{task.id}", html)
            self.assertIn('action="/history/clear"', html)
            self.assertIn("只清除已结束任务记录", html)
            self.assertIn("清理已结束记录", html)
            self.assertIn("需要关注", html)
            self.assertIn("未找到 STRM", html)
            self.assertIn("查看详情", html)
            self.assertIn("status-failed", html)


    def test_render_task_title_prefers_folder_name_over_share_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("swfup1z3np7", "nkrk", "https://115cdn.com/s/swfup1z3np7?password=nkrk")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.RUNNING,
                "moved",
                title="https://115cdn.com/s/swfup1z3np7?password=nkrk",
                metadata_patch={"dest_path": "/mnt/user/Unraid/strm/转存/TV/S-实习医生格蕾-2005-[tmdb=1416]"},
            )

            list_html = render_task_list(store)
            detail_html = render_task_detail(store, task.id)

            self.assertIn("S-实习医生格蕾-2005-[tmdb=1416]", list_html)
            self.assertIn("S-实习医生格蕾-2005-[tmdb=1416]", detail_html)
            self.assertNotIn("swfup1z3np7</td>", list_html)
            self.assertNotIn("标题：https://115cdn.com", detail_html)

    def test_render_task_list_shows_lock_wait_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待资源锁",
                title="等待电影",
                metadata_patch={
                    "_lock_key": "115:global",
                    "_lock_reason": "115/CMS 全局阶段",
                    "_lock_waiting": True,
                    "_lock_owner_task_id": 9,
                },
            )

            html = render_task_list(store)

            self.assertIn("等待资源锁: #9 115/CMS 全局阶段", html)

    def test_render_task_list_and_detail_show_observability_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.RUNNING,
                "等待自有分享 STRM 源目录生成",
                title="等待电影",
                metadata_patch={
                    "_defer_message": "等待自有分享 STRM 源目录生成",
                    "_defer_count": 3,
                    "stage_elapsed_seconds": 8.2,
                    "stage_wait_seconds": 15.0,
                    "stage_elapsed_seconds_by_stage": {
                        "organizing": 3.0,
                        "strm_ready": 8.2,
                    },
                    "p115_stage_request_count": 1,
                    "p115_total_request_count": 6,
                    "p115_request_counts_by_stage": {
                        "organizing": 2,
                        "strm_ready": 1,
                    },
                },
                next_run_at=9999999999.0,
            )

            list_html = render_task_list(store)
            detail_html = render_task_detail(store, task.id)

            self.assertIn("为什么慢：等分享 STRM 生成", list_html)
            self.assertIn("耗时：执行 8.2 秒，排队/等待 15 秒", list_html)
            self.assertIn("115调用：本阶段1次/累计6次", list_html)
            self.assertIn("为什么慢", detail_html)
            self.assertIn("等分享 STRM 生成", detail_html)
            self.assertIn('<div class="summary-label">115 调用</div><div class="summary-value">本阶段1次/累计6次</div>', detail_html)
            self.assertIn("CMS 整理 3 秒", detail_html)
            self.assertIn("STRM 生成 8.2 秒", detail_html)
            self.assertIn("CMS 整理 2次", detail_html)
            self.assertIn("STRM 生成 1次", detail_html)

    def test_render_task_list_treats_unscheduled_running_task_as_attention_not_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("orphan", "", "https://115cdn.com/s/orphan")
            store.record_event(
                task.id,
                TaskStage.CMS_SUBMITTED,
                TaskStatus.RUNNING,
                "链接已存在",
                title="历史遗留任务",
            )

            html = render_task_list(store)
            detail_html = render_task_detail(store, task.id)

            self.assertIn('<div class="stat-label">运行中</div><div class="stat-value">0</div>', html)
            self.assertIn('<div class="stat-label">需处理/失败</div><div class="stat-value">1</div>', html)
            self.assertIn("不在自动调度队列", html)
            self.assertIn('<span class="badge status-attention">需处理</span>', html)
            self.assertIn('<span class="badge status-attention">需处理</span>', detail_html)
            self.assertIn("历史遗留任务", html)
            attention_html = html.split('data-section="attention"', 1)[1].split('data-section="queue"', 1)[0]
            queue_html = html.split('data-section="queue"', 1)[1].split('data-section="maintenance"', 1)[0]
            self.assertEqual(attention_html.count("历史遗留任务"), 1)
            self.assertNotIn("历史遗留任务", queue_html)

    def test_render_task_list_does_not_count_cleared_lock_reason_as_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(
                task.id,
                TaskStage.NEEDS_ACTION,
                TaskStatus.NEEDS_ACTION,
                "需要人工检查",
                title="人工任务",
                metadata_patch={
                    "_defer_message": "等待 CMS 整理完成",
                    "_lock_reason": "115/CMS 全局阶段",
                    "_lock_waiting": False,
                },
                error_summary="需要人工检查",
            )

            html = render_task_list(store)

            self.assertIn('<div class="stat-label">等待资源</div><div class="stat-value">0</div>', html)
            self.assertIn("需要人工检查", html)

    def test_clear_history_endpoint_removes_finished_tasks_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            done = store.upsert_task("done", "", "https://115cdn.com/s/done")
            store.record_event(done.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "failed")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            store.enqueue_task(pending.id, TaskStage.RECEIVED, next_run_at=0)
            manual = store.upsert_task("manual", "", "https://115cdn.com/s/manual")
            store.record_event(manual.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "choose category")
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", "/history/clear", {}, b"")
            remaining = {task.share_code for task in store.list_recent_tasks(limit=10)}

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/")
            self.assertEqual(remaining, {"pending", "manual"})
            self.assertEqual(store.list_events(done.id), [])
            self.assertEqual(store.list_events(failed.id), [])
            self.assertEqual(body, b"")

    def test_render_task_detail_contains_event_timeline_and_retry_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.CMS_SUBMITTED, TaskStatus.SUCCEEDED, "CMS submitted")
            store.record_event(task.id, TaskStage.EMBY_CONFIRMED, TaskStatus.FAILED, "STRM missing", error_summary="未找到 STRM")

            html = render_task_detail(store, task.id)

            self.assertIn("CMS submitted", html)
            self.assertIn("STRM missing", html)
            self.assertIn(f'action="/task/{task.id}/retry"', html)
            self.assertIn("重试当前阶段", html)
            self.assertIn(f'action="/task/{task.id}/emby"', html)
            self.assertIn("查 Emby", html)
            self.assertIn(f'action="/task/{task.id}/restore"', html)
            self.assertIn("恢复 STRM", html)
            self.assertIn(f'action="/task/{task.id}/reprocess"', html)
            self.assertIn("从头重跑", html)
            self.assertIn("任务详情", html)
            self.assertIn("任务摘要", html)
            self.assertIn("处理时间线", html)
            self.assertIn("detail-grid", html)
            self.assertIn("timeline", html)

    def test_task_detail_prioritizes_recommendation_and_isolates_danger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("detail-focus", "", "https://115cdn.com/s/detail-focus")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM missing",
                title="示例电影",
                error_summary="未找到 STRM",
                metadata_patch={
                    "dest_path": "/media/示例电影",
                    "emby_parent": "电影库",
                },
            )

            page_html = render_task_detail(store, task.id)
            self.assertIn('class="incident-strip"', page_html)
            self.assertIn('<details class="danger-zone">', page_html)
            incident_html = page_html.split('class="incident-strip"', 1)[1].split("</section>", 1)[0]
            danger_html = page_html.split('<details class="danger-zone">', 1)[1].split("</details>", 1)[0]

            self.assertIn("示例电影", page_html)
            self.assertIn("未找到 STRM", incident_html)
            self.assertIn('<button class="button button-primary" type="submit">重试当前阶段</button>', incident_html)
            self.assertIn('<details class="diagnostic-details">', page_html)
            self.assertIn("<summary>技术详情与文件路径</summary>", page_html)
            self.assertIn("<summary>高风险操作</summary>", danger_html)
            self.assertEqual(
                re.findall(r'<div class="summary-label">([^<]+)</div>', page_html),
                ["当前阶段", "目标媒体库", "为什么慢", "执行耗时", "115 调用", "推荐操作"],
            )
            self.assertEqual(page_html.count(f'action="/task/{task.id}/reprocess"'), 1)
            self.assertIn(f'action="/task/{task.id}/reprocess"', danger_html)
            self.assertIn("return confirm('将从接收阶段重新执行该任务。确定继续？')", danger_html)
            self.assertLess(page_html.index("重试当前阶段"), page_html.index("从头重跑"))

    def test_task_detail_shows_recent_events_newest_first_and_folds_older_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("event-order", "", "https://115cdn.com/s/event-order")
            messages = [f"事件消息 {index:02d}" for index in range(1, 11)]
            for message in messages:
                store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.SUCCEEDED, message)

            page_html = render_task_detail(store, task.id)
            self.assertIn('<ul class="timeline recent-timeline">', page_html)
            self.assertIn('<details class="older-events">', page_html)
            recent_html = page_html.split('<ul class="timeline recent-timeline">', 1)[1].split("</ul>", 1)[0]
            older_html = page_html.split('<details class="older-events">', 1)[1].split("</details>", 1)[0]

            self.assertLess(recent_html.index(messages[-1]), recent_html.index(messages[-2]))
            for message in messages[-8:]:
                self.assertIn(message, recent_html)
            for message in messages[:2]:
                self.assertNotIn(message, recent_html)
                self.assertIn(message, older_html)
            self.assertIn("<summary>查看更早事件</summary>", older_html)
            for message in messages:
                self.assertEqual(page_html.count(message), 1)

    def test_claimed_running_task_hides_and_rejects_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("claimed-running", "", "https://115cdn.com/s/claimed-running")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=0)
            claimed = store.claim_next_runnable("worker-1", now=0)
            before = store.find_task(task.id)
            events_before = store.list_events(task.id)
            app = WebApp(store, web_token="")

            page_html = render_task_detail(store, task.id)
            status, headers, body = app.handle_request("POST", f"/task/{task.id}/retry", {}, b"")
            updated = store.find_task(task.id)

            self.assertIsNotNone(claimed)
            self.assertNotIn(f'action="/task/{task.id}/retry"', page_html)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated, before)
            self.assertEqual(store.list_events(task.id), events_before)
            self.assertIsNone(store.claim_next_runnable("worker-2", now=0))
            self.assertEqual(body, b"")

    def test_claimed_failed_task_preserves_owner_until_claim_is_cleared(self):
        for action in ("retry", "reprocess"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                store = TaskStore(Path(tmp) / "tasks.db")
                task = store.upsert_task(f"claimed-failed-{action}", "", f"https://115cdn.com/s/claimed-failed-{action}")
                store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=0)
                store.claim_next_runnable("worker-a", now=0)
                store.record_event(
                    task.id,
                    TaskStage.STRM_READY,
                    TaskStatus.FAILED,
                    "STRM missing",
                    error_summary="未找到 STRM",
                )
                before = store.find_task(task.id)
                events_before = store.list_events(task.id)
                app = WebApp(store, web_token="")

                page_html = render_task_detail(store, task.id)
                status, headers, body = app.handle_request("POST", f"/task/{task.id}/{action}", {}, b"")

                self.assertEqual(before.claimed_by, "worker-a")
                self.assertNotIn(f'action="/task/{task.id}/retry"', page_html)
                self.assertNotIn(f'action="/task/{task.id}/reprocess"', page_html)
                self.assertEqual(status, 303)
                self.assertEqual(headers["Location"], f"/task/{task.id}")
                self.assertEqual(store.find_task(task.id), before)
                self.assertEqual(store.list_events(task.id), events_before)
                self.assertIsNone(store.claim_next_runnable("worker-b", now=0))
                self.assertEqual(body, b"")

                store.record_event(
                    task.id,
                    TaskStage.STRM_READY,
                    TaskStatus.FAILED,
                    "已释放失败任务 claim",
                    error_summary="未找到 STRM",
                    clear_claim=True,
                )
                released_html = render_task_detail(store, task.id)

                self.assertEqual(store.find_task(task.id).claimed_by, "")
                self.assertIn(f'action="/task/{task.id}/retry"', released_html)
                self.assertIn(f'action="/task/{task.id}/reprocess"', released_html)

    def test_fresh_pending_task_hides_and_rejects_all_recovery_actions(self):
        for action in ("retry", "emby", "restore", "reprocess"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                store = TaskStore(Path(tmp) / "tasks.db")
                task = store.upsert_task(f"fresh-{action}", "", f"https://115cdn.com/s/fresh-{action}")
                before = store.find_task(task.id)
                app = WebApp(store, web_token="")

                page_html = render_task_detail(store, task.id)
                status, headers, body = app.handle_request("POST", f"/task/{task.id}/{action}", {}, b"")

                self.assertNotIn(f'action="/task/{task.id}/{action}"', page_html)
                self.assertEqual(status, 303)
                self.assertEqual(headers["Location"], f"/task/{task.id}")
                self.assertEqual(store.find_task(task.id), before)
                self.assertEqual(store.list_events(task.id), [])
                self.assertEqual(body, b"")

    def test_scheduled_active_task_rejects_downstream_and_reprocess_actions(self):
        for action in ("emby", "restore", "reprocess"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                store = TaskStore(Path(tmp) / "tasks.db")
                task = store.upsert_task(f"scheduled-{action}", "", f"https://115cdn.com/s/scheduled-{action}")
                store.enqueue_task(task.id, TaskStage.MOVED, next_run_at=9999999999.0)
                before = store.find_task(task.id)
                events_before = store.list_events(task.id)
                app = WebApp(store, web_token="")

                page_html = render_task_detail(store, task.id)
                status, headers, body = app.handle_request("POST", f"/task/{task.id}/{action}", {}, b"")

                self.assertNotIn(f'action="/task/{task.id}/{action}"', page_html)
                self.assertEqual(status, 303)
                self.assertEqual(headers["Location"], f"/task/{task.id}")
                self.assertEqual(store.find_task(task.id), before)
                self.assertEqual(store.list_events(task.id), events_before)
                self.assertEqual(body, b"")

    def test_unscheduled_legacy_task_allows_reprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("legacy-reprocess", "", "https://115cdn.com/s/legacy-reprocess")
            store.record_event(task.id, TaskStage.CMS_SUBMITTED, TaskStatus.RUNNING, "legacy task")
            app = WebApp(store, web_token="")

            page_html = render_task_detail(store, task.id)
            status, headers, body = app.handle_request("POST", f"/task/{task.id}/reprocess", {}, b"")
            updated = store.find_task(task.id)

            self.assertIn(f'action="/task/{task.id}/reprocess"', page_html)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertTrue(updated.metadata["force_reprocess"])
            self.assertEqual(body, b"")

    def test_terminal_and_downstream_tasks_show_only_eligible_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            cleaned = store.upsert_task("cleaned-actions", "", "https://115cdn.com/s/cleaned-actions")
            store.record_event(cleaned.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            failed_emby = store.upsert_task("failed-emby-actions", "", "https://115cdn.com/s/failed-emby-actions")
            store.record_event(failed_emby.id, TaskStage.EMBY_CONFIRMED, TaskStatus.FAILED, "failed")

            cleaned_html = render_task_detail(store, cleaned.id)
            failed_emby_html = render_task_detail(store, failed_emby.id)

            self.assertNotIn(f'action="/task/{cleaned.id}/retry"', cleaned_html)
            for action in ("emby", "restore", "reprocess"):
                self.assertIn(f'action="/task/{cleaned.id}/{action}"', cleaned_html)
                self.assertIn(f'action="/task/{failed_emby.id}/{action}"', failed_emby_html)

    def test_normal_eventless_task_uses_neutral_incident_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("neutral-copy", "", "https://115cdn.com/s/neutral-copy")

            page_html = render_task_detail(store, task.id)

            self.assertIn('class="incident-strip is-neutral"', page_html)
            self.assertIn("等待任务引擎执行", page_html)
            self.assertIn("任务正在按计划处理", page_html)
            self.assertNotIn("将从当前阶段重试", page_html)

    def test_task_detail_timeline_handles_empty_and_fold_boundary(self):
        for event_count in (0, 8, 9):
            with self.subTest(event_count=event_count), tempfile.TemporaryDirectory() as tmp:
                store = TaskStore(Path(tmp) / "tasks.db")
                task = store.upsert_task(f"events-{event_count}", "", f"https://115cdn.com/s/events-{event_count}")
                messages = [f"边界事件 {index:02d}" for index in range(1, event_count + 1)]
                for message in messages:
                    store.record_event(task.id, TaskStage.ORGANIZING, TaskStatus.SUCCEEDED, message)

                page_html = render_task_detail(store, task.id)

                if event_count == 0:
                    self.assertIn("暂无处理事件", page_html)
                if event_count <= 8:
                    self.assertNotIn('<details class="older-events">', page_html)
                else:
                    recent_html = page_html.split('<ul class="timeline recent-timeline">', 1)[1].split("</ul>", 1)[0]
                    older_html = page_html.split('<details class="older-events">', 1)[1].split("</details>", 1)[0]
                    self.assertEqual(recent_html.count("边界事件"), 8)
                    self.assertEqual(older_html.count("边界事件"), 1)
                for message in messages:
                    self.assertEqual(page_html.count(message), 1)

    def test_task_detail_long_title_has_overflow_containment(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("long-title", "", "https://115cdn.com/s/long-title")
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done", title="长" * 500)

            page_html = render_task_detail(store, task.id)

            self.assertIn('<h1 class="task-detail-title">', page_html)
            self.assertIn(".topbar > div { min-width: 0; }", page_html)
            self.assertIn(".task-detail-title { max-width: 100%; overflow-wrap: anywhere; }", page_html)

    def test_task_routes_return_404_for_malformed_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("strict-routes", "", "https://115cdn.com/s/strict-routes")
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")
            events_before = store.list_events(task.id)
            app = WebApp(store, web_token="")

            cases = [
                ("GET", "/task/"),
                ("GET", "/task/not-a-number"),
                ("GET", f"/task/{task.id}/extra"),
                ("POST", "/task/not-a-number/retry"),
                ("POST", "/task/not-a-number/emby"),
                ("POST", "/task/not-a-number/restore"),
                ("POST", "/task/not-a-number/reprocess"),
                ("POST", f"/task/{task.id}/extra/emby"),
            ]
            for method, path in cases:
                with self.subTest(method=method, path=path):
                    status, headers, body = app.handle_request(method, path, {}, b"")
                    self.assertEqual(status, 404)
                    self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
                    self.assertEqual(body, b"not found")
            self.assertEqual(store.find_task(task.id).status, TaskStatus.SUCCEEDED)
            self.assertEqual(store.find_task(task.id).current_stage, TaskStage.CLEANED)
            self.assertEqual(store.list_events(task.id), events_before)

    def test_retry_endpoint_enqueues_failed_stage_for_worker_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM missing",
                error_summary="未找到 STRM",
                clear_claim=True,
            )
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1.0)
            store.claim_next_runnable("stale-worker", now=1.0)
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.FAILED,
                "STRM missing",
                error_summary="未找到 STRM",
                clear_claim=True,
            )
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", f"/task/{task.id}/retry", {}, b"")
            updated = store.find_task(task.id)
            events = store.list_events(task.id)
            claimed = store.claim_next_runnable("worker", now=0)

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.STRM_READY)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.next_run_at, 0)
            self.assertEqual(updated.retry_count, 1)
            self.assertTrue(any(event["message"] == "手动触发重试" for event in events))
            self.assertTrue(any(event["message"] == "手动重试已入队" for event in events))
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.STRM_READY)
            self.assertEqual(body, b"")

    def test_reprocess_endpoint_requeues_task_from_received_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "1234", "https://115cdn.com/s/abc?password=1234")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "cleanup complete",
                title="重跑电影",
                metadata_patch={"own_share_code": "ownabc"},
            )
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", f"/task/{task.id}/reprocess", {}, b"")
            updated = store.find_task(task.id)
            events = store.list_events(task.id)
            claimed = store.claim_next_runnable("worker", now=0)

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.RECEIVED)
            self.assertEqual(updated.next_run_at, 0)
            self.assertEqual(updated.claimed_by, "")
            self.assertEqual(updated.retry_count, 1)
            self.assertEqual(updated.metadata["retry_from_stage"], TaskStage.CLEANED.value)
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.RECEIVED.value)
            self.assertTrue(updated.metadata["force_reprocess"])
            self.assertTrue(any(event["message"] == "Web 触发从头重跑" for event in events))
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, task.id)
            self.assertEqual(claimed.current_stage, TaskStage.RECEIVED)
            self.assertEqual(body, b"")

    def test_emby_endpoint_enqueues_emby_confirmation_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.MOVED, TaskStatus.SUCCEEDED, "moved")
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", f"/task/{task.id}/emby", {}, b"")
            updated = store.find_task(task.id)
            claimed = store.claim_next_runnable("worker", now=0)
            events = store.list_events(task.id)

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(updated.next_run_at, 0)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertTrue(any(event["message"] == "Web 触发 Emby 检查" for event in events))
            self.assertEqual(body, b"")

    def test_restore_endpoint_enqueues_emby_confirmation_restore_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                metadata_patch={"dest_path": "/missing/movie", "own_share_code": "ownabc"},
            )
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", f"/task/{task.id}/restore", {}, b"")
            updated = store.find_task(task.id)
            claimed = store.claim_next_runnable("worker", now=0)
            events = store.list_events(task.id)

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, TaskStatus.PENDING)
            self.assertEqual(updated.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(updated.metadata["retry_from_stage"], TaskStage.CLEANED.value)
            self.assertEqual(updated.metadata["retry_stage"], TaskStage.EMBY_CONFIRMED.value)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertTrue(any(event["message"] == "Web 触发 STRM 恢复" for event in events))
            self.assertEqual(body, b"")

    def test_quality_page_runs_local_taskstore_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / "tasks.db")
            dest = root / "direct"
            dest.mkdir()
            (dest / "movie.strm").write_text("https://115.com/d/direct.mkv", encoding="utf-8")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(
                task.id,
                TaskStage.MOVED,
                TaskStatus.SUCCEEDED,
                "moved",
                title="直链电影",
                metadata_patch={"dest_path": str(dest), "own_share_code": "ownabc"},
            )
            app = WebApp(store, web_token="")

            status, _headers, body = app.handle_request("GET", "/quality", {}, b"")
            html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertIn("TaskStore 本地轻量巡检", html)
            self.assertIn("直链电影", html)
            self.assertIn("发现直链 STRM", html)
            self.assertIn(str(dest / "movie.strm"), html)
            self.assertIn('action="/quality/fix"', html)
            self.assertIn("修复全部巡检问题", html)
            self.assertIn("本地质量巡检", html)
            self.assertIn("diagnostic", html)
            self.assertIn("不会扫描 115", html)

    def test_quality_fix_endpoint_restores_missing_dest_and_reprocesses_bad_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / "tasks.db")
            missing = store.upsert_task("missing", "", "https://115cdn.com/s/missing")
            store.record_event(
                missing.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                title="丢失目录",
                metadata_patch={"dest_path": str(root / "missing-dest"), "own_share_code": "ownmissing"},
            )
            direct_dest = root / "direct"
            direct_dest.mkdir()
            (direct_dest / "movie.strm").write_text("https://115.com/d/direct.mkv", encoding="utf-8")
            direct = store.upsert_task("direct", "", "https://115cdn.com/s/direct")
            store.record_event(
                direct.id,
                TaskStage.CLEANED,
                TaskStatus.SUCCEEDED,
                "done",
                title="直链电影",
                metadata_patch={"dest_path": str(direct_dest), "own_share_code": "owndirect"},
            )
            pending_dest = root / "pending"
            pending_dest.mkdir()
            (pending_dest / "movie.strm").write_text("https://115.com/d/direct.mkv", encoding="utf-8")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            store.record_event(
                pending.id,
                TaskStage.MOVED,
                TaskStatus.PENDING,
                "waiting",
                metadata_patch={"dest_path": str(pending_dest), "own_share_code": "ownpending"},
                next_run_at=0,
            )
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", "/quality/fix", {}, b"")
            missing_task = store.find_task(missing.id)
            direct_task = store.find_task(direct.id)
            pending_task = store.find_task(pending.id)
            missing_events = [event["message"] for event in store.list_events(missing.id)]
            direct_events = [event["message"] for event in store.list_events(direct.id)]

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/quality")
            self.assertEqual(missing_task.status, TaskStatus.PENDING)
            self.assertEqual(missing_task.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(missing_task.next_run_at, 0)
            self.assertEqual(missing_task.metadata["retry_stage"], TaskStage.EMBY_CONFIRMED.value)
            self.assertEqual(direct_task.status, TaskStatus.PENDING)
            self.assertEqual(direct_task.current_stage, TaskStage.RECEIVED)
            self.assertEqual(direct_task.next_run_at, 0)
            self.assertTrue(direct_task.metadata["force_reprocess"])
            self.assertEqual(pending_task.status, TaskStatus.PENDING)
            self.assertEqual(pending_task.current_stage, TaskStage.MOVED)
            self.assertIn("Web 巡检自动修复：恢复 STRM", missing_events)
            self.assertIn("Web 巡检自动修复：从头重跑", direct_events)
            self.assertEqual(body, b"")

    def test_health_page_shows_local_taskstore_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            pending = store.upsert_task("pending", "", "https://115cdn.com/s/pending")
            store.enqueue_task(pending.id, TaskStage.RECEIVED, next_run_at=0)
            running = store.upsert_task("running", "", "https://115cdn.com/s/running")
            store.enqueue_task(running.id, TaskStage.ORGANIZING, next_run_at=0)
            store.claim_next_runnable("worker", now=0)
            failed = store.upsert_task("failed", "", "https://115cdn.com/s/failed")
            store.record_event(failed.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", title="失败电影", error_summary="未找到 STRM")
            app = WebApp(store, web_token="")

            status, _headers, body = app.handle_request("GET", "/health", {}, b"")
            html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertIn("TaskStore 本地健康", html)
            self.assertIn("TaskEngine: ENABLED", html)
            self.assertIn("TaskStore最近任务: 3", html)
            self.assertIn("待执行: 1", html)
            self.assertIn("运行中: 1", html)
            self.assertIn("失败/需处理: 1", html)
            self.assertIn("最近问题: #3 失败电影", html)
            self.assertIn("本地队列健康", html)
            self.assertIn("diagnostic", html)
            self.assertIn("只展示本地 TaskStore 状态", html)

    def test_health_page_shows_taskstore_wait_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.PENDING,
                "等待自有分享 STRM",
                title="等待电影",
                metadata_patch={"_defer_message": "等待自有分享 STRM", "_defer_count": 2},
                next_run_at=9999999999.0,
            )
            app = WebApp(store, web_token="")

            status, _headers, body = app.handle_request("GET", "/health", {}, b"")
            html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertIn("等待自有分享 STRM", html)
            self.assertIn("第 2 次", html)

    def test_health_page_treats_unscheduled_running_task_as_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("orphan", "", "https://115cdn.com/s/orphan")
            store.record_event(
                task.id,
                TaskStage.CMS_SUBMITTED,
                TaskStatus.RUNNING,
                "链接已存在",
                title="历史遗留任务",
            )
            app = WebApp(store, web_token="")

            status, _headers, body = app.handle_request("GET", "/health", {}, b"")
            html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertIn("运行中: 0", html)
            self.assertIn("失败/需处理: 1", html)
            self.assertIn("不在自动调度队列", html)

    def test_health_page_shows_active_115_risk_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cooldown", "", "https://115cdn.com/s/cooldown")
            store.record_event(
                task.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "115 风控冷却中",
                title="冷却电影",
                metadata_patch={"p115_risk_cooldown_until": 9999999999.0},
                next_run_at=9999999999.0,
            )

            report = format_taskstore_health(store, enabled=True)

            self.assertIn("115风控冷却: ACTIVE", report)
            self.assertIn("剩余", report)

    def test_health_page_limits_wait_details_and_reports_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            for index in range(6):
                task = store.upsert_task(f"waiting-{index}", "", f"https://115cdn.com/s/waiting-{index}")
                store.record_event(
                    task.id,
                    TaskStage.STRM_READY,
                    TaskStatus.PENDING,
                    f"等待自有分享 STRM {index}",
                    title=f"等待电影 {index}",
                    metadata_patch={"_defer_message": f"等待自有分享 STRM {index}", "_defer_count": index + 1},
                    next_run_at=9999999999.0,
                )
            app = WebApp(store, web_token="")

            status, _headers, body = app.handle_request("GET", "/health", {}, b"")
            html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertEqual(html.count("等待详情: #"), 5)
            self.assertIn("等待详情: 另有 1 个任务等待中", html)
            self.assertIn("等待电影 5", html)
            self.assertNotIn("等待详情: #1 等待电影 0", html)

    def test_health_page_truncates_long_wait_detail_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            long_title = "等待电影" + "A" * 120
            long_reason = "等待自有分享 STRM" + "B" * 260
            store.record_event(
                task.id,
                TaskStage.STRM_READY,
                TaskStatus.PENDING,
                long_reason,
                title=long_title,
                metadata_patch={"_defer_message": long_reason, "_defer_count": 2},
                next_run_at=9999999999.0,
            )

            report = format_taskstore_health(store, enabled=True)
            wait_lines = [line for line in report.splitlines() if line.startswith("等待详情: #")]

            self.assertEqual(len(wait_lines), 1)
            self.assertIn("等待自有分享 STRM", wait_lines[0])
            self.assertIn("第 2 次", wait_lines[0])
            self.assertIn("...", wait_lines[0])
            self.assertNotIn("A" * 80, wait_lines[0])
            self.assertNotIn("B" * 160, wait_lines[0])
            self.assertLessEqual(len(wait_lines[0]), 240)

    def test_health_page_shows_lock_wait_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            waiting = store.upsert_task("waiting", "", "https://115cdn.com/s/waiting")
            store.record_event(
                waiting.id,
                TaskStage.ORGANIZING,
                TaskStatus.RUNNING,
                "等待资源锁",
                title="等待电影",
                metadata_patch={
                    "_lock_key": "115:global",
                    "_lock_reason": "115/CMS 全局阶段",
                    "_lock_waiting": True,
                    "_lock_owner_task_id": 7,
                },
            )
            app = WebApp(store, web_token="")

            status, _headers, body = app.handle_request("GET", "/health", {}, b"")
            html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertIn("锁等待: 1", html)
            self.assertIn("最近锁等待: #1 等待电影 / 115/CMS 全局阶段 / holder #7", html)

    def test_retry_endpoint_ignores_completed_cleaned_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "cleanup complete")
            before = store.find_task(task.id)
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", f"/task/{task.id}/retry", {}, b"")
            updated = store.find_task(task.id)
            claimed = store.claim_next_runnable("worker", now=0)

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, before.status)
            self.assertEqual(updated.current_stage, before.current_stage)
            self.assertEqual(updated.retry_count, before.retry_count)
            self.assertIsNone(claimed)
            self.assertEqual(body, b"")

    def test_retry_endpoint_ignores_manual_action_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.NEEDS_ACTION, TaskStatus.NEEDS_ACTION, "needs manual choice")
            before = store.find_task(task.id)
            app = WebApp(store, web_token="")

            status, headers, body = app.handle_request("POST", f"/task/{task.id}/retry", {}, b"")
            updated = store.find_task(task.id)
            claimed = store.claim_next_runnable("worker", now=0)

            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], f"/task/{task.id}")
            self.assertEqual(updated.status, before.status)
            self.assertEqual(updated.current_stage, before.current_stage)
            self.assertEqual(updated.retry_count, before.retry_count)
            self.assertIsNone(claimed)
            self.assertEqual(body, b"")


    def test_task_detail_lazy_backfills_legacy_submission_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_store = TaskStore(Path(tmp) / "tasks.db")
            existing = task_store.upsert_task("existing", "", "https://115cdn.com/s/existing")
            task_store.record_event(existing.id, TaskStage.RECEIVED, TaskStatus.PENDING, "已有 TaskStore 任务")
            submission_store = bridge.SubmissionStore(Path(tmp) / "submissions.db")
            submission_store.upsert_submission(
                bridge.ShareKey("dummy-1", ""),
                "https://115cdn.com/s/dummy-1",
                "done",
                title="占位旧记录一",
            )
            submission_store.upsert_submission(
                bridge.ShareKey("dummy-2", ""),
                "https://115cdn.com/s/dummy-2",
                "done",
                title="占位旧记录二",
            )
            row = submission_store.upsert_submission(
                bridge.ShareKey("legacy", ""),
                "https://115cdn.com/s/legacy",
                "done",
                title="旧电影",
            )
            row = submission_store.update_move(
                int(row["id"]),
                "moved",
                dest_path="/library/旧电影",
                category_final="欧美电影",
            ) or row
            row = submission_store.update_emby(
                int(row["id"]),
                "confirmed",
                item_id="emby-1",
                title="旧电影",
                path="/library/旧电影/movie.strm",
                parent="Strm欧美电影",
            ) or row
            app = WebApp(task_store, web_token="", submission_store=submission_store)

            status, headers, body = app.handle_request("GET", f"/task/{row['id']}", {}, b"")
            task = next(task for task in task_store.list_recent_tasks(limit=10) if task.share_code == "legacy")
            html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertEqual(task.share_code, "legacy")
            self.assertEqual(task.current_stage, TaskStage.EMBY_CONFIRMED)
            self.assertEqual(task.status, TaskStatus.SUCCEEDED)
            self.assertIn("旧电影", html)
            self.assertIn("Emby 确认", html)
            self.assertIn("Strm欧美电影", html)
            self.assertIn("打开详情页时懒回填旧记录", html)

    def test_web_token_blocks_requests_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store, web_token="secret")

            status, headers, body = app.handle_request("GET", "/", {}, b"")

            self.assertEqual(status, 403)
            self.assertIn(b"Forbidden", body)

    def test_overview_endpoint_reports_disabled_task_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            app = WebApp(store, web_token="", task_engine_enabled=False)

            status, headers, body = app.handle_request("GET", "/", {}, b"")
            page_html = body.decode("utf-8")

            self.assertEqual(status, 200)
            self.assertIn("任务引擎已停用", page_html)
            self.assertNotIn("任务引擎正常", page_html)


if __name__ == "__main__":
    unittest.main()
