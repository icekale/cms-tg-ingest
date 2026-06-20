from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import TaskSnapshot


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    detail: str = ""


def inspect_task_files(task: TaskSnapshot, *, dest_path: str | Path, own_share_code: str = "") -> list[QualityIssue]:
    del task
    dest = Path(dest_path)
    if not dest.exists():
        return [QualityIssue("missing_dest", "目标目录不存在", str(dest))]
    files = sorted(path for path in dest.rglob("*.strm") if path.is_file())
    if not files:
        return [QualityIssue("missing_strm", "目标目录没有 STRM 文件", str(dest))]
    issues: list[QualityIssue] = []
    expected_marker = f"/s/{own_share_code}_1212_" if own_share_code else "/s/"
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if "/d/" in text:
            issues.append(QualityIssue("direct_strm", "发现直链 STRM", str(path)))
        elif expected_marker not in text:
            issues.append(QualityIssue("unexpected_strm", "STRM 不是预期的分享链接", str(path)))
    return issues
