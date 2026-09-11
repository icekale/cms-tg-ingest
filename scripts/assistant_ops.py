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
import time
from pathlib import Path

_REPO_CANDIDATES = [Path("/app"), Path(__file__).resolve().parent.parent]
for candidate in _REPO_CANDIDATES:
    if (candidate / "app" / "task_store.py").exists():
        sys.path.insert(0, str(candidate))
        break

from app.assistant import DIAGNOSIS_META_KEY, assistant_session_dir, record_assistant_action  # noqa: E402
from app.task_actions import TASK_ACTIONS, apply_task_action  # noqa: E402
from app.task_store import TaskStore  # noqa: E402

DEFAULT_MAX_RETRIES = 3


def _record_diagnosis_action(store: TaskStore, task_id: int, action: str, result) -> None:
    """把 pi 发起的动作回写任务诊断 metadata。

    同一个形状（auto_repair_*）就是 Python 自动修复用的字段：pi 已经做过的动作
    不会再被巡检重复执行，Web 任务详情也能看到是助手动手的。
    """
    task = result.task or store.find_task(task_id)
    metadata = getattr(task, "metadata", {}) or {}
    diagnosis = dict(metadata.get(DIAGNOSIS_META_KEY) or {}) if isinstance(metadata, dict) else {}
    tried = [str(item) for item in (diagnosis.get("auto_repair_tried") or [])]
    if result.applied and action not in tried:
        tried.append(action)
    diagnosis.update(
        {
            "auto_repair_action": action,
            "auto_repair_applied": bool(result.applied),
            "auto_repair_reason": str(result.reason or "")[:300],
            "auto_repair_at": time.time(),
            "auto_repair_tried": tried,
            "auto_repair_source": "pi",
        }
    )
    store.patch_metadata(task_id, {DIAGNOSIS_META_KEY: diagnosis})


def cmd_act(args) -> None:
    db_path = os.environ.get("DATABASE_PATH") or "/data/cms-tg-ingest.db"
    if not Path(db_path).exists():
        print(json.dumps({"applied": False, "reason": f"database not found: {db_path}"}))
        raise SystemExit(1)
    # 自动巡检会话（无人确认）里终止是硬拦：扩展拦一次，这里再拦一次。
    if args.action == "terminate" and os.environ.get("CMS_TOOLS_AUTOMATION") == "1":
        print(
            json.dumps(
                {"applied": False, "reason": "自动巡检会话不允许 terminate（破坏性动作需人工确认）"},
                ensure_ascii=False,
            )
        )
        raise SystemExit(0)
    store = TaskStore(db_path)
    result = apply_task_action(
        store,
        args.task_id,
        args.action,
        max_retries=DEFAULT_MAX_RETRIES,
        actor="AI助手",
    )
    record_assistant_action(
        action=args.action,
        applied=bool(result.applied),
        session_dir=assistant_session_dir(store),
    )
    try:
        _record_diagnosis_action(store, args.task_id, args.action, result)
    except Exception as exc:  # 动作已生效，回写失败不能当作执行失败
        print(json.dumps({"warning": f"diagnosis metadata write failed: {exc}"}, ensure_ascii=False), file=sys.stderr)
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
    raise SystemExit(0)


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
