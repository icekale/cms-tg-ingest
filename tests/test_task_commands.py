from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.config import MoveConfig
from app.models import TaskStatus
from app.task_runner import TaskRunner
from app.task_store import TaskStore, command_key


class TaskCommandAndLeaseTests(unittest.TestCase):
    def test_enqueue_command_is_idempotent_and_rejects_payload_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cmd", "", "https://115cdn.com/s/cmd")
            first = store.enqueue_command(task.id, "retry", {"n": 1}, idempotency_key="retry:1", actor="Web")
            second = store.enqueue_command(task.id, "retry", {"n": 1}, idempotency_key="retry:1", actor="Web")
            self.assertEqual(first["id"], second["id"])
            with self.assertRaises(ValueError):
                store.enqueue_command(task.id, "retry", {"n": 2}, idempotency_key="retry:1", actor="Web")

    def test_command_claim_and_complete_are_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("cmd2", "", "https://115cdn.com/s/cmd2")
            store.enqueue_command(task.id, "emby_check", {}, idempotency_key="emby:1", actor="Web")
            claimed = store.claim_next_command("runner-a", now=1.0)
            self.assertEqual(claimed["command_type"], "emby_check")
            self.assertIsNone(store.claim_next_command("runner-b", now=1.0))
            self.assertFalse(store.complete_command(claimed["id"], "wrong-token", result={"ok": True}))
            self.assertTrue(store.complete_command(claimed["id"], claimed["claim_token"], result={"ok": True}))
            self.assertIsNone(store.claim_next_command("runner-a", now=2.0))

    def test_runner_lease_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tasks.db"
            a = TaskStore(db)
            b = TaskStore(db)
            first = a.acquire_runner_lease("owner-a", now=10.0, ttl_seconds=30)
            self.assertTrue(first)
            self.assertIsNone(b.acquire_runner_lease("owner-b", now=11.0, ttl_seconds=30))
            self.assertTrue(a.renew_runner_lease("owner-a", first, now=20.0, ttl_seconds=30))
            self.assertFalse(a.renew_runner_lease("owner-a", "stale-token", now=21.0, ttl_seconds=30))
            expired = b.acquire_runner_lease("owner-b", now=50.0, ttl_seconds=30)
            self.assertTrue(expired)
            self.assertFalse(a.release_runner_lease("owner-a", first))
            self.assertTrue(b.release_runner_lease("owner-b", expired))

    def test_observer_commands_are_idempotent_and_do_not_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            task = store.upsert_task("quality-cmd", "", "https://115cdn.com/s/quality-cmd")
            key = command_key("quality", task.id, "missing_strm", "1", "reprocess")
            first = store.enqueue_command(
                task.id,
                "quality_repair",
                {"action": "reprocess"},
                idempotency_key=key,
                actor="quality",
            )
            second = store.enqueue_command(
                task.id,
                "quality_repair",
                {"action": "reprocess"},
                idempotency_key=key,
                actor="quality",
            )
            current = store.find_task(task.id)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(current.claimed_by, "")
            self.assertNotIn("missing_strm", key)

    def test_command_key_hashes_material(self):
        key = command_key("repair-move", 447, "/library/L-movie")
        self.assertTrue(key.startswith("repair-move:"))
        self.assertNotIn("447", key)
        self.assertNotIn("/library/L-movie", key)
        self.assertEqual(key, command_key("repair-move", 447, "/library/L-movie"))
        self.assertNotEqual(key, command_key("repair-move", 447, "/library/other"))

    def test_invalidate_share_command_removes_validated_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "library" / "华语电影" / "S-示例-2026-[tmdb=123]"
            dest.mkdir(parents=True)
            (dest / "movie.strm").write_text("http://cms/s/owncode_1212_movie.mkv", encoding="utf-8")
            store = TaskStore(root / "tasks.db")
            task = store.upsert_task("src", "pass", "https://115cdn.com/s/src")
            store.write_facts(
                task.id,
                share={
                    "canonical_name": dest.name,
                    "own_share_code": "owncode",
                    "own_share_receive_code": "1212",
                    "file_id": "fid",
                },
                move={"move_status": "moved", "dest_path": str(dest)},
                emby={"status": "confirmed", "path": str(dest)},
            )
            store.enqueue_command(
                task.id,
                "invalidate_share",
                {"observed_state": "unavailable"},
                idempotency_key="invalid-share:1",
                actor="probe",
            )

            class Workflow:
                move_config = MoveConfig(source_roots=[], library_roots={"华语电影": dest.parent})

                def run_stage(self, task):
                    raise AssertionError("stage should not run")

            runner = TaskRunner(store, Workflow(), worker_id="inv")
            command = store.claim_next_command(runner.worker_id)
            runner._run_command(command)
            updated = store.find_task(task.id)
            facts = store.workflow_facts(task.id)
            self.assertFalse(dest.exists())
            self.assertEqual(updated.status, TaskStatus.NEEDS_ACTION)
            self.assertEqual(facts["move_status"], "invalid_share_cleaned")


if __name__ == "__main__":
    unittest.main()
