from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("cms-tg-ingest")


def parse_bool_env(value: str | None, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled", "enable"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_quality_auto_time(value: str) -> str:
    if re.fullmatch(r"\d{2}:\d{2}", value or "") is None:
        raise ValueError("QUALITY_AUTO_TIME must use HH:MM format")
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        datetime_time(hour, minute)
    except ValueError as exc:
        raise ValueError("QUALITY_AUTO_TIME must be a valid time") from exc
    return value


def parse_quality_auto_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("QUALITY_AUTO_TIMEZONE must be a valid IANA timezone") from exc
    return value


def positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw_value!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass
class Config:
    tg_bot_token: str
    tg_allowed_chat_id: str
    cms_base_url: str
    cms_username: str
    cms_password: str
    poll_timeout: int = 30
    http_timeout: int = 60
    db_path: str = "/data/submissions.db"
    status_poll_seconds: int = 300
    status_poll_interval: int = 20
    emby_base_url: str = ""
    emby_api_key: str = ""
    emby_user_id: str = ""
    strm_source_roots: str = "/mnt/user/Unraid/strm/转存"
    strm_library_map: str = ""
    move_conflict_policy: str = "skip"
    strm_stable_seconds: int = 30
    openai_classify_enabled: bool = False
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    openai_high_confidence: float = 0.75
    openai_suggest_confidence: float = 0.45
    tmdb_api_key: str = ""
    tmdb_bearer_token: str = ""
    hdhive_enabled: bool = False
    hdhive_proxy_base_url: str = "https://authx.771885.xyz"
    hdhive_token_config_path: str = "/config/hdhive-openapi.json"
    hdhive_search_session_ttl_seconds: int = 900
    hdhive_auto_unlock_max_points: int = 20
    hdhive_subscription_auto_enabled: bool = True
    hdhive_subscription_time: str = "01:30"
    hdhive_subscription_timezone: str = "Asia/Shanghai"
    workflow_mode: str = "direct"
    p115_cookie_path: str = "/config/115-cookies.txt"
    p115_min_request_interval_seconds: float = 2.0
    p115_risk_cooldown_seconds: int = 900
    self_share_receive_cid: str = ""
    self_share_strm_root: str = "/mnt/user/Unraid/strm/share"
    self_share_cms_local_path: str = "/media/share"
    self_share_cms_cid: str = "0"
    self_share_cleanup_after_emby: bool = False
    self_share_source_cleanup_parent_ids: str = ""
    self_share_auto_organize_retry_seconds: int = 15
    self_share_cloud_poll_seconds: int = 30
    self_share_cloud_timeout_seconds: int = 86400
    self_share_invalid_cleanup_enabled: bool = False
    self_share_invalid_check_interval_seconds: int = 21600
    self_share_invalid_check_limit: int = 3
    status_repair_enabled: bool = True
    status_repair_interval_seconds: int = 300
    status_repair_limit: int = 50
    cms_parent_cid_category_map: str = ""
    self_share_organized_scan_parent_ids: str = ""
    cms_state_db_path: str = "/cms/cms-online.db"
    task_db_path: str = "/data/tasks.db"
    task_engine_enabled: bool = False
    web_enabled: bool = False
    web_host: str = "0.0.0.0"
    web_port: int = 8787
    web_token: str = ""
    task_worker_interval_seconds: int = 5
    task_max_retries: int = 3
    quality_auto_enabled: bool = False
    quality_auto_time: str = "02:50"
    quality_auto_timezone: str = "Asia/Shanghai"
    quality_auto_max_tasks: int = 50
    quality_auto_115_check_limit: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        required = [
            "TG_BOT_TOKEN",
            "TG_ALLOWED_CHAT_ID",
            "CMS_BASE_URL",
            "CMS_USERNAME",
            "CMS_PASSWORD",
        ]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))
        return cls(
            tg_bot_token=os.environ["TG_BOT_TOKEN"],
            tg_allowed_chat_id=os.environ["TG_ALLOWED_CHAT_ID"],
            cms_base_url=os.environ["CMS_BASE_URL"].rstrip("/"),
            cms_username=os.environ["CMS_USERNAME"],
            cms_password=os.environ["CMS_PASSWORD"],
            poll_timeout=int(os.environ.get("TG_POLL_TIMEOUT", "30")),
            http_timeout=int(os.environ.get("HTTP_TIMEOUT", "60")),
            db_path=os.environ.get("DB_PATH", "/data/submissions.db"),
            status_poll_seconds=int(os.environ.get("STATUS_POLL_SECONDS", "300")),
            status_poll_interval=int(os.environ.get("STATUS_POLL_INTERVAL", "20")),
            emby_base_url=(os.environ.get("EMBY_BASE_URL") or os.environ.get("EMBY_HOST_PORT") or "").rstrip("/"),
            emby_api_key=os.environ.get("EMBY_API_KEY", ""),
            emby_user_id=os.environ.get("EMBY_USER_ID", ""),
            strm_source_roots=os.environ.get("STRM_SOURCE_ROOTS", "/mnt/user/Unraid/strm/转存"),
            strm_library_map=os.environ.get("STRM_LIBRARY_MAP", ""),
            move_conflict_policy=os.environ.get("MOVE_CONFLICT_POLICY", "skip"),
            strm_stable_seconds=int(os.environ.get("STRM_STABLE_SECONDS", "30")),
            openai_classify_enabled=parse_bool_env(os.environ.get("OPENAI_CLASSIFY_ENABLED"), False),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            openai_high_confidence=env_float("OPENAI_HIGH_CONFIDENCE", 0.75),
            openai_suggest_confidence=env_float("OPENAI_SUGGEST_CONFIDENCE", 0.45),
            tmdb_api_key=os.environ.get("TMDB_API_KEY", ""),
            tmdb_bearer_token=os.environ.get("TMDB_BEARER_TOKEN", ""),
            hdhive_enabled=parse_bool_env(os.environ.get("HDHIVE_ENABLED"), False),
            hdhive_proxy_base_url=os.environ.get("HDHIVE_PROXY_BASE_URL", "https://authx.771885.xyz").rstrip("/"),
            hdhive_token_config_path=os.environ.get("HDHIVE_TOKEN_CONFIG_PATH", "/config/hdhive-openapi.json"),
            hdhive_search_session_ttl_seconds=max(60, int(os.environ.get("HDHIVE_SEARCH_SESSION_TTL_SECONDS", "900"))),
            hdhive_auto_unlock_max_points=max(0, int(os.environ.get("HDHIVE_AUTO_UNLOCK_MAX_POINTS", "20"))),
            hdhive_subscription_auto_enabled=parse_bool_env(
                os.environ.get("HDHIVE_SUBSCRIPTION_AUTO_ENABLED"), True
            ),
            hdhive_subscription_time=parse_quality_auto_time(
                os.environ.get("HDHIVE_SUBSCRIPTION_TIME", "01:30")
            ),
            hdhive_subscription_timezone=parse_quality_auto_timezone(
                os.environ.get("HDHIVE_SUBSCRIPTION_TIMEZONE", "Asia/Shanghai")
            ),
            workflow_mode=os.environ.get("WORKFLOW_MODE", "direct").strip().lower() or "direct",
            p115_cookie_path=os.environ.get("P115_COOKIE_PATH", "/config/115-cookies.txt"),
            p115_min_request_interval_seconds=env_float("P115_MIN_REQUEST_INTERVAL_SECONDS", 2.0),
            p115_risk_cooldown_seconds=int(os.environ.get("P115_RISK_COOLDOWN_SECONDS", "900")),
            self_share_receive_cid=os.environ.get("SELF_SHARE_RECEIVE_CID", ""),
            self_share_strm_root=os.environ.get("SELF_SHARE_STRM_ROOT", "/mnt/user/Unraid/strm/share"),
            self_share_cms_local_path=os.environ.get("SELF_SHARE_CMS_LOCAL_PATH", "/media/share"),
            self_share_cms_cid=os.environ.get("SELF_SHARE_CMS_CID", "0"),
            self_share_cleanup_after_emby=parse_bool_env(os.environ.get("SELF_SHARE_CLEANUP_AFTER_EMBY"), False),
            self_share_source_cleanup_parent_ids=os.environ.get("SELF_SHARE_SOURCE_CLEANUP_PARENT_IDS", ""),
            self_share_auto_organize_retry_seconds=int(os.environ.get("SELF_SHARE_AUTO_ORGANIZE_RETRY_SECONDS", "15")),
            self_share_cloud_poll_seconds=max(30, int(os.environ.get("SELF_SHARE_CLOUD_POLL_SECONDS", "30"))),
            self_share_cloud_timeout_seconds=max(300, int(os.environ.get("SELF_SHARE_CLOUD_TIMEOUT_SECONDS", "86400"))),
            self_share_invalid_cleanup_enabled=parse_bool_env(os.environ.get("SELF_SHARE_INVALID_CLEANUP_ENABLED"), False),
            self_share_invalid_check_interval_seconds=max(60, int(os.environ.get("SELF_SHARE_INVALID_CHECK_INTERVAL_SECONDS", "21600"))),
            self_share_invalid_check_limit=max(1, int(os.environ.get("SELF_SHARE_INVALID_CHECK_LIMIT", "3"))),
            status_repair_enabled=parse_bool_env(os.environ.get("STATUS_REPAIR_ENABLED"), True),
            status_repair_interval_seconds=int(os.environ.get("STATUS_REPAIR_INTERVAL_SECONDS", "300")),
            status_repair_limit=int(os.environ.get("STATUS_REPAIR_LIMIT", "50")),
            cms_parent_cid_category_map=os.environ.get("CMS_PARENT_CID_CATEGORY_MAP", ""),
            self_share_organized_scan_parent_ids=os.environ.get("SELF_SHARE_ORGANIZED_SCAN_PARENT_IDS", ""),
            cms_state_db_path=os.environ.get("CMS_STATE_DB_PATH", "/cms/cms-online.db"),
            task_db_path=os.environ.get("TASK_DB_PATH", "/data/tasks.db"),
            task_engine_enabled=parse_bool_env(os.environ.get("TASK_ENGINE_ENABLED"), False),
            web_enabled=parse_bool_env(os.environ.get("WEB_ENABLED"), False),
            web_host=os.environ.get("WEB_HOST", "0.0.0.0"),
            web_port=int(os.environ.get("WEB_PORT", "8787")),
            web_token=os.environ.get("WEB_TOKEN", ""),
            task_worker_interval_seconds=int(os.environ.get("TASK_WORKER_INTERVAL_SECONDS", "5")),
            task_max_retries=int(os.environ.get("TASK_MAX_RETRIES", "3")),
            quality_auto_enabled=parse_bool_env(os.environ.get("QUALITY_AUTO_ENABLED"), False),
            quality_auto_time=parse_quality_auto_time(os.environ.get("QUALITY_AUTO_TIME", "02:50")),
            quality_auto_timezone=parse_quality_auto_timezone(
                os.environ.get("QUALITY_AUTO_TIMEZONE", "Asia/Shanghai")
            ),
            quality_auto_max_tasks=positive_int_env("QUALITY_AUTO_MAX_TASKS", 50),
            quality_auto_115_check_limit=positive_int_env("QUALITY_AUTO_115_CHECK_LIMIT", 3),
        )


