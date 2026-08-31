from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.hdhive_subscription_store import HdhiveSubscriptionStore
from app.task_store import TaskStore
from tests.legacy_submission_store import SubmissionStore



@dataclass(frozen=True)
class LegacyFixture:
    tasks_db: Path
    submissions_db: Path


def build_legacy_databases(root: str | Path) -> LegacyFixture:
    root_path = Path(root)
    tasks_db = root_path / "tasks.db"
    submissions_db = root_path / "submissions.db"
    TaskStore(tasks_db)
    SubmissionStore(submissions_db)
    HdhiveSubscriptionStore(tasks_db)
    with sqlite3.connect(tasks_db) as tasks, sqlite3.connect(submissions_db) as submissions:
        tasks.execute(
            """
            INSERT INTO tasks (
                id, share_code, receive_code, source_type, source_key, url, title, tmdb_id, category,
                current_stage, status, created_at, updated_at, submission_id, metadata_json, next_run_at
            ) VALUES (
                10, 'abc', '1111', 'share', 'share:abc:1111', 'https://115cdn.com/s/abc?password=1111',
                'Matched JSON', '100', '华语电影', 'cleaned', 'succeeded', 1, 1, NULL,
                '{"submission_id": 1}', -1
            )
            """
        )
        tasks.execute(
            """
            INSERT INTO tasks (
                id, share_code, receive_code, source_type, source_key, url, title, tmdb_id, category,
                current_stage, status, created_at, updated_at, submission_id, metadata_json, next_run_at
            ) VALUES (
                20, 'def', '2222', 'share', 'share:def:2222', 'https://115cdn.com/s/def?password=2222',
                'Matched Typed', '200', '华语电影', 'cleaned', 'succeeded', 1, 1, 2,
                '{}', -1
            )
            """
        )
        tasks.execute(
            """
            INSERT INTO tasks (
                id, share_code, receive_code, source_type, source_key, url, title,
                current_stage, status, created_at, updated_at, metadata_json, next_run_at
            ) VALUES (
                30, '', '', 'ed2k', 'ed2k:hash', 'ed2k://file', 'Task Only',
                'received', 'pending', 1, 1, '{}', -1
            )
            """
        )
        tasks.execute(
            """
            INSERT INTO task_events (id, task_id, stage, status, message, created_at)
            VALUES (100, 10, 'cleaned', 'succeeded', 'done', 1)
            """
        )
        tasks.execute(
            """
            INSERT INTO task_operations (
                id, task_id, operation_key, operation_type, status, request_json, result_json,
                created_at, updated_at
            ) VALUES (7, 10, 'receive:abc', 'receive_share', 'succeeded', '{"share_code":"abc"}', '{}', 1, 1)
            """
        )
        tasks.execute(
            "INSERT INTO runtime_state (key, value, updated_at) VALUES ('strm_default_mode', 'shared', 1)"
        )
        tasks.execute(
            """
            INSERT INTO quality_runs (
                run_id, run_date, status, started_at, finished_at, created_at
            ) VALUES ('run-1', '2026-08-01', 'succeeded', 1, 2, 1)
            """
        )
        tasks.execute(
            """
            INSERT INTO hdhive_subscriptions (
                id, chat_id, source_type, source_value, tmdb_id, created_at, updated_at
            ) VALUES (1, 'chat', 'tmdb', '1', '1', 1, 1)
            """
        )
        submissions.execute(
            """
            INSERT INTO submissions (
                id, share_code, receive_code, url, cms_task_id, title, status, created_at, updated_at,
                category_final, workflow_mode, own_share_code, dest_path, move_status, emby_status
            ) VALUES (
                1, 'abc', '1111', 'https://115cdn.com/s/abc?password=1111', 'cms-1', 'Matched JSON',
                'cleaned', 1, 1, '华语电影', 'self_share_sync', 'own1', '/library/a', 'moved', 'confirmed'
            )
            """
        )
        submissions.execute(
            """
            INSERT INTO submissions (
                id, share_code, receive_code, url, title, status, created_at, updated_at,
                category_final, workflow_mode, dest_path, move_status
            ) VALUES (
                2, 'def', '2222', 'https://115cdn.com/s/def?password=2222', 'Matched Typed',
                'cleaned', 1, 1, '华语电影', 'self_share_sync', '/library/b', 'moved'
            )
            """
        )
        submissions.execute(
            """
            INSERT INTO submissions (
                id, share_code, receive_code, url, title, status, created_at, updated_at,
                category_final, dest_path, move_status, emby_status
            ) VALUES (
                9, 'xyz', '3333', 'https://115cdn.com/s/xyz?password=3333', 'History Only',
                'cleaned', 1, 1, '华语电影', '/library/c', 'moved', 'confirmed'
            )
            """
        )
        submissions.execute(
            """
            INSERT INTO parent_category_memory (parent_id, category, source, created_at, updated_at)
            VALUES ('parent-1', '华语电影', 'manual', 1, 1)
            """
        )
        for name, seq in (("tasks", 30), ("task_events", 100), ("task_operations", 7), ("quality_runs", 1), ("hdhive_subscriptions", 1)):
            tasks.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (?, ?)", (name, seq))
        submissions.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('submissions', 9)")
        tasks.commit()
        submissions.commit()
    return LegacyFixture(tasks_db, submissions_db)
