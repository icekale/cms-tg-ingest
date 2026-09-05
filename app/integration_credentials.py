"""Emby / TMDB 集成凭据的解析与脱敏展示。

仿照 self_share_settings.py 的 override 模式：Web 写入的值存 TaskStore
runtime_state（重启保留），优先于 .env 环境配置。客户端实例（EmbyClient /
TmdbApiResolver）在保存时热更新属性，无需重启即生效；未配置时 source 为
unset。

敏感值永不以明文返回给前端，只有 masked 形态；来源标识帮助用户判断当前
生效的是 Web 写入值还是环境配置。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _mask_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


@dataclass(frozen=True)
class EmbyCredentials:
    base_url: str
    api_key: str
    source: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def masked_payload(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url or "",
            "api_key": _mask_secret(self.api_key),
            "source": self.source,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class TmdbCredentials:
    api_key: str
    bearer_token: str
    source: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key or self.bearer_token)

    def masked_payload(self) -> dict[str, Any]:
        return {
            "api_key": _mask_secret(self.api_key),
            "bearer_token": _mask_secret(self.bearer_token),
            "source": self.source,
            "configured": self.configured,
        }


def _web_overrides(store: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, getter in (
        ("emby_base_url", getattr(store, "get_emby_base_url_override", None)),
        ("emby_api_key", getattr(store, "get_emby_api_key_override", None)),
        ("tmdb_api_key", getattr(store, "get_tmdb_api_key_override", None)),
        ("tmdb_bearer_token", getattr(store, "get_tmdb_bearer_token_override", None)),
    ):
        if callable(getter):
            value = str(getter() or "").strip()
            if value:
                values[key] = value
    return values


def resolve_emby_credentials(store: Any, client: Any) -> EmbyCredentials:
    """当前生效的 Emby 凭据：Web override 优先，否则读客户端实例（来自 .env）。"""
    overrides = _web_overrides(store)
    base_url = overrides.get("emby_base_url") or str(getattr(client, "base_url", "") or "")
    api_key = overrides.get("emby_api_key") or str(getattr(client, "api_key", "") or "")
    if overrides:
        source = "web"
    elif bool(base_url and api_key):
        source = "env"
    else:
        source = "unset"
    return EmbyCredentials(base_url, api_key, source)


def resolve_tmdb_credentials(store: Any, resolver: Any) -> TmdbCredentials:
    """当前生效的 TMDB 凭据：Web override 优先，否则读 resolver 实例（来自 .env）。

    resolver 可能是 TmdbApiResolver（有 api_key/bearer_token）或纯网页回退
    解析器（无 key 属性）——后者视为未配置。
    """
    overrides = _web_overrides(store)
    api_key = overrides.get("tmdb_api_key") or str(getattr(resolver, "api_key", "") or "")
    bearer = overrides.get("tmdb_bearer_token") or str(getattr(resolver, "bearer_token", "") or "")
    if overrides:
        source = "web"
    elif bool(api_key or bearer):
        source = "env"
    else:
        source = "unset"
    return TmdbCredentials(api_key, bearer, source)


@dataclass(frozen=True)
class AiCredentials:
    enabled: bool
    base_url: str
    model: str
    api_key: str
    source: str

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url)

    def masked_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url or "",
            "model": self.model or "",
            "api_key": _mask_secret(self.api_key),
            "source": self.source,
            "configured": self.configured,
        }


def resolve_ai_credentials(store: Any, classifier: Any) -> AiCredentials:
    """当前生效的 AI 凭据：Web override 优先，否则读 classifier 实例（.env）。

    classifier 可能缺失（旧流程未接线）——此时仅反映 Web override。
    """
    overrides = _web_overrides_ai(store)
    enabled_override = None
    getter = getattr(store, "get_openai_enabled_override", None)
    if callable(getter):
        enabled_override = getter()
    base_url = overrides.get("openai_base_url") or str(getattr(classifier, "base_url", "") or "")
    model = overrides.get("openai_model") or str(getattr(classifier, "model", "") or "")
    api_key = overrides.get("openai_api_key") or str(getattr(classifier, "api_key", "") or "")
    if overrides or enabled_override is not None:
        source = "web"
    elif bool(api_key):
        source = "env"
    else:
        source = "unset"
    if enabled_override is not None:
        enabled = enabled_override
    else:
        enabled = bool(getattr(classifier, "enabled_flag", getattr(classifier, "enabled", False)))
    return AiCredentials(enabled, base_url, model, api_key, source)


def _web_overrides_ai(store: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, getter in (
        ("openai_base_url", getattr(store, "get_openai_base_url_override", None)),
        ("openai_model", getattr(store, "get_openai_model_override", None)),
        ("openai_api_key", getattr(store, "get_openai_api_key_override", None)),
    ):
        if callable(getter):
            value = str(getter() or "").strip()
            if value:
                values[key] = value
    return values
