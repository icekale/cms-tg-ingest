import unittest
from dataclasses import FrozenInstanceError

from app.series_rules import (
    EpisodeKey,
    EpisodeFilter,
    completion_state,
    episode_filter_matches,
    is_special_episode,
    normalize_episode_key,
    parse_episode_filter,
    parse_episode_key,
)


class SeriesRuleTests(unittest.TestCase):
    def test_normalizes_short_and_padded_episode_tokens(self):
        self.assertEqual(parse_episode_key("Show.S1E2.2160p"), EpisodeKey(1, 2))
        self.assertEqual(parse_episode_key("S01E02"), EpisodeKey(1, 2))
        self.assertEqual(parse_episode_key("S01"), None)
        self.assertEqual(normalize_episode_key("show_s1e2"), "S01E02")

    def test_episode_key_is_immutable_and_ordered(self):
        self.assertLess(EpisodeKey(1, 2), EpisodeKey(1, 10))
        with self.assertRaises(FrozenInstanceError):
            EpisodeKey(1, 2).season = 3

    def test_default_filter_excludes_specials_but_explicit_s00_allows_them(self):
        self.assertTrue(is_special_episode(EpisodeKey(0, 1)))
        self.assertFalse(parse_episode_filter("").matches(EpisodeKey(0, 1)))
        self.assertTrue(parse_episode_filter("S00").matches(EpisodeKey(0, 1)))
        self.assertFalse(parse_episode_filter("S00").matches(EpisodeKey(1, 1)))

    def test_filter_supports_single_episode_range_season_and_union(self):
        episode_filter = parse_episode_filter("S01E01-S01E03,S02")
        self.assertTrue(episode_filter.matches(EpisodeKey(1, 2)))
        self.assertFalse(episode_filter.matches(EpisodeKey(1, 4)))
        self.assertTrue(episode_filter.matches(EpisodeKey(2, 99)))

    def test_public_episode_filter_matches_wrapper(self):
        episode_filter = parse_episode_filter("S01E01-S01E03")
        self.assertTrue(episode_filter_matches(episode_filter, EpisodeKey(1, 2)))
        self.assertFalse(episode_filter_matches(episode_filter, EpisodeKey(1, 4)))

    def test_filter_copies_mutable_constructor_inputs(self):
        exact_keys = {EpisodeKey(1, 1)}
        seasons = {2}
        ranges = [(EpisodeKey(3, 1), EpisodeKey(3, 2))]
        episode_filter = EpisodeFilter(exact_keys, seasons, ranges)

        exact_keys.clear()
        seasons.clear()
        ranges.clear()

        self.assertTrue(episode_filter.matches(EpisodeKey(1, 1)))
        self.assertTrue(episode_filter.matches(EpisodeKey(2, 1)))
        self.assertTrue(episode_filter.matches(EpisodeKey(3, 2)))

    def test_rejects_cross_season_ranges_and_bad_tokens(self):
        with self.assertRaises(ValueError):
            parse_episode_filter("S01E01-S02E02")
        with self.assertRaises(ValueError):
            parse_episode_filter("S01E")
        with self.assertRaises(ValueError):
            parse_episode_filter("S01E01,")

    def test_completion_requires_ended_status_and_complete_terminal_coverage(self):
        expected = {EpisodeKey(1, 1), EpisodeKey(1, 2)}
        self.assertEqual(
            completion_state("Ended", expected, {EpisodeKey(1, 1)}, set()),
            "active",
        )
        self.assertEqual(
            completion_state("Returning Series", expected, expected, set()),
            "active",
        )
        self.assertEqual(
            completion_state("Canceled", expected, expected, {EpisodeKey(1, 2)}),
            "active",
        )
        self.assertEqual(completion_state("ended", expected, expected, set()), "completed")

    def test_completion_requires_non_empty_expected_set(self):
        self.assertEqual(completion_state("Ended", set(), set(), set()), "active")


if __name__ == "__main__":
    unittest.main()
