from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.clients.http import HttpJson

LOG = logging.getLogger("cms-tg-ingest")


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(_as_text(item) for item in value if _as_text(item))
    text = _as_text(value)
    return (text,) if text else ()


def _json_from_error(error: RuntimeError) -> dict[str, Any] | None:
    text = str(error)
    marker = ": {"
    start = text.find(marker)
    if start < 0:
        return None
    try:
        parsed = json.loads(text[start + 2 :])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class HdhiveAccount:
    nickname: str
    points: int
    weekly_free_quota_remaining: int
    weekly_free_quota_unlimited: bool
    level: str
    is_blocked: bool
    is_forever_vip: bool = False


@dataclass(frozen=True)
class HdhiveResource:
    slug: str
    title: str
    pan_type: str
    share_size: str
    video_resolution: tuple[str, ...]
    source: tuple[str, ...]
    subtitle_language: tuple[str, ...]
    subtitle_type: tuple[str, ...]
    unlock_points: int | None
    validate_status: str
    validate_message: str
    is_unlocked: bool
    season_number: int | None = None
    episode_number: int | None = None
    episode_key: str = ""


@dataclass(frozen=True)
class HdhiveTvPage:
    slug: str
    tmdb_id: str
    title: str
    year: str
    url: str


@dataclass(frozen=True)
class HdhiveUnlockItem:
    slug: str
    success: bool
    full_url: str
    message: str
    error_code: str
    already_owned: bool


class HdhiveProxyError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        self.error_code = _as_text(error_code) or "HDHIVE_ERROR"
        self.message = _as_text(message) or "HDHive request failed"
        super().__init__(self.message)


