"""Read CMS cloud_data metadata without calling 115."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.media.classify import extract_tmdb_id_from_name

from .sqlite_utils import sqlite_connection
from app.config import safe_resolve


_DIRECT_PICKCODE_RE = re.compile(r"/d/([A-Za-z0-9]+)(?:\.[^/?\s]+)?(?:[?\s/]|$)")
_SEASON_EPISODE_RE = re.compile(r"s\d{1,3}e\d{1,3}", re.IGNORECASE)

_MEDIA_LOCAL_PATH_PREFIX = "/media/"


def _media_strm_name(name: str) -> str:
    """Map a media file name to its expected .strm sibling name."""
    value = str(name or "").strip()
    if not value:
        return ""
    return f"{Path(value).stem}.strm"


def _episode_key(name: str) -> str:
    match = _SEASON_EPISODE_RE.search(str(name or ""))
    return match.group(0).lower() if match else ""


def _dir_has_episode(parent: Path, episode: str) -> bool:
    try:
        paths = list(parent.glob("*.strm"))
    except OSError:
        return False
    return any(_episode_key(path.name) == episode for path in paths)


def _share_domain_from_dir(parent: Path) -> str:
    try:
        paths = list(parent.glob("*.strm"))
    except OSError:
        return ""
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.match(r"(https?://[^/\s]+)/s/", text.strip())
        if match:
            return match.group(1)
    return ""


def share_strm_line(domain: str, share_code: str, receive_code: str, fid: str, name: str) -> str:
    """Playback URL used by existing self-share library STRM files."""
    suffix = Path(name).suffix or ".mkv"
    return f"{domain.rstrip('/')}/s/{share_code}_{receive_code}_{fid}{suffix}?/{name}"


def _share_create_blocked(exc: BaseException) -> bool:
    if exc.__class__.__name__ == "P115RiskControlError":
        return True
    return "限制分享" in str(exc)


class CmsCloudDataIndex:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def direct_302_domain(self) -> str:
        """Read CMS DIRECT_115_302_DOMAIN from cms_config.core.strm."""
        if not self.db_path.is_file():
            return ""
        try:
            with sqlite_connection(
                f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, read_only=True, row_factory=sqlite3.Row
            ) as conn:
                row = conn.execute("SELECT config_json FROM cms_config WHERE key = 'core' LIMIT 1").fetchone()
            if row is None:
                return ""
            config = json.loads(row["config_json"] or "{}")
            return str(((config.get("strm") or {}).get("DIRECT_115_302_DOMAIN") or "")).strip()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return ""

    def missing_media_strm_candidates(self, host_media_root: str | Path, limit: int = 200) -> list[dict[str, str]]:
        """Return cloud_data STRM rows whose local .strm file is missing on the host.

        Only action='STRM' AND status=1 rows under /media/ are considered.
        """
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 200
        host_root = Path(host_media_root)
        if not self.db_path.is_file():
            return []
        candidates: list[dict[str, str]] = []
        try:
            with sqlite_connection(
                f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, read_only=True, row_factory=sqlite3.Row
            ) as conn:
                rows = conn.execute(
                    """
                    SELECT fid, name, pick_code, local_path
                    FROM cloud_data
                    WHERE action = 'STRM' AND status = 1 AND local_path LIKE ?
                    """,
                    (_MEDIA_LOCAL_PATH_PREFIX + "%",),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []
        for row in rows:
            expected = self._expected_media_strm_path(row, host_root)
            if expected is None:
                continue
            if expected.exists():
                continue
            candidates.append(
                {
                    "fid": str(row["fid"] or ""),
                    "name": str(row["name"] or ""),
                    "pick_code": str(row["pick_code"] or ""),
                    "expected_path": str(expected),
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def repair_missing_media_strms(
        self,
        host_media_root: str | Path,
        direct_domain: str = "",
        limit: int = 200,
        dry_run: bool = False,
    ) -> int:
        """Regenerate missing media-library .strm files from CMS direct links."""
        domain = str(direct_domain or "").strip().rstrip("/")
        if not domain or not self.db_path.is_file():
            return 0
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 200
        repaired = 0
        for candidate in self.missing_media_strm_candidates(host_media_root, limit=limit):
            pick_code = str(candidate.get("pick_code") or "").strip()
            name = str(candidate.get("name") or "").strip()
            if not pick_code or not name:
                continue
            content = f"{domain}/d/{pick_code}.mkv?/{name}"
            expected = Path(candidate["expected_path"])
            if dry_run:
                repaired += 1
                continue
            try:
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_text(content, encoding="utf-8")
                repaired += 1
            except OSError:
                continue
        return repaired

    def missing_share_strm_holes(self, host_media_root: str | Path, limit: int = 200) -> list[dict[str, str]]:
        """STRM rows whose library file is gone and no same-episode STRM remains.

        Skips a missing directory (CMS path drift) and a season that already has
        another filename for the same SxxExx. Does not create directories.
        """
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 200
        host_root = Path(host_media_root)
        if not self.db_path.is_file():
            return []
        try:
            with sqlite_connection(
                f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, read_only=True, row_factory=sqlite3.Row
            ) as conn:
                rows = conn.execute(
                    """
                    SELECT fid, name, pick_code, local_path
                    FROM cloud_data
                    WHERE action = 'STRM' AND status = 1 AND local_path LIKE ?
                    """,
                    (_MEDIA_LOCAL_PATH_PREFIX + "%",),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []
        holes: list[dict[str, str]] = []
        for row in rows:
            expected = self._expected_media_strm_path(row, host_root)
            if expected is None or expected.exists() or not expected.parent.is_dir():
                continue
            name = str(row["name"] or "")
            episode = _episode_key(name)
            if episode and _dir_has_episode(expected.parent, episode):
                continue
            holes.append(
                {
                    "fid": str(row["fid"] or ""),
                    "name": name,
                    "pick_code": str(row["pick_code"] or ""),
                    "expected_path": str(expected),
                }
            )
            if len(holes) >= limit:
                break
        return holes

    def repair_missing_share_strms(
        self,
        host_media_root: str | Path,
        share_factory,
        domain: str = "",
        limit: int = 200,
        dry_run: bool = False,
    ) -> int:
        """Create a permanent 115 share and write a /s/ STRM for each hole.

        share_factory(fid) must return share_code and receive_code. Direct /d/
        files are not written: the CMS guard deletes those.
        """
        fallback = str(domain or "").strip().rstrip("/")
        repaired = 0
        grouped: dict[str, list[tuple[Path, str, str]]] = defaultdict(list)
        for hole in self.missing_share_strm_holes(host_media_root, limit=limit):
            expected = Path(hole["expected_path"])
            fid = str(hole.get("fid") or "").strip()
            name = str(hole.get("name") or "").strip()
            if not fid or not name or not expected.parent.is_dir():
                continue
            grouped[str(expected.parent)].append((expected, fid, name))
        for items in grouped.values():
            used_domain = _share_domain_from_dir(items[0][0].parent) or fallback
            if not used_domain:
                continue
            if dry_run:
                repaired += len(items)
                continue
            # ponytail: one share per season directory. Per-file shares trip 115's share limit.
            try:
                share = share_factory(",".join(fid for _path, fid, _name in items)) or {}
            except Exception as exc:
                if _share_create_blocked(exc):
                    raise
                continue
            code = str(share.get("share_code") or "").strip()
            receive = str(share.get("receive_code") or "").strip()
            if not code or not receive:
                continue
            for expected, fid, name in items:
                try:
                    if not expected.parent.is_dir():
                        continue
                    expected.write_text(
                        share_strm_line(used_domain, code, receive, fid, name),
                        encoding="utf-8",
                    )
                except OSError:
                    continue
                repaired += 1
        return repaired

    @staticmethod
    def _expected_media_strm_path(row: sqlite3.Row, host_root: Path) -> Path | None:
        local_path = str(row["local_path"] or "").strip()
        name = str(row["name"] or "").strip()
        if not local_path.startswith(_MEDIA_LOCAL_PATH_PREFIX) or not name:
            return None
        strm_name = _media_strm_name(name)
        if not strm_name:
            return None
        relative = local_path[len(_MEDIA_LOCAL_PATH_PREFIX):].strip("/")
        # Reject path traversal: a cloud_data row must stay under the media
        # root. safe_resolve would otherwise let a "../.." local_path escape
        # and write .strm files anywhere on the host.
        parts = [part for part in relative.split("/") if part]
        if any(part in {"..", "."} or "\\" in part or "\x00" in part for part in parts):
            return None
        expected = safe_resolve(host_root / Path(*parts) / strm_name)
        try:
            expected.relative_to(safe_resolve(host_root))
        except ValueError:
            return None
        return expected

    def has_file_id(self, file_id: str) -> bool:
        file_id = str(file_id or "").strip()
        if not file_id or not self.db_path.is_file():
            return False
        try:
            with sqlite_connection(
                f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, read_only=True
            ) as conn:
                return conn.execute("SELECT 1 FROM cloud_data WHERE fid = ? LIMIT 1", (file_id,)).fetchone() is not None
        except (OSError, sqlite3.Error):
            return False

    def folder_contains_cloud_output(self, folder: dict[str, Any], cloud_output_file_ids: list[str]) -> bool:
        """Whether a resolved media folder actually contains this task's cloud output.

        CMS renames and moves the downloaded file after the worker polls, so an
        organizing search that raced ahead of CMS can resolve an unrelated
        folder.  Anchor ownership to the task's own 115 file ids: the file row
        keeps its fid across CMS moves, so a folder is owned by the task when
        one of those ids sits inside it (either directly via
        ``direct_file_id`` or as a descendant).  An empty id list or an
        unavailable index returns True to keep legacy behaviour.
        """
        ids = [str(value).strip() for value in (cloud_output_file_ids or []) if str(value).strip()]
        if not ids:
            return True
        folder_id = str((folder or {}).get("file_id") or "").strip()
        if folder_id and str((folder or {}).get("direct_file_id") or "").strip() in ids:
            return True
        if not self.db_path.is_file():
            return True
        try:
            with sqlite_connection(
                f"{self.db_path.resolve().as_uri()}?mode=ro",
                uri=True,
                read_only=True,
                row_factory=sqlite3.Row,
            ) as conn:
                for file_id in ids:
                    row = conn.execute(
                        "SELECT fid, pid, name, is_dir FROM cloud_data WHERE fid = ? LIMIT 1",
                        (file_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    seen: set[str] = set()
                    while row:
                        fid = str(row["fid"] or "").strip()
                        if not fid or fid in seen:
                            break
                        seen.add(fid)
                        if fid == folder_id:
                            return True
                        parent_id = str(row["pid"] or "").strip()
                        if not parent_id:
                            break
                        row = conn.execute(
                            "SELECT fid, pid, name, is_dir FROM cloud_data WHERE fid = ? LIMIT 1",
                            (parent_id,),
                        ).fetchone()
        except (OSError, sqlite3.Error):
            return True
        return False

    def folder_for_direct_strm(self, source: Path, tmdb_id: str) -> dict[str, str] | None:
        tmdb_id = str(tmdb_id or "").strip()
        if not tmdb_id or not self.db_path.is_file() or not source.is_dir():
            return None
        candidates: list[tuple[float, Path]] = []
        for strm_path in source.rglob("*.strm"):
            try:
                candidates.append((strm_path.stat().st_mtime, strm_path))
            except OSError:
                continue
        for _mtime, strm_path in sorted(candidates, key=lambda item: item[0], reverse=True):
            pickcode = self._direct_pickcode(strm_path)
            if not pickcode:
                continue
            folder = self._folder_for_pickcode(pickcode, tmdb_id)
            if folder:
                folder["direct_relative_path"] = str(strm_path.relative_to(source))
                return folder
        return None

    def folder_for_cloud_output_name(self, file_name: str, started_at: float = 0) -> dict[str, str] | None:
        """Resolve a completed cloud-download file to its CMS media folder."""
        name = Path(str(file_name or "").strip().replace("\\", "/")).name
        if not name or not self.db_path.is_file():
            return None
        try:
            with sqlite_connection(
                f"{self.db_path.resolve().as_uri()}?mode=ro",
                uri=True,
                read_only=True,
                row_factory=sqlite3.Row,
            ) as conn:
                rows = conn.execute(
                    "SELECT fid, pid, name, is_dir FROM cloud_data WHERE name = ? ORDER BY fid",
                    (name,),
                ).fetchall()
                folder = self._unique_media_folder(conn, rows)
                if folder:
                    return folder
                try:
                    started_at = float(started_at or 0)
                except (TypeError, ValueError):
                    started_at = 0
                if started_at <= 0:
                    return None
                rows = conn.execute(
                    """
                    SELECT fid, pid, name, is_dir, f_modify_time
                    FROM cloud_data
                    WHERE is_dir = 0 AND f_modify_time BETWEEN ? AND ?
                    ORDER BY f_modify_time DESC, fid DESC
                    """,
                    (started_at - 300, started_at + 3600),
                ).fetchall()
                marker = _SEASON_EPISODE_RE.search(name)
                if marker:
                    marker_text = marker.group(0).lower()
                    rows = [
                        row
                        for row in rows
                        if marker_text in str(row["name"] or "").lower()
                    ]
                return self._unique_media_folder(conn, rows)
        except (OSError, sqlite3.Error):
            return None

    @staticmethod
    def _unique_media_folder(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, str] | None:
        matches: dict[str, dict[str, str]] = {}
        for row in rows:
            file_id = str(row["fid"] or "").strip()
            if not file_id:
                continue
            folder = CmsCloudDataIndex._folder_for_row(conn, row)
            if folder:
                matches.setdefault(folder["file_id"], folder)
        return next(iter(matches.values())) if len(matches) == 1 else None

    @staticmethod
    def _direct_pickcode(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        match = _DIRECT_PICKCODE_RE.search(text)
        return match.group(1) if match else ""

    def _folder_for_pickcode(self, pickcode: str, tmdb_id: str) -> dict[str, str] | None:
        try:
            with sqlite_connection(
                f"{self.db_path.resolve().as_uri()}?mode=ro",
                uri=True,
                read_only=True,
                row_factory=sqlite3.Row,
            ) as conn:
                row = conn.execute(
                    "SELECT fid, pid, name, is_dir FROM cloud_data WHERE pick_code = ? LIMIT 1",
                    (pickcode,),
                ).fetchone()
                return self._folder_for_row(conn, row, tmdb_id=tmdb_id) if row else None
        except (OSError, sqlite3.Error):
            return None
        return None

    @staticmethod
    def _folder_for_row(conn: sqlite3.Connection, row: sqlite3.Row, tmdb_id: str = "") -> dict[str, str] | None:
        try:
            is_direct_file = not int(row["is_dir"] or 0)
        except (TypeError, ValueError):
            is_direct_file = False
        direct_file_id = str(row["fid"] or "").strip() if is_direct_file else ""
        direct_file_name = str(row["name"] or "").strip() if is_direct_file else ""
        direct_parent_id = str(row["pid"] or "").strip() if is_direct_file else ""
        seen: set[str] = set()
        while row:
            fid = str(row["fid"] or "").strip()
            if not fid or fid in seen:
                return None
            seen.add(fid)
            name = str(row["name"] or "").strip()
            row_tmdb = extract_tmdb_id_from_name(name)
            try:
                is_dir = int(row["is_dir"] or 0)
            except (TypeError, ValueError):
                is_dir = 0
            if is_dir and row_tmdb and (not tmdb_id or row_tmdb == tmdb_id):
                return {
                    "file_id": fid,
                    "file_name": name,
                    "parent_id": str(row["pid"] or "").strip(),
                    "direct_file_id": direct_file_id,
                    "direct_file_name": direct_file_name,
                    "direct_parent_id": direct_parent_id,
                }
            parent_id = str(row["pid"] or "").strip()
            if not parent_id:
                return None
            row = conn.execute(
                "SELECT fid, pid, name, is_dir FROM cloud_data WHERE fid = ? LIMIT 1",
                (parent_id,),
            ).fetchone()
        return None
