import unittest

from app.models import TaskStage, TaskStatus, TaskSnapshot
from app.quality import QualityIssue
from app.quality_rules import QUALITY_RULE_VERSION, QualityRuleEngine, is_rule_enabled


def task(**metadata):
    return TaskSnapshot(
        id=1,
        share_code="share",
        receive_code="",
        url="https://115cdn.com/s/share",
        title="Example",
        tmdb_id="1",
        category="movie",
        current_stage=TaskStage.MOVED,
        status=TaskStatus.SUCCEEDED,
        error_type="",
        error_summary="",
        retry_count=0,
        metadata=metadata,
    )


class QualityRuleEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = QualityRuleEngine()

    def test_rule_version_is_stable(self):
        self.assertEqual(QUALITY_RULE_VERSION, "1")

    def test_reprocess_rule_switch_is_read_from_the_built_in_config(self):
        self.assertTrue(is_rule_enabled({"allow_auto_reprocess": True}, "reprocess"))
        self.assertTrue(is_rule_enabled({"allow_auto_reprocess": True, "max_attempts": 3}, "strm_mode_mismatch"))
        self.assertFalse(is_rule_enabled({"allow_auto_reprocess": False}, "reprocess"))

    def test_direct_strm_is_valid_for_direct_mode(self):
        match = self.engine.evaluate(
            task(strm_mode="direct"),
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
        )

        self.assertEqual(match.rule_id, "no_issue")

    def test_shared_direct_strm_requires_reprocess(self):
        match = self.engine.evaluate(
            task(strm_mode="shared"),
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
            config={"allow_auto_reprocess": True},
        )

        self.assertEqual(match.rule_id, "strm_mode_mismatch")
        self.assertEqual(match.auto_action, "reprocess")
        self.assertTrue(match.auto_allowed)

    def test_shared_direct_strm_is_manual_without_auto_config(self):
        match = self.engine.evaluate(
            task(strm_mode="source_shared"),
            [QualityIssue("direct_strm", "direct", "")],
        )

        self.assertEqual(match.rule_id, "strm_mode_mismatch")
        self.assertEqual(match.auto_action, "reprocess")
        self.assertFalse(match.auto_allowed)

    def test_unexpected_strm_can_reprocess_with_complete_safe_evidence(self):
        match = self.engine.evaluate(
            task(strm_mode="direct"),
            [QualityIssue("unexpected_strm", "unexpected", "/library/movie.strm")],
            config={"allow_auto_reprocess": True},
        )

        self.assertEqual(match.rule_id, "unexpected_strm")
        self.assertEqual(match.auto_action, "reprocess")
        self.assertTrue(match.auto_allowed)

    def test_unexpected_strm_is_manual_without_safe_reprocess_conditions(self):
        match = self.engine.evaluate(
            task(strm_mode="direct", retry_count=3),
            [QualityIssue("unexpected_strm", "unexpected", "")],
            config={"allow_auto_reprocess": True, "max_attempts": 3},
        )

        self.assertEqual(match.rule_id, "unexpected_strm")
        self.assertEqual(match.auto_action, "reprocess")
        self.assertFalse(match.auto_allowed)

    def test_cleaned_invalid_share_is_terminal_and_manual(self):
        match = self.engine.evaluate(
            task(invalid_share_cleaned=True),
            [QualityIssue("missing_dest", "missing", "/library/movie")],
            config={"allow_auto_reprocess": True},
        )

        self.assertEqual(match.rule_id, "terminal_invalid_share")
        self.assertFalse(match.auto_allowed)
        self.assertIn("view", match.manual_actions)
        self.assertIn("resume", match.manual_actions)

    def test_missing_destination_is_manual(self):
        match = self.engine.evaluate(task(), [QualityIssue("missing_dest", "missing", "/library/movie")])

        self.assertEqual(match.rule_id, "missing_destination")
        self.assertEqual(match.auto_action, "none")
        self.assertFalse(match.auto_allowed)

    def test_rule_priority_prefers_unsafe_path_and_risk_over_strm_mismatch(self):
        unsafe = self.engine.evaluate(
            task(strm_mode="shared"),
            [
                QualityIssue("direct_strm", "direct", "/library/movie.strm"),
                QualityIssue("unsafe_metadata", "unsafe", "/tmp/movie"),
            ],
        )
        risk = self.engine.evaluate(
            task(strm_mode="shared", p115_risk_controlled=True),
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
        )

        self.assertEqual(unsafe.rule_id, "unsafe_path")
        self.assertEqual(risk.rule_id, "risk_controlled")

    def test_retry_limit_and_repeated_failure_are_manual(self):
        match = self.engine.evaluate(
            task(retry_count=3),
            [QualityIssue("repeated_failure", "failed repeatedly", "/library/movie.strm")],
            config={"max_attempts": 3, "allow_auto_reprocess": True},
        )

        self.assertEqual(match.rule_id, "repeated_failure")
        self.assertFalse(match.auto_allowed)


if __name__ == "__main__":
    unittest.main()
