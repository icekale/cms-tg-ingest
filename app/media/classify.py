from __future__ import annotations

import html as html_lib
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any

from app.clients.http import HttpJson
from app.config import default_library_roots

LOG = logging.getLogger("cms-tg-ingest")


def normalize_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def clean_share_title(value: str) -> str:
    text = re.sub(r"\{?\[?tmdb(?:id)?[=_\-]\d+\]?\}?", "", str(value or ""), flags=re.I)
    text = re.sub(r"\(\d{4}[^)]*\)", "", text)
    return text.strip()


CHINESE_LANGUAGE_MARKERS = {"zh", "cn", "中文", "普通话", "汉语", "粤语", "國語", "国语"}
CHINESE_COUNTRY_MARKERS = {"CN", "HK", "TW", "MO"}
ASIAN_MOVIE_COUNTRY_MARKERS = {"JP", "KR", "TH", "ID", "MY", "SG", "PH", "VN"}
ASIAN_MOVIE_LANGUAGE_MARKERS = {
    "ja",
    "jp",
    "日语",
    "日本語",
    "ko",
    "kr",
    "韩语",
    "韓語",
    "한국어",
    "th",
    "泰语",
    "id",
    "印尼语",
}
INDIAN_MOVIE_MARKERS = {"印度", "印地", "宝莱坞", "bollywood", "hindi", "andhadhun", "tamil", "telugu", "印地语", "泰米尔语", "泰卢固语"}
# Two-letter language codes (hi/ta/te) collide with common English words when
# matched as substrings ("ta" in "Titanic", "te" in "Interest", "hi" in
# "Spirited Away"). Only treat them as hints when they appear as standalone
# words in the raw (un-normalized) name, e.g. "Hi Nanna".
INDIAN_TWO_LETTER_MARKERS = ("hi", "ta", "te")


def normalized_tmdb_language(language: str) -> str:
    return re.sub(r"\s+", "", str(language or "").strip()).lower()


def language_matches(normalized_language: str, markers: set[str]) -> bool:
    if not normalized_language:
        return False
    parts = {part for part in re.split(r"[/,;，、|]+", normalized_language) if part}
    for marker in markers:
        normalized_marker = normalized_tmdb_language(marker)
        if not normalized_marker:
            continue
        if normalized_language == normalized_marker or normalized_marker in parts:
            return True
        if len(normalized_marker) > 2 and normalized_marker in normalized_language:
            return True
    return False


def has_indian_movie_hint(*values: str) -> bool:
    raw = " ".join(str(value or "") for value in values)
    text = normalize_text(raw)
    if not text:
        return False
    if any(normalize_text(marker) in text for marker in INDIAN_MOVIE_MARKERS):
        return True
    lowered = raw.lower()
    return any(
        re.search(rf"(?<![a-z0-9]){word}(?![a-z0-9])", lowered)
        for word in INDIAN_TWO_LETTER_MARKERS
    )


def user_movie_category_bucket(category: str, media_type: str, *hints: str) -> str:
    if media_type == "movie" and category == "亚洲电影" and has_indian_movie_hint(*hints):
        return "欧美电影"
    return category


def infer_region_category(media_type: str, title: str, language: str = "", countries: list[str] | None = None, genres: list[str] | None = None) -> str:
    normalized_language = normalized_tmdb_language(language)
    has_language = bool(normalized_language)
    country_set = {str(country or "").upper() for country in (countries or []) if str(country or "").strip()}
    genre_text = normalize_text(" ".join(str(genre or "") for genre in (genres or [])))
    is_animation = any(marker in genre_text for marker in ("animation", "anime", "动画", "動畫", "动漫", "番剧"))
    is_documentary = any(marker in genre_text for marker in ("documentary", "纪录", "紀錄"))
    if media_type == "tv":
        if language_matches(normalized_language, CHINESE_LANGUAGE_MARKERS) or country_set & CHINESE_COUNTRY_MARKERS:
            return "国产电视"
        if is_animation and country_set & {"JP"}:
            return "番剧"
        return "外国电视"
    if media_type == "movie":
        if is_documentary:
            return "纪录片"
        if is_animation:
            return "动漫电影"
        if language_matches(normalized_language, CHINESE_LANGUAGE_MARKERS) or country_set & CHINESE_COUNTRY_MARKERS:
            return "华语电影"
        if language_matches(normalized_language, ASIAN_MOVIE_LANGUAGE_MARKERS) or country_set & ASIAN_MOVIE_COUNTRY_MARKERS:
            return "亚洲电影"
        if not has_language and re.search(r"[\u4e00-\u9fff]", title):
            return "华语电影"
        return "欧美电影"
    return ""


