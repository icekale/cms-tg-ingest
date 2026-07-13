import tempfile
import unittest
from pathlib import Path

import bridge
from app.clients.p115 import P115RiskControlError, P115ShareUnavailableError
from app.models import TaskStage
from app.self_share_health import probe_invalid_self_shares, probe_invalid_self_shares_if_idle
from app.task_store import TaskStore


class FakeEmby:
    enabled = True

    def __init__(self):
        self.refreshes = []

    def refresh_library_for_path(self, path):
        self.refreshes.append(str(path))
        return "电影库"


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))


class InvalidShareP115:
    def __init__(self):
        self.calls = 0

    def share_snap(self, share_code, receive_code, cid="0", limit=100):
        self.calls += 1
        raise P115ShareUnavailableError("分享已失效")


class RiskControlledP115:
    def share_snap(self, share_code, receive_code, cid="0", limit=100):
        raise P115RiskControlError("访问过于频繁")


class GenericErrorP115:
    def share_snap(self, share_code, receive_code, cid="0", limit=100):
        raise RuntimeError("115 service unavailable")


class InvalidShareCleanupTests(unittest.TestCase):
    def _row_with_share_strm(self, root):
        store = bridge.SubmissionStore(Path(root) / "submissions.db")
        destination = Path(root) / "library" / "华语电影" / "S-示例电影-2026-[tmdb=123]"
        destination.mkdir(parents=True)
        (destination / "movie.strm").write_text("http://cms/s/owncode_1212_movie.mkv", encoding="utf-8")
        row = store.upsert_submission(
            bridge.ShareKey("source", "pass"),
            "https://115cdn.com/s/source?password=pass",
            "received",
            title="示例电影",
        )
        row = store.update_self_share(
            int(row["id"]),
            workflow_mode="self_share_sync",
            own_share_file_name=destination.name,
            own_share_code="owncode",
            own_share_receive_code="1212",
        ) or row
        row = store.update_move(
            int(row["id"]),
            "moved",
            source_path=str(Path(root) / "share" / destination.name),
            dest_path=str(destination),
            category_final="华语电影",
        ) or row
        row = store.update_emby(int(row["id"]), "confirmed", path=str(destination), parent="电影库") or row
        move_config = bridge.MoveConfig(source_roots=[], library_roots={"华语电影": destination.parent})
        return store, row, destination, move_config

    def test_removes_only_validated_self_share_destination_when_share_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row, destination, move_config = self._row_with_share_strm(tmp)
            task_store = TaskStore(Path(tmp) / "tasks.db")
            emby = FakeEmby()
            telegram = FakeTelegram()

            summary = probe_invalid_self_shares(
                store,
                task_store,
                InvalidShareP115(),
                emby,
                telegram,
                "chat-id",
                move_config,
                limit=1,
            )

            updated = store.find_by_id(int(row["id"]))
            task = task_store.list_recent_tasks(limit=1)[0]
            self.assertEqual(summary.checked_count, 1)
            self.assertEqual(summary.cleaned_count, 1)
            self.assertFalse(destination.exists())
            self.assertEqual(updated["move_status"], "invalid_share_cleaned")
            self.assertEqual(updated["emby_status"], "invalid_share_cleaned")
            self.assertEqual(emby.refreshes, [str(bridge.safe_resolve(destination))])
            self.assertEqual(task.current_stage, bridge.TaskStage.NEEDS_ACTION)
            self.assertIn("分享已失效", telegram.messages[0][1])

    def test_keeps_destination_when_115_is_risk_controlled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row, destination, move_config = self._row_with_share_strm(tmp)

            summary = probe_invalid_self_shares(
                store,
                TaskStore(Path(tmp) / "tasks.db"),
                RiskControlledP115(),
                FakeEmby(),
                FakeTelegram(),
                "chat-id",
                move_config,
                limit=1,
            )

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(summary.checked_count, 1)
            self.assertEqual(summary.cleaned_count, 0)
            self.assertTrue(summary.risk_controlled)
            self.assertTrue(destination.exists())
            self.assertEqual(updated["move_status"], "moved")

    def test_keeps_destination_when_115_returns_an_unclassified_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row, destination, move_config = self._row_with_share_strm(tmp)

            summary = probe_invalid_self_shares(
                store,
                TaskStore(Path(tmp) / "tasks.db"),
                GenericErrorP115(),
                FakeEmby(),
                FakeTelegram(),
                "chat-id",
                move_config,
                limit=1,
            )

            updated = store.find_by_id(int(row["id"]))
            self.assertEqual(summary.checked_count, 1)
            self.assertEqual(summary.cleaned_count, 0)
            self.assertFalse(summary.risk_controlled)
            self.assertTrue(destination.exists())
            self.assertEqual(updated["move_status"], "moved")

    def test_skips_invalid_share_probe_while_task_engine_has_active_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row, destination, move_config = self._row_with_share_strm(tmp)
            task_store = TaskStore(Path(tmp) / "tasks.db")
            active = task_store.upsert_task("another-share", "", "https://115cdn.com/s/another-share")
            task_store.enqueue_task(active.id, TaskStage.RECEIVED)
            p115 = InvalidShareP115()

            summary = probe_invalid_self_shares_if_idle(
                store,
                task_store,
                p115,
                FakeEmby(),
                FakeTelegram(),
                "chat-id",
                move_config,
                limit=1,
            )

            self.assertEqual(summary.checked_count, 0)
            self.assertEqual(p115.calls, 0)
            self.assertTrue(destination.exists())
            self.assertEqual(store.find_by_id(int(row["id"]))["move_status"], "moved")

    def test_probes_when_only_unscheduled_taskstore_records_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, row, destination, move_config = self._row_with_share_strm(tmp)
            task_store = TaskStore(Path(tmp) / "tasks.db")
            task_store.upsert_task("stale-share", "", "https://115cdn.com/s/stale-share")
            p115 = InvalidShareP115()

            summary = probe_invalid_self_shares_if_idle(
                store,
                task_store,
                p115,
                FakeEmby(),
                FakeTelegram(),
                "chat-id",
                move_config,
                limit=1,
            )

            self.assertEqual(summary.checked_count, 1)
            self.assertEqual(summary.cleaned_count, 1)
            self.assertEqual(p115.calls, 1)
            self.assertFalse(destination.exists())
            self.assertEqual(store.find_by_id(int(row["id"]))["move_status"], "invalid_share_cleaned")
