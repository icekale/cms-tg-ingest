"""CMS version detection and best-effort image update support."""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.request
from http.client import HTTPConnection
from typing import Any, Callable
from urllib.parse import quote


LOG = logging.getLogger("cms-tg-ingest")
CMS_VERSION_STATE_KEY = "cms_version_state"


def _version_core(value: str) -> str:
    """Extract the numeric version core from an arbitrary version string.

    CMS reports versions like ``v0.4.9.2 - PRO`` while the Docker Hub tag is a
    plain ``0.4.9.2``; a raw string comparison would never consider them equal
    and would keep reporting an update after the container was upgraded.
    """
    match = re.search(r"\d+(?:\.\d+)+", str(value or ""))
    return match.group(0) if match else str(value or "").strip()


def _split_image(image: str) -> tuple[str, str]:
    """Split an image ref into (repo, tag). Returns ("", "") for non-Docker-Hub refs.

    Only plain Docker Hub images (``owner/name:tag``) support the remote tag
    lookup; digests, registry-prefixed refs, and bare names return empty.
    """
    image = str(image or "").strip()
    if not image or "@" in image or "/" not in image:
        return "", ""
    repo, _, tag = image.rpartition(":")
    if "/" not in repo or not tag or ":" in repo:
        return "", ""
    # Registry-prefixed refs (ghcr.io/..., host:port/...) are not Docker Hub
    # images; the hub.docker.com tag API cannot resolve them.
    namespace = repo.split("/", 1)[0]
    if "." in namespace or ":" in namespace:
        return "", ""
    return repo, tag


def fetch_remote_latest_tag(image: str, timeout: float = 10.0) -> str:
    """Return the most recently updated non-'latest' tag for a Docker Hub image.

    Uses the public Docker Hub tags API (no credentials). Returns "" when the
    image is not a Docker Hub ref, the repo is unknown, or the lookup fails, so
    a remote check never breaks the existing local version detection.
    """
    repo, _ = _split_image(image)
    if not repo:
        return ""
    try:
        # The repo slash is a path separator in the tags API URL; quote it with
        # safe='/' (escaping to %2F makes Docker Hub answer 400, unlike the
        # image pull API which expects %2F in the query string).
        url = (
            f"https://hub.docker.com/v2/repositories/{quote(repo, safe='/')}"
            f"/tags?page_size=25&ordering=last_updated"
        )
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "cms-tg-ingest"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        for item in payload.get("results") or []:
            name = str(item.get("name") or "").strip()
            if name and name != "latest":
                return name
        return ""
    except Exception:  # noqa: BLE001 - remote lookup must never break the loop
        LOG.debug("CMS remote tag lookup failed image=%s", image, exc_info=True)
        return ""


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
    is_digest = "@" in image
    if "@" in image:
        repo, explicit_tag = image, ""
    elif ":" in image:
        repo, _, explicit_tag = image.rpartition(":")
        if "/" in explicit_tag or not repo:
            repo, explicit_tag = image, ""
    else:
        repo, explicit_tag = image, ""
    pull_tag = explicit_tag or ("" if is_digest else tag)
    conn = None
    try:
        conn = _UnixHTTPConnection(socket_path, timeout=600)
        query = f"fromImage={quote(repo, safe='')}"
        if pull_tag:
            query += f"&tag={quote(pull_tag, safe='')}"
        conn.request(
            "POST",
            f"/images/create?{query}",
        )
        response = conn.getresponse()
        while response.read(65536):
            pass
        status = int(response.status or 0)
        conn.close()
        conn = None
        if status in {200, 201}:
            return "pulled"
        return f"pull failed status={status}"
    except Exception as exc:  # noqa: BLE001 - updater must never crash the loop
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return f"pull error: {type(exc).__name__}: {exc}"


