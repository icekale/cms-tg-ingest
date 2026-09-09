from __future__ import annotations

import re
from dataclasses import dataclass

from app.clients.hdhive import HdhiveResource
from app.series_rules import EpisodeKey


DECISION_AUTO = "auto"
DECISION_PENDING = "pending"
DECISION_REJECT = "reject"
MAX_LLM_CALLS_PER_CHECK = 8
LLM_EPISODE_PENDING_PREFIX = "LLM 识别待确认："

_CLUE_RE = re.compile(
    r"更新至|"
    r"第[0-9一二三四五六七八九十百]+集|"
    r"全\d+集|"
    r"(?<![A-Za-z0-9])EP?\d+(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])S\d+(?![A-Za-z0-9])|"
    r"(?<![A-Za-z])Season\s*\d+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LlmEpisodeParse:
    ok: bool
    keys: tuple[EpisodeKey, ...] = ()
    season: int | None = None
    confidence: float = 0.0
    reason: str = ""
    evidence: str = ""


def has_episode_clue(text: str) -> bool:
    return bool(_CLUE_RE.search(str(text or "")))


def resource_clue_text(resource: HdhiveResource) -> str:
    parts = (
        getattr(resource, "title", ""),
        getattr(resource, "remark", ""),
        getattr(resource, "episode_key", ""),
        getattr(resource, "episode_code", ""),
    )
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def tmdb_season_numbers(details: dict | None) -> set[int]:
    seasons = (details or {}).get("seasons")
    if not isinstance(seasons, list):
        return set()
    numbers: set[int] = set()
    for season in seasons:
        if not isinstance(season, dict):
            continue
        raw = season.get("season_number")
        if isinstance(raw, bool):
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            numbers.add(number)
    return numbers


def keys_from_llm_payload(payload: dict | None, source_text: str) -> LlmEpisodeParse:
    data = payload if isinstance(payload, dict) else {}
    evidence = str(data.get("evidence") or "").strip()
    reason = str(data.get("reason") or "").strip()
    source = str(source_text or "")
    try:
        season = int(data.get("season"))
        start = int(data.get("episode_start"))
        end = int(data.get("episode_end"))
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        return LlmEpisodeParse(ok=False, reason=reason, evidence=evidence)
    if evidence == "" or evidence not in source:
        return LlmEpisodeParse(ok=False, confidence=confidence, reason=reason, evidence=evidence)
    if season < 0 or start <= 0 or start > end or end - start > 200:
        return LlmEpisodeParse(ok=False, confidence=confidence, reason=reason, evidence=evidence)
    keys = tuple(EpisodeKey(season, number) for number in range(start, end + 1))
    return LlmEpisodeParse(
        ok=True,
        keys=keys,
        season=season,
        confidence=max(0.0, min(1.0, confidence)),
        reason=reason,
        evidence=evidence,
    )


def decide_llm_episode(
    parsed: LlmEpisodeParse,
    tmdb_seasons: set[int],
    high_confidence: float,
    suggest_confidence: float,
) -> str:
    if not parsed.ok or not parsed.keys or parsed.season is None:
        return DECISION_REJECT
    if parsed.confidence < float(suggest_confidence):
        return DECISION_REJECT
    if parsed.confidence >= float(high_confidence) and parsed.season in tmdb_seasons:
        return DECISION_AUTO
    return DECISION_PENDING


def format_llm_pending_reason(keys: tuple[EpisodeKey, ...]) -> str:
    if not keys:
        return f"{LLM_EPISODE_PENDING_PREFIX}未知"
    if len(keys) == 1:
        span = keys[0].normalized
    else:
        span = f"{keys[0].normalized}-{keys[-1].normalized}"
    return f"{LLM_EPISODE_PENDING_PREFIX}{span}"
