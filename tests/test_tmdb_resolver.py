import unittest

from app.media.classify import TmdbApiResolver


class TmdbTvDetailsTests(unittest.TestCase):
    def test_omits_malformed_top_level_tv_counts(self):
        result = TmdbApiResolver._normalize_details(
            {
                "id": 1416,
                "name": "Grey's Anatomy",
                "original_language": "en",
                "origin_country": ["US"],
                "status": "Returning Series",
                "number_of_seasons": "unknown",
                "number_of_episodes": None,
            },
            "tv",
        )

        self.assertNotIn("number_of_seasons", result)
        self.assertNotIn("number_of_episodes", result)

    def test_normalizes_tv_completion_metadata_and_sanitizes_seasons(self):
        result = TmdbApiResolver._normalize_details(
            {
                "id": 1416,
                "name": "Grey's Anatomy",
                "original_language": "en",
                "origin_country": ["US"],
                "genres": [],
                "status": "Ended",
                "number_of_seasons": "2",
                "number_of_episodes": "4",
                "seasons": [
                    {"season_number": 1, "episode_count": "2", "air_date": "2005-03-27"},
                    {"season_number": "2", "episode_count": 2, "air_date": None},
                    {"season_number": "specials", "episode_count": 1, "air_date": "2020-01-01"},
                    {"season_number": 3, "episode_count": "not-a-number", "air_date": "2021-01-01"},
                ],
            },
            "tv",
        )

        self.assertEqual(result["status"], "Ended")
        self.assertEqual(result["number_of_seasons"], 2)
        self.assertEqual(result["number_of_episodes"], 4)
        self.assertEqual(
            result["seasons"],
            [
                {"season_number": 1, "episode_count": 2, "air_date": "2005-03-27"},
                {"season_number": 2, "episode_count": 2, "air_date": ""},
            ],
        )


class TmdbResolverRegressionTests(unittest.TestCase):
    def test_movie_normalization_preserves_category_fields_without_tv_metadata(self):
        result = TmdbApiResolver._normalize_details(
            {
                "id": 581526,
                "title": "从邪恶中拯救我",
                "original_language": "ko",
                "production_countries": [{"iso_3166_1": "KR"}],
                "genres": [{"name": "动作"}],
            },
            "movie",
        )

        self.assertEqual(result["category"], "亚洲电影")
        self.assertEqual(result["countries"], ["KR"])
        self.assertEqual(result["genres"], ["动作"])
        self.assertNotIn("status", result)
        self.assertNotIn("number_of_seasons", result)
        self.assertNotIn("number_of_episodes", result)
        self.assertNotIn("seasons", result)

    def test_lookup_preserves_fallback_when_tmdb_api_request_fails(self):
        class FailingHttp:
            def request(self, url, method="GET", payload=None, headers=None):
                raise RuntimeError("HTTP 401")

        class FakeFallback:
            enabled = True

            def lookup(self, tmdb_id, media_type, share_name):
                self.args = (tmdb_id, media_type, share_name)
                return {
                    "ok": True,
                    "title": "从邪恶中拯救我",
                    "type": media_type,
                    "tmdb_id": tmdb_id,
                    "category": "亚洲电影",
                    "source": "tmdb_web",
                }

        fallback = FakeFallback()
        resolver = TmdbApiResolver(api_key="test-key", http=FailingHttp(), fallback=fallback)

        result = resolver.lookup("581526", "movie", "从邪恶中拯救我 2020")

        self.assertEqual(result["source"], "tmdb_web")
        self.assertEqual(result["category"], "亚洲电影")
        self.assertEqual(fallback.args, ("581526", "movie", "从邪恶中拯救我 2020"))


if __name__ == "__main__":
    unittest.main()
