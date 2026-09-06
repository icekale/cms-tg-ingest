"""AI 助手动作执行 CLI：让助手能对任务执行白名单内的操作。

与 Web/TG 按钮走同一条入口（app.task_actions.apply_task_action）：
- 资格校验内置（任务被认领/状态不符会拒绝），命令入队后由 TaskRunner 执行；
- 只开放 TASK_ACTIONS 白名单（retry/emby/restore/reprocess/resume_organizing/
  terminate），删除任务记录等破坏性操作不在其中；
- actor 记为 "AI助手"，审计可与人工操作区分。

用法：assistant_ops.py act <task_id> <action>
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

from app.task_actions import TASK_ACTIONS, apply_task_action  # noqa: E402
from app.task_store import TaskStore  # noqa: E402

DEFAULT_MAX_RETRIES = 3


def cmd_act(args) -> None:
    db_path = os.environ.get("DATABASE_PATH") or "/data/cms-tg-ingest.db"
    if not Path(db_path).exists():
        print(json.dumps({"applied": False, "reason": f"database not found: {db_path}"}))
        raise SystemExit(1)
    store = TaskStore(db_path)
    result = apply_task_action(
        store,
        args.task_id,
        args.action,
        max_retries=DEFAULT_MAX_RETRIES,
        actor="AI助手",
    )
    task = result.task
    print(
        json.dumps(
            {
                "applied": result.applied,
                "reason": result.reason,
                "task": (
                    {
                        "id": task.id,
                        "title": task.title or task.share_code,
                        "status": str(getattr(task.status, "value", task.status)),
                        "stage": str(getattr(task.current_stage, "value", task.current_stage)),
                    }
                    if task is not None
                    else None
                ),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result.applied else 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="cms-tg-ingest assistant action executor")
    sub = parser.add_subparsers(dest="command", required=True)
    p_act = sub.add_parser("act")
    p_act.add_argument("task_id", type=int)
    p_act.add_argument("action", choices=sorted(TASK_ACTIONS))
    args = parser.parse_args()
    cmd_act(args)


if __name__ == "__main__":
    main()
