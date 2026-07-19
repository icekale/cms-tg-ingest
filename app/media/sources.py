from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass


_SHARE_RE = re.compile(r"https?://(?:www\.)?(?:115cdn|115|anxia)\.com/s/[^\s<>'\"]+", re.I)
_MAGNET_RE = re.compile(r"magnet:\?[^\s<>'\"]+", re.I)
_ED2K_RE = re.compile(r"ed2k://\|file\|([^|]*)\|([0-9]+)\|([0-9a-f]{32})\|/", re.I)
_TRAILING_PUNCTUATION = ".,;)。），]】》>"


@dataclass(frozen=True)
class MediaSource:
    source_type: str
    source_key: str
    raw_url: str
    display_name: str = ""


def _strip_trailing_punctuation(value: str) -> str:
    return value.rstrip(_TRAILING_PUNCTUATION)


def _parse_share(value: str) -> MediaSource | None:
    parsed = urllib.parse.urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "s":
        return None
    share_code = parts[1].strip().lower()
    if not share_code:
        return None
    query = urllib.parse.parse_qs(parsed.query)
    receive_code = (query.get("password") or query.get("pwd") or query.get("code") or [""])[0].strip().lower()
    return MediaSource("share", f"share:{share_code}:{receive_code}", value)


def _parse_magnet(value: str) -> MediaSource | None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "magnet":
        return None
    query = urllib.parse.parse_qs(parsed.query)
    xt_values = query.get("xt") or []
    btih = ""
    for item in xt_values:
        match = re.fullmatch(r"urn:btih:([0-9a-z]+)", item, re.I)
        if match:
            btih = match.group(1).lower()
            break
    if not btih:
        return None
    display_name = (query.get("dn") or [""])[0].strip()
    return MediaSource("magnet", f"btih:{btih}", value, display_name=display_name)


def _parse_ed2k(match: re.Match[str], value: str) -> MediaSource:
    filename, size, file_hash = match.groups()
    return MediaSource(
        "ed2k",
        f"ed2k:{file_hash.lower()}:{size}",
        value,
        display_name=urllib.parse.unquote(filename),
    )


def parse_media_sources(text: str) -> list[MediaSource]:
    candidates: list[tuple[int, str, MediaSource]] = []
    content = str(text or "")
    for match in _SHARE_RE.finditer(content):
        value = _strip_trailing_punctuation(match.group(0))
        source = _parse_share(value)
        if source:
            candidates.append((match.start(), value, source))
    for match in _MAGNET_RE.finditer(content):
        value = _strip_trailing_punctuation(match.group(0))
        source = _parse_magnet(value)
        if source:
            candidates.append((match.start(), value, source))
    for match in _ED2K_RE.finditer(content):
        value = _strip_trailing_punctuation(match.group(0))
        candidates.append((match.start(), value, _parse_ed2k(match, value)))

    sources: list[MediaSource] = []
    seen: set[tuple[str, str]] = set()
    for _, _, source in sorted(candidates, key=lambda item: item[0]):
        identity = (source.source_type, source.source_key)
        if identity in seen:
            continue
        seen.add(identity)
        sources.append(source)
    return sources