class TmdbWebResolver:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return True

    def lookup(self, tmdb_id: str, media_type: str, share_name: str) -> dict[str, Any]:
        url = f"https://www.themoviedb.org/{media_type}/{tmdb_id}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception:
            LOG.debug("TMDB web lookup failed media_type=%s tmdb_id=%s", media_type, tmdb_id, exc_info=True)
            return {"ok": False, "type": media_type, "tmdb_id": tmdb_id}
        title = extract_tmdb_page_title(raw)
        language = extract_tmdb_default_language(raw)
        if not title:
            return {"ok": False, "type": media_type, "tmdb_id": tmdb_id}
        return {
            "ok": True,
            "title": title,
            "type": media_type,
            "tmdb_id": tmdb_id,
            "language": language,
            "source": "tmdb_web",
        }

    def search(self, query: str, media_type: str = "tv") -> dict[str, Any]:
        query = str(query or "").strip()
        media_type = "movie" if media_type == "movie" else "tv"
        if not query:
            return {"ok": False, "type": media_type}
        url = "https://www.themoviedb.org/search?" + urllib.parse.urlencode({"query": query})
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception:
            LOG.debug("TMDB web search failed media_type=%s query=%s", media_type, query, exc_info=True)
            return {"ok": False, "type": media_type}
        pattern = re.compile(rf'href="/{re.escape(media_type)}/(\d+)[^"]*"[^>]*>.*?alt="([^"]+)"', re.I | re.S)
        match = pattern.search(raw)
        if not match:
            match = re.search(rf'href="/{re.escape(media_type)}/(\d+)[^"]*"', raw, re.I)
        if not match:
            return {"ok": False, "type": media_type}
        title = html_lib.unescape(match.group(2)).strip() if match.lastindex and match.lastindex >= 2 else query
        return {"ok": True, "title": title, "type": media_type, "tmdb_id": match.group(1), "source": "tmdb_search"}


