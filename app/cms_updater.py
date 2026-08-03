"""CMS version detection and best-effort image update support."""

from __future__ import annotations

import json
import logging
import socket
import time
from http.client import HTTPConnection
from typing import Any, Callable
from urllib.parse import quote


LOG = logging.getLogger("cms-tg-ingest")
CMS_VERSION_STATE_KEY = "cms_version_state"


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 30.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(socket_path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._socket_path)


def docker_pull_image(socket_path: str, image: str, tag: str = "latest") -> str:
    """Pull a Docker image through the Docker Engine API over a unix socket."""
    image = str(image or "").strip()
    socket_path = str(socket_path or "").strip()
    if not image or not socket_path:
        return "no docker socket or image configured"
    if "@" in image:
        repo, explicit_tag = image, ""
    elif ":" in image:
        repo, _, explicit_tag = image.rpartition(":")
        if "/" in explicit_tag or not repo:
            repo, explicit_tag = image, ""
    else:
        repo, explicit_tag = image, ""
    pull_tag = explicit_tag or tag
    try:
        conn = _UnixHTTPConnection(socket_path)
        conn.request(
            "POST",
            f"/images/create?fromImage={quote(repo, safe='')}&tag={quote(pull_tag, safe='')}",
        )
        response = conn.getresponse()
        response.read(8192)
        status = int(response.status or 0)
        conn.close()
        if status in {200, 201}:
            return "pulled"
        return f"pull failed status={status}"
    except Exception as exc:  # noqa: BLE001 - updater must never crash the loop
        try:
            conn.close()
        except Exception:
            pass
        return f"pull error: {type(exc).__name__}: {exc}"


class CmsVersionChecker:
    def __init__(
        self,
        store: Any,
        cms: Any,
        *,
        enabled: bool = False,
        interval_seconds: int = 3600,
        image: str = "",
        container: str = "cms",
        docker_socket: str = "/var/run/docker.sock",
        auto_pull: bool = False,
    ) -> None:
        self.store = store
        self.cms = cms
        self._defaults = {
            "enabled": bool(enabled),
            "interval_seconds": max(300, int(interval_seconds)),
            "image": str(image or "").strip(),
            "container": str(container or "cms").strip(),
            "docker_socket": str(docker_socket or "").strip(),
            "auto_pull": bool(auto_pull),
        }

    def _effective(self) -> dict[str, Any]:
        overrides = self.store.get_cms_version_overrides()
        settings = dict(self._defaults)
        for key, value in overrides.items():
            if key not in settings:
                continue
            if key in {"enabled", "auto_pull"}:
                settings[key] = bool(value)
            elif key == "interval_seconds":
                try:
                    settings[key] = max(300, int(value))
                except (TypeError, ValueError):
                    pass
            else:
                settings[key] = str(value or "").strip()
        return settings

    def effective_interval(self) -> int:
        return int(self._effective()["interval_seconds"])

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "enabled",
            "interval_seconds",
            "image",
            "container",
            "docker_socket",
            "auto_pull",
        }
        clean: dict[str, Any] = {}
        for key, value in (patch or {}).items():
            if key not in allowed or value is None:
                continue
            if key in {"enabled", "auto_pull"}:
                clean[key] = bool(value)
            elif key == "interval_seconds":
                try:
                    clean[key] = max(300, int(value))
                except (TypeError, ValueError):
                    continue
            else:
                clean[key] = str(value or "").strip()
        self.store.set_cms_version_overrides(clean)
        return self._effective()

    def reset_settings(self) -> dict[str, Any]:
        self.store.clear_cms_version_overrides()
        return self._effective()

    def status(self) -> dict[str, Any]:
        settings = self._effective()
        state = self.store.get_runtime_state(CMS_VERSION_STATE_KEY)
        payload: dict[str, Any] = {}
        if state:
            try:
                stored = json.loads(str(state["value"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                stored = {}
            if isinstance(stored, dict):
                payload.update(stored)
        payload.update(
            {
                "enabled": settings["enabled"],
                "interval_seconds": settings["interval_seconds"],
                "image": settings["image"],
                "container": settings["container"],
                "docker_socket": settings["docker_socket"],
                "auto_pull": settings["auto_pull"],
            }
        )
        payload.setdefault("current_version", "")
        payload.setdefault("last_seen_version", "")
        payload.setdefault("last_seen_at", 0)
        payload.setdefault("last_changed_at", 0)
        payload.setdefault("update_ready", False)
        payload.setdefault("pull_result", "")
        payload.setdefault("message", "")
        return payload

    def check(self, *, notify: Callable[[str, str], None] | None = None) -> dict[str, Any]:
        settings = self._effective()
        if not settings["enabled"]:
            return self.status()
        version = str(self.cms.get_version() or "").strip()
        state = self.status()
        last_seen = str(state.get("last_seen_version") or "").strip()
        changed = bool(version and last_seen and version != last_seen)
        now = time.time()
        if not version:
            return state
        payload = {
            "current_version": version,
            "last_seen_version": version,
            "last_seen_at": now,
            "last_changed_at": now if changed else float(state.get("last_changed_at") or 0),
            "update_ready": state.get("update_ready") if not changed else True,
            "image": settings["image"],
            "container": settings["container"],
            "pull_result": state.get("pull_result") or "",
            "message": state.get("message") or "",
            "enabled": True,
            "interval_seconds": settings["interval_seconds"],
            "docker_socket": settings["docker_socket"],
            "auto_pull": settings["auto_pull"],
        }
        if changed:
            pull_result = ""
            if settings["auto_pull"] and settings["image"]:
                pull_result = docker_pull_image(settings["docker_socket"], settings["image"])
            payload["update_ready"] = True
            payload["pull_result"] = pull_result
            payload["message"] = f"检测到 CMS 新版本 {version}，请执行更新"
            if callable(notify):
                try:
                    notify(version, pull_result)
                except Exception:
                    LOG.debug("CMS version notify failed", exc_info=True)
        self.store.set_runtime_state(
            CMS_VERSION_STATE_KEY,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        return payload


def start_cms_version_check_loop(
    checker: CmsVersionChecker,
    telegram: Any,
    chat_id: str,
    stop_event: Any,
    interval_seconds: int = 3600,
) -> Any:
    import threading

    interval = max(5, int(interval_seconds))

    def loop() -> None:
        while not stop_event.wait(checker.effective_interval()):
            try:
                checker.check(
                    notify=lambda version, pull_result: telegram.send_message(
                        chat_id,
                        f"检测到 CMS 新版本：{version}\n{pull_result or '等待拉取镜像'}。"
                        "请在宿主机执行更新脚本完成容器切换。",
                    )
                )
            except Exception:
                LOG.exception("CMS version check loop failed")

    thread = threading.Thread(target=loop, name="cms-version-check", daemon=True)
    thread.start()
    return thread