class HdhiveProxyClient:
    """Client for the authorization proxy used by the CMS HDHive integration."""

    def __init__(
        self,
        base_url: str,
        token_path: str | Path,
        http: HttpJson | None = None,
        refresh_via_cms: Callable[[], None] | None = None,
        page_fetcher: Callable[[str], str] | None = None,
    ):
        self.base_url = _as_text(base_url).rstrip("/")
        self.token_path = Path(token_path)
        self.http = http or HttpJson(timeout=60)
        self.refresh_via_cms = refresh_via_cms
        self.page_fetcher = page_fetcher

    def _access_token(self) -> str:
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HdhiveProxyError("HDHIVE_NOT_AUTHORIZED", "HDHive 尚未在 CMS 中完成授权") from exc
        token = _as_text(payload.get("access_token")) if isinstance(payload, dict) else ""
        if not token:
            raise HdhiveProxyError("HDHIVE_NOT_AUTHORIZED", "HDHive 尚未在 CMS 中完成授权")
        return token

    @staticmethod
    def _is_success(response: dict[str, Any]) -> bool:
        if response.get("success") is False:
            return False
        code = response.get("code")
        return code in (None, 200, "200", "SUCCESS", "success") or response.get("success") is True

    @staticmethod
    def _response_error(response: dict[str, Any]) -> tuple[str, str]:
        code = _as_text(response.get("error_code") or response.get("code"))
        message = _as_text(response.get("message") or response.get("description") or response.get("msg"))
        return code or "HDHIVE_ERROR", message or "HDHive request failed"

    @classmethod
    def _is_expired_response(cls, response: dict[str, Any]) -> bool:
        code, message = cls._response_error(response)
        haystack = f"{code} {message}".upper()
        return "TOKEN_EXPIRED" in haystack or "TOKEN_REFRESH_REQUIRED" in haystack

    def _request(self, path: str, fields: dict[str, Any]) -> dict[str, Any]:
        refreshed = False
        while True:
            token = self._access_token()
            payload = dict(fields)
            payload["access_token"] = token
            try:
                response = self.http.request(
                    f"{self.base_url}{path}",
                    method="POST",
                    payload=payload,
                )
            except RuntimeError as exc:
                response = _json_from_error(exc)
                if response is None:
                    raise HdhiveProxyError("HDHIVE_NETWORK_ERROR", "HDHive 授权代理暂时不可用") from exc
            if self._is_expired_response(response):
                if refreshed or self.refresh_via_cms is None:
                    raise HdhiveProxyError("HDHIVE_TOKEN_EXPIRED", "HDHive 授权已过期，请在 CMS 中重新授权")
                refreshed = True
                self.refresh_via_cms()
                continue
            if not self._is_success(response):
                code, message = self._response_error(response)
                raise HdhiveProxyError(code, message)
            return response

    def account(self) -> HdhiveAccount:
        response = self._request("/api/hdhive/me", {})
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        return HdhiveAccount(
            nickname=_as_text(data.get("nickname") or data.get("username")) or "HDHive 用户",
            points=_as_int(data.get("points")) or 0,
            weekly_free_quota_remaining=_as_int(data.get("weekly_free_quota_remaining")) or 0,
            weekly_free_quota_unlimited=bool(data.get("weekly_free_quota_unlimited")),
            level=_as_text(data.get("level")) or "user",
            is_blocked=bool(data.get("is_blocked")),
            is_forever_vip=bool(data.get("is_forever_vip")),
        )

    def resources(self, media_type: str, tmdb_id: str) -> list[HdhiveResource]:
        media_type = _as_text(media_type).lower()
        if media_type not in {"movie", "tv"}:
            raise HdhiveProxyError("INVALID_MEDIA_TYPE", "媒体类型必须是电影或剧集")
        response = self._request(
            "/api/hdhive/resources",
            {"resource_type": media_type, "tmdb_id": _as_text(tmdb_id)},
        )
        data = response.get("data")
        items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
        return [self._resource(item) for item in items if isinstance(item, dict) and _as_text(item.get("slug"))]

    def resolve_tv_page(self, url: str) -> HdhiveTvPage:
        from app.hdhive_subscriptions import parse_hdhive_tv_url

        try:
            parsed = parse_hdhive_tv_url(url)
            html = self.page_fetcher(url) if self.page_fetcher is not None else self._fetch_page(url)
        except HdhiveProxyError:
            raise
        except Exception as exc:
            raise HdhiveProxyError("HDHIVE_PAGE_UNRESOLVED", "HDHive 剧集页面暂时无法访问") from exc

        normalized = str(html or "").replace(r'\"', '"')
        slug_pattern = re.escape(parsed.slug)
        match = re.search(
            rf'"slug"\s*:\s*"{slug_pattern}"(?P<body>.{{0,3000}}?)"tmdb_id"\s*:\s*"?(?P<tmdb>\d+)"?',
            normalized,
            re.DOTALL,
        )
        if match is None:
            raise HdhiveProxyError("HDHIVE_PAGE_UNRESOLVED", "HDHive 页面没有可用的 TMDB 剧集信息")
        body = normalized[match.start() : match.end() + 2000]
        title_match = re.search(r'"(?:name|title)"\s*:\s*"([^"]+)"', body)
        year_match = re.search(r'"(?:first_air_date|release_date)"\s*:\s*"(\d{4})', body)
        return HdhiveTvPage(
            slug=parsed.slug,
            tmdb_id=match.group("tmdb"),
            title=title_match.group(1) if title_match else "",
            year=year_match.group(1) if year_match else "",
            url=parsed.url,
        )

    def _fetch_page(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 cms-tg-ingest",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.http.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except (OSError, URLError, TimeoutError) as exc:
            raise HdhiveProxyError("HDHIVE_PAGE_UNRESOLVED", "HDHive 剧集页面暂时无法访问") from exc

    @staticmethod
    def _resource(item: dict[str, Any]) -> HdhiveResource:
        return HdhiveResource(
            slug=_as_text(item.get("slug")),
            title=_as_text(item.get("title") or item.get("name")),
            pan_type=_as_text(item.get("pan_type") or item.get("cloud_type")) or "unknown",
            share_size=_as_text(item.get("share_size")),
            video_resolution=_as_tuple(item.get("video_resolution")),
            source=_as_tuple(item.get("source")),
            subtitle_language=_as_tuple(item.get("subtitle_language")),
            subtitle_type=_as_tuple(item.get("subtitle_type")),
            unlock_points=_as_int(item.get("unlock_points")),
            validate_status=_as_text(item.get("validate_status")),
            validate_message=_as_text(item.get("validate_message")),
            is_unlocked=bool(item.get("is_unlocked")),
            season_number=_as_int(item.get("season_number")),
            episode_number=_as_int(item.get("episode_number")),
            episode_key=_as_text(item.get("episode_key") or item.get("episode_code")),
        )

    def unlock(self, slugs: list[str]) -> list[HdhiveUnlockItem]:
        normalized = list(dict.fromkeys(_as_text(slug) for slug in slugs if _as_text(slug)))
        if not normalized:
            raise HdhiveProxyError("INVALID_RESOURCE", "没有选择 HDHive 资源")
        field = {"slug": normalized[0]} if len(normalized) == 1 else {"slugs": normalized}
        response = self._request("/api/hdhive/resources/unlock", field)
        data = response.get("data")
        if len(normalized) == 1 and isinstance(data, dict) and "items" not in data:
            return [self._unlock_item(data, normalized[0])]
        items = data.get("items", []) if isinstance(data, dict) else []
        return [self._unlock_item(item, normalized[index]) for index, item in enumerate(items) if isinstance(item, dict)]

    @staticmethod
    def _unlock_item(item: dict[str, Any], fallback_slug: str) -> HdhiveUnlockItem:
        return HdhiveUnlockItem(
            slug=_as_text(item.get("slug")) or fallback_slug,
            success=bool(item.get("success", bool(item.get("full_url") or item.get("url")))),
            full_url=_as_text(item.get("full_url") or item.get("url")),
            message=_as_text(item.get("message") or item.get("msg")),
            error_code=_as_text(item.get("error_code") or item.get("code")),
            already_owned=bool(item.get("already_owned")),
        )

    def healthcheck(self) -> bool:
        try:
            self.account()
        except HdhiveProxyError:
            return False
        return True
