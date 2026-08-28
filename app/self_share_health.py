from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.p115 import P115RiskControlError, P115ShareUnavailableError
from app.config import DEFAULT_OWN_SHARE_RECEIVE_CODE, is_relative_to, safe_resolve
from app.media.strm import validate_self_share_strm_destination
from app.logging_system import safe_telegram_text
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
    rows = store.self_share_probe_candidates(limit=max(1, int(limit)))
    states: dict[str, dict[str, Any]] | None = None
    if hasattr(p115, "list_own_share_states"):
        try:
            states = p115.list_own_share_states(limit=100)
        except P115RiskControlError:
            LOG.warning("Stopped invalid-share probe after 115 risk control during batch share listing")
            return InvalidShareProbeSummary(0, 0, risk_controlled=True)
        except RuntimeError as exc:
            LOG.warning("Deferred invalid-share probe after batch share listing error: %s", exc)
            return InvalidShareProbeSummary(0, 0)

    for row in rows:
        checked_count += 1
        row_id = int(row["id"])
        share_code = str(row.get("own_share_code") or "").strip()
        try:
            if states is not None and share_code in states:
                status = states[share_code]
                share_state = str(status.get("share_state") or "").strip().lower()
                have_vio_file = str(status.get("have_vio_file") or "").strip().lower() in {"1", "true", "yes"}
                if not have_vio_file and share_state in {"0", "1", "true"}:
                    store.update_share_probe(row_id)
                    continue
                if not have_vio_file and not share_state:
                    store.update_share_probe(row_id)
                    continue
                if have_vio_file:
                    raise P115ShareUnavailableError("115 分享标记 have_vio_file")
                raise P115ShareUnavailableError(f"115 分享状态不可用: {share_state}")

            # The list endpoint is capped and can omit an older share. One precise
            # fallback keeps the probe bounded without scanning or retrying pages.
            if states is not None and hasattr(p115, "inspect_share"):
                p115.inspect_share(
                    share_code,
                    str(row.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE),
                )
            elif hasattr(p115, "share_snap"):
                p115.share_snap(
                    share_code,
                    str(row.get("own_share_receive_code") or DEFAULT_OWN_SHARE_RECEIVE_CODE),
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
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    interval = max(60, int(interval_seconds))
    probe_limit = max(1, int(limit))

    loop_stop_event = stop_event or threading.Event()

    def run_loop() -> None:
        while not loop_stop_event.wait(interval):
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

    # The worker is defined separately so the caller can stop/restart ownership at runtime.
    thread = threading.Thread(target=run_loop, name="invalid-self-share-probe", daemon=True)
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
        blocked = {
            str(updated.get(field) or "").strip()
            for field in ("share_code", "receive_code", "own_share_code", "own_share_receive_code")
            if str(updated.get(field) or "").strip()
        }
        raw_title = str(updated.get("emby_title") or updated.get("own_share_file_name") or updated.get("title") or "媒体").strip()
        title = raw_title if raw_title not in blocked else f"任务 #{updated.get('cms_task_id') or updated.get('id') or '?'}"
        title = safe_telegram_text(title, 120, blocked_values=blocked)
        clean_reason = safe_telegram_text(reason, 120, blocked_values=blocked)
        suffix = f"，已刷新 Emby 媒体库：{safe_telegram_text(library, 80, blocked_values=blocked)}" if library else ""
        telegram.send_message(chat_id, safe_telegram_text(f"分享失效已清理：{title}（{clean_reason}）{suffix}", 320, blocked_values=blocked))
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
