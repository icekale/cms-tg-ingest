import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("bridge", Path(__file__).resolve().parents[1] / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class FakeOpenAI:
    enabled = True

    def __init__(self, result):
        self.result = result
        self.calls = []

    def classify_media(self, recognition, share_name):
        self.calls.append((dict(recognition), share_name))
        return dict(self.result)


def uncertain_recognition():
    return {
        "ok": False,
        "title": "",
        "type": "",
        "category": "",
        "tmdb_id": "",
        "raw_msg": "recognition failed",
        "share_name": "军中乐园（2014）{tmdb-287888}",
    }


class OpenAIFallbackTests(unittest.TestCase):
    def test_openai_high_confidence_becomes_confident_without_prompt(self):
        fallback = FakeOpenAI({
            "category": "华语电影",
            "confidence": 0.86,
            "media_type": "movie",
            "title": "军中乐园",
            "tmdb_id": "287888",
            "reason": "中文电影",
        })

        enriched, should_prompt = bridge.apply_openai_category_fallback(
            uncertain_recognition(),
            "军中乐园（2014）{tmdb-287888}",
            fallback,
        )

        self.assertIs(should_prompt, False)
        self.assertIs(enriched["ok"], True)
        self.assertEqual(enriched["category"], "华语电影")
        self.assertEqual(enriched["type"], "movie")
        self.assertEqual(enriched["tmdb_id"], "287888")
        self.assertEqual(enriched["openai_confidence"], 0.86)
        self.assertTrue(fallback.calls)

    def test_openai_medium_confidence_keeps_manual_prompt_with_suggestion(self):
        fallback = FakeOpenAI({
            "category": "亚洲电影",
            "confidence": 0.6,
            "media_type": "movie",
            "reason": "可能是亚洲电影",
        })

        enriched, should_prompt = bridge.apply_openai_category_fallback(
            uncertain_recognition(),
            "Some.Asian.Movie.2024.mkv",
            fallback,
        )

        self.assertIs(should_prompt, True)
        self.assertEqual(enriched["category"], "亚洲电影")
        self.assertEqual(enriched["category_suggestion"], "亚洲电影")
        self.assertEqual(enriched["category_status"], "openai_suggested")

    def test_openai_disabled_does_not_change_recognition(self):
        class Disabled:
            enabled = False

        original = uncertain_recognition()
        enriched, should_prompt = bridge.apply_openai_category_fallback(original, "x", Disabled())

        self.assertIs(should_prompt, True)
        self.assertEqual(enriched, original)

    def test_openai_indian_movie_suggestion_maps_to_user_western_bucket(self):
        fallback = FakeOpenAI({
            "category": "亚洲电影",
            "confidence": 0.99,
            "media_type": "movie",
            "title": "D-调音师",
            "tmdb_id": "534780",
            "reason": "对应印度电影《Andhadhun》，印度影片应归入亚洲电影。",
        })

        enriched, should_prompt = bridge.apply_openai_category_fallback(
            uncertain_recognition(),
            "Andhadhun.2018.1080p.Blu-ray Remux.DTS-HD 5.1.H.264.mkv",
            fallback,
        )

        self.assertIs(should_prompt, False)
        self.assertEqual(enriched["category"], "欧美电影")
        self.assertEqual(enriched["category_suggestion"], "欧美电影")
        self.assertEqual(enriched["type"], "movie")


if __name__ == "__main__":
    unittest.main()

class OpenAISuggestionMessageTests(unittest.TestCase):
    def test_manual_prompt_includes_openai_suggestion(self):
        class FakeStore:
            def update_recognition(self, row_id, recognition, status):
                self.status = status
                self.recognition = recognition
                return {"id": row_id, "title": "Some.Asian.Movie.2024.mkv"}

        class FakeTelegram:
            def send_message(self, chat_id, text, reply_markup=None):
                self.chat_id = chat_id
                self.text = text
                self.reply_markup = reply_markup

        store = FakeStore()
        telegram = FakeTelegram()
        bridge.maybe_request_category_confirmation(
            telegram,
            464100862,
            store,
            {"id": 7, "title": "Some.Asian.Movie.2024.mkv"},
            {
                "code": 500,
                "data": {
                    "title": "Some Asian Movie",
                    "type": "movie",
                    "category": "亚洲电影",
                    "category_suggestion": "亚洲电影",
                    "category_status": "openai_suggested",
                    "openai_confidence": 0.6,
                    "openai_reason": "可能是亚洲电影",
                },
                "msg": "recognition failed",
            },
        )

        self.assertEqual(store.status, "uncertain")
        self.assertIn("OpenAI建议：亚洲电影", telegram.text)
        self.assertIn("置信度 0.60", telegram.text)
        self.assertIn("请选择建议分类", telegram.text)

class RecentLibrarySourceMatchTests(unittest.TestCase):
    def test_unrelated_recent_library_dir_does_not_match_without_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            anime_root = base / "Dongman"
            tv_root = base / "TV"
            unrelated = anime_root / "H-航海王-1999-[tmdb=37854]" / "Season 01"
            unrelated.mkdir(parents=True)
            (unrelated / "航海王 (1999) - S01E01.strm").write_text("x", encoding="utf-8")
            config = bridge.MoveConfig(
                source_roots=[base],
                library_roots={"番剧": anime_root, "外国电视": tv_root},
                stable_seconds=0,
            )
            row = {"created_at": time.time(), "updated_at": time.time()}
            recognition = {"ok": False, "title": "", "type": "", "category": "", "tmdb_id": ""}

            found = bridge.find_recent_library_strm_source_dir(
                config,
                row,
                recognition,
                share_name="暴君的厨师 (2025)",
            )

            self.assertIsNone(found)

class OpenAIRequestHeaderTests(unittest.TestCase):
    def test_classifier_sends_browser_user_agent(self):
        class FakeConfig:
            http_timeout = 60
            openai_high_confidence = 0.75
            openai_suggest_confidence = 0.45
            openai_classify_enabled = True
            openai_api_key = "test-key"
            openai_base_url = "https://open.sub2api.top/v1"
            openai_model = "gpt-5.4"

        class FakeHttp:
            def request(self, url, method="GET", payload=None, headers=None):
                self.url = url
                self.method = method
                self.payload = payload
                self.headers = headers or {}
                return {"output_text": "{\"category\":\"国产电视\",\"confidence\":0.9,\"media_type\":\"tv\",\"title\":\"少年歌行\",\"tmdb_id\":\"216943\",\"reason\":\"国产剧\"}"}

        http = FakeHttp()
        classifier = bridge.OpenAIClassifier(FakeConfig(), http=http)
        classifier.classify_media({"ok": False}, "少年歌行 (2022) {tmdb-216943}")

        self.assertIn("Mozilla/5.0", http.headers.get("User-Agent", ""))
        self.assertEqual(http.headers.get("Accept"), "application/json")


class TmdbHintResolutionTests(unittest.TestCase):
    def test_tmdb_movie_uses_default_language_not_localized_chinese_title_for_region(self):
        self.assertEqual(bridge.infer_region_category("movie", "蜘蛛侠2", "英语"), "欧美电影")

    def test_tmdb_movie_maps_korean_native_default_language_to_asian_movie(self):
        self.assertEqual(bridge.infer_region_category("movie", "从邪恶中拯救我", "한국어/조선말"), "亚洲电影")

    def test_tmdb_movie_maps_hindi_to_user_western_bucket(self):
        self.assertEqual(bridge.infer_region_category("movie", "调音师", "hi"), "欧美电影")

    def test_tmdb_hint_resolves_korean_movie_as_asian_movie(self):
        class FakeTmdb:
            enabled = True
            def lookup(self, tmdb_id, media_type, share_name):
                if media_type == "movie":
                    return {"ok": True, "title": "从邪恶中拯救我", "type": "movie", "tmdb_id": tmdb_id, "language": "한국어/조선말"}
                return {"ok": False}

        resolved, should_prompt = bridge.apply_tmdb_hint_resolution(
            {"ok": False, "title": "", "type": "", "category": "", "tmdb_id": "581526"},
            "从邪恶中拯救我 2020 加长版 韩国 黄政民 李政宰 蓝光原盘REMUX DIY原盘中字",
            FakeTmdb(),
        )

        self.assertFalse(should_prompt)
        self.assertEqual(resolved["category"], "亚洲电影")
        self.assertEqual(resolved["tmdb_id"], "581526")

    def test_tmdb_hint_resolves_english_movie_with_chinese_localized_title_as_western(self):
        class FakeTmdb:
            enabled = True
            def lookup(self, tmdb_id, media_type, share_name):
                if media_type == "movie":
                    return {"ok": True, "title": "蜘蛛侠2", "type": "movie", "tmdb_id": tmdb_id, "language": "英语"}
                return {"ok": False}

        resolved, should_prompt = bridge.apply_tmdb_hint_resolution(
            {"ok": False, "title": "", "type": "", "category": "", "tmdb_id": "558"},
            "蜘蛛侠2.剧场版.Spider-Man.2.2004.mkv",
            FakeTmdb(),
        )

        self.assertFalse(should_prompt)
        self.assertEqual(resolved["category"], "欧美电影")
        self.assertEqual(resolved["tmdb_id"], "558")

    def test_extract_tmdb_search_query_prefers_release_english_series_title(self):
        share_name = "[龙之家族.第二季].House.of.the.Dragon.S02.2024.UHD.BluRay.Remux.2160p.HEVC.DoVi.HDR.TrueHD7.1.Atmos-CMCT等2个文件(夹)"

        self.assertEqual(bridge.extract_tmdb_search_query(share_name), "House of the Dragon")

    def test_extract_tmdb_search_query_accepts_two_token_series_title(self):
        share_name = "Greys.Anatomy.S22.1080p.DSNP.WEB-DL.DDP5.1.H.264-HiveWeb"

        self.assertEqual(bridge.extract_tmdb_search_query(share_name), "Greys Anatomy")

    def test_tmdb_search_resolver_uses_search_result_id(self):
        class FakeResolver:
            enabled = True
            def search(self, query, media_type):
                self.query = query
                self.media_type = media_type
                return {"ok": True, "title": "权力的游戏前传：龙族", "type": "tv", "tmdb_id": "94997", "language": "en"}

        resolver = FakeResolver()
        share_name = "[龙之家族.第二季].House.of.the.Dragon.S02.2024.UHD.BluRay.Remux.2160p.HEVC.DoVi.HDR.TrueHD7.1.Atmos-CMCT等2个文件(夹)"

        resolved, should_prompt = bridge.apply_tmdb_search_resolution({"ok": False, "title": "", "tmdb_id": ""}, share_name, resolver)

        self.assertFalse(should_prompt)
        self.assertEqual(resolver.query, "House of the Dragon")
        self.assertEqual(resolver.media_type, "tv")
        self.assertEqual(resolved["tmdb_id"], "94997")
        self.assertEqual(resolved["category"], "外国电视")

    def test_tmdb_api_resolver_maps_movie_details_to_region_category(self):
        class FakeHttp:
            def request(self, url, method="GET", payload=None, headers=None):
                self.url = url
                self.method = method
                self.headers = headers or {}
                return {
                    "id": 581526,
                    "title": "从邪恶中拯救我",
                    "original_language": "ko",
                    "production_countries": [{"iso_3166_1": "KR"}],
                    "genres": [{"name": "动作"}],
                }

        resolver = bridge.TmdbApiResolver(api_key="test-key", http=FakeHttp())

        item = resolver.lookup("581526", "movie", "从邪恶中拯救我 2020")

        self.assertTrue(item["ok"])
        self.assertEqual(item["type"], "movie")
        self.assertEqual(item["tmdb_id"], "581526")
        self.assertEqual(item["language"], "ko")
        self.assertEqual(item["countries"], ["KR"])
        self.assertEqual(item["category"], "亚洲电影")
        self.assertIn("api_key=test-key", resolver.http.url)

    def test_tmdb_api_resolver_uses_bearer_token_without_logging_secret(self):
        class FakeHttp:
            def request(self, url, method="GET", payload=None, headers=None):
                self.url = url
                self.headers = headers or {}
                return {"id": 255522, "name": "周二谋杀定律", "original_language": "en", "origin_country": ["US"], "genres": []}

        resolver = bridge.TmdbApiResolver(bearer_token="secret-token", http=FakeHttp())

        item = resolver.lookup("255522", "tv", "周二谋杀定律")

        self.assertTrue(item["ok"])
        self.assertEqual(item["type"], "tv")
        self.assertEqual(item["category"], "外国电视")
        self.assertEqual(resolver.http.headers["Authorization"], "Bearer secret-token")
        self.assertNotIn("secret-token", resolver.http.url)

    def test_tmdb_api_resolver_search_maps_result_details(self):
        class FakeHttp:
            def __init__(self):
                self.urls = []
            def request(self, url, method="GET", payload=None, headers=None):
                self.urls.append(url)
                if "/search/tv" in url:
                    return {"results": [{"id": 94997, "name": "权力的游戏前传：龙族"}]}
                return {"id": 94997, "name": "权力的游戏前传：龙族", "original_language": "en", "origin_country": ["US"]}

        resolver = bridge.TmdbApiResolver(api_key="test-key", http=FakeHttp())

        item = resolver.search("House of the Dragon", "tv")

        self.assertTrue(item["ok"])
        self.assertEqual(item["tmdb_id"], "94997")
        self.assertEqual(item["category"], "外国电视")
        self.assertTrue(any("/search/tv" in url for url in resolver.http.urls))
        self.assertTrue(any("/tv/94997" in url for url in resolver.http.urls))

    def test_tmdb_api_resolver_falls_back_to_web_resolver_when_api_fails(self):
        class FailingHttp:
            def request(self, url, method="GET", payload=None, headers=None):
                raise RuntimeError("HTTP 401 from TMDB")

        class FakeFallback:
            def lookup(self, tmdb_id, media_type, share_name):
                self.lookup_args = (tmdb_id, media_type, share_name)
                return {"ok": True, "title": "从邪恶中拯救我", "type": media_type, "tmdb_id": tmdb_id, "language": "ko", "source": "tmdb_web"}
            def search(self, query, media_type):
                self.search_args = (query, media_type)
                return {"ok": True, "title": query, "type": media_type, "tmdb_id": "94997", "source": "tmdb_search"}

        fallback = FakeFallback()
        resolver = bridge.TmdbApiResolver(api_key="bad-key", http=FailingHttp(), fallback=fallback)

        looked_up = resolver.lookup("581526", "movie", "从邪恶中拯救我")
        searched = resolver.search("House of the Dragon", "tv")

        self.assertEqual(looked_up["source"], "tmdb_web")
        self.assertEqual(fallback.lookup_args, ("581526", "movie", "从邪恶中拯救我"))
        self.assertEqual(searched["source"], "tmdb_search")
        self.assertEqual(fallback.search_args, ("House of the Dragon", "tv"))

    def test_tmdb_hint_prefers_tv_page_when_title_matches_tv_not_movie(self):
        class FakeTmdb:
            enabled = True
            def lookup(self, tmdb_id, media_type, share_name):
                if media_type == "tv":
                    return {"ok": True, "title": "周二谋杀定律", "type": "tv", "tmdb_id": tmdb_id, "source": "tmdb_web"}
                if media_type == "movie":
                    return {"ok": True, "title": "In the Mirror Dimly", "type": "movie", "tmdb_id": tmdb_id, "source": "tmdb_web"}
                return {"ok": False}

        resolved, should_prompt = bridge.apply_tmdb_hint_resolution(
            uncertain_recognition(),
            "周二谋杀定律 (2026) {tmdb-255522}",
            FakeTmdb(),
        )

        self.assertFalse(should_prompt)
        self.assertEqual(resolved["type"], "tv")
        self.assertEqual(resolved["category"], "外国电视")
        self.assertEqual(resolved["tmdb_id"], "255522")
        self.assertEqual(resolved["category_status"], "tmdb_resolved")

    def test_openai_is_not_called_when_tmdb_hint_resolves_type_and_category(self):
        class FakeTmdb:
            enabled = True
            def lookup(self, tmdb_id, media_type, share_name):
                if media_type == "tv":
                    return {"ok": True, "title": "周二谋杀定律", "type": "tv", "tmdb_id": tmdb_id, "source": "tmdb_web"}
                return {"ok": False}

        class ExplodingOpenAI:
            enabled = True
            def classify_media(self, recognition, share_name):
                raise AssertionError("OpenAI should not run after TMDB resolves")

        resolved, should_prompt = bridge.resolve_category_with_fallbacks(
            uncertain_recognition(),
            "周二谋杀定律 (2026) {tmdb-255522}",
            ExplodingOpenAI(),
            FakeTmdb(),
        )

        self.assertFalse(should_prompt)
        self.assertEqual(resolved["category"], "外国电视")

    def test_openai_tmdb_result_can_be_used_for_exact_115_folder_lookup(self):
        class FakeClassifier:
            enabled = True
            high_confidence = 0.75
            suggest_confidence = 0.45
            def classify_media(self, recognition, share_name):
                return {
                    "category": "外国电视",
                    "confidence": 0.92,
                    "media_type": "tv",
                    "title": "权力的游戏前传：龙族",
                    "tmdb_id": "94997",
                    "reason": "文件名包含 House.of.the.Dragon.S02",
                }

        class FakeHttp:
            def __init__(self):
                self.queries = []
            def request(self, url, method="GET", data=None, headers=None, params=None):
                query = (params or {}).get("search_value", "")
                self.queries.append(query)
                if query == "94997":
                    return {"state": True, "data": [{"cid": "target", "n": "Q-权力的游戏前传：龙族-2022-[tmdb=94997]", "pid": "3254119954860998447", "t": "1781950658"}]}
                if query in {"2024 tmdb", "2024"}:
                    return {"state": True, "data": [{"cid": "wrong", "n": "G-诡才之道-2024-[tmdb=1006724]", "pid": "3277370369039662171", "t": "1781928598"}]}
                return {"state": True, "data": []}

        share_name = "[龙之家族.第二季].House.of.the.Dragon.S02.2024.UHD.BluRay.Remux.2160p.HEVC.DoVi.HDR.TrueHD7.1.Atmos-CMCT等2个文件(夹)"
        resolved, should_prompt = bridge.resolve_category_with_fallbacks({"ok": False, "title": "", "tmdb_id": ""}, share_name, FakeClassifier(), None)
        client = bridge.P115WebClient("UID=1", http=FakeHttp(), timeout=3)

        selected = client.find_organized_folder(resolved, share_name, min_update_time=1781950000)

        self.assertFalse(should_prompt)
        self.assertEqual(resolved["tmdb_id"], "94997")
        self.assertEqual(selected["file_id"], "target")
        self.assertEqual(resolved["type"], "tv")

    def test_tmdb_hint_does_not_guess_when_title_matches_neither_namespace(self):
        class FakeTmdb:
            enabled = True
            def lookup(self, tmdb_id, media_type, share_name):
                return {"ok": True, "title": "Completely Different", "type": media_type, "tmdb_id": tmdb_id, "source": "tmdb_web"}

        resolved, should_prompt = bridge.apply_tmdb_hint_resolution(
            uncertain_recognition(),
            "周二谋杀定律 (2026) {tmdb-255522}",
            FakeTmdb(),
        )

        self.assertTrue(should_prompt)
        self.assertEqual(resolved["tmdb_id"], "")

class CmsFirstFallbackTests(unittest.TestCase):
    def test_uncertain_recognition_uses_emby_tmdb_match_before_openai_prompt(self):
        class FakeStore:
            def __init__(self):
                self.recognition_update = None
                self.category_update = None
                self.move_update = None
                self.emby_update = None
            def update_recognition(self, row_id, recognition, status):
                self.recognition_update = (row_id, dict(recognition), status)
                return {"id": row_id, "category_status": status, "recognition_json": "{}"}
            def update_category(self, row_id, choice, status):
                self.category_update = (row_id, choice, status)
                return {"id": row_id, "category_choice": choice, "category_status": status}
            def update_move(self, row_id, status, source_path=None, dest_path=None, category_final=None, error=None):
                self.move_update = (row_id, status, source_path, dest_path, category_final, error)
                return {"id": row_id, "move_status": status, "source_path": source_path, "dest_path": dest_path, "category_final": category_final}
            def update_emby(self, row_id, status, item_id=None, title=None, path=None, parent=None):
                self.emby_update = (row_id, status, item_id, title, path, parent)
                return {"id": row_id, "emby_status": status, "emby_title": title, "emby_path": path, "emby_parent": parent}

        class FakeTelegram:
            def __init__(self):
                self.messages = []
            def send_message(self, chat_id, text, reply_markup=None):
                self.messages.append((text, reply_markup))

        class FakeEmby:
            enabled = True
            def find_item_by_tmdb(self, tmdb_id):
                return {"Id": "emby-tv", "Name": "周二谋杀定律", "Path": "/mnt/user/Unraid/strm/转存/TV/Z-周二谋杀定律-2026-[tmdb=255522]", "ProviderIds": {"Tmdb": tmdb_id}}
            def library_name_for_item(self, item):
                return "Strm外国电视"

        class ExplodingOpenAI:
            enabled = True
            def classify_media(self, recognition, share_name):
                raise AssertionError("OpenAI should not run when Emby already has the TMDB item")

        class ExplodingTmdb:
            enabled = True
            def lookup(self, tmdb_id, media_type, share_name):
                raise AssertionError("TMDB web should not run when Emby already has the TMDB item")

        store = FakeStore()
        telegram = FakeTelegram()
        recognition = uncertain_recognition()
        recognition["tmdb_id"] = "255522"

        resolved, should_prompt = bridge.resolve_category_or_existing_import(
            telegram,
            464100862,
            store,
            {"id": 106, "title": "周二谋杀定律 (2026) {tmdb-255522}"},
            recognition,
            "周二谋杀定律 (2026) {tmdb-255522}",
            move_config=None,
            emby=FakeEmby(),
            openai_classifier=ExplodingOpenAI(),
            tmdb_resolver=ExplodingTmdb(),
        )

        self.assertFalse(should_prompt)
        self.assertEqual(resolved["category"], "外国电视")
        self.assertEqual(resolved["type"], "tv")
        self.assertEqual(store.category_update, (106, "外国电视", "selected"))
        self.assertEqual(store.emby_update[1], "confirmed")
        self.assertEqual(store.emby_update[5], "Strm外国电视")
        self.assertEqual(telegram.messages, [])
