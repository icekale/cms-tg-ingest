from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.p115 import P115RiskControlError, P115ShareUnavailableError
from app.config import is_relative_to, safe_resolve
from app.media.strm import validate_self_share_strm_destination
from app.models import TaskStage, TaskStatus

LOG = logging.getLogger("cms-tg-ingest")


@dataclass(frozen=True)
class InvalidShareProbeSummary:
    checked_count: int = 0
    cleaned_count: int = 0
    risk_controlled: bool = False


def probe_invalid_self_shares(
    store: Any,
    task_store: Any,
    p115: Any,
    emby: Any | None,
    telegram: Any | None,
    chat_id: str,
    move_config: Any,
    *,
    limit: int,
) -> InvalidShareProbeSummary:
    checked_count = 0
    cleaned_count = 0
    for row in store.self_share_probe_candidates(limit=max(1, int(limit))):
        checked_count += 1
        row_id = int(row["id"])
        try:
            p115.share_snap(
                str(row.get("own_share_code") or ""),
                str(row.get("own_share_receive_code") or "1212"),
                cid="0",
                limit=1,
            )
        except P115RiskControlError:
            store.update_share_probe(row_id)
            LOG.warning("Stopped invalid-share probe after 115 risk control row_id=%s", row_id)
            return InvalidShareProbeSummary(checked_count, cleaned_count, risk_controlled=True)
        except P115ShareUnavailableError as exc:
            store.update_share_probe(row_id)
            if _clean_invalid_self_share(store, task_store, emby, telegram, chat_id, move_config, row, str(exc)):
                cleaned_count += 1
        except RuntimeError as exc:
            store.update_share_probe(row_id)
            LOG.warning("Invalid-share probe returned an unclassified error row_id=%s error=%s", row_id, exc)
        else:
            store.update_share_probe(row_id)
    return InvalidShareProbeSummary(checked_count, cleaned_count)


def probe_invalid_self_shares_if_idle(
    store: Any,
    task_store: Any,
    p115: Any,
    emby: Any | None,
    telegram: Any | None,
    chat_id: str,
    move_config: Any,
    *,
    limit: int,
) -> InvalidShareProbeSummary:
    if task_store and task_store.has_active_task_work():
        LOG.info("Skipped invalid-share probe while TaskStore has active work")
        return InvalidShareProbeSummary()
    return probe_invalid_self_shares(store, task_store, p115, emby, telegram, chat_id, move_config, limit=limit)


def start_invalid_self_share_probe_loop(
    store: Any,
    task_store: Any,
    p115: Any,
    emby: Any | None,
    telegram: Any | None,
    chat_id: str,
    move_config: Any,
    *,
    interval_seconds: int,
    limit: int,
) -> threading.Thread:
    interval = max(60, int(interval_seconds))
    probe_limit = max(1, int(limit))

    def loop() -> None:
        # Wait first so startup never adds an immediate 115 request burst.
        while not stop_event.wait(interval):
            try:
                summary = probe_invalid_self_shares_if_idle(
                    store,
                    task_store,
                    p115,
                    emby,
                    telegram,
                    chat_id,
                    move_config,
                    limit=probe_limit,
                )
                if summary.checked_count:
                    LOG.info(
                        "Invalid-share probe completed checked=%s cleaned=%s risk_controlled=%s",
                        summary.checked_count,
                        summary.cleaned_count,
                        summary.risk_controlled,
                    )
            except Exception:
                LOG.exception("Invalid-share probe loop failed; retaining all STRM files")

    stop_event = threading.Event()
    thread = threading.Thread(target=loop, name="invalid-self-share-probe", daemon=True)
    thread.start()
    LOG.info("Invalid-share probe loop enabled interval_seconds=%s limit=%s", interval, probe_limit)
    return thread


def _clean_invalid_self_share(
    store: Any,
    task_store: Any,
    emby: Any | None,
    telegram: Any | None,
    chat_id: str,
    move_config: Any,
    row: dict[str, Any],
    reason: str,
) -> bool:
    destination_text = str(row.get("dest_path") or "").strip()
    if not destination_text:
        return False
    destination = safe_resolve(Path(destination_text))
    library_roots = [safe_resolve(Path(path)) for path in (move_config.library_roots or {}).values()]
    if not destination.is_dir() or not any(is_relative_to(destination, root) for root in library_roots):
        LOG.warning("Refused invalid-share cleanup outside configured library row_id=%s path=%s", row.get("id"), destination)
        return False
    issue = validate_self_share_strm_destination(destination, row)
    if issue:
        LOG.warning("Refused invalid-share cleanup without self-share proof row_id=%s issue=%s", row.get("id"), issue)
        return False
    try:
        shutil.rmtree(destination)
    except OSError:
        LOG.exception("Failed to remove invalid self-share destination row_id=%s", row.get("id"))
        return False

    message = f"115 自有分享已失效，已删除对应 STRM：{reason}"
    updated = store.mark_invalid_share_cleaned(int(row["id"]), message) or row
    _mark_task_needs_action(task_store, updated, message)
    library = _refresh_emby(emby, destination)
    if telegram:
        title = str(updated.get("emby_title") or updated.get("own_share_file_name") or updated.get("title") or "媒体")
        suffix = f"，已刷新 Emby 媒体库：{library}" if library else ""
        telegram.send_message(chat_id, f"分享失效已清理：{title}（{reason}）{suffix}")
    return True


def _mark_task_needs_action(task_store: Any, row: dict[str, Any], message: str) -> None:
    if task_store is None:
        return
    task = task_store.upsert_task(
        str(row.get("share_code") or ""),
        str(row.get("receive_code") or ""),
        str(row.get("url") or ""),
    )
    task_store.record_event(
        task.id,
        TaskStage.NEEDS_ACTION,
        TaskStatus.NEEDS_ACTION,
        message,
        submission_id=int(row["id"]),
        metadata_patch={
            "submission_id": int(row["id"]),
            "invalid_share_cleaned": True,
            "invalid_share_reason": message,
            "dest_path": str(row.get("dest_path") or ""),
        },
        error_type="invalid_self_share",
        error_summary=message,
        clear_claim=True,
    )


def _refresh_emby(emby: Any | None, destination: Path) -> str:
    if not emby or not getattr(emby, "enabled", False):
        return ""
    try:
        return str(emby.refresh_library_for_path(destination) or "")
    except Exception:
        LOG.exception("Failed to refresh Emby after invalid-share cleanup path=%s", destination)
        return ""
