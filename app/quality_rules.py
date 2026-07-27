from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import is_under_any_root
from .models import TaskSnapshot
from .strm_mode import effective_task_strm_mode

if TYPE_CHECKING:
    from .quality import QualityIssue


QUALITY_RULE_VERSION = "1"


@dataclass(frozen=True)
class QualityRuleMatch:
    rule_id: str
    priority: int
    risk_level: str
    reason: str
    issue_codes: tuple[str, ...]
    auto_action: str = "none"
    auto_allowed: bool = False
    manual_actions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


_RULE_PRIORITIES = {
    "terminal_invalid_share": 100,
    "unsafe_path": 90,
    "risk_controlled": 80,
    "strm_mode_mismatch": 70,
    "missing_destination": 60,
    "missing_strm": 50,
    "unexpected_strm": 40,
    "repeated_failure": 30,
    "no_issue": 10,
    "manual_required": 1,
}
_MANUAL_ACTIONS = ("view", "resume")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE_VALUES


def _config_value(config: object | None, key: str) -> Any:
    if isinstance(config, Mapping):
        return config.get(key)
    return getattr(config, key, None) if config is not None else None


def rule_config(config: object | None = None) -> dict[str, bool | int]:
    """Return only the built-in quality-rule controls."""
    raw_attempts = _config_value(config, "max_attempts")
    raw_cooldown = _config_value(config, "cooldown_seconds")
    try:
        max_attempts = int(raw_attempts) if raw_attempts is not None and not isinstance(raw_attempts, bool) else 3
    except (OverflowError, TypeError, ValueError):
        max_attempts = 3
    try:
        cooldown_seconds = (
            int(raw_cooldown) if raw_cooldown is not None and not isinstance(raw_cooldown, bool) else 0
        )
    except (OverflowError, TypeError, ValueError):
        cooldown_seconds = 0
    return {
        "allow_auto_reprocess": _bool_value(_config_value(config, "allow_auto_reprocess")),
        "max_attempts": max(1, max_attempts),
        "cooldown_seconds": max(0, cooldown_seconds),
    }


def is_path_within_allowed_roots(path: str | Path, allowed_roots: Iterable[str | Path] | None) -> bool:
    if allowed_roots is None:
        return True
    try:
        return is_under_any_root(Path(path), [Path(root) for root in allowed_roots])
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def is_rule_enabled(config: object | None, rule_id: str) -> bool:
    controls = rule_config(config)
    if rule_id in {"reprocess", "strm_mode_mismatch", "unexpected_strm"}:
        return bool(controls["allow_auto_reprocess"])
    return False


def mode_rule_for_issue(mode: str, issue_code: str) -> str | None:
    if issue_code != "direct_strm":
        return issue_code if issue_code == "unexpected_strm" else None
    return None if mode == "direct" else "strm_mode_mismatch"


def has_risk_control_marker(task: TaskSnapshot, *, now: float | None = None, cooldown_seconds: int = 0) -> bool:
    metadata = task.metadata
    if _bool_value(metadata.get("p115_risk_controlled")):
        return True
    try:
        cooldown_until = float(metadata.get("p115_risk_cooldown_until") or 0)
    except (TypeError, ValueError):
        cooldown_until = 0
    current_time = time.time() if now is None else float(now)
    if cooldown_until > current_time:
        return True
    if cooldown_seconds <= 0:
        return False
    try:
        marked_at = float(metadata.get("p115_risk_controlled_at") or 0)
    except (TypeError, ValueError):
        marked_at = 0
    return marked_at > 0 and marked_at + cooldown_seconds > current_time


def has_terminal_invalid_share_marker(task: TaskSnapshot) -> bool:
    metadata = task.metadata
    for key in ("invalid_share_cleaned", "source_deleted"):
        if _bool_value(metadata.get(key)):
            return True
    for key in ("invalid_share_status", "share_validation_status"):
        if str(metadata.get(key) or "").strip().lower() == "invalid":
            return True
    for key in ("move_status", "emby_status", "share_validation_status", "invalid_share_status"):
        if str(metadata.get(key) or "").strip().lower() in {"invalid_share_cleaned", "source_deleted"}:
            return True
    return False


def has_complete_evidence(issues: Iterable[QualityIssue], issue_code: str) -> bool:
    matching = [issue for issue in issues if issue.code == issue_code]
    return bool(matching) and all(str(issue.detail).strip() for issue in matching)


def _parse_attempt_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        attempts = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return attempts if attempts >= 0 else None


def quality_attempt_count(task: TaskSnapshot) -> int:
    """Resolve current quality attempts while preserving legacy counters."""
    for key in ("quality_repair_attempts", "quality_attempts"):
        attempts = _parse_attempt_count(task.metadata.get(key))
        if attempts is not None:
            return attempts
    return max(0, int(task.retry_count or 0))


def attempts_exhausted(task: TaskSnapshot, config: object | None = None) -> bool:
    controls = rule_config(config)
    return quality_attempt_count(task) >= int(controls["max_attempts"])


def _match(
    rule_id: str,
    risk_level: str,
    reason: str,
    issue_codes: Iterable[str] = (),
    *,
    auto_action: str = "none",
    auto_allowed: bool = False,
    manual_actions: tuple[str, ...] = (),
    evidence: Iterable[str] = (),
) -> QualityRuleMatch:
    return QualityRuleMatch(
        rule_id=rule_id,
        priority=_RULE_PRIORITIES[rule_id],
        risk_level=risk_level,
        reason=reason,
        issue_codes=tuple(issue_codes),
        auto_action=auto_action,
        auto_allowed=auto_allowed,
        manual_actions=manual_actions,
        evidence=tuple(evidence),
    )


