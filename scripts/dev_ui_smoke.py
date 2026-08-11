#!/usr/bin/env python3
"""Local demo backend for reviewing the redesigned web UI.

Serves the real /api/v1 surface plus the /login page, backed by an in-memory
TaskStore seeded with succeeded tasks carrying poster metadata, so the
"最近入库" media wall and both themes can be reviewed in a browser.

    python scripts/dev_ui_smoke.py                 # backend on :8737
    VITE_API_PROXY=http://127.0.0.1:8737 npm run dev   # Vite on :5173

Then open http://localhost:5173/app/ (login: dev / dev).
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402
from app.cms_updater import CmsVersionChecker  # noqa: E402
from app.models import TaskStage, TaskStatus  # noqa: E402
from app.task_store import TaskStore  # noqa: E402
from app.web import start_web_server  # noqa: E402


class FakeCms:
    def get_version(self):
        # Simulate the running CMS container reporting an older version than
        # the latest Docker Hub tag, so "立即检查" demonstrates remote detection.
        return "0.4.9.1"


def fake_remote_lookup(image):
    return "0.4.9.2"

SEED = [
    # (share_key, title, media_type, tmdb_id, poster, rating, release, genres)
    # All poster paths were fetched from the real TMDB API and verified
    # reachable on image.tmdb.org, so the media wall renders actual artwork.
    ("demo1", "龙兄虎弟", "movie", "10974", "/i9zrfkod6qM3CWvNUmllJ104K7g.jpg", 7.0, "1986-08-16", ["冒险", "动作", "喜剧"]),
    ("demo2", "力王", "movie", "17467", "/hatZvnFcZtsFknZSRs7KloOEduW.jpg", 6.9, "1991-10-05", ["动作", "科幻", "恐怖"]),
    ("demo3", "九门", "tv", "146419", "/8Vt6mWEReuy4Of61Lnj5Xj704m8.jpg", 9.0, "2026-01-01", ["剧情", "动作冒险"]),
    ("demo4", "半熟恋人", "tv", "154420", "/ghkFZxjGlst1TVjG6agyzc04r2H.jpg", 5.2, "2021-12-28", ["真人秀"]),
]


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="ui-smoke-")
    store = TaskStore(Path(tmp) / "tasks.db")
    submissions = bridge.SubmissionStore(Path(tmp) / "submissions.db")
    for share_key, title, media_type, tmdb_id, poster, rating, release, genres in SEED:
        task = store.upsert_task(share_key, "", f"https://115cdn.com/s/{share_key}")
        store.patch_metadata(task.id, {
            "strm_mode": "shared",
            "category": "电影" if media_type == "movie" else "剧集",
            "title": title,
            "type": media_type,
            "tmdb_id": tmdb_id,
            "poster_path": poster,
            "overview": f"{title} 的剧情简介。",
            "genres": genres,
            "vote_average": rating,
            "release_date": release,
        })
        store.record_event(task.id, TaskStage.CLEANED, TaskStatus.SUCCEEDED, "done")

    start_web_server(
        store,
        host="127.0.0.1",
        port=8737,
        web_username="dev",
        web_password="dev",
        submission_store=submissions,
        frontend_dist_path=str(ROOT / "frontend" / "dist"),
        cms_version_checker=CmsVersionChecker(
            store,
            FakeCms(),
            enabled=True,
            image="imaliang/cloud-media-sync:latest",
            container="cloud-media-sync",
            docker_socket="/var/run/docker.sock",
            remote_lookup=fake_remote_lookup,
        ),
    )
    print("backend serving on http://127.0.0.1:8737 (login dev/dev)", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
