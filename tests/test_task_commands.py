from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

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

    def test_command_key_hashes_material(self):
        key = command_key("repair-move", 447, "/library/L-movie")
        self.assertTrue(key.startswith("repair-move:"))
        self.assertNotIn("447", key)
        self.assertNotIn("/library/L-movie", key)
        self.assertEqual(key, command_key("repair-move", 447, "/library/L-movie"))
        self.assertNotEqual(key, command_key("repair-move", 447, "/library/other"))


if __name__ == "__main__":
    unittest.main()
