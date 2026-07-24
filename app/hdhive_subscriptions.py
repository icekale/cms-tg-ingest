from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class HdhiveUrlError(ValueError):
    """Raised when a URL is not an HDHive TV page accepted by subscriptions."""


@dataclass(frozen=True)
class HdhiveTvUrl:
    slug: str
    url: str


_SLUG_RE = re.compile(r"^[A-Za-z0-9]{8,96}$")


def parse_hdhive_tv_url(url: str) -> HdhiveTvUrl:
    raw = str(url or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise HdhiveUrlError("HDHive 链接必须使用 HTTP 或 HTTPS")
    if (parsed.hostname or "").lower() not in {"hdhive.com", "www.hdhive.com"}:
        raise HdhiveUrlError("这不是受支持的 HDHive 域名")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].lower() != "tv" or not _SLUG_RE.fullmatch(parts[1]):
        raise HdhiveUrlError("HDHive 链接必须是 /tv/<slug> 剧集页面")
    return HdhiveTvUrl(slug=parts[1], url=f"{parsed.scheme.lower()}://{parsed.netloc}{parsed.path}")
