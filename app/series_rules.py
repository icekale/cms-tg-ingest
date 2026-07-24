"""Pure season and episode parsing rules for HDHive subscriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_EPISODE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])S(?P<season>\d{1,3})\s*E(?P<episode>\d{1,3})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_EXACT_EPISODE_RE = re.compile(r"^S(?P<season>\d{1,3})E(?P<episode>\d{1,3})$", re.IGNORECASE)
_SEASON_RE = re.compile(r"^S(?P<season>\d{1,3})$", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class EpisodeKey:
    season: int
    episode: int

    def __post_init__(self) -> None:
        if isinstance(self.season, bool) or not isinstance(self.season, int) or self.season < 0:
            raise ValueError("season must be a non-negative integer")
        if isinstance(self.episode, bool) or not isinstance(self.episode, int) or self.episode <= 0:
            raise ValueError("episode must be a positive integer")

    @property
    def normalized(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    def __str__(self) -> str:
        return self.normalized


@dataclass(frozen=True)
class EpisodeFilter:
    exact_keys: frozenset[EpisodeKey] = field(default_factory=frozenset)
    seasons: frozenset[int] = field(default_factory=frozenset)
    ranges: tuple[tuple[EpisodeKey, EpisodeKey], ...] = ()

    def __post_init__(self) -> None:
        normalized_ranges = tuple(tuple(item) for item in self.ranges)
        for item in normalized_ranges:
            if len(item) != 2 or not all(isinstance(key, EpisodeKey) for key in item):
                raise ValueError("ranges must contain episode-key pairs")
            start, end = item
            if start.season != end.season:
                raise ValueError("episode ranges cannot cross seasons")
            if start > end:
                raise ValueError("episode range start must not exceed its end")
        object.__setattr__(self, "exact_keys", frozenset(self.exact_keys))
        object.__setattr__(self, "seasons", frozenset(self.seasons))
        object.__setattr__(self, "ranges", normalized_ranges)

    def matches(self, key: EpisodeKey) -> bool:
        return (
            key in self.exact_keys
            or key.season in self.seasons
            or any(start <= key <= end for start, end in self.ranges)
        ) or (not self.exact_keys and not self.seasons and not self.ranges and not is_special_episode(key))


def episode_filter_matches(episode_filter: EpisodeFilter, episode_key: EpisodeKey) -> bool:
    return episode_filter.matches(episode_key)


def _episode_key(season: int, episode: int) -> EpisodeKey:
    if season == 0 or season > 0:
        return EpisodeKey(season, episode)
    raise ValueError("season must be non-negative")


def parse_episode_key(value: str | None) -> EpisodeKey | None:
    """Find one bounded SxxExx token in a title or return ``None``."""
    match = _EPISODE_TOKEN_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return _episode_key(int(match.group("season")), int(match.group("episode")))
    except ValueError:
        return None


def normalize_episode_key(value: str | None) -> str:
    key = parse_episode_key(value)
    return key.normalized if key is not None else ""


def parse_episode_filter(value: str | None) -> EpisodeFilter:
    raw = str(value or "").strip()
    if not raw:
        return EpisodeFilter()

    exact_keys: set[EpisodeKey] = set()
    seasons: set[int] = set()
    ranges: list[tuple[EpisodeKey, EpisodeKey]] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            raise ValueError("episode filter contains an empty token")

        if "-" in token:
            parts = [part.strip() for part in token.split("-")]
            if len(parts) != 2:
                raise ValueError(f"invalid episode range: {token}")
            start = _parse_exact_episode(parts[0])
            end = _parse_exact_episode(parts[1])
            if start is None or end is None:
                raise ValueError(f"episode ranges require SxxExx endpoints: {token}")
            if start.season != end.season:
                raise ValueError("episode ranges cannot cross seasons")
            if start > end:
                raise ValueError("episode range start must not exceed its end")
            ranges.append((start, end))
            continue

        season_match = _SEASON_RE.fullmatch(token)
        if season_match:
            seasons.add(int(season_match.group("season")))
            continue

        exact = _parse_exact_episode(token)
        if exact is None:
            raise ValueError(f"invalid episode filter token: {token}")
        exact_keys.add(exact)

    return EpisodeFilter(frozenset(exact_keys), frozenset(seasons), tuple(ranges))


def _parse_exact_episode(value: str) -> EpisodeKey | None:
    match = _EXACT_EPISODE_RE.fullmatch(value)
    if not match:
        return None
    return _episode_key(int(match.group("season")), int(match.group("episode")))


def is_special_episode(key: EpisodeKey) -> bool:
    return key.season == 0


def completion_state(
    tmdb_status: str,
    expected: set[EpisodeKey],
    terminal: set[EpisodeKey],
    blocked: set[EpisodeKey],
) -> str:
    status = str(tmdb_status or "").strip().lower()
    if status not in {"ended", "canceled"} or not expected:
        return "active"
    if blocked & expected or not expected.issubset(terminal):
        return "active"
    return "completed"
