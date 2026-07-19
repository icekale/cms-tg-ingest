import unittest

from app.clients.p115 import P115WebClient, normalize_cloud_status, validate_cloud_output


ED2K = "ed2k://|file|Example.mkv|10|ABCDEF0123456789ABCDEF0123456789|/"
TARGET_CID = "3298928530653445613"


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, method="GET", data=None, headers=None, params=None):
        self.calls.append({"url": url, "method": method, "data": dict(data or {}), "params": dict(params or {})})
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


class P115CloudDownloadTests(unittest.TestCase):
    def test_cloud_download_add_sends_target_cid_and_empty_savepath(self):
        http = FakeHttp([{"state": True, "data": {"info_hash": "HASH", "task_id": "task-1", "name": "Example"}}])
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_add(ED2K, TARGET_CID)

        self.assertEqual(result["info_hash"], "hash")
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(http.calls, [{
            "url": "https://clouddownload.115.com/lixianssp/?ac=add_task_url",
            "method": "POST",
            "data": {"url": ED2K, "wp_path_id": TARGET_CID, "savepath": ""},
            "params": {},
        }])

    def test_cloud_download_status_maps_completed(self):
        http = FakeHttp([{"state": True, "data": {"status": 11, "info_hash": "HASH", "cid": "folder", "pid": TARGET_CID}}])
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_status({"info_hash": "HASH"})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["file_id"], "folder")
        self.assertEqual(result["parent_id"], TARGET_CID)
        self.assertEqual(http.calls[0]["url"], "https://clouddownload.115.com/?ac=get_user_task")

    def test_cloud_download_status_maps_running_and_failed(self):
        self.assertEqual(normalize_cloud_status({"status": 12}), "running")
        self.assertEqual(normalize_cloud_status({"status": 9}), "failed")

    def test_cloud_download_status_matches_task_id_in_task_list(self):
        http = FakeHttp([
            {
                "state": True,
                "data": {
                    "list": [
                        {"task_id": "other", "status": 11, "cid": "wrong", "pid": TARGET_CID},
                        {"task_id": "task-1", "status": 12, "cid": "folder", "pid": TARGET_CID},
                    ]
                },
            }
        ])
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_status({"task_id": "task-1"})

        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["file_id"], "folder")

    def test_cloud_download_output_rejects_wrong_parent_cid(self):
        with self.assertRaises(RuntimeError):
            validate_cloud_output({"file_id": "folder", "parent_id": "999"}, TARGET_CID)


if __name__ == "__main__":
    unittest.main()
