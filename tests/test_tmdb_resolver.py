import unittest

from app.media.classify import TmdbApiResolver


class TmdbTvDetailsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