class TmdbApiResolver:
    def __init__(self, api_key: str = "", bearer_token: str = "", timeout: int = 15, http: HttpJson | None = None, fallback: Any | None = None):
        self.api_key = str(api_key or "").strip()
        self.bearer_token = str(bearer_token or "").strip()
        self.timeout = timeout
        self.http = http or HttpJson(timeout)
        self.fallback = fallback

    @property
    def enabled(self) -> bool:
        return bool(self.api_key or self.bearer_token)

    def lookup(self, tmdb_id: str, media_type: str, share_name: str) -> dict[str, Any]:
        media_type = "movie" if media_type == "movie" else "tv"
        tmdb_id = str(tmdb_id or "").strip()
        if not tmdb_id or not self.enabled:
            return {"ok": False, "type": media_type, "tmdb_id": tmdb_id}
        try:
            return self._normalize_details(self._request(f"/{media_type}/{tmdb_id}", {"language": "zh-CN"}), media_type)
        except Exception:
            LOG.debug("TMDB API lookup failed media_type=%s tmdb_id=%s", media_type, tmdb_id, exc_info=True)
            if self.fallback and getattr(self.fallback, "enabled", True):
                return self.fallback.lookup(tmdb_id, media_type, share_name)
            return {"ok": False, "type": media_type, "tmdb_id": tmdb_id}

    def search(self, query: str, media_type: str = "tv") -> dict[str, Any]:
        query = str(query or "").strip()
        media_type = "movie" if media_type == "movie" else "tv"
        if not query or not self.enabled:
            return {"ok": False, "type": media_type}
        try:
            data = self._request(f"/search/{media_type}", {"query": query, "language": "zh-CN", "include_adult": "false"})
            results = data.get("results") if isinstance(data, dict) else []
            if not isinstance(results, list) or not results:
                return {"ok": False, "type": media_type}
            tmdb_id = str(results[0].get("id") or "").strip()
            if not tmdb_id:
                return {"ok": False, "type": media_type}
            return self.lookup(tmdb_id, media_type, query)
        except Exception:
            LOG.debug("TMDB API search failed media_type=%s query=%s", media_type, query, exc_info=True)
            if self.fallback and getattr(self.fallback, "enabled", True):
                return self.fallback.search(query, media_type)
            return {"ok": False, "type": media_type}

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {key: value for key, value in params.items() if value not in (None, "")}
        if self.api_key:
            query["api_key"] = self.api_key
        url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return self.http.request(url, headers=headers)

    @staticmethod
    def _normalize_details(data: dict[str, Any], media_type: str) -> dict[str, Any]:
        if not isinstance(data, dict) or not data.get("id"):
            return {"ok": False, "type": media_type}
        countries = []
        if media_type == "movie":
            countries = [item.get("iso_3166_1") for item in data.get("production_countries") or [] if isinstance(item, dict)]
        else:
            countries = [str(value or "") for value in data.get("origin_country") or []]
        genres = [str(item.get("name") or "") for item in data.get("genres") or [] if isinstance(item, dict)]
        title = str(data.get("title") or data.get("name") or data.get("original_title") or data.get("original_name") or "")
        language = str(data.get("original_language") or "")
        category = infer_region_category(media_type, title, language, countries, genres)
        vote_average = _normalized_float(data.get("vote_average"))
        release_date = str(data.get("release_date") or data.get("first_air_date") or "")
        result = {
            "ok": True,
            "title": title,
            "type": media_type,
            "tmdb_id": str(data.get("id") or ""),
            "language": language,
            "countries": [country for country in countries if country],
            "genres": genres,
            "poster_path": str(data.get("poster_path") or ""),
            "backdrop_path": str(data.get("backdrop_path") or ""),
            "overview": str(data.get("overview") or ""),
            "vote_average": vote_average,
            "release_date": release_date,
            "category": category,
            "source": "tmdb_api",
        }
        if media_type == "tv":
            tv_metadata = {
                "status": str(data.get("status") or ""),
                "seasons": _normalize_tv_seasons(data.get("seasons")),
            }
            for field in ("number_of_seasons", "number_of_episodes"):
                value = _normalized_int(data.get(field))
                if value is not None:
                    tv_metadata[field] = value
            result.update(tv_metadata)
        return result


def _normalized_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if number < 0:
        return None
    return number


def _normalized_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 10:
        return None
    return number


def _normalize_tv_seasons(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    seasons = []
    for item in value:
        if not isinstance(item, dict):
            continue
        season_number = _normalized_int(item.get("season_number"))
        episode_count = _normalized_int(item.get("episode_count"))
        if season_number is None or episode_count is None or season_number < 0 or episode_count < 0:
            continue
        seasons.append(
            {
                "season_number": season_number,
                "episode_count": episode_count,
                "air_date": str(item.get("air_date") or ""),
            }
        )
    return seasons


def extract_tmdb_search_query(share_name: str) -> str:
    text = str(share_name or "")
    # Multi-word titles first: prefer consuming the full title up to the first
    # season or release-year marker (e.g. "Cyberpunk.2077.2020" -> "Cyberpunk 2077").
    multi_word_pattern = r"([A-Za-z][A-Za-z0-9'&:]+(?:[ ._-][A-Za-z0-9'&:]+){1,}?)"
    # Single-word titles ("Dune.2021") are only matched when a marker directly
    # follows, so a year that belongs to the title ("Cyberpunk.2077") is not
    # mistaken for the release-year boundary.
    single_word_pattern = r"([A-Za-z][A-Za-z0-9'&:]+)"
    # SxxEyy has no word boundary between the episode number and the next token
    # ("S01E01.1080p"), so the season marker must consume an optional episode.
    markers = (
        r"(?=[ ._-]S\d{1,2}(?:E\d{1,4})?\b)",
        r"(?=[ ._-](?:19|20)\d{2}\b)",
    )
    for pattern in (multi_word_pattern, single_word_pattern):
        for marker in markers:
            match = re.search(rf"{pattern}{marker}", text, re.I)
            if match:
                return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z]+", " ", match.group(1))).strip()
    return extract_primary_chinese_title(text)


