"""Compatibility entry point for the legacy bridge status polling path."""

from __future__ import annotations

import inspect
import sys
from typing import Any


def _resolve_bridge_module() -> Any:
    for module_name in ("bridge", "__main__"):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "_start_status_poll_impl"):
            return module
    return __import__("bridge")


def start_status_poll(
    cms: Any,
    telegram: Any,
    chat_id: Any,
    store: Any,
    row: dict[str, Any],
    *poll_args: Any,
    emby: Any = None,
    move_config: Any = None,
    openai_classifier: Any = None,
    tmdb_resolver: Any = None,
    self_share_workflow: Any = None,
    cleanup_client: Any = None,
    task_store: Any = None,
    **poll_kwargs: Any,
) -> None:
    status_poll_seconds, status_poll_interval = _resolve_poll_args(poll_args, poll_kwargs)
    bridge_module = _resolve_bridge_module()
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


def _resolve_poll_args(poll_args: tuple[Any, ...], poll_kwargs: dict[str, Any]) -> tuple[Any, Any]:
    if len(poll_args) > 2:
        raise TypeError(f"start_status_poll() takes 7 positional arguments but {5 + len(poll_args)} were given")

    old_names = {"max_seconds": "status_poll_seconds", "interval": "status_poll_interval"}
    values: dict[str, Any] = {}
    for index, name in enumerate(("status_poll_seconds", "status_poll_interval")):
        if index < len(poll_args):
            values[name] = poll_args[index]

    for old_name, new_name in old_names.items():
        if old_name in poll_kwargs:
            if new_name in values or new_name in poll_kwargs:
                raise TypeError(f"start_status_poll() got both {new_name!r} and legacy {old_name!r}")
            values[new_name] = poll_kwargs.pop(old_name)

    for name in ("status_poll_seconds", "status_poll_interval"):
        if name in poll_kwargs:
            if name in values:
                raise TypeError(f"start_status_poll() got multiple values for argument {name!r}")
            values[name] = poll_kwargs.pop(name)

    if poll_kwargs:
        unexpected = next(iter(poll_kwargs))
        raise TypeError(f"start_status_poll() got an unexpected keyword argument {unexpected!r}")

    missing = [name for name in ("status_poll_seconds", "status_poll_interval") if name not in values]
    if missing:
        raise TypeError(f"start_status_poll() missing required argument: {missing[0]!r}")
    return values["status_poll_seconds"], values["status_poll_interval"]


start_status_poll.__signature__ = inspect.Signature(
    parameters=[
        inspect.Parameter("cms", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("telegram", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("chat_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("store", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("row", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("status_poll_seconds", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("status_poll_interval", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        inspect.Parameter("emby", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("move_config", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("openai_classifier", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("tmdb_resolver", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("self_share_workflow", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("cleanup_client", inspect.Parameter.KEYWORD_ONLY, default=None),
        inspect.Parameter("task_store", inspect.Parameter.KEYWORD_ONLY, default=None),
    ],
    return_annotation=None,
)
