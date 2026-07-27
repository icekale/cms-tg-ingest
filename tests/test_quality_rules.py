import unittest
from dataclasses import replace

from app.models import TaskStage, TaskStatus, TaskSnapshot
from app.quality import QualityIssue
from app.quality_rules import (
    QUALITY_RULE_VERSION,
    QualityRuleEngine,
    has_risk_control_marker,
    is_rule_enabled,
    rule_config,
)


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
        self.assertTrue(is_rule_enabled({"allow_auto_reprocess": True}, "unexpected_strm"))
        self.assertFalse(is_rule_enabled({"allow_auto_reprocess": False}, "reprocess"))

    def test_rule_config_handles_overflowing_integer_values(self):
        controls = rule_config({"max_attempts": float("inf"), "cooldown_seconds": float("inf")})

        self.assertEqual(controls["max_attempts"], 3)
        self.assertEqual(controls["cooldown_seconds"], 0)

    def test_invalid_task_strm_mode_requires_manual_review(self):
        match = self.engine.evaluate(
            task(strm_mode="not-a-mode"),
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
        )

        self.assertEqual(match.rule_id, "manual_required")
        self.assertIn("invalid_strm_mode", match.issue_codes)

    def test_direct_strm_is_valid_for_direct_mode(self):
        match = self.engine.evaluate(
            task(strm_mode="direct"),
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
        )

        self.assertEqual(match.rule_id, "no_issue")

    def test_non_finite_risk_cooldown_is_treated_as_active_risk_control(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                current = task(p115_risk_cooldown_until=value)
                self.assertTrue(has_risk_control_marker(current, now=100.0))
                match = self.engine.evaluate(
                    current,
                    [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
                    config={"allow_auto_reprocess": True},
                )
                self.assertEqual(match.rule_id, "risk_controlled")

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

    def test_real_issue_rules_expose_human_takeover_actions(self):
        cases = (
            (
                "missing_destination",
                task(strm_mode="shared"),
                [QualityIssue("missing_dest", "missing", "/library/missing")],
            ),
            (
                "missing_strm",
                task(strm_mode="shared"),
                [QualityIssue("missing_strm", "missing", "/library/empty")],
            ),
            (
                "strm_mode_mismatch",
                task(strm_mode="shared"),
                [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
            ),
            (
                "unexpected_strm",
                task(strm_mode="direct"),
                [QualityIssue("unexpected_strm", "unexpected", "/library/movie.strm")],
            ),
        )

        for expected_rule, current_task, issues in cases:
            with self.subTest(rule=expected_rule):
                match = self.engine.evaluate(current_task, issues, config={"allow_auto_reprocess": True})
                self.assertEqual(match.rule_id, expected_rule)
                self.assertIn("view", match.manual_actions)
                self.assertIn("snooze", match.manual_actions)
                self.assertIn("ignore", match.manual_actions)

    def test_restricted_rules_do_not_gain_snooze_or_ignore(self):
        for current_task, issues in (
            (task(unsafe_path=True), [QualityIssue("unsafe_path", "unsafe", "/outside")]),
            (task(invalid_share_cleaned=True), [QualityIssue("missing_dest", "missing", "/library/missing")]),
            (task(p115_risk_controlled=True), [QualityIssue("missing_dest", "missing", "/library/missing")]),
        ):
            match = self.engine.evaluate(current_task, issues, config={"allow_auto_reprocess": True})
            with self.subTest(rule=match.rule_id):
                self.assertNotIn("snooze", match.manual_actions)
                self.assertNotIn("ignore", match.manual_actions)

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

    def test_explicit_invalid_share_is_terminal_even_with_strm_issue(self):
        for key in ("invalid_share_status", "share_validation_status"):
            with self.subTest(key=key):
                match = self.engine.evaluate(
                    task(**{key: "invalid"}, strm_mode="shared"),
                    [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
                    config={"allow_auto_reprocess": True, "max_attempts": 2},
                )

                self.assertEqual(match.rule_id, "terminal_invalid_share")
                self.assertFalse(match.auto_allowed)
                self.assertEqual(match.auto_action, "none")

    def test_new_quality_repair_attempts_limit_auto_reprocess(self):
        match = self.engine.evaluate(
            task(strm_mode="shared", quality_repair_attempts=2),
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
            config={"allow_auto_reprocess": True, "max_attempts": 2},
        )

        self.assertEqual(match.rule_id, "repeated_failure")
        self.assertFalse(match.auto_allowed)
        self.assertIn("view", match.manual_actions)

    def test_invalid_new_attempts_fall_back_to_legacy_attempts(self):
        match = self.engine.evaluate(
            task(strm_mode="shared", quality_repair_attempts="invalid", quality_attempts="2"),
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
            config={"allow_auto_reprocess": True, "max_attempts": 2},
        )

        self.assertEqual(match.rule_id, "repeated_failure")
        self.assertFalse(match.auto_allowed)

    def test_attempts_fall_back_to_task_retry_count(self):
        current = replace(task(strm_mode="shared"), retry_count=2)
        match = self.engine.evaluate(
            current,
            [QualityIssue("direct_strm", "direct", "/library/movie.strm")],
            config={"allow_auto_reprocess": True, "max_attempts": 2},
        )

        self.assertEqual(match.rule_id, "repeated_failure")
        self.assertFalse(match.auto_allowed)

    def test_missing_rules_keep_specific_reason_when_attempts_are_exhausted(self):
        for issue_code, rule_id in (("missing_dest", "missing_destination"), ("missing_strm", "missing_strm")):
            with self.subTest(issue_code=issue_code):
                match = self.engine.evaluate(
                    task(quality_repair_attempts=2),
                    [QualityIssue(issue_code, "missing", "/library/movie")],
                    config={"allow_auto_reprocess": True, "max_attempts": 2},
                )

                self.assertEqual(match.rule_id, rule_id)
                self.assertFalse(match.auto_allowed)

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
