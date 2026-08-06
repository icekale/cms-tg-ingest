from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.clients.hdhive import HdhiveResource, HdhiveUnlockItem
from app.hdhive_subscription_store import HdhiveSubscription, HdhiveSubscriptionItem, HdhiveSubscriptionStore
from app.series_rules import EpisodeKey, completion_state, is_special_episode, parse_episode_filter, parse_episode_key


LOG = logging.getLogger("cms-tg-ingest")
_UNLOCK_STALE_AFTER_SECONDS = 3600
_DEFINITIVE_NO_CHARGE_ERRORS = {"INSUFFICIENT_POINTS", "INVALID_RESOURCE"}


class HdhiveUrlError(ValueError):
    """Raised when a URL is not an HDHive TV page accepted by subscriptions."""


@dataclass(frozen=True)
class HdhiveTvUrl:
    slug: str
    url: str


_SLUG_RE = re.compile(r"^[A-Za-z0-9]{8,96}$")
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;)。），]】》>"


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


def extract_hdhive_tv_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.findall(str(text or "")):
        candidate = match.rstrip(_TRAILING_PUNCT)
        try:
            parsed = parse_hdhive_tv_url(candidate)
        except HdhiveUrlError:
            continue
        if parsed.url not in seen:
            seen.add(parsed.url)
            urls.append(parsed.url)
    return urls


_INVALID_STATUSES = {"invalid", "expired", "unavailable"}
_VALID_STATUSES = {"valid", "ok", "success", "available", "active"}
_EPISODE_RE = re.compile(r"s(\d{1,3})\s*e(\d{1,3})", re.IGNORECASE)
_EPISODE_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])s(?P<season>\d{1,3})\s*e(?P<start>\d{1,3})"
    r"\s*[-~至到]\s*(?:s(?P<end_season>\d{1,3})\s*)?e?(?P<end>\d{1,3})"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CHINESE_SEASON_RANGE_RE = re.compile(
    r"第(?P<season>[0-9一二三四五六七八九十百]+)季.*?"
    r"(?:第)?(?P<start>\d{1,3})\s*[-~至到]\s*(?:第)?(?P<end>\d{1,3})集?",
    re.IGNORECASE,
)
_UPDATED_THROUGH_RE = re.compile(
    r"(?:(?:第(?P<chinese_season>[0-9一二三四五六七八九十百]+)季|S(?P<season>\d{1,3}))\s*)?"
    r".{0,24}?(?:更新至|更)\s*第?E?(?P<end>\d{1,3})集?",
    re.IGNORECASE,
)
_RESOLUTION_RE = re.compile(r"(8k|4k|2160p|1440p|1080p|720p|576p|480p)", re.IGNORECASE)


@dataclass(frozen=True)
class SubscriptionCheckResult:
    discovered: int = 0
    enqueued: int = 0
    pending_confirmation: int = 0
    failed: int = 0
    skipped: int = 0
    error: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    subscription_status: str = "active"


@dataclass(frozen=True)
class HdhiveScheduledRun:
    run_id: str
    status: str
    started_at: str
    finished_at: str
    summary: dict[str, Any]