@dataclass
class MoveConfig:
    source_roots: list[Path]
    library_roots: dict[str, Path]
    conflict_policy: str = "skip"
    stable_seconds: int = 0

    @classmethod
    def from_config(cls, config: Config) -> "MoveConfig":
        source_roots = [Path(part).expanduser() for part in split_env_list(config.strm_source_roots)]
        library_roots = default_library_roots()
        if config.strm_library_map.strip():
            library_roots.update(parse_library_map(config.strm_library_map))
        return cls(
            source_roots=source_roots,
            library_roots=library_roots,
            conflict_policy=config.move_conflict_policy or "skip",
            stable_seconds=max(0, int(config.strm_stable_seconds)),
        )


@dataclass
class MovePlan:
    status: str
    reason: str
    source_path: Path | None = None
    dest_path: Path | None = None
    category: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class SelfShareConfig:
    enabled: bool = False
    strm_root: Path = Path("/mnt/user/Unraid/strm/share")
    cms_local_path: str = "/media/share"
    cms_cid: str = "0"
    excluded_parent_ids: set[str] | None = None
    cleanup_after_emby: bool = False
    source_cleanup_parent_ids: set[str] | None = None
    auto_organize_retry_seconds: int = 90
    cloud_poll_seconds: int = 30
    cloud_timeout_seconds: int = 86400
    parent_cid_category_map: dict[str, str] | None = None
    organized_scan_parent_ids: set[str] | None = None
    cms_state_db_path: Path = Path("/cms/cms-online.db")

    @classmethod
    def from_config(cls, config: Config, cms: Any | None = None) -> "SelfShareConfig":
        excluded = set()
        organized_scan_parent_ids = set(split_env_list(config.self_share_organized_scan_parent_ids))
        if cms:
            try:
                excluded.update(cms.auto_organize_excluded_parent_ids())
            except Exception:
                LOG.debug("Failed to load CMS auto organize excluded folders", exc_info=True)
            if not organized_scan_parent_ids:
                try:
                    organized_scan_parent_ids.update(cms.auto_organize_existing_parent_ids())
                except Exception:
                    LOG.debug("Failed to load CMS organized scan folders", exc_info=True)
        return cls(
            enabled=config.workflow_mode == "self_share_sync",
            strm_root=Path(config.self_share_strm_root).expanduser(),
            cms_local_path=config.self_share_cms_local_path,
            cms_cid=config.self_share_cms_cid,
            excluded_parent_ids=excluded,
            cleanup_after_emby=config.self_share_cleanup_after_emby,
            source_cleanup_parent_ids=set(split_env_list(config.self_share_source_cleanup_parent_ids)),
            auto_organize_retry_seconds=max(0, int(config.self_share_auto_organize_retry_seconds)),
            cloud_poll_seconds=max(30, int(config.self_share_cloud_poll_seconds)),
            cloud_timeout_seconds=max(300, int(config.self_share_cloud_timeout_seconds)),
            parent_cid_category_map=parse_parent_cid_category_map(config.cms_parent_cid_category_map),
            organized_scan_parent_ids=organized_scan_parent_ids,
            cms_state_db_path=Path(config.cms_state_db_path).expanduser(),
        )