def extract_tmdb_page_title(html: str) -> str:
    match = re.search(r'<meta property="og:title" content="([^"]+)"', html, re.I)
    if not match:
        match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if not match:
        return ""
    title = html_lib.unescape(match.group(1))
    title = re.sub(r"\s+—\s+The Movie Database.*$", "", title)
    title = re.sub(r"\s+\((?:TV Series|Movie).*?\)\s*$", "", title)
    return title.strip()


def extract_tmdb_default_language(html: str) -> str:
    match = re.search(r"<strong><bdi>默认语言</bdi></strong>\s*([^<]+)</p>", html, re.I)
    return html_lib.unescape(match.group(1)).strip() if match else ""


def tmdb_match_score(tmdb_result: dict[str, Any], share_name: str) -> int:
    if not tmdb_result.get("ok"):
        return 0
    share_norm = normalize_text(clean_share_title(share_name))
    title_norm = normalize_text(str(tmdb_result.get("title") or ""))
    if not share_norm or not title_norm:
        return 0
    if title_norm in share_norm or share_norm in title_norm:
        return 10
    return 0


def _tmdb_media_type_hint(recognition: dict[str, Any], share_name: str) -> str:
    recognized_type = str(recognition.get("type") or "").strip().lower()
    if recognized_type in {"movie", "tv"}:
        return recognized_type
    category_type = media_type_for_category(str(recognition.get("category") or "").strip())
    if category_type:
        return category_type
    if re.search(r"(?:^|[ ._\-\[(])S\d{1,2}(?:E\d{1,4})?\b", str(share_name or ""), re.I):
        return "tv"
    return "movie"


def apply_tmdb_hint_resolution(
    recognition: dict[str, Any],
    share_name: str,
    tmdb_resolver: Any | None,
) -> tuple[dict[str, Any], bool]:
    # A TMDB marker in the source name is an explicit identity assertion. It
    # must take precedence over a stale or incorrect CMS recognition result.
    name_tmdb_id = extract_tmdb_id_from_name(share_name)
    uncertain = is_recognition_uncertain(recognition)
    if not name_tmdb_id and not uncertain:
        return recognition, False
    tmdb_id = str(name_tmdb_id or recognition.get("tmdb_id") or "").strip()
    if not tmdb_id:
        if not uncertain:
            return recognition, False
        return recognition, True
    if not tmdb_id or not tmdb_resolver or not getattr(tmdb_resolver, "enabled", False):
        return recognition, uncertain
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    preferred_type = _tmdb_media_type_hint(recognition, share_name)
    for media_type in ("tv", "movie"):
        try:
            item = tmdb_resolver.lookup(tmdb_id, media_type, share_name)
        except Exception:
            LOG.debug("TMDB resolver failed", exc_info=True)
            item = {"ok": False}
        if not item.get("ok"):
            continue
        returned_id = str(item.get("tmdb_id") or "").strip()
        if returned_id and returned_id != tmdb_id:
            LOG.warning("TMDB resolver returned mismatched id requested=%s returned=%s", tmdb_id, returned_id)
            continue
        title_score = tmdb_match_score(item, share_name)
        type_score = 1 if media_type == preferred_type else 0
        candidates.append((title_score, type_score, item))
    if not candidates:
        return recognition, uncertain
    # Title matching is only a tie-breaker. The numeric TMDB marker remains
    # authoritative even when the source title is obfuscated or misleading.
    candidates.sort(key=lambda value: (value[0], value[1], 1 if str(value[2].get("type") or "") == "movie" else 0), reverse=True)
    best = candidates[0][2]
    media_type = str(best.get("type") or "")
    category = str(best.get("category") or "") or infer_region_category(
        media_type,
        str(best.get("title") or ""),
        str(best.get("language") or ""),
        best.get("countries") if isinstance(best.get("countries"), list) else None,
        best.get("genres") if isinstance(best.get("genres"), list) else None,
    )
    if not category:
        return recognition, True
    enriched = dict(recognition)
    enriched.update(
        {
            "ok": True,
            "title": str(best.get("title") or recognition.get("title") or share_name),
            "type": media_type,
            "category": category,
            "tmdb_id": tmdb_id,
            "category_status": "tmdb_resolved",
            "openai_source": str(best.get("source") or "tmdb_web"),
            "tmdb_source": str(best.get("source") or "tmdb_web"),
            "poster_path": str(best.get("poster_path") or ""),
            "backdrop_path": str(best.get("backdrop_path") or ""),
            "overview": str(best.get("overview") or ""),
            "genres": best.get("genres") if isinstance(best.get("genres"), list) else [],
            "vote_average": best.get("vote_average"),
            "release_date": str(best.get("release_date") or ""),
        }
    )
    return enriched, False


