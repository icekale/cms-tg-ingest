import unittest

from app.clients.hdhive import HdhiveResource
from app.hdhive_episode_llm import (
    DECISION_AUTO,
    DECISION_PENDING,
    DECISION_REJECT,
    decide_llm_episode,
    format_llm_pending_reason,
    has_episode_clue,
    keys_from_llm_payload,
    resource_clue_text,
    tmdb_season_numbers,
)
from app.series_rules import EpisodeKey


class HasEpisodeClueTests(unittest.TestCase):
    def test_clue_patterns(self):
        for text in (
            "更新至07集",
            "第12集",
            "第十二集",
            "全08集",
            "EP07",
            "E01-E08",
            "Show.S02.2160p",
            "Season 2 extra",
        ):
            with self.subTest(text=text):
                self.assertTrue(has_episode_clue(text))

    def test_season_word_without_episode_is_not_a_clue(self):
        self.assertFalse(has_episode_clue("最后生还者 第二季"))
        self.assertFalse(has_episode_clue("The Last of Us"))
        self.assertFalse(has_episode_clue("4K 2160P WEB-DL"))

    def test_resource_clue_text_joins_title_remark_and_episode_key(self):
        resource = HdhiveResource(
            slug="pack",
            title="最后生还者",
            pan_type="115",
            share_size="",
            video_resolution=(),
            source=(),
            subtitle_language=(),
            subtitle_type=(),
            unlock_points=8,
            validate_status="valid",
            validate_message="",
            is_unlocked=False,
            episode_key="",
            remark="更新至07集",
        )
        self.assertIn("更新至07集", resource_clue_text(resource))
        self.assertTrue(has_episode_clue(resource_clue_text(resource)))


class KeysFromLlmPayloadTests(unittest.TestCase):
    def test_valid_range_requires_evidence_substring(self):
        source = "最后生还者 第二季 更新至07集"
        parsed = keys_from_llm_payload(
            {
                "season": 2,
                "episode_start": 1,
                "episode_end": 7,
                "confidence": 0.9,
                "reason": "title season plus updated-through",
                "evidence": "更新至07集",
            },
            source,
        )
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.keys[0], EpisodeKey(2, 1))
        self.assertEqual(parsed.keys[-1], EpisodeKey(2, 7))
        self.assertEqual(parsed.confidence, 0.9)

    def test_missing_or_foreign_evidence_is_rejected(self):
        source = "更新至07集"
        self.assertFalse(keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.9, "evidence": ""},
            source,
        ).ok)
        self.assertFalse(keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.9, "evidence": "S02E10"},
            source,
        ).ok)

    def test_invalid_range_is_rejected(self):
        source = "更新至07集"
        payload = {
            "season": 2,
            "episode_start": 1,
            "episode_end": 300,
            "confidence": 0.9,
            "evidence": "更新至07集",
        }
        self.assertFalse(keys_from_llm_payload(payload, source).ok)

    def test_updated_through_allows_start_one_when_end_is_in_evidence(self):
        parsed = keys_from_llm_payload(
            {
                "season": 2,
                "episode_start": 1,
                "episode_end": 7,
                "confidence": 0.9,
                "evidence": "更新至07集",
            },
            "更新至07集",
        )
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.keys[-1].episode, 7)

    def test_updated_through_rejects_end_missing_from_evidence_and_source(self):
        parsed = keys_from_llm_payload(
            {
                "season": 2,
                "episode_start": 1,
                "episode_end": 10,
                "confidence": 0.9,
                "evidence": "更新至07集",
            },
            "更新至07集",
        )
        self.assertFalse(parsed.ok)


class DecideLlmEpisodeTests(unittest.TestCase):
    def test_high_confidence_in_tmdb_is_auto(self):
        parsed = keys_from_llm_payload(
            {
                "season": 2,
                "episode_start": 1,
                "episode_end": 7,
                "confidence": 0.8,
                "evidence": "更新至07集",
            },
            "更新至07集",
        )
        self.assertEqual(decide_llm_episode(parsed, {1, 2}, 0.75, 0.45), DECISION_AUTO)

    def test_high_confidence_missing_tmdb_season_is_pending(self):
        parsed = keys_from_llm_payload(
            {
                "season": 9,
                "episode_start": 1,
                "episode_end": 1,
                "confidence": 0.95,
                "evidence": "第1集",
            },
            "第1集",
        )
        self.assertEqual(decide_llm_episode(parsed, {1, 2}, 0.75, 0.45), DECISION_PENDING)

    def test_medium_confidence_is_pending_and_low_is_reject(self):
        source = "更新至07集"
        medium = keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.5, "evidence": "更新至07集"},
            source,
        )
        low = keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.2, "evidence": "更新至07集"},
            source,
        )
        self.assertEqual(decide_llm_episode(medium, {2}, 0.75, 0.45), DECISION_PENDING)
        self.assertEqual(decide_llm_episode(low, {2}, 0.75, 0.45), DECISION_REJECT)

    def test_empty_tmdb_seasons_cannot_auto(self):
        parsed = keys_from_llm_payload(
            {"season": 2, "episode_start": 1, "episode_end": 7, "confidence": 0.99, "evidence": "更新至07集"},
            "更新至07集",
        )
        self.assertEqual(decide_llm_episode(parsed, set(), 0.75, 0.45), DECISION_PENDING)

    def test_pending_reason_and_tmdb_season_numbers(self):
        keys = (EpisodeKey(2, 1), EpisodeKey(2, 7))
        self.assertEqual(format_llm_pending_reason(keys), "LLM 识别待确认：S02E01-S02E07")
        self.assertEqual(
            tmdb_season_numbers({"seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": "x"}]}),
            {0, 1},
        )


if __name__ == "__main__":
    unittest.main()
