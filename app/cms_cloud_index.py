"""Read CMS cloud_data metadata without calling 115."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.media.classify import extract_tmdb_id_from_name


_DIRECT_PICKCODE_RE = re.compile(r"/d/([A-Za-z0-9]+)(?:\.[^/?\s]+)?(?:[?\s/]|$)")


class CmsCloudDataIndex:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

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
                direct_file_id = str(row["fid"] or "").strip() if row else ""
                seen: set[str] = set()
                while row:
                    fid = str(row["fid"] or "").strip()
                    if not fid or fid in seen:
                        return None
                    seen.add(fid)
                    name = str(row["name"] or "").strip()
                    if int(row["is_dir"] or 0) and extract_tmdb_id_from_name(name) == tmdb_id:
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
        except (OSError, sqlite3.Error):
            return None
        return None