def apply_tmdb_search_resolution(
    recognition: dict[str, Any],
    share_name: str,
    tmdb_resolver: Any | None,
) -> tuple[dict[str, Any], bool]:
    if not is_recognition_uncertain(recognition):
        return recognition, False
    if recognition.get("tmdb_id") or not tmdb_resolver or not getattr(tmdb_resolver, "enabled", False):
        return recognition, True
    query = extract_tmdb_search_query(share_name)
    if not query:
        return recognition, True
    media_type = "tv" if re.search(r"\bS\d{1,2}\b|\.S\d{1,2}", str(share_name or ""), re.I) else "movie"
    try:
        item = tmdb_resolver.search(query, media_type)
    except Exception:
        LOG.debug("TMDB search resolver failed", exc_info=True)
        return recognition, True
    if not item or not item.get("ok") or not item.get("tmdb_id"):
        return recognition, True
    category = str(item.get("category") or "") or infer_region_category(
        str(item.get("type") or media_type),
        str(item.get("title") or ""),
        str(item.get("language") or ""),
        item.get("countries") if isinstance(item.get("countries"), list) else None,
        item.get("genres") if isinstance(item.get("genres"), list) else None,
    )
    if not category:
        return recognition, True
    enriched = dict(recognition)
    enriched.update(
        {
            "ok": True,
            "title": str(item.get("title") or query),
            "type": str(item.get("type") or media_type),
            "category": category,
            "tmdb_id": str(item.get("tmdb_id") or ""),
            "category_status": "tmdb_search_resolved",
            "openai_source": str(item.get("source") or "tmdb_search"),
            "poster_path": str(item.get("poster_path") or ""),
            "backdrop_path": str(item.get("backdrop_path") or ""),
            "overview": str(item.get("overview") or ""),
            "genres": item.get("genres") if isinstance(item.get("genres"), list) else [],
            "vote_average": item.get("vote_average"),
            "release_date": str(item.get("release_date") or ""),
        }
    )
    return enriched, False


def extract_tmdb_id_from_name(value: str) -> str:
    match = re.search(r"tmdb(?:id)?[=_\-](\d+)", str(value or ""), re.I)
    return match.group(1) if match else ""


def normalize_tmdb_hint_name(value: str, tmdb_id: str, title: str = "") -> str:
    """Build a CMS-friendly name while retaining the useful media suffix."""
    original = str(value or "").strip()
    tmdb_id = str(tmdb_id or "").strip()
    if not original or not tmdb_id:
        return original

    extension = ""
    extension_match = re.search(r"(?i)(\.(?:mkv|mp4|ts|iso|avi|mov|wmv|m2ts))$", original)
    if extension_match:
        extension = extension_match.group(1)
        stem = original[: -len(extension)]
    else:
        stem = original

    resolved_title = re.sub(r"\s+", " ", str(title or "").strip()).strip()
    if not resolved_title:
        resolved_title = re.sub(r"\s+", " ", re.sub(r"[\[{(]?tmdb(?:id)?[=_\-]\d+[\]})]?", "", stem, flags=re.I)).strip(" .-_()[]{}")
        resolved_title = re.sub(r"\s*[\[(]?((?:19|20)\d{2})[\])]?(?=\s|$)", " ", resolved_title)
    resolved_title = resolved_title.replace("/", "-").replace("\\", "-").replace("\x00", "")
    resolved_title = re.sub(r"\s+", " ", resolved_title).strip()
    resolved_title = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", resolved_title).strip()

    year = extract_year_from_name(stem)
    season = ""
    season_match = re.search(r"(?i)(?:^|[ ._\-\[(])(S\d{1,2}(?:E\d{1,4})?)\b", stem)
    if season_match:
        season = season_match.group(1).upper()
    season_folder = re.search(r"(?i)(?:^|[ ._\-\[(])(Season\s+\d{1,2})\b", stem)
    if season_folder and not season:
        season = re.sub(r"\s+", " ", season_folder.group(1)).title()

    parts = [resolved_title]
    if year:
        parts.append(f"({year})")
    if season:
        parts.append(season)
    parts.append(f"[tmdb={tmdb_id}]")
    return " ".join(part for part in parts if part).strip() + extension