def docker_container_started_at(socket_path: str, container: str) -> str:
    """Return the container's ``State.StartedAt`` via the Docker Engine API.

    Returns "" when the socket is unavailable, the container does not exist,
    or any other error occurs, so version checks never fail because of it.
    """
    socket_path = str(socket_path or "").strip()
    container = str(container or "").strip()
    if not socket_path or not container:
        return ""
    conn = None
    try:
        conn = _UnixHTTPConnection(socket_path, timeout=15)
        conn.request("GET", f"/containers/{quote(container, safe='')}/json")
        response = conn.getresponse()
        # Container inspect JSON can exceed 64KB when the container has many
        # mounts/env entries; read to EOF so the started_at lookup does not
        # silently fail on truncated JSON.
        content_length = int(response.getheader("Content-Length") or 0)
        body = response.read(65536)
        while content_length <= 0 or len(body) < content_length:
            chunk = response.read(65536)
            if not chunk:
                break
            body += chunk
        status = int(response.status or 0)
        conn.close()
        conn = None
        if status != 200:
            return ""
        payload = json.loads(body.decode("utf-8", "replace"))
        return str(payload.get("State", {}).get("StartedAt") or "")
    except Exception as exc:  # noqa: BLE001 - introspection must never crash the loop
        LOG.debug("docker container started_at lookup failed: %s", exc)
        if conn is not None:
            try:
                conn.close()
            except Exception as close_exc:  # noqa: BLE001
                LOG.debug("closing docker socket connection failed: %s", close_exc)
                pass
        return ""


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
        remote_lookup: Callable[[str], str] | None = None,
    ) -> None:
        self.store = store
        self.cms = cms
        self._remote_lookup = remote_lookup or fetch_remote_latest_tag
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
        payload.setdefault("last_container_started_at", "")
        payload.setdefault("pull_result", "")
        payload.setdefault("message", "")
        payload.setdefault("remote_version", "")
        payload.setdefault("update_available", False)
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
        started_at = docker_container_started_at(settings["docker_socket"], settings["container"])
        last_started_at = str(state.get("last_container_started_at") or "")
        container_restarted = bool(started_at and last_started_at and started_at != last_started_at)
        # Remote (Docker Hub) latest tag is the "new version available" signal
        # the local running version can never see. It is advisory: display and
        # message only, never auto-pull or flip update_ready.
        remote_version = ""
        update_available = False
        try:
            if settings["image"]:
                remote_version = str(self._remote_lookup(settings["image"]) or "").strip()
                # Compare the numeric core: CMS reports "v0.4.9.2 - PRO" while
                # the tag is "0.4.9.2"; raw comparison would never converge.
                if remote_version and _version_core(remote_version) != _version_core(version):
                    update_available = True
        except Exception:  # noqa: BLE001 - remote lookup must never break the loop
            LOG.debug("CMS remote version check failed", exc_info=True)
        payload = {
            "current_version": version,
            "last_seen_version": version,
            "last_seen_at": now,
            "last_changed_at": now if changed else float(state.get("last_changed_at") or 0),
            "update_ready": True if changed else (False if container_restarted else state.get("update_ready")),
            "last_container_started_at": started_at,
            "image": settings["image"],
            "container": settings["container"],
            "pull_result": state.get("pull_result") or "",
            "message": state.get("message") or "",
            "enabled": True,
            "interval_seconds": settings["interval_seconds"],
            "docker_socket": settings["docker_socket"],
            "auto_pull": settings["auto_pull"],
            "remote_version": remote_version,
            "update_available": update_available,
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
        elif update_available:
            payload["message"] = f"发现远程新版本 {remote_version}（当前 {version}），可执行更新脚本升级"
        self.store.set_runtime_state(
            CMS_VERSION_STATE_KEY,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        return payload

    def pull(self) -> dict[str, Any]:
        """Pull the configured CMS image through the docker socket.

        Container switching stays on the host (update-cms.sh) so the
        guard-verify + auto-rollback loop is never bypassed; this only makes
        the image available locally so the host upgrade is fast and offline.
        """
        settings = self._effective()
        pull_result = ""
        if settings["image"] and settings["docker_socket"]:
            pull_result = docker_pull_image(settings["docker_socket"], settings["image"])
        state = self.status()
        state["pull_result"] = pull_result
        if pull_result == "pulled":
            state["message"] = "镜像已拉取，请在宿主机执行升级脚本完成容器切换"
        else:
            state["message"] = f"镜像拉取失败：{pull_result}"
        self.store.set_runtime_state(
            CMS_VERSION_STATE_KEY,
            json.dumps(state, ensure_ascii=False, sort_keys=True),
        )
        return state


def start_cms_version_check_loop(
    checker: CmsVersionChecker,
    telegram: Any,
    chat_id: str,
    stop_event: Any,
    interval_seconds: int = 3600,
) -> Any:
    import threading

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
