"""AI 助手只读查询 CLI：供 pi 扩展工具调用，绝无写操作。

用法：
  assistant_read.py tasks [--status STATUS] [--limit N]
  assistant_read.py task <id>
  assistant_read.py events <task_id> [--limit N]
  assistant_read.py stats
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_CANDIDATES = [Path("/app"), Path(__file__).resolve().parent.parent]
for candidate in _REPO_CANDIDATES:
    if (candidate / "app" / "task_store.py").exists():
        sys.path.insert(0, str(candidate))
        break

from app.models import TaskStatus  # noqa: E402
from app.task_store import TaskStore  # noqa: E402
from app.web_api import api_task_detail, serialize_event  # noqa: E402


def _open_store() -> TaskStore:
    db_path = os.environ.get("DATABASE_PATH") or "/data/cms-tg-ingest.db"
    if not Path(db_path).exists():
        print(json.dumps({"error": f"database not found: {db_path}"}))
        raise SystemExit(1)
    return TaskStore(db_path)


def _brief(task) -> dict:
    return {
        "id": task.id,
        "title": task.display_title if hasattr(task, "display_title") else (task.title or task.share_code),
        "status": str(getattr(getattr(task, "status", ""), "value", getattr(task, "status", ""))),
        "stage": str(getattr(getattr(task, "current_stage", ""), "value", getattr(task, "current_stage", ""))),
        "error": task.error_summary or "",
        "updated_at": task.updated_at,
    }


def cmd_tasks(args) -> None:
    store = _open_store()
    tasks = store.list_recent_tasks(limit=min(max(args.limit, 1), 200))
    if args.status:
        wanted = args.status.lower()
        tasks = [t for t in tasks if str(getattr(t.status, "value", t.status)).lower() == wanted]
    print(json.dumps({"count": len(tasks), "tasks": [_brief(t) for t in tasks]}, ensure_ascii=False))


def cmd_task(args) -> None:
    store = _open_store()
    detail = api_task_detail(store, args.id)
    if detail is None:
        print(json.dumps({"error": f"task {args.id} not found"}))
        raise SystemExit(1)
    detail.pop("metadata", None)  # 技术细节噪音，助手有事件流足够
    print(json.dumps(detail, ensure_ascii=False))


def cmd_events(args) -> None:
    store = _open_store()
    events = store.list_events(args.task_id)[-min(max(args.limit, 1), 100):]
    print(json.dumps({"count": len(events), "events": [serialize_event(e) for e in events]}, ensure_ascii=False))


def cmd_stats(args) -> None:
    store = _open_store()
    tasks = store.list_recent_tasks(limit=200)
    by_status: dict[str, int] = {}
    for task in tasks:
        key = str(getattr(task.status, "value", task.status))
        by_status[key] = by_status.get(key, 0) + 1
    needs_action = [
        _brief(t)
        for t in tasks
        if str(getattr(t.status, "value", t.status)) == TaskStatus.NEEDS_ACTION.value
    ][:15]
    print(json.dumps({"total_recent": len(tasks), "by_status": by_status, "needs_action": needs_action}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="cms-tg-ingest assistant read-only queries")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tasks = sub.add_parser("tasks")
    p_tasks.add_argument("--status", default="")
    p_tasks.add_argument("--limit", type=int, default=15)

    p_task = sub.add_parser("task")
    p_task.add_argument("id", type=int)

    p_events = sub.add_parser("events")
    p_events.add_argument("task_id", type=int)
    p_events.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats")

    args = parser.parse_args()
    {"tasks": cmd_tasks, "task": cmd_task, "events": cmd_events, "stats": cmd_stats}[args.command](args)


if __name__ == "__main__":
    main()