def extract_year_from_name(value: str) -> str:
    text = str(value or "")
    # Resolution dimensions like 1920x1080 / 3840×2160 contain 4-digit numbers
    # that are not release years; drop them before scanning.
    text = re.sub(r"\d{3,4}[xX×*]\d{3,4}", " ", text)
    # Prefer the last bounded 19xx/20xx token: "Cyberpunk.2077.2020" is a 2020
    # release whose title itself contains a year, and a trailing year in a
    # release-group name is the release year.
    matches = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text)
    return matches[-1] if matches else ""


def media_type_for_category(category: str) -> str:
    if category in {"华语电影", "欧美电影", "外语电影", "亚洲电影", "动漫电影", "动画电影", "纪录片"}:
        return "movie"
    if category in {
        "国产电视",
        "外国电视",
        "番剧",
        "国产剧",
        "欧美剧",
        "日韩剧",
        "日番",
        "国漫",
        "儿童动画",
        "综艺",
    }:
        return "tv"
    return ""


def enrich_task_media_metadata(
    store: Any,
    tasks: list[dict[str, Any]],
    resolver: Any | None,
    *,
    max_enrich: int = 12,
) -> list[dict[str, Any]]:
    """Backfill poster/rating/year metadata for succeeded tasks that lack it.

    Tasks created before the media wall existed carry a tmdb_id in their
    metadata but no TMDB media fields, so the wall falls back to text cards.
    This lazily fills the gap on read: only tasks without poster_path are
    touched, the result is persisted to the task metadata (so a later refresh
    is a no-op), and at most `max_enrich` tasks are resolved per call to bound
    TMDB API usage. Tasks without a resolvable tmdb_id/type are left as-is.
    """
    if not tasks or not resolver or not getattr(resolver, "enabled", False):
        return tasks
    to_enrich: list[dict[str, Any]] = []
    for task in tasks:
        metadata = task.get("metadata") or {}
        if metadata.get("poster_path") or not metadata.get("tmdb_id"):
            continue
        media_type = str(metadata.get("type") or "").strip().lower()
        if media_type not in {"movie", "tv"}:
            media_type = media_type_for_category(str(metadata.get("category") or ""))
        if not media_type:
            continue
        to_enrich.append(task)
    enriched: set[int] = set()
    for task in to_enrich[:max_enrich]:
        task_id = task.get("id")
        metadata = task.get("metadata") or {}
        tmdb_id = str(metadata.get("tmdb_id") or "").strip()
        media_type = str(metadata.get("type") or "").strip().lower()
        if media_type not in {"movie", "tv"}:
            media_type = media_type_for_category(str(metadata.get("category") or ""))
        if not tmdb_id or media_type not in {"movie", "tv"}:
            continue
        try:
            item = resolver.lookup(tmdb_id, media_type, str(metadata.get("title") or "")[:80])
        except Exception:  # noqa: BLE001 - enrichment must never break the overview
            LOG.debug("media metadata enrichment failed task=%s", task_id, exc_info=True)
            continue
        if not item or not item.get("ok"):
            continue
        patch = {
            "poster_path": str(item.get("poster_path") or ""),
            "overview": str(item.get("overview") or ""),
            "genres": item.get("genres") if isinstance(item.get("genres"), list) else [],
            "vote_average": item.get("vote_average"),
            "release_date": str(item.get("release_date") or ""),
        }
        try:
            store.patch_metadata(int(task_id), patch)
        except Exception:  # noqa: BLE001 - persistence failure must not break the overview
            LOG.debug("media metadata enrichment persist failed task=%s", task_id, exc_info=True)
            continue
        metadata.update(patch)
        enriched.add(int(task_id))
    return tasks


