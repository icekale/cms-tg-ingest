"""Read CMS cloud_data metadata without calling 115."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.media.classify import extract_tmdb_id_from_name


_DIRECT_PICKCODE_RE = re.compile(r"/d/([A-Za-z0-9]+)(?:\.[^/?\s]+)?(?:[?\s/]|$)")
_SEASON_EPISODE_RE = re.compile(r"s\d{1,3}e\d{1,3}", re.IGNORECASE)


class CmsCloudDataIndex:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def has_file_id(self, file_id: str) -> bool:
        file_id = str(file_id or "").strip()
        if not file_id or not self.db_path.is_file():
            return False
        try:
            with sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                return conn.execute("SELECT 1 FROM cloud_data WHERE fid = ? LIMIT 1", (file_id,)).fetchone() is not None
        except (OSError, sqlite3.Error):
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
            with sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
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
            with sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
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
        direct_file_id = "" if int(row["is_dir"] or 0) else str(row["fid"] or "").strip()
        seen: set[str] = set()
        while row:
            fid = str(row["fid"] or "").strip()
            if not fid or fid in seen:
                return None
            seen.add(fid)
            name = str(row["name"] or "").strip()
            row_tmdb = extract_tmdb_id_from_name(name)
            if int(row["is_dir"] or 0) and row_tmdb and (not tmdb_id or row_tmdb == tmdb_id):
                return {
                    "file_id": fid,
                    "file_name": name,
                    "parent_id": str(row["pid"] or "").strip(),
                    "direct_file_id": direct_file_id,
                }
            parent_id = str(row["pid"] or "").strip()
            if not parent_id:
                return None
            row = conn.execute(
                "SELECT fid, pid, name, is_dir FROM cloud_data WHERE fid = ? LIMIT 1",
                (parent_id,),
            ).fetchone()
        return None