def split_env_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[|,]", value or "") if part.strip()]


def default_library_roots() -> dict[str, Path]:
    base = Path("/mnt/user/Unraid/strm/转存")
    return {
        "华语电影": base / "Movie/电影/华语电影",
        "欧美电影": base / "Movie/电影/欧美电影",
        "亚洲电影": base / "Movie/电影/亚洲电影",
        "动漫电影": base / "Movie/电影/动漫电影",
        "国产电视": base / "TVCN",
        "外国电视": base / "TV",
        "番剧": base / "Dongman",
    }


def parse_library_map(value: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for item in split_env_list(value):
        if "=" not in item:
            continue
        key, path = item.split("=", 1)
        key = key.strip()
        path = path.strip()
        if key and path:
            result[key] = Path(path).expanduser()
    return result


def parse_parent_cid_category_map(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in split_env_list(value):
        if "=" not in item:
            continue
        parent_id, category = item.split("=", 1)
        parent_id = parent_id.strip()
        category = category.strip()
        if parent_id and category:
            result[parent_id] = category
    return result


def safe_resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_relative_to(path: Path, root: Path) -> bool:
    path = safe_resolve(path)
    root = safe_resolve(root)
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_under_any_root(path: Path, roots: list[Path]) -> bool:
    return any(is_relative_to(path, root) for root in roots)