def extract_primary_chinese_title(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^[A-Za-z]-", "", text)
    match = re.match(r"([\u4e00-\u9fff][\u4e00-\u9fff·・：:]+)", text)
    if not match:
        return ""
    title = match.group(1).strip("·・：:")
    return title if len(normalize_text(title)) >= 2 else ""


def candidate_tokens(recognition: dict[str, Any], share_name: str = "") -> list[str]:
    tokens = []
    for value in (recognition.get("tmdb_id"), recognition.get("title"), recognition.get("share_name"), share_name):
        value = str(value or "").strip()
        if value:
            tokens.append(value)
        primary_title = extract_primary_chinese_title(value)
        if primary_title:
            tokens.append(primary_title)
    normalized = []
    seen = set()
    for token in tokens:
        norm = normalize_text(token)
        if norm and norm not in seen:
            seen.add(norm)
            normalized.append(norm)
    return normalized


CATEGORY_ALIASES = {
    "动画电影": "动漫电影",
}


def map_category_label(label: str, recognition: dict[str, Any]) -> str:
    label = str(label or "").strip()
    label = CATEGORY_ALIASES.get(label, label)
    if label in default_library_roots() or label == "纪录片":
        return label
    return label


def final_category_for_move(row: dict[str, Any], recognition: dict[str, Any]) -> str:
    for value in (
        row.get("category_choice"),
        row.get("category_final"),
        recognition.get("category"),
    ):
        value = str(value or "").strip()
        if value:
            return map_category_label(value, recognition)
    media_type = str(recognition.get("type") or "")
    if media_type == "movie":
        return "欧美电影"
    if media_type == "tv":
        return "外国电视"
    return ""


def parse_recognition_json(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("recognition_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def explicit_task_tmdb_id(
    recognition: dict[str, Any],
    row: dict[str, Any] | None = None,
    share_name: str = "",
) -> str:
    """Return a TMDB marker from source metadata, never generated output paths."""
    row = row or {}
    for value in (
        row.get("received_title"),
        row.get("title"),
        recognition.get("share_name"),
        share_name,
        row.get("url"),
    ):
        tmdb_id = extract_tmdb_id_from_name(str(value or ""))
        if tmdb_id:
            return tmdb_id
    return ""


def expected_task_tmdb_id(recognition: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    row = row or {}
    # Keep the task identity anchored to source metadata.  A generated
    # self-share or media-library path is an output and may itself be wrong;
    # it must only be used as a last-resort hint for legacy rows.
    explicit = explicit_task_tmdb_id(recognition, row)
    if explicit:
        return explicit

    recognized_tmdb_id = str(recognition.get("tmdb_id") or "").strip()
    if recognized_tmdb_id:
        return recognized_tmdb_id

    for value in (
        row.get("own_share_file_name"),
        row.get("dest_path"),
        row.get("source_path"),
        row.get("emby_path"),
    ):
        tmdb_id = extract_tmdb_id_from_name(str(value or ""))
        if tmdb_id:
            return tmdb_id
    return ""


def item_tmdb_id(item: dict[str, Any]) -> str:
    provider_ids = item.get("ProviderIds") or item.get("ProviderIDs") or {}
    tmdb_id = str(provider_ids.get("Tmdb") or provider_ids.get("TMDB") or "").strip()
    if tmdb_id:
        return tmdb_id
    return extract_tmdb_id_from_name(" ".join(str(item.get(k) or "") for k in ("Path", "Name", "OriginalTitle")))


def is_recognition_uncertain(result: dict[str, Any]) -> bool:
    if not result.get("ok"):
        return True
    if not result.get("tmdb_id") and not result.get("title"):
        return True
    if result.get("type") not in {"movie", "tv"}:
        return True
    if not result.get("category"):
        return True
    return False
