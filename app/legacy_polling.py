"""Compatibility entry point for the legacy bridge status polling path."""

from __future__ import annotations

import sys
from typing import Any


def start_status_poll(
    cms: Any,
    telegram: Any,
    chat_id: Any,
    store: Any,
    row: dict[str, Any],
    status_poll_seconds: int,
    status_poll_interval: int,
    *,
    emby: Any = None,
    move_config: Any = None,
    openai_classifier: Any = None,
    tmdb_resolver: Any = None,
    self_share_workflow: Any = None,
    cleanup_client: Any = None,
    task_store: Any = None,
) -> None:
    bridge_module = sys.modules.get("bridge") or __import__("bridge")
    if not hasattr(bridge_module, "_start_status_poll_impl"):
        raise RuntimeError("bridge legacy polling implementation is not loaded")
    bridge_module._start_status_poll_impl(
        cms,
        telegram,
        chat_id,
        store,
        row,
        status_poll_seconds,
        status_poll_interval,
        emby=emby,
        move_config=move_config,
        openai_classifier=openai_classifier,
        tmdb_resolver=tmdb_resolver,
        self_share_workflow=self_share_workflow,
        cleanup_client=cleanup_client,
        task_store=task_store,
    )
