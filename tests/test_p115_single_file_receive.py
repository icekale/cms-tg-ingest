import unittest
from types import SimpleNamespace

from app.workflows.self_share import BridgeSelfShareTaskWorkflow


class P115SingleFileReceiveTests(unittest.TestCase):
    def test_hint_recovery_uses_real_file_id_and_parent_cid(self):
        class FakeP115:
            def list_files(self, parent_id, limit=500):
                self.call = (parent_id, limit)
                return [
                    {
                        "fid": "old-local-id",
                        "cid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "fc": 1,
                    },
                    {
                        "fid": "new-local-id",
                        "cid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "fc": 1,
                    },
                ]

        workflow = object.__new__(BridgeSelfShareTaskWorkflow)
        workflow.p115 = FakeP115()
        task = SimpleNamespace(
            source_type="share",
            metadata={
                "receive_target_cid": "pending-cid",
                "received_existing_file_ids": ["old-local-id"],
                "received_snapshot_complete": True,
            },
        )

        result = workflow._recover_received_items_for_hint(task, "1228710", 1)

        self.assertEqual(workflow.p115.call, ("pending-cid", 500))
        self.assertEqual(
            result,
            [
                {
                    "file_id": "new-local-id",
                    "file_name": "123 (2026) {tmdb-1228710}.mkv",
                    "is_folder": False,
                    "parent_id": "pending-cid",
                    "received_item_verified": True,
                }
            ],
        )

    def test_hint_recovery_rejects_legacy_snapshot_containing_target_cid(self):
        class FakeP115:
            def list_files(self, parent_id, limit=500):
                return [
                    {
                        "fid": "same-name-local-id",
                        "cid": "pending-cid",
                        "n": "123 (2026) {tmdb-1228710}.mkv",
                        "fc": 1,
                    }
                ]

        workflow = object.__new__(BridgeSelfShareTaskWorkflow)
        workflow.p115 = FakeP115()
        task = SimpleNamespace(
            source_type="share",
            metadata={
                "receive_target_cid": "pending-cid",
                "received_existing_file_ids": ["pending-cid"],
                "received_snapshot_complete": True,
            },
        )

        result = workflow._recover_received_items_for_hint(task, "1228710", 1)

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
