import unittest

from app.clients.p115 import (
    P115WebClient,
    lixian_rsa_encrypt,
    normalize_cloud_status,
    validate_cloud_output,
)


ED2K = "ed2k://|file|Example.mkv|10|" + "ABCDEF0123456789" + "ABCDEF0123456789|/"
TARGET_CID = "3298928530653445613"
INFO_HASH = "ABCDEF0123456789" + "ABCDEF0123456789"


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, method="GET", data=None, headers=None, params=None):
        self.calls.append(
            {
                "url": url,
                "method": method,
                "data": dict(data or {}),
                "headers": dict(headers or {}),
                "params": dict(params or {}),
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


class P115CloudDownloadTests(unittest.TestCase):
    def test_lixian_rsa_encrypt_matches_reference_vector(self):
        self.assertEqual(
            lixian_rsa_encrypt(b"{}"),
            "QziJUnPHbi0I4oCpi2wbgE6JIoqYnjMAmJjQoYp53fHHWmueKuTw8Jcm1YyuCZhpSaKDV6bjXPp3+alZXHBq8RL8W6np85ltUboOBzs2fWLiQUTsi2R+epcGrbMp2etroEq9UggYRBlA1cN3ldvPF6+7bMiLYxQ98gylcTjBCOI=",
        )

    def test_cloud_download_add_sends_encrypted_payload_to_lixian_endpoint(self):
        http = FakeHttp([{"state": True, "data": {"info_hash": "HASH", "task_id": "task-1", "name": "Example"}}])
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_add(ED2K, TARGET_CID)

        self.assertEqual(result["info_hash"], "hash")
        self.assertEqual(result["task_id"], "task-1")
        call = http.calls[0]
        self.assertEqual(call["url"], "https://lixian.115.com/lixianssp/")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(set(call["data"]), {"data"})
        self.assertTrue(call["data"]["data"])
        self.assertEqual(
            call["headers"]["User-Agent"],
            "Mozilla/5.0 115disk/99.99.99.99 115Browser/99.99.99.99 115wangpan_android/99.99.99.99",
        )
        self.assertNotIn(ED2K, call["data"]["data"])

    def test_cloud_download_add_resolves_identity_from_task_list(self):
        http = FakeHttp(
            [
                {"state": True},
                {
                    "state": True,
                    "tasks": [
                        {"info_hash": "OTHER", "url": "ed2k://other", "status": 12},
                        {"info_hash": INFO_HASH, "url": ED2K, "status": 12},
                    ],
                },
            ]
        )
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_add(ED2K, TARGET_CID)

        self.assertEqual(result["info_hash"], INFO_HASH.lower())
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[1]["url"], "https://lixian.115.com/lixian/")

    def test_cloud_download_status_maps_completed(self):
        http = FakeHttp([{"state": True, "tasks": [{"status": 11, "info_hash": "HASH", "cid": "folder", "pid": TARGET_CID}]}])
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_status({"info_hash": "HASH"})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["file_id"], "folder")
        self.assertEqual(result["parent_id"], TARGET_CID)
        self.assertEqual(http.calls[0]["url"], "https://lixian.115.com/lixian/")
        self.assertEqual(http.calls[0]["params"], {"ct": "lixian", "ac": "task_lists", "page": 1, "page_size": 30})

    def test_cloud_download_status_maps_running_and_failed(self):
        self.assertEqual(normalize_cloud_status({"status": 12}), "running")
        self.assertEqual(normalize_cloud_status({"status": 9}), "failed")

    def test_cloud_download_status_matches_task_id_in_task_list(self):
        http = FakeHttp([
            {
                "state": True,
                "tasks": [
                    {"task_id": "other", "status": 11, "cid": "wrong", "pid": TARGET_CID},
                    {"task_id": "task-1", "status": 12, "cid": "folder", "pid": TARGET_CID},
                ],
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
