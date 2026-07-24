from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.clients.hdhive import HdhiveResource, HdhiveUnlockItem
from app.hdhive_subscription_store import HdhiveSubscription, HdhiveSubscriptionItem, HdhiveSubscriptionStore


LOG = logging.getLogger("cms-tg-ingest")


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
_RESOLUTION_RE = re.compile(r"(8k|4k|2160p|1440p|1080p|720p|576p|480p)", re.IGNORECASE)


@dataclass(frozen=True)
class SubscriptionCheckResult:
    discovered: int = 0
    enqueued: int = 0
    pending_confirmation: int = 0
    failed: int = 0
    skipped: int = 0
    error: str = ""


@dataclass(frozen=True)
class HdhiveScheduledRun:
    run_id: str
    status: str
    started_at: str
    finished_at: str
    summary: dict[str, Any]


def episode_key(resource: HdhiveResource) -> str:
    if resource.episode_key:
        return resource.episode_key.strip().lower()
    if resource.season_number is not None and resource.episode_number is not None:
        return f"s{resource.season_number:02d}e{resource.episode_number:02d}"
    match = _EPISODE_RE.search(resource.title or "")
    if match:
        return f"s{int(match.group(1)):02d}e{int(match.group(2)):02d}"
    return resource.slug


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
    ):
        self.proxy = proxy
        self.store = store
        self.enqueue_links = enqueue_links
        self.auto_unlock_max_points = max(0, int(auto_unlock_max_points))

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

        grouped: dict[str, list[HdhiveResource]] = {}
        discovered = 0
        for resource in resources:
            if str(resource.pan_type or "").strip().lower() != "115":
                continue
            key = episode_key(resource)
            grouped.setdefault(key, []).append(resource)
            self.store.upsert_item(
                subscription.id,
                key,
                resource.slug,
                resource.validate_status,
                resolution_score(resource),
                resource.unlock_points,
                resource.title,
            )
            discovered += 1

        enqueued = pending = failed = skipped = 0
        for key, candidates in grouped.items():
            stored_items = {item.resource_slug: item for item in self.store.list_items(subscription.id) if item.episode_key == key}
            if any(item.status == "enqueued" for item in stored_items.values()):
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
            elif selected_item.status in {"pending_confirmation", "unlocking"}:
                pending += 1 if selected_item.status == "pending_confirmation" else 0
                skipped += 1 if selected_item.status == "unlocking" else 0
                continue

            requires_confirmation = not selected.is_unlocked and (
                selected.unlock_points is None or selected.unlock_points > self.auto_unlock_max_points
            )
            if requires_confirmation and confirmed_item_id != selected_item.id:
                self.store.mark_item_pending(selected_item.id, "积分超过自动解锁阈值或费用未知")
                pending += 1
                continue

            self.store.mark_item_unlocking(selected_item.id)
            try:
                result = self._unlock_one(selected)
                if not result.success or not result.full_url:
                    self.store.mark_item_failed(selected_item.id, result.message or result.error_code or "HDHive 解锁失败")
                    failed += 1
                    continue
                if not _is_115_share_url(result.full_url):
                    self.store.mark_item_failed(selected_item.id, "解锁结果不是 115 分享链接")
                    failed += 1
                    continue
                intake_result = self.enqueue_links([result.full_url], subscription.chat_id)
                if result.points_spent is not None:
                    unlock_points_spent = result.points_spent
                    unlock_points_source = "actual"
                elif result.already_owned:
                    unlock_points_spent = 0
                    unlock_points_source = "actual"
                else:
                    unlock_points_spent = selected.unlock_points
                    unlock_points_source = "estimated" if unlock_points_spent is not None else "unknown"
                self.store.mark_item_enqueued(
                    selected_item.id,
                    _task_id_from_intake_result(intake_result),
                    unlock_points_spent=unlock_points_spent,
                    unlock_points_source=unlock_points_source,
                    unlocked_at=time.time(),
                )
                enqueued += 1
            except Exception as exc:
                self.store.mark_item_failed(selected_item.id, str(exc))
                failed += 1

        self.store.record_check(subscription.id, "")
        return SubscriptionCheckResult(
            discovered=discovered,
            enqueued=enqueued,
            pending_confirmation=pending,
            failed=failed,
            skipped=skipped,
        )

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
        if not self.store.claim_daily_run(run_date, run_id, local_now.timestamp()):
            return None
        return self._run_owned(run_id, local_now)

    def run_now(self) -> HdhiveScheduledRun | None:
        local_now = self._local_now()
        run_id = f"hdhive-manual-{time.monotonic_ns():x}"
        run_date = f"manual-{local_now.date().isoformat()}-{time.monotonic_ns():x}"
        if not self.store.claim_daily_run(run_date, run_id, local_now.timestamp()):
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
