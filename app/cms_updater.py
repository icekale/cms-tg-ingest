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
    try:
        conn = _UnixHTTPConnection(socket_path)
        conn.request(
            "POST",
            f"/v1.41/images/create?fromImage={quote(image, safe='')}&tag={quote(tag, safe='')}",
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
        image: str = "",
        container: str = "cms",
        docker_socket: str = "/var/run/docker.sock",
        auto_pull: bool = False,
    ) -> None:
        self.store = store
        self.cms = cms
        self.image = str(image or "").strip()
        self.container = str(container or "cms").strip()
        self.docker_socket = str(docker_socket or "").strip()
        self.auto_pull = bool(auto_pull)

    def status(self) -> dict[str, Any]:
        state = self.store.get_runtime_state(CMS_VERSION_STATE_KEY)
        if not state:
            return {
                "current_version": "",
                "last_seen_version": "",
                "last_seen_at": 0,
                "last_changed_at": 0,
                "update_ready": False,
                "image": self.image,
                "container": self.container,
                "pull_result": "",
                "message": "",
            }
        try:
            payload = json.loads(str(state["value"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def check(self, *, notify: Callable[[str, str], None] | None = None) -> dict[str, Any]:
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
            "image": self.image,
            "container": self.container,
            "pull_result": state.get("pull_result") or "",
            "message": state.get("message") or "",
        }
        if changed:
            pull_result = ""
            if self.auto_pull and self.image:
                pull_result = docker_pull_image(self.docker_socket, self.image)
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
        while not stop_event.wait(interval):
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
