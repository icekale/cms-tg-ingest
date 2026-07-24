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
INDIAN_MOVIE_MARKERS = {"印度", "印地", "宝莱坞", "bollywood", "hindi", "andhadhun", "tamil", "telugu", "hi", "ta", "te", "印地语", "泰米尔语", "泰卢固语"}


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
    text = normalize_text(" ".join(str(value or "") for value in values))
    if not text:
        return False
    return any(normalize_text(marker) in text for marker in INDIAN_MOVIE_MARKERS)


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
        return {
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
            "category": category,
            "source": "tmdb_api",
        }


def extract_tmdb_search_query(share_name: str) -> str:
    text = str(share_name or "")
    title_pattern = r"([A-Za-z][A-Za-z0-9'&:]+(?:[ ._-][A-Za-z0-9'&:]+){1,}?)"
    for marker in (r"(?=[ ._-]S\d{1,2}\b)", r"(?=[ ._-](?:19|20)\d{2}\b)"):
        match = re.search(rf"{title_pattern}{marker}", text, re.I)
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


def apply_tmdb_hint_resolution(
    recognition: dict[str, Any],
    share_name: str,
    tmdb_resolver: Any | None,
) -> tuple[dict[str, Any], bool]:
    if not is_recognition_uncertain(recognition):
        return recognition, False
    tmdb_id = str(recognition.get("tmdb_id") or extract_tmdb_id_from_name(share_name) or "").strip()
    if not tmdb_id or not tmdb_resolver or not getattr(tmdb_resolver, "enabled", False):
        return recognition, True
    candidates = []
    for media_type in ("tv", "movie"):
        try:
            item = tmdb_resolver.lookup(tmdb_id, media_type, share_name)
        except Exception:
            LOG.debug("TMDB resolver failed", exc_info=True)
            item = {"ok": False}
        score = tmdb_match_score(item, share_name)
        if score:
            candidates.append((score, item))
    if not candidates:
        return recognition, True
    candidates.sort(key=lambda value: value[0], reverse=True)
    best = candidates[0][1]
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
        }
    )
    return enriched, False


def extract_tmdb_id_from_name(value: str) -> str:
    match = re.search(r"tmdb(?:id)?[=_\-](\d+)", str(value or ""), re.I)
    return match.group(1) if match else ""


def extract_year_from_name(value: str) -> str:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return match.group(0) if match else ""


def media_type_for_category(category: str) -> str:
    if category in {"华语电影", "欧美电影", "亚洲电影", "动漫电影"}:
        return "movie"
    if category in {"国产电视", "外国电视", "番剧"}:
        return "tv"
    return ""


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


def expected_task_tmdb_id(recognition: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    row = row or {}
    explicit = str(recognition.get("tmdb_id") or "").strip()
    if explicit:
        return explicit
    for value in (
        row.get("title"),
        recognition.get("share_name"),
        row.get("url"),
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