class QualityRuleEngine:
    def evaluate(
        self,
        task: TaskSnapshot,
        issues: Iterable[QualityIssue],
        *,
        config: object | None = None,
    ) -> QualityRuleMatch:
        issue_list = tuple(issues)
        issue_codes = tuple(dict.fromkeys(issue.code for issue in issue_list))
        controls = rule_config(config)
        terminal_issue = {"invalid_share_cleaned", "source_deleted"}.intersection(issue_codes)
        if has_terminal_invalid_share_marker(task) or terminal_issue:
            return _match(
                "terminal_invalid_share",
                "critical",
                "share or source has reached a terminal invalid state",
                issue_codes,
                manual_actions=_MANUAL_ACTIONS,
                evidence=(issue.detail for issue in issue_list if issue.detail),
            )

        if {"unsafe_metadata", "unsafe_path"}.intersection(issue_codes):
            return _match(
                "unsafe_path",
                "high",
                "path is outside the allowed boundary",
                issue_codes,
                manual_actions=_MANUAL_ACTIONS,
                evidence=(issue.detail for issue in issue_list if issue.detail),
            )

        if has_risk_control_marker(task, cooldown_seconds=int(controls["cooldown_seconds"])):
            return _match(
                "risk_controlled",
                "high",
                "115 risk control or cooldown is active",
                issue_codes,
                manual_actions=_MANUAL_ACTIONS,
                evidence=(issue.detail for issue in issue_list if issue.detail),
            )

        try:
            mode = effective_task_strm_mode(task)
        except ValueError as exc:
            raw_mode = str(task.metadata.get("strm_mode") or "").strip()
            invalid_codes = ("invalid_strm_mode", *issue_codes)
            return _match(
                "manual_required",
                "high",
                "task STRM mode is invalid and requires manual review",
                invalid_codes,
                manual_actions=_MANUAL_ACTIONS,
                evidence=(raw_mode or str(exc),),
            )
        mismatch_issues = tuple(
            issue for issue in issue_list if mode_rule_for_issue(mode, issue.code) == "strm_mode_mismatch"
        )
        if mismatch_issues:
            if attempts_exhausted(task, controls):
                return _match(
                    "repeated_failure",
                    "high",
                    "quality attempts have reached the configured limit",
                    issue_codes,
                    manual_actions=_MANUAL_ACTIONS,
                    evidence=(issue.detail for issue in issue_list if issue.detail),
                )
            evidence = tuple(issue.detail for issue in mismatch_issues if str(issue.detail).strip())
            auto_allowed = (
                bool(controls["allow_auto_reprocess"])
                and has_complete_evidence(mismatch_issues, "direct_strm")
                and not attempts_exhausted(task, controls)
            )
            return _match(
                "strm_mode_mismatch",
                "medium",
                "direct STRM conflicts with the task STRM mode",
                ("direct_strm",),
                auto_action="reprocess",
                auto_allowed=auto_allowed,
                manual_actions=() if auto_allowed else _MANUAL_ACTIONS,
                evidence=evidence,
            )

        if "missing_dest" in issue_codes:
            return _match(
                "missing_destination",
                "medium",
                "destination directory is missing",
                ("missing_dest",),
                manual_actions=_MANUAL_ACTIONS,
                evidence=(issue.detail for issue in issue_list if issue.code == "missing_dest" and issue.detail),
            )
        if "missing_strm" in issue_codes:
            return _match(
                "missing_strm",
                "medium",
                "destination has no STRM file",
                ("missing_strm",),
                manual_actions=_MANUAL_ACTIONS,
                evidence=(issue.detail for issue in issue_list if issue.code == "missing_strm" and issue.detail),
            )
        if "unexpected_strm" in issue_codes:
            unexpected_issues = tuple(issue for issue in issue_list if issue.code == "unexpected_strm")
            if attempts_exhausted(task, controls):
                return _match(
                    "repeated_failure",
                    "high",
                    "quality attempts have reached the configured limit",
                    issue_codes,
                    manual_actions=_MANUAL_ACTIONS,
                    evidence=(issue.detail for issue in issue_list if issue.detail),
                )
            auto_allowed = (
                bool(controls["allow_auto_reprocess"])
                and has_complete_evidence(unexpected_issues, "unexpected_strm")
                and not attempts_exhausted(task, controls)
            )
            return _match(
                "unexpected_strm",
                "medium",
                "STRM content does not match the expected mode",
                ("unexpected_strm",),
                auto_action="reprocess",
                auto_allowed=auto_allowed,
                manual_actions=() if auto_allowed else _MANUAL_ACTIONS,
                evidence=(issue.detail for issue in unexpected_issues if issue.detail),
            )
        if "repeated_failure" in issue_codes:
            return _match(
                "repeated_failure",
                "high",
                "quality attempts have reached the configured limit",
                issue_codes,
                manual_actions=_MANUAL_ACTIONS,
                evidence=(issue.detail for issue in issue_list if issue.detail),
            )
        if not issue_list or (mode == "direct" and issue_codes == ("direct_strm",)):
            return _match("no_issue", "none", "no actionable quality issue")
        return _match(
            "manual_required",
            "medium",
            "issue requires an explicit manual decision",
            issue_codes,
            manual_actions=_MANUAL_ACTIONS,
            evidence=(issue.detail for issue in issue_list if issue.detail),
        )
