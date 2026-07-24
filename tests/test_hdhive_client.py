import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.clients.cms import CmsClient
from app.clients.hdhive import HdhiveProxyClient, HdhiveProxyError


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, method="GET", payload=None, headers=None):
        self.calls.append((url, method, payload, headers or {}))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class HdhiveProxyClientTests(unittest.TestCase):
    def token_file(self, directory: str, access_token: str = "access-1") -> Path:
        path = Path(directory) / "hdhive-openapi.json"
        path.write_text(
            json.dumps({"access_token": access_token, "refresh_token": "refresh-should-not-be-used"}),
            encoding="utf-8",
        )
        return path

    def test_resources_posts_query_fields_and_normalizes_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            http = FakeHttp(
                [
                    {
                        "success": True,
                        "code": "200",
                        "data": [
                            {
                                "slug": "slug-1",
                                "title": "Example (2026)",
                                "pan_type": "115",
                                "share_size": "10GB",
                                "video_resolution": ["2160P"],
                                "source": ["WEB-DL"],
                                "subtitle_language": ["简中"],
                                "subtitle_type": ["内封"],
                                "unlock_points": 8,
                                "validate_status": "valid",
                                "validate_message": "链接有效",
                                "is_unlocked": False,
                            }
                        ],
                    }
                ]
            )
            client = HdhiveProxyClient("https://proxy.test", self.token_file(directory), http=http)

            resources = client.resources("movie", "550")

            self.assertEqual(resources[0].slug, "slug-1")
            self.assertEqual(resources[0].pan_type, "115")
            self.assertEqual(resources[0].unlock_points, 8)
            self.assertEqual(
                http.calls[0][0:3],
                (
                    "https://proxy.test/api/hdhive/resources",
                    "POST",
                    {"resource_type": "movie", "tmdb_id": "550", "access_token": "access-1"},
                ),
            )
            self.assertIn("Mozilla/5.0", http.calls[0][3]["User-Agent"])
            self.assertNotIn("refresh-should-not-be-used", repr(http.calls[0]))

    def test_resolve_tv_page_extracts_server_rendered_metadata(self):
        html = r'''
        <script>
        self.__next_f.push([1,"data:{\"id\":126561,\"slug\":\"542a1c1fe6ac4a5aab152369079596b5\",\"tmdb_id\":\"255358\",\"name\":\"攻壳机动队\",\"first_air_date\":\"2026-07-07\"}"])
        </script>
        '''
        with tempfile.TemporaryDirectory() as directory:
            client = HdhiveProxyClient(
                "https://proxy.test",
                self.token_file(directory),
                http=FakeHttp([]),
                page_fetcher=lambda _url: html,
            )

            page = client.resolve_tv_page(
                "https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5"
            )

            self.assertEqual(page.slug, "542a1c1fe6ac4a5aab152369079596b5")
            self.assertEqual(page.tmdb_id, "255358")
            self.assertEqual(page.title, "攻壳机动队")
            self.assertEqual(page.year, "2026")

    def test_resolve_tv_page_rejects_page_without_tmdb_id(self):
        with tempfile.TemporaryDirectory() as directory:
            client = HdhiveProxyClient(
                "https://proxy.test",
                self.token_file(directory),
                http=FakeHttp([]),
                page_fetcher=lambda _url: '"slug":"542a1c1fe6ac4a5aab152369079596b5","name":"无 TMDB"',
            )

            with self.assertRaises(HdhiveProxyError) as context:
                client.resolve_tv_page("https://hdhive.com/tv/542a1c1fe6ac4a5aab152369079596b5")
            self.assertEqual(context.exception.error_code, "HDHIVE_PAGE_UNRESOLVED")

    def test_unlock_uses_slug_for_one_and_slugs_for_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            http = FakeHttp(
                [
                    {"success": True, "code": "200", "data": {"url": "https://115cdn.com/s/a", "full_url": "https://115cdn.com/s/a?password=x", "already_owned": False}},
                    {
                        "success": True,
                        "code": "200",
                        "data": {
                            "items": [
                                {"slug": "slug-1", "success": True, "full_url": "https://115cdn.com/s/a?password=x", "already_owned": False},
                                {"slug": "slug-2", "success": False, "message": "积分不足", "error_code": "INSUFFICIENT_POINTS"},
                            ]
                        },
                    },
                ]
            )
            client = HdhiveProxyClient("https://proxy.test", self.token_file(directory), http=http)

            single = client.unlock(["slug-1"])
            batch = client.unlock(["slug-1", "slug-2"])

            self.assertTrue(single[0].success)
            self.assertEqual(batch[1].error_code, "INSUFFICIENT_POINTS")
            self.assertEqual(http.calls[0][2], {"slug": "slug-1", "access_token": "access-1"})
            self.assertEqual(http.calls[1][2], {"slugs": ["slug-1", "slug-2"], "access_token": "access-1"})

    def test_expired_access_token_delegates_refresh_to_cms_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.token_file(directory, access_token="expired")
            http = FakeHttp(
                [
                    {"success": False, "code": "OPENAPI_TOKEN_EXPIRED", "message": "expired"},
                    {"success": True, "code": "200", "data": []},
                ]
            )
            refreshed = []

            def refresh_via_cms():
                refreshed.append(True)
                path.write_text(json.dumps({"access_token": "fresh"}), encoding="utf-8")

            client = HdhiveProxyClient(
                "https://proxy.test",
                path,
                http=http,
                refresh_via_cms=refresh_via_cms,
            )

            client.resources("tv", "1399")

            self.assertEqual(refreshed, [True])
            self.assertEqual(http.calls[0][2]["access_token"], "expired")
            self.assertEqual(http.calls[1][2]["access_token"], "fresh")

    def test_missing_token_is_a_stable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            client = HdhiveProxyClient("https://proxy.test", path, http=FakeHttp([]))

            with self.assertRaisesRegex(HdhiveProxyError, "尚未在 CMS"):
                client.account()

    def test_cms_hdhive_info_and_tmdb_search_use_authorized_routes(self):
        config = SimpleNamespace(
            cms_base_url="https://cms.test",
            cms_username="user",
            cms_password="password",
            http_timeout=5,
        )
        http = FakeHttp(
            [
                {"code": 200, "data": {"token": "cms-token"}},
                {"code": 200, "data": {"nickname": "Kale"}},
                {"code": 200, "data": {"results": [{"id": 550, "title": "Example"}]}},
                {"code": 200, "data": {"results": [{"id": 1399, "name": "Example TV"}]}},
            ]
        )
        client = CmsClient(config, http=http)

        self.assertEqual(client.get_hdhive_info()["data"]["nickname"], "Kale")
        self.assertEqual(client.search_movie("Example")["data"]["results"][0]["id"], 550)
        self.assertEqual(client.search_tv("Example")["data"]["results"][0]["id"], 1399)
        self.assertEqual(http.calls[1][0:3], ("https://cms.test/api/hdhive/info", "GET", None))
        self.assertIn("keyword=Example", http.calls[2][0])
        self.assertIn("keyword=Example", http.calls[3][0])


if __name__ == "__main__":
    unittest.main()