def _season_number(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdecimal():
        return int(text)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if text == "十":
        return 10
    if text.startswith("十") and text[1:] in digits:
        return 10 + digits[text[1:]]
    if text.endswith("十") and text[:-1] in digits:
        return digits[text[:-1]] * 10
    return None


def _episode_range(season: int, start: int, end: int) -> tuple[EpisodeKey, ...]:
    if season < 0 or start <= 0 or start > end or end - start > 200:
        return ()
    return tuple(EpisodeKey(season, number) for number in range(start, end + 1))


def _parse_episode_keys(value: str, default_season: int | None = None) -> tuple[EpisodeKey, ...]:
    text = str(value or "")
    range_match = _EPISODE_RANGE_RE.search(text)
    if range_match:
        season = int(range_match.group("season"))
        end_season = range_match.group("end_season")
        end_season_number = int(end_season) if end_season else season
        start = int(range_match.group("start"))
        end = int(range_match.group("end"))
        if end_season_number == season and start <= end and end - start <= 200:
            return tuple(EpisodeKey(season, number) for number in range(start, end + 1))
        return ()
    chinese_range = _CHINESE_SEASON_RANGE_RE.search(text)
    if chinese_range:
        season = _season_number(chinese_range.group("season"))
        if season is not None:
            return _episode_range(season, int(chinese_range.group("start")), int(chinese_range.group("end")))
    updated_through = _UPDATED_THROUGH_RE.search(text)
    if updated_through:
        if updated_through.group("chinese_season"):
            season = _season_number(updated_through.group("chinese_season"))
        elif updated_through.group("season") is not None:
            season = int(updated_through.group("season"))
        else:
            # A season-less "更新至第20集" note is interpreted against the
            # resource's own season when known; otherwise it is skipped rather
            # than guessed (a wrong season would corrupt emby matching).
            season = default_season
        if season is not None:
            return _episode_range(season, 1, int(updated_through.group("end")))
    match = _EPISODE_RE.search(text)
    if not match:
        return ()
    return (EpisodeKey(int(match.group(1)), int(match.group(2))),)


def episode_keys(resource: HdhiveResource) -> tuple[EpisodeKey, ...]:
    if resource.season_number is not None and resource.episode_number is not None:
        try:
            season_number = int(resource.season_number)
            episode_number = int(resource.episode_number)
        except (TypeError, ValueError):
            pass
        else:
            if season_number >= 0 and episode_number > 0:
                return (EpisodeKey(season_number, episode_number),)
    for value in (
        getattr(resource, "episode_key", ""),
        getattr(resource, "episode_code", ""),
        getattr(resource, "remark", ""),
        getattr(resource, "title", ""),
    ):
        if value:
            default_season = None
            if resource.season_number is not None:
                try:
                    default_season = int(resource.season_number)
                except (TypeError, ValueError):
                    default_season = None
            parsed = _parse_episode_keys(str(value), default_season=default_season)
            if parsed:
                return parsed
    return ()


def _format_episode_keys(keys: tuple[EpisodeKey, ...]) -> str:
    if not keys:
        return ""
    if len(keys) == 1:
        return keys[0].normalized
    if all(key.season == keys[0].season for key in keys) and all(
        current.episode == previous.episode + 1
        for previous, current in zip(keys, keys[1:])
    ):
        return f"{keys[0].normalized}-S{keys[-1].season:02d}E{keys[-1].episode:02d}"
    return ",".join(key.normalized for key in keys)


def episode_key(resource: HdhiveResource) -> str:
    parsed = episode_keys(resource)
    if parsed:
        return _format_episode_keys(parsed)
    return str(getattr(resource, "episode_key", "") or resource.slug).strip().lower()


def resolution_score(resource: HdhiveResource) -> int:
    scores = {"8k": 4320, "4k": 2160, "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "576p": 576, "480p": 480}
    values = list(resource.video_resolution) + [resource.title]
    return max((scores.get(match.group(1).lower(), 0) for value in values for match in [_RESOLUTION_RE.search(value or "")] if match), default=0)


def _is_valid_status(status: str) -> bool:
    return str(status or "").strip().lower() in _VALID_STATUSES


def select_best_resource(resources: list[HdhiveResource]) -> HdhiveResource | None:
    eligible = [resource for resource in resources if str(resource.validate_status or "").strip().lower() not in _INVALID_STATUSES]
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda resource: (
            0 if _is_valid_status(resource.validate_status) else 1,
            -resolution_score(resource),
            resource.unlock_points if resource.unlock_points is not None else 10**9,
            resource.slug,
        ),
    )[0]


def _is_115_share_url(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    return host in {"115.com", "www.115.com", "115cdn.com", "www.115cdn.com"} and parsed.path.lower().startswith("/s/")


def _task_id_from_intake_result(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        candidate = value.get("task_id") or value.get("id")
        return int(candidate) if str(candidate or "").isdigit() else None
    if isinstance(value, (list, tuple)) and value:
        return _task_id_from_intake_result(value[0])
    return None


class HdhiveSubscriptionService:
    def __init__(
        self,
        proxy: Any,
        store: HdhiveSubscriptionStore,
        enqueue_links: Callable[[list[str], str], Any],
        auto_unlock_max_points: int = 20,
        on_item_enqueued: Callable[[HdhiveSubscription, HdhiveSubscriptionItem], None] | None = None,
        tmdb_resolver: Any | None = None,
        emby: Any | None = None,
    ):
        self.proxy = proxy
        self.store = store
        self.enqueue_links = enqueue_links
        self.auto_unlock_max_points = max(0, int(auto_unlock_max_points))
        self.on_item_enqueued = on_item_enqueued
        self.tmdb_resolver = tmdb_resolver
        self.emby = emby

    def create_from_url(self, chat_id: str, url: str) -> HdhiveSubscription:
        page = self.proxy.resolve_tv_page(url)
        return self.store.create_subscription(
            str(chat_id),
            "hdhive_tv",
            page.slug,
            page.title or page.slug,
            page.tmdb_id,
            source_url=page.url,
        )

    def create_from_tmdb(self, chat_id: str, tmdb_id: str, title: str) -> HdhiveSubscription:
        tmdb_id = str(tmdb_id or "").strip()
        if not tmdb_id.isdigit():
            raise ValueError("TMDB 剧集 ID 无效")
        return self.store.create_subscription(str(chat_id), "tmdb_tv", tmdb_id, title, tmdb_id)

    def list(self, chat_id: str | None = None) -> list[HdhiveSubscription]:
        return self.store.list_subscriptions(chat_id)

    def set_episode_filter(self, subscription_id: int, value: str) -> HdhiveSubscription:
        normalized = str(value or "").strip()
        parse_episode_filter(normalized)
        return self.store.update_episode_filter(subscription_id, normalized)

    def pause(self, subscription_id: int) -> HdhiveSubscription:
        return self.store.set_status(subscription_id, "paused")

    def resume(self, subscription_id: int) -> HdhiveSubscription:
        return self.store.set_status(subscription_id, "active")

    def delete(self, subscription_id: int) -> HdhiveSubscription:
        return self.store.set_status(subscription_id, "deleted")

    def confirm_item(self, item_id: int) -> SubscriptionCheckResult:
        item = self.store.get_item(item_id)
        if item is None:
            raise KeyError(f"HDHive subscription item {item_id} does not exist")
        return self.check(item.subscription_id, confirmed_item_id=item.id)

    def check(self, subscription_id: int, confirmed_item_id: int | None = None) -> SubscriptionCheckResult:
        subscription = self.store.get_subscription(subscription_id)
        if subscription is None or subscription.status == "deleted":
            raise KeyError(f"HDHive subscription {subscription_id} does not exist")
        try:
            resources = self.proxy.resources("tv", subscription.tmdb_id)
        except Exception as exc:
            self.store.record_check(subscription.id, str(exc))
            raise

        episode_filter = parse_episode_filter(subscription.episode_filter)
        grouped: dict[str, list[HdhiveResource]] = {}
        parsed_by_resource: dict[int, tuple[EpisodeKey, ...]] = {}
        resource_item_ids: dict[int, int] = {}
        discovered = 0
        for resource in resources:
            if str(resource.pan_type or "").strip().lower() != "115":
                continue
            parsed_keys = episode_keys(resource)
            key = episode_key(resource)
            grouped.setdefault(key, []).append(resource)
            parsed_by_resource[id(resource)] = parsed_keys
            item = self.store.upsert_item(
                subscription.id,
                key,
                resource.slug,
                resource.validate_status,
                resolution_score(resource),
                resource.unlock_points,
                resource.title,
                normalized_episode_key=key if parsed_keys else "",
            )
            resource_item_ids[id(resource)] = item.id
            discovered += 1

        enqueued = pending = failed = skipped = 0
        filtered_groups: set[str] = set()
        emby_groups: set[str] = set()
        unparsed_groups: set[str] = set()

        def group_items(key: str) -> list[HdhiveSubscriptionItem]:
            items: list[HdhiveSubscriptionItem] = []
            for candidate in grouped[key]:
                item_id = resource_item_ids.get(id(candidate))
                if item_id is None:
                    continue
                item = self.store.get_item(item_id)
                if item is not None:
                    items.append(item)
            return items

        def protects_unlock_outcome(item: HdhiveSubscriptionItem) -> bool:
            return item.status in {"unlocking", "unlocked"} or (
                item.status == "pending_confirmation" and item.skip_reason == "unlock_outcome_unknown"
            )

        persisted_items = self.store.list_items(subscription.id)
        for item in persisted_items:
            if item.status == "unlocking":
                self.store.reconcile_stale_unlocking(
                    item.id,
                    stale_after_seconds=_UNLOCK_STALE_AFTER_SECONDS,
                )

        persisted_items = self.store.list_items(subscription.id)
        enqueued_by_episode = {
            self._stored_episode_keys(item): item
            for item in persisted_items
            if item.status == "enqueued" and self._stored_episode_keys(item)
        }
        for item in persisted_items:
            if item.status == "unlocked" and item.unlocked_url:
                saved_episode = self._stored_episode_keys(item)
                terminal_sibling = enqueued_by_episode.get(saved_episode)
                if terminal_sibling is not None:
                    self.store.mark_item_enqueued(item.id, terminal_sibling.task_id)
                    continue
                try:
                    enqueued_item = self._enqueue_saved_item(subscription, item)
                    enqueued += 1
                    if saved_episode:
                        enqueued_by_episode[saved_episode] = enqueued_item
                except Exception as exc:
                    self.store.mark_item_intake_failed(item.id, str(exc))
                    failed += 1

        persisted_by_episode: dict[str, list[HdhiveSubscriptionItem]] = {}
        for item in self.store.list_items(subscription.id):
            persisted_by_episode.setdefault(item.normalized_episode_key or item.episode_key, []).append(item)

        for key, candidates in grouped.items():
            parsed_keys = parsed_by_resource.get(id(candidates[0]), ())
            items = group_items(key)
            if not parsed_keys:
                unparsed_groups.add(key)
                for item in items:
                    if item.status != "enqueued" and not protects_unlock_outcome(item):
                        self.store.mark_item_skipped(item.id, "unparsed", "无法识别季集编号")
                continue
            if not any(episode_filter.matches(parsed) for parsed in parsed_keys):
                filtered_groups.add(key)
                reason = "不在订阅集数过滤范围内"
                if all(is_special_episode(parsed) for parsed in parsed_keys) and not subscription.episode_filter.strip():
                    reason = "特殊集默认跳过"
                for item in items:
                    if item.status != "enqueued" and not protects_unlock_outcome(item):
                        self.store.mark_item_skipped(item.id, "filtered", reason)
                continue
            for item in items:
                if item.status == "filtered":
                    self.store.reset_item_for_check(item.id, "filtered")

        emby_keys: set[str] = set()
        emby_skip_unavailable = not self._dependency_enabled(self.emby)
        emby_lookup_failed = False
        if not emby_skip_unavailable:
            try:
                raw_emby_keys = self.emby.existing_episode_keys_by_tmdb(subscription.tmdb_id)
                for value in raw_emby_keys or ():
                    parsed = parse_episode_key(str(value))
                    if parsed is not None:
                        emby_keys.add(parsed.normalized)
            except Exception:
                emby_skip_unavailable = True
                emby_lookup_failed = True
                LOG.warning("HDHive Emby episode lookup unavailable subscription_id=%s", subscription.id, exc_info=True)

        for key, candidates in grouped.items():
            parsed_keys = parsed_by_resource.get(id(candidates[0]), ())
            if not parsed_keys or not any(episode_filter.matches(parsed) for parsed in parsed_keys):
                continue
            items = group_items(key)
            if all(parsed.normalized in emby_keys for parsed in parsed_keys):
                emby_groups.add(key)
                for item in items:
                    if item.status not in {"enqueued", "emby_exists"} and not protects_unlock_outcome(item):
                        self.store.mark_item_skipped(item.id, "emby_exists", "Emby 中已存在该集")
            else:
                for item in items:
                    if item.status == "emby_exists":
                        self.store.reset_item_for_check(item.id, "emby_exists")

        for key, candidates in grouped.items():
            parsed_keys = parsed_by_resource.get(id(candidates[0]), ())
            stored_items = {item.resource_slug: item for item in group_items(key)}
            persisted_items = persisted_by_episode.get(key, [])
            unknown_items = [
                item
                for item in persisted_items
                if item.status == "pending_confirmation" and item.skip_reason == "unlock_outcome_unknown"
            ]
            if unknown_items and confirmed_item_id not in {item.id for item in unknown_items}:
                pending += 1
                continue
            if any(item.status in {"unlocking", "unlocked"} for item in persisted_items):
                skipped += 1
                continue
            if not parsed_keys or not any(episode_filter.matches(parsed) for parsed in parsed_keys):
                skipped += 1
                continue
            if all(parsed.normalized in emby_keys for parsed in parsed_keys):
                skipped += 1
                continue
            if any(item.status == "enqueued" for item in persisted_items):
                skipped += 1
                continue
            if emby_lookup_failed and confirmed_item_id is None:
                skipped += 1
                continue
            selected = select_best_resource(candidates)
            if selected is None:
                skipped += 1
                continue
            selected_item = stored_items.get(selected.slug)
            if selected_item is None:
                skipped += 1
                continue
            if confirmed_item_id is not None:
                if selected_item.id != int(confirmed_item_id):
                    selected = next((item for item in candidates if stored_items.get(item.slug, None) and stored_items[item.slug].id == int(confirmed_item_id)), None)
                    selected_item = stored_items.get(selected.slug) if selected is not None else None
                if selected is None or selected_item is None:
                    continue
            elif selected_item.status == "pending_confirmation":
                pending += 1
                continue

            saved_item = selected_item if selected_item.status == "unlocked" and selected_item.unlocked_url else None
            if saved_item is None:
                requires_confirmation = not selected.is_unlocked and (
                    selected.unlock_points is None or selected.unlock_points > self.auto_unlock_max_points
                )
                if (
                    requires_confirmation
                    and confirmed_item_id != selected_item.id
                    and selected_item.status != "unlocking"
                ):
                    self.store.mark_item_pending(selected_item.id, "积分超过自动解锁阈值或费用未知")
                    pending += 1
                    continue

                claimed_item = self.store.claim_item_unlocking(
                    selected_item.id,
                    stale_after_seconds=_UNLOCK_STALE_AFTER_SECONDS,
                )
                if claimed_item is None:
                    current_item = self.store.get_item(selected_item.id)
                    if current_item is not None and current_item.status == "pending_confirmation":
                        pending += 1
                    else:
                        skipped += 1
                    continue
                try:
                    result = self._unlock_one(selected)
                    if not result.success or not result.full_url:
                        if (
                            not result.success
                            and result.points_spent is None
                            and result.error_code in _DEFINITIVE_NO_CHARGE_ERRORS
                        ):
                            self.store.mark_item_failed(
                                selected_item.id,
                                result.message or result.error_code,
                            )
                            failed += 1
                        else:
                            self.store.mark_item_unlock_unknown(selected_item.id)
                            pending += 1
                        continue
                    if not _is_115_share_url(result.full_url):
                        self.store.mark_item_unlock_unknown(selected_item.id)
                        pending += 1
                        continue
                    if result.points_spent is not None:
                        unlock_points_spent = result.points_spent
                        unlock_points_source = "actual"
                    elif result.already_owned:
                        unlock_points_spent = 0
                        unlock_points_source = "actual"
                    else:
                        unlock_points_spent = selected.unlock_points
                        unlock_points_source = "estimated" if unlock_points_spent is not None else "unknown"
                    saved_item = self.store.mark_item_unlocked(
                        selected_item.id,
                        result.full_url,
                        unlock_points_spent,
                        unlock_points_source,
                        time.time(),
                    )
                except Exception as exc:
                    # The unlock failure is surfaced through the pending
                    # counter, but swallow it silently and it becomes
                    # un-debuggable in production logs.
                    LOG.warning(
                        "hdhive unlock failed for item %s (sub %s): %s",
                        selected_item.id,
                        getattr(subscription, "id", "?"),
                        exc,
                        exc_info=True,
                    )
                    self.store.mark_item_unlock_unknown(selected_item.id)
                    pending += 1
                    continue

            try:
                self._enqueue_saved_item(subscription, saved_item)
                enqueued += 1
            except Exception as exc:
                self.store.mark_item_intake_failed(saved_item.id, str(exc))
                failed += 1

        tmdb_details: dict[str, Any] = {}
        tmdb_status = ""
        tmdb_lookup_failed = False
        if self._dependency_enabled(self.tmdb_resolver):
            try:
                details = self.tmdb_resolver.lookup(subscription.tmdb_id, "tv", subscription.title)
                if isinstance(details, dict):
                    tmdb_details = details
                    tmdb_status = str(details.get("status") or "")
            except Exception:
                tmdb_lookup_failed = True
                LOG.warning("HDHive TMDB lookup unavailable subscription_id=%s", subscription.id, exc_info=True)

        expected = self._expected_episode_keys(tmdb_details, episode_filter)
        terminal: set[Any] = set()
        blocked: set[Any] = set()
        current_item_ids = set(resource_item_ids.values())
        unparsed_count = 0
        for item in self.store.list_items(subscription.id):
            item_keys = self._stored_episode_keys(item)
            if not item_keys:
                if item.status == "unparsed":
                    unparsed_count += 1
                continue
            if item.status in {"enqueued", "emby_exists"}:
                terminal.update(item_keys)
            elif item.status == "filtered" and item.id in current_item_ids:
                # A filter only proves an episode was intentionally skipped while
                # the corresponding HDHive resource was present in this check.
                terminal.update(item_keys)
            if item.status in {"pending_confirmation", "failed", "unlocking", "unlocked", "unparsed"}:
                blocked.update(item_keys)
        blocked.update(expected - terminal)
        completion = completion_state(tmdb_status, expected, terminal, blocked)
        if unparsed_count or emby_skip_unavailable or tmdb_lookup_failed:
            completion = "active"

        subscription_status = subscription.status
        if completion == "completed":
            subscription_status = self.store.set_status(subscription.id, "completed").status
        elif subscription.status == "active":
            subscription_status = "active"
        summary: dict[str, Any] = {
            "discovered": discovered,
            "enqueued": enqueued,
            "pending_confirmation": pending,
            "failed": failed,
            "emby_exists": len(emby_groups),
            "filtered": len(filtered_groups),
            "unparsed": len(unparsed_groups),
            "blocked": len(blocked) + unparsed_count,
            "expected": len(expected),
            "tmdb_status": tmdb_status,
            "emby_skip_unavailable": emby_skip_unavailable,
        }
        self.store.record_check(subscription.id, "", summary=summary)
        return SubscriptionCheckResult(
            discovered=discovered,
            enqueued=enqueued,
            pending_confirmation=pending,
            failed=failed,
            skipped=skipped,
            summary=summary,
            subscription_status=subscription_status,
        )

    def _enqueue_saved_item(
        self,
        subscription: HdhiveSubscription,
        item: HdhiveSubscriptionItem,
    ) -> HdhiveSubscriptionItem:
        saved_item = self.store.mark_item_enqueue_started(item.id)
        intake_result = self.enqueue_links([saved_item.unlocked_url], subscription.chat_id)
        saved_item = self.store.mark_item_enqueued(
            saved_item.id,
            _task_id_from_intake_result(intake_result),
        )
        if self.on_item_enqueued is not None:
            try:
                self.on_item_enqueued(subscription, saved_item)
            except Exception:
                LOG.exception("HDHive unlock notification failed item_id=%s", saved_item.id)
        return saved_item

    @staticmethod
    def _dependency_enabled(dependency: Any | None) -> bool:
        return dependency is not None and bool(getattr(dependency, "enabled", True))

    @staticmethod
    def _stored_episode_key(item: HdhiveSubscriptionItem):
        return next(iter(HdhiveSubscriptionService._stored_episode_keys(item)), None)

    @staticmethod
    def _stored_episode_keys(item: HdhiveSubscriptionItem) -> tuple[EpisodeKey, ...]:
        value = item.normalized_episode_key or item.episode_key
        return _parse_episode_keys(value)

    @staticmethod
    def _expected_episode_keys(details: dict[str, Any], episode_filter: Any) -> set[Any]:
        if not details or details.get("ok") is False:
            return set()
        expected: set[Any] = set()
        seasons = details.get("seasons")
        if not isinstance(seasons, list):
            return expected
        for season in seasons:
            if not isinstance(season, dict):
                continue
            season_number = season.get("season_number")
            episode_count = season.get("episode_count")
            if isinstance(season_number, bool) or isinstance(episode_count, bool):
                continue
            try:
                season_number = int(season_number)
                episode_count = int(episode_count)
            except (TypeError, ValueError):
                continue
            if season_number < 0 or episode_count <= 0:
                continue
            for number in range(1, episode_count + 1):
                parsed = parse_episode_key(f"S{season_number}E{number}")
                if parsed is not None and episode_filter.matches(parsed):
                    expected.add(parsed)
        return expected

    def _unlock_one(self, resource: HdhiveResource) -> HdhiveUnlockItem:
        results = self.proxy.unlock([resource.slug])
        for item in results:
            if item.slug == resource.slug:
                return item
        return results[0] if results else HdhiveUnlockItem(resource.slug, False, "", "没有返回解锁结果", "EMPTY_RESULT", False)


class HdhiveSubscriptionScheduler:
    def __init__(
        self,
        service: HdhiveSubscriptionService,
        store: HdhiveSubscriptionStore,
        *,
        enabled: bool = True,
        run_time: str = "01:30",
        timezone_name: str = "Asia/Shanghai",
        interval_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
        on_run: Callable[[HdhiveScheduledRun], None] | None = None,
    ):
        self.service = service
        self.store = store
        self.enabled = bool(enabled)
        self._run_time = self._parse_time(run_time)
        self._timezone = self._parse_timezone(timezone_name)
        self.interval_seconds = max(5, int(interval_seconds))
        self.clock = clock or (lambda: datetime.now(self._timezone))
        self.on_run = on_run
        self._status = "idle"
        self._last_run: HdhiveScheduledRun | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

        stored_time = self.store.get_setting("time")
        stored_timezone = self.store.get_setting("timezone")
        stored_enabled = self.store.get_setting("enabled")
        if stored_time:
            self._run_time = self._parse_time(stored_time)
        if stored_timezone:
            self._timezone = self._parse_timezone(stored_timezone)
        if stored_enabled is not None:
            self.enabled = stored_enabled == "1"

    @staticmethod
    def _parse_time(value: str) -> datetime_time:
        if re.fullmatch(r"\d{2}:\d{2}", str(value or "")) is None:
            raise ValueError("HDHIVE_SUBSCRIPTION_TIME must use HH:MM format")
        hour, minute = (int(part) for part in str(value).split(":", 1))
        try:
            return datetime_time(hour, minute)
        except ValueError as exc:
            raise ValueError("HDHIVE_SUBSCRIPTION_TIME must be a valid time") from exc

    @staticmethod
    def _parse_timezone(value: str) -> ZoneInfo:
        try:
            return ZoneInfo(str(value))
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("HDHIVE_SUBSCRIPTION_TIMEZONE must be a valid IANA timezone") from exc

    def _local_now(self, now: datetime | None = None) -> datetime:
        value = now if now is not None else self.clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=self._timezone)
        return value.astimezone(self._timezone)

    def _scheduled_on(self, reference: datetime, run_date) -> datetime:
        candidate = reference.replace(
            year=run_date.year,
            month=run_date.month,
            day=run_date.day,
            hour=self._run_time.hour,
            minute=self._run_time.minute,
            second=0,
            microsecond=0,
        )
        return candidate.astimezone(timezone.utc).astimezone(self._timezone)

    def next_run_at(self, now: datetime | None = None) -> datetime:
        local_now = self._local_now(now)
        scheduled = self._scheduled_on(local_now, local_now.date())
        if local_now >= scheduled:
            scheduled = self._scheduled_on(local_now, local_now.date() + timedelta(days=1))
        return scheduled

    def run_if_due(self, now: datetime | None = None) -> HdhiveScheduledRun | None:
        if not self.enabled:
            return None
        local_now = self._local_now(now)
        if local_now < self._scheduled_on(local_now, local_now.date()):
            return None
        run_date = local_now.date().isoformat()
        run_id = f"hdhive-{run_date}-{time.monotonic_ns():x}"
        if not self.store.claim_daily_run(run_date, run_id, local_now.timestamp(), serialize_active=True):
            return None
        return self._run_owned(run_id, local_now)

    def run_now(self) -> HdhiveScheduledRun | None:
        local_now = self._local_now()
        run_id = f"hdhive-manual-{time.monotonic_ns():x}"
        run_date = f"manual-{local_now.date().isoformat()}-{time.monotonic_ns():x}"
        if not self.store.claim_daily_run(run_date, run_id, local_now.timestamp(), serialize_active=True):
            return None
        return self._run_owned(run_id, local_now)

    def _run_owned(self, run_id: str, local_now: datetime) -> HdhiveScheduledRun:
        started_at = local_now.isoformat()
        summary: dict[str, Any] = {
            "subscriptions": 0,
            "discovered": 0,
            "enqueued": 0,
            "pending_confirmation": 0,
            "failed": 0,
            "errors": [],
        }
        with self._lock:
            self._status = "running"
        try:
            subscriptions = self.store.list_subscriptions()
            summary["subscriptions"] = len(subscriptions)
            for subscription in subscriptions:
                if subscription.status != "active":
                    continue
                try:
                    result = self.service.check(subscription.id)
                    for field in ("discovered", "enqueued", "pending_confirmation", "failed"):
                        summary[field] += int(getattr(result, field, 0))
                except Exception as exc:
                    summary["failed"] += 1
                    summary["errors"].append(f"#{subscription.id}: {type(exc).__name__}: {exc}")
                    LOG.exception("HDHive subscription check failed subscription_id=%s", subscription.id)
            status = "failed" if summary["failed"] else "succeeded"
        except Exception as exc:
            status = "failed"
            summary["failed"] += 1
            summary["errors"].append(f"scheduler: {type(exc).__name__}: {exc}")
            LOG.exception("HDHive subscription run failed")
        finished_at = self._local_now().isoformat()
        self.store.finish_run(run_id, status, summary, self._local_now().timestamp())
        result = HdhiveScheduledRun(run_id, status, started_at, finished_at, summary)
        with self._lock:
            self._status = status
            self._last_run = result
        if callable(self.on_run):
            self.on_run(result)
        return result

    def update_settings(self, *, enabled: bool, run_time: str, timezone_name: str) -> dict[str, Any]:
        parsed_time = self._parse_time(run_time)
        timezone = self._parse_timezone(timezone_name)
        self.enabled = bool(enabled)
        self._run_time = parsed_time
        self._timezone = timezone
        self.store.set_setting("enabled", "1" if self.enabled else "0")
        self.store.set_setting("time", parsed_time.strftime("%H:%M"))
        self.store.set_setting("timezone", str(timezone_name))
        return self.settings()

    def settings(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "time": self._run_time.strftime("%H:%M"),
            "timezone": str(self._timezone),
        }

    def status_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        latest = self._last_run or self.store.latest_run()
        summary: dict[str, Any] = {}
        if latest is not None:
            in_memory_summary = getattr(latest, "summary", None)
            if isinstance(in_memory_summary, dict):
                summary = dict(in_memory_summary)
            else:
                try:
                    parsed = json.loads(getattr(latest, "summary_json", "{}"))
                    summary = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError):
                    summary = {}
        return {
            **self.settings(),
            "status": self._status,
            "last_run_id": latest.run_id if latest is not None else "",
            "last_summary": summary,
            "next_run_at": self.next_run_at(now).isoformat(),
        }

    def start(self) -> threading.Thread:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop_event.clear()

            def loop() -> None:
                while not self._stop_event.wait(self.interval_seconds):
                    try:
                        self.run_if_due()
                    except Exception:
                        LOG.exception("HDHive subscription scheduler loop failed")

            self._thread = threading.Thread(target=loop, name="hdhive-subscriptions", daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, join_timeout: float = 5) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        self._thread = None
