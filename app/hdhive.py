from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.clients.hdhive import HdhiveAccount, HdhiveProxyClient, HdhiveResource, HdhiveUnlockItem


class HdhiveSelectionError(RuntimeError):
    pass


@dataclass
class HdhiveSession:
    session_id: str
    chat_id: str
    query: str
    candidates: list[dict[str, str]] = field(default_factory=list)
    media_type: str = ""
    tmdb_id: str = ""
    resources: list[HdhiveResource] = field(default_factory=list)
    pan_type: str = "115"
    selected_indexes: list[int] = field(default_factory=list)
    created_at: float = 0.0


@dataclass(frozen=True)
class UnlockPreview:
    selected_slugs: tuple[str, ...]
    maximum_points: int
    requires_confirmation: bool
    account: HdhiveAccount


class HdhiveSessionStore:
    def __init__(self, ttl_seconds: int = 900, clock: Callable[[], float] | None = None):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.clock = clock or time.time
        self._sessions: dict[str, HdhiveSession] = {}
        self._chat_sessions: dict[str, str] = {}
        self._lock = threading.RLock()

    def _purge(self) -> None:
        now = self.clock()
        expired = [sid for sid, session in self._sessions.items() if now - session.created_at > self.ttl_seconds]
        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if session is not None and self._chat_sessions.get(session.chat_id) == session_id:
                self._chat_sessions.pop(session.chat_id, None)

    def begin(self, chat_id: str, query: str) -> str:
        with self._lock:
            self._purge()
            chat_id = str(chat_id)
            previous = self._chat_sessions.pop(chat_id, None)
            if previous:
                self._sessions.pop(previous, None)
            session_id = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
            self._sessions[session_id] = HdhiveSession(
                session_id=session_id,
                chat_id=chat_id,
                query=str(query).strip(),
                created_at=self.clock(),
            )
            self._chat_sessions[chat_id] = session_id
            return session_id

    def get(self, session_id: str) -> HdhiveSession | None:
        with self._lock:
            self._purge()
            return self._sessions.get(str(session_id))

    def remove(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(str(session_id), None)
            if session is not None and self._chat_sessions.get(session.chat_id) == session.session_id:
                self._chat_sessions.pop(session.chat_id, None)


class HdhiveWorkflow:
    def __init__(
        self,
        cms: Any,
        proxy: HdhiveProxyClient,
        sessions: HdhiveSessionStore,
        auto_unlock_max_points: int = 20,
    ):
        self.cms = cms
        self.proxy = proxy
        self.sessions = sessions
        self.auto_unlock_max_points = max(0, int(auto_unlock_max_points))

    @staticmethod
    def _items(response: Any) -> list[dict[str, Any]]:
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, dict):
            data = data.get("results") or data.get("items") or data.get("list") or []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def search_candidates(self, query: str) -> list[dict[str, str]]:
        query = str(query or "").strip()
        if not query:
            raise HdhiveSelectionError("请输入片名或 TMDB ID")
        candidates: list[dict[str, str]] = []
        errors: list[Exception] = []
        for media_type, method in (("movie", self.cms.search_movie), ("tv", self.cms.search_tv)):
            try:
                for item in self._items(method(query, page=1, page_size=8)):
                    tmdb_id = str(item.get("id") or item.get("tmdb_id") or "").strip()
                    title = str(item.get("title") or item.get("name") or "").strip()
                    if not tmdb_id or not title:
                        continue
                    raw_date = str(item.get("release_date") or item.get("first_air_date") or "")
                    candidates.append(
                        {
                            "media_type": media_type,
                            "tmdb_id": tmdb_id,
                            "title": title,
                            "year": raw_date[:4],
                        }
                    )
            except Exception as exc:
                errors.append(exc)
        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in candidates:
            key = (item["media_type"], item["tmdb_id"])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        if not unique and errors:
            raise HdhiveSelectionError("TMDB 搜索暂时失败") from errors[-1]
        if not unique:
            raise HdhiveSelectionError("没有匹配到电影或剧集")
        return unique[:12]

    def _session(self, session_id: str) -> HdhiveSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise HdhiveSelectionError("HDHive 操作会话已过期，请重新搜索")
        return session

    def set_candidates(self, session_id: str, candidates: list[dict[str, str]]) -> None:
        session = self._session(session_id)
        session.candidates = list(candidates)

    def load_resources(self, session_id: str, media_type: str, tmdb_id: str) -> list[HdhiveResource]:
        session = self._session(session_id)
        resources = self.proxy.resources(media_type, tmdb_id)
        session.media_type = media_type
        session.tmdb_id = str(tmdb_id)
        session.resources = list(resources)
        session.pan_type = "115" if any(item.pan_type.lower() == "115" for item in resources) else "all"
        session.selected_indexes = []
        return self.visible_resources(session_id)

    def available_pan_types(self, session_id: str) -> list[str]:
        session = self._session(session_id)
        result: list[str] = []
        for item in session.resources:
            if item.pan_type not in result:
                result.append(item.pan_type)
        return result

    def set_filter(self, session_id: str, pan_type: str) -> list[HdhiveResource]:
        session = self._session(session_id)
        pan_type = str(pan_type or "all").strip() or "all"
        if pan_type != "all" and pan_type not in self.available_pan_types(session_id):
            raise HdhiveSelectionError("该网盘类型没有资源")
        session.pan_type = pan_type
        session.selected_indexes = [index for index in session.selected_indexes if index in self.selectable_resource_indexes(session_id)]
        return self.visible_resources(session_id)

    def visible_resource_indexes(self, session_id: str) -> list[int]:
        session = self._session(session_id)
        if session.pan_type == "all":
            return list(range(len(session.resources)))
        return [index for index, item in enumerate(session.resources) if item.pan_type == session.pan_type]

    def selectable_resource_indexes(self, session_id: str) -> list[int]:
        session = self._session(session_id)
        return [
            index
            for index in self.visible_resource_indexes(session_id)
            if session.resources[index].validate_status.lower() != "invalid"
        ]

    def visible_resources(self, session_id: str) -> list[HdhiveResource]:
        session = self._session(session_id)
        return [session.resources[index] for index in self.visible_resource_indexes(session_id)]

    def toggle_selection(self, session_id: str, index: int) -> list[int]:
        session = self._session(session_id)
        if index not in self.selectable_resource_indexes(session_id):
            raise HdhiveSelectionError("该资源不可选择")
        if index in session.selected_indexes:
            session.selected_indexes.remove(index)
        else:
            session.selected_indexes.append(index)
        return list(session.selected_indexes)

    @staticmethod
    def batch_limit(account: HdhiveAccount) -> int:
        level = account.level.lower()
        if account.is_forever_vip or level in {"forever_vip", "long_term_vip", "lifetime_vip"}:
            return 10
        if level in {"vip", "premium"}:
            return 5
        return 1

    def _selected_resources(self, session_id: str) -> list[HdhiveResource]:
        session = self._session(session_id)
        if not session.selected_indexes:
            raise HdhiveSelectionError("请先选择至少一个资源")
        limit = self.batch_limit(self.proxy.account())
        if len(session.selected_indexes) > limit:
            raise HdhiveSelectionError(f"当前账号单次最多解锁 {limit} 个资源")
        return [session.resources[index] for index in session.selected_indexes]

    def unlock_preview(self, session_id: str) -> UnlockPreview:
        selected = self._selected_resources(session_id)
        account = self.proxy.account()
        if account.is_blocked:
            raise HdhiveSelectionError("HDHive 账号已被封禁")
        maximum_points = sum(item.unlock_points or 0 for item in selected if not item.is_unlocked)
        requires_confirmation = any(
            not item.is_unlocked
            and (item.unlock_points is None or item.unlock_points > self.auto_unlock_max_points)
            for item in selected
        )
        return UnlockPreview(
            selected_slugs=tuple(item.slug for item in selected),
            maximum_points=maximum_points,
            requires_confirmation=requires_confirmation,
            account=account,
        )

    def unlock(self, session_id: str, confirmed: bool = False) -> list[HdhiveUnlockItem]:
        preview = self.unlock_preview(session_id)
        if preview.requires_confirmation and not confirmed:
            raise HdhiveSelectionError("本次解锁可能消耗较多积分，请确认后继续")
        results = self.proxy.unlock(list(preview.selected_slugs))
        session = self._session(session_id)
        session.selected_indexes = []
        return results
