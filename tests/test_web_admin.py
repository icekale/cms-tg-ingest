import tempfile
import unittest
from pathlib import Path

import bridge
from app.models import TaskStage, TaskStatus
from app.task_store import TaskStore
from app.web import WebApp, render_task_detail, render_task_list


class WebAdminTests(unittest.TestCase):
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
            self.assertIn("资源锁", html)
            self.assertIn(f"/task/{task.id}", html)
            self.assertIn('action="/history/clear"', html)
            self.assertIn("只清除已结束任务记录", html)
            self.assertIn("清除历史记录", html)

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
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", error_summary="未找到 STRM")

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

    def test_retry_endpoint_enqueues_failed_stage_for_worker_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("abc", "", "https://115cdn.com/s/abc")
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", error_summary="未找到 STRM")
            store.enqueue_task(task.id, TaskStage.STRM_READY, next_run_at=1.0)
            store.claim_next_runnable("stale-worker", now=1.0)
            store.record_event(task.id, TaskStage.STRM_READY, TaskStatus.FAILED, "STRM missing", error_summary="未找到 STRM")
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
            store.enqueue_task(task.id, TaskStage.CLEANED, next_run_at=1.0)
            store.claim_next_runnable("stale-worker", now=1.0)
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


if __name__ == "__main__":
    unittest.main()
