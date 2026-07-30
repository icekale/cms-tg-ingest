import unittest

from app.clients.p115 import (
    P115CloudOutputPendingError,
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
    @staticmethod
    def _cloud_page(items):
        return {"state": True, "tasks": list(items)}

    def test_discover_cloud_outputs_returns_all_children_without_moving(self):
        http = FakeHttp(
            [
                {
                    "state": True,
                    "data": [
                        {"fid": "video", "cid": "container", "n": "S01E01.mkv"},
                        {"fid": "subtitle", "cid": "container", "n": "S01E01.zh.srt"},
                        {"cid": "season", "pid": "container", "n": "Season 01"},
                    ],
                }
            ]
        )
        client = P115WebClient("UID=1", http=http)

        items = client.discover_cloud_download_outputs({"file_id": "container"})

        self.assertEqual(
            [item["file_id"] for item in items],
            ["video", "subtitle", "season"],
        )
        self.assertEqual(items[0]["parent_id"], "container")
        self.assertFalse(items[0]["is_folder"])
        self.assertTrue(items[2]["is_folder"])
        self.assertFalse(any(call["url"].endswith("/files/move") for call in http.calls))

    def test_discover_cloud_outputs_empty_listing_is_retryable(self):
        client = P115WebClient("UID=1", http=FakeHttp([{"state": True, "data": []}]))

        with self.assertRaises(P115CloudOutputPendingError):
            client.discover_cloud_download_outputs({"file_id": "container"})

    def test_discover_cloud_outputs_propagates_listing_error(self):
        client = P115WebClient(
            "UID=1",
            http=FakeHttp([{"state": False, "error": "temporary listing failure"}]),
        )

        with self.assertRaisesRegex(RuntimeError, "temporary listing failure"):
            client.discover_cloud_download_outputs({"file_id": "container"})

    def test_discover_cloud_outputs_accepts_explicit_file_record(self):
        client = P115WebClient("UID=1", http=FakeHttp([]))

        items = client.discover_cloud_download_outputs(
            {"fid": "video", "cid": TARGET_CID, "n": "Example.mkv"}
        )

        self.assertEqual(
            items,
            [
                {
                    "file_id": "video",
                    "file_name": "Example.mkv",
                    "parent_id": TARGET_CID,
                    "is_folder": False,
                }
            ],
        )

    def test_discover_cloud_outputs_accepts_wrapped_raw_file_record(self):
        client = P115WebClient("UID=1", http=FakeHttp([]))

        items = client.discover_cloud_download_outputs(
            {
                "file_id": "container",
                "file_name": "Example.mkv",
                "raw": {"fid": "video", "cid": TARGET_CID, "n": "Example.mkv"},
            }
        )

        self.assertEqual(items[0]["file_id"], "video")
        self.assertEqual(items[0]["parent_id"], TARGET_CID)
        self.assertFalse(client.http.calls)

    def test_ensure_cloud_outputs_moves_only_missing_items_and_preserves_flags(self):
        http = FakeHttp(
            [
                {
                    "state": True,
                    "data": [{"fid": "video", "cid": TARGET_CID, "n": "S01E01.mkv"}],
                },
                {"state": True},
                {"state": True},
            ]
        )
        client = P115WebClient("UID=1", http=http)
        items = [
            {
                "file_id": "video",
                "file_name": "S01E01.mkv",
                "parent_id": "container",
                "is_folder": False,
            },
            {
                "file_id": "subtitle",
                "file_name": "S01E01.zh.srt",
                "parent_id": "container",
                "is_folder": False,
            },
            {
                "file_id": "season",
                "file_name": "Season 01",
                "parent_id": "container",
                "is_folder": True,
            },
        ]

        moved = client.ensure_cloud_outputs_in_target(items, TARGET_CID)

        self.assertEqual([item["file_id"] for item in moved], ["video", "subtitle", "season"])
        self.assertTrue(moved[2]["is_folder"])
        move_calls = [call for call in http.calls if call["url"].endswith("/files/move")]
        self.assertEqual([call["data"]["fid"] for call in move_calls], ["subtitle", "season"])
        self.assertEqual(len([call for call in http.calls if call["url"].endswith("/files")]), 1)

    def test_ensure_cloud_outputs_uses_target_listing_when_persisted_parent_matches(self):
        http = FakeHttp([{"state": True, "data": []}, {"state": True}])
        client = P115WebClient("UID=1", http=http)

        client.ensure_cloud_outputs_in_target(
            [
                {
                    "file_id": "video",
                    "file_name": "Example.mkv",
                    "parent_id": TARGET_CID,
                    "is_folder": False,
                }
            ],
            TARGET_CID,
        )

        move_calls = [call for call in http.calls if call["url"].endswith("/files/move")]
        self.assertEqual(len(move_calls), 1)

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
        http = FakeHttp([{"state": True, "tasks": [{"status": 2, "info_hash": "HASH", "file_id": "folder", "wp_path_id": TARGET_CID}]}])
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_status({"info_hash": "HASH"})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["file_id"], "folder")
        self.assertEqual(result["parent_id"], TARGET_CID)
        self.assertEqual(http.calls[0]["url"], "https://lixian.115.com/lixian/")
        self.assertEqual(http.calls[0]["params"], {"ct": "lixian", "ac": "task_lists", "page": 1, "page_size": 30})

    def test_find_cloud_download_by_source_returns_normalized_identity(self):
        http = FakeHttp(
            [
                {
                    "state": True,
                    "tasks": [
                        {
                            "status": 12,
                            "info_hash": INFO_HASH,
                            "task_id": "task-1",
                            "url": ED2K,
                            "fid": "folder",
                            "pid": TARGET_CID,
                            "name": "Example",
                        }
                    ],
                }
            ]
        )
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.find_cloud_download_by_source(ED2K)

        self.assertEqual(result["info_hash"], INFO_HASH.lower())
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["file_id"], "folder")
        self.assertEqual(result["parent_id"], TARGET_CID)

    def test_cloud_download_status_maps_running_and_failed(self):
        self.assertEqual(normalize_cloud_status({"status": 0}), "running")
        self.assertEqual(normalize_cloud_status({"status": 1}), "running")
        self.assertEqual(normalize_cloud_status({"status": 2}), "completed")
        self.assertEqual(normalize_cloud_status({"status": -1}), "failed")
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

    def test_cloud_download_status_paginates_only_after_a_full_page(self):
        unrelated = [
            {"task_id": f"other-{index}", "status": 12}
            for index in range(30)
        ]
        http = FakeHttp(
            [
                self._cloud_page(unrelated),
                self._cloud_page([{"task_id": "task-2", "status": 2, "fid": "folder"}]),
            ]
        )
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_status({"task_id": "task-2"})

        self.assertEqual(result["task_id"], "task-2")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(http.calls), 2)
        self.assertEqual([call["params"]["page"] for call in http.calls], [1, 2])
        self.assertTrue(all(call["params"]["page_size"] == 30 for call in http.calls))

    def test_cloud_download_source_lookup_is_bounded_to_three_full_pages(self):
        pages = [
            self._cloud_page(
                [{"task_id": f"other-{page}-{index}", "status": 12} for index in range(30)]
            )
            for page in range(1, 4)
        ]
        http = FakeHttp(pages)
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client._find_cloud_task(source_url="magnet:?xt=urn:btih:MISSING")

        self.assertEqual(result, {})
        self.assertEqual(len(http.calls), 3)
        self.assertEqual([call["params"]["page"] for call in http.calls], [1, 2, 3])

    def test_cloud_download_add_recovers_source_identity_from_second_page(self):
        http = FakeHttp(
            [
                {"state": True},
                self._cloud_page(
                    [{"task_id": f"other-{index}", "status": 12} for index in range(30)]
                ),
                self._cloud_page(
                    [{"info_hash": INFO_HASH, "url": ED2K, "status": 12}]
                ),
            ]
        )
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_add(ED2K, TARGET_CID)

        self.assertEqual(result["info_hash"], INFO_HASH.lower())
        list_calls = [call for call in http.calls if call["url"] == "https://lixian.115.com/lixian/"]
        self.assertEqual([call["params"]["page"] for call in list_calls], [1, 2])

    def test_cloud_download_lookup_stops_after_a_short_page(self):
        http = FakeHttp(
            [
                self._cloud_page(
                    [{"task_id": f"other-{index}", "status": 12} for index in range(2)]
                )
            ]
        )
        client = P115WebClient("UID=1", http=http, timeout=3)

        self.assertEqual(client._find_cloud_task(identity={"task_id": "missing"}), {})
        self.assertEqual(len(http.calls), 1)

    def test_cloud_download_output_rejects_wrong_parent_cid(self):
        with self.assertRaises(RuntimeError):
            validate_cloud_output({"file_id": "folder", "parent_id": "999"}, TARGET_CID)

    def test_cloud_download_output_moves_media_out_of_cloud_download_folder(self):
        http = FakeHttp(
            [
                {
                    "state": True,
                    "tasks": [
                        {
                            "status": 2,
                            "info_hash": "HASH",
                            "file_id": "cloud-folder",
                            "wp_path_id": TARGET_CID,
                            "name": "Example.mkv",
                        }
                    ],
                },
                {
                    "state": True,
                    "data": [{"cid": "cloud-folder", "fid": "media-file", "n": "Example.mkv"}],
                },
                {"state": True, "data": []},
                {"state": True},
            ]
        )
        client = P115WebClient("UID=1", http=http, timeout=3)

        result = client.cloud_download_output({"info_hash": "HASH"}, TARGET_CID)

        self.assertEqual(result["file_id"], "media-file")
        self.assertEqual(result["parent_id"], TARGET_CID)
        self.assertEqual(http.calls[3]["url"], "https://webapi.115.com/files/move")
        self.assertEqual(http.calls[3]["data"], {"fid": "media-file", "pid": TARGET_CID})


if __name__ == "__main__":
    unittest.main()
