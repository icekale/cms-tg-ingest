"""资源版本质量解析与洗版（upgrade）判定。

从资源/任务标题解析分辨率（2160p/1080p/...）与片源类型（REMUX/BluRay/
WEB-DL/HDTV/...），并判定一个新版本是否严格优于现有版本。严格比较防止
同质量资源被反复解锁浪费积分；跨分辨率与同分辨率换源都算洗版。

任务质量是按需从标题计算的（title / received_title / own_share_file_name
等字段在任务创建后即稳定），不额外写 metadata、不侵入工作流状态机。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# 分辨率分值与 hdhive_subscriptions.resolution_score 保持同一量纲。
_RESOLUTION_SCORES: dict[str, int] = {
    "8k": 4320,
    "4k": 2160,
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "576p": 576,
    "480p": 480,
}

# 片源优先级：REMUX > BluRay 原盘/压缩 > WEB-DL > WEBRip/HDTV > 其它。
# (规范化名, rank, 匹配变体)；按 rank 从高到低评估，命中即返回——
# "web-dl" 先于 "web" 被检查，避免子串误匹配。
_SOURCE_TOKENS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("remux", 5, ("remux",)),
    ("blu-ray", 4, ("blu-ray", "bluray", "blu", "bdrip", "bdmv", "bd")),
    ("web-dl", 3, ("web-dl", "webdl", "web-dl.")),
    ("webrip", 2, ("webrip",)),
    ("web", 2, ("web", "hmax", "nf", "dsnp", "amzn")),
    ("hdtv", 1, ("hdtv",)),
)

_RESOLUTION_RE = re.compile(r"(8k|4k|2160p|1440p|1080p|720p|576p|480p)", re.IGNORECASE)
_SOURCE_RES = tuple(
    (token, rank, tuple(re.compile(rf"\b{re.escape(variant)}\b", re.IGNORECASE) for variant in variants))
    for token, rank, variants in _SOURCE_TOKENS
)


@dataclass(frozen=True)
class ReleaseQuality:
    resolution: str
    resolution_score: int
    source: str
    source_rank: int

    @property
    def label(self) -> str:
        parts = []
        if self.resolution:
            parts.append(self.resolution.upper())
        if self.source:
            parts.append(self.source.upper())
        return " · ".join(parts) or "未知质量"

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "resolution_score": self.resolution_score,
            "source": self.source,
            "source_rank": self.source_rank,
            "label": self.label,
        }


def parse_release_quality(text: str) -> ReleaseQuality | None:
    """从资源名解析质量；解析不出任何维度返回 None。"""
    value = str(text or "")
    if not value.strip():
        return None
    resolution = ""
    resolution_score = 0
    match = _RESOLUTION_RE.search(value)
    if match:
        resolution = match.group(1).lower()
        resolution_score = _RESOLUTION_SCORES.get(resolution, 0)
    source = ""
    source_rank = -1
    for token, rank, patterns in _SOURCE_RES:
        if any(pattern.search(value) for pattern in patterns):
            source = token
            source_rank = rank
            break
    if not resolution and not source:
        return None
    return ReleaseQuality(resolution, resolution_score, source, source_rank)


def quality_from_names(*candidates: Any) -> ReleaseQuality | None:
    """依次尝试一组候选标题，返回第一个能解析出质量的结果。"""
    for value in candidates:
        parsed = parse_release_quality(str(value or ""))
        if parsed is not None:
            return parsed
    return None


def is_upgrade(old: ReleaseQuality | None, new: ReleaseQuality | None) -> bool:
    """严格更优：分辨率明显更高，或同分辨率下片源等级更高。

    任一版本解析失败返回 False（无法判定时不动现有库内容）。
    """
    if old is None or new is None:
        return False
    if new.resolution_score > old.resolution_score:
        return True
    if new.resolution_score == old.resolution_score and old.resolution_score > 0:
        return new.source_rank > old.source_rank
    if old.resolution_score == 0 and new.source_rank > old.source_rank:
        # 双方都无分辨率标记时仅按片源判定（如 WEB-DL → REMUX 目录包）。
        return True
    return False


def upgrade_reason(old: ReleaseQuality | None, new: ReleaseQuality | None) -> str:
    if old is None or new is None:
        return ""
    if new.resolution_score > old.resolution_score:
        return f"分辨率提升 {old.label} → {new.label}"
    if new.source_rank > old.source_rank:
        return f"片源提升 {old.label} → {new.label}"
    return ""


def normalize_strm_base(name: str) -> str:
    """strm 文件名去掉分辨率 token 后的归一化基准，用于识别同一集的不同版本。"""
    value = str(name or "").strip().lower()
    if value.endswith(".strm"):
        value = value[: -len(".strm")]
    value = _RESOLUTION_RE.sub(" ", value)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def remove_superseded_strms(dest_path: str, older_than: float, *, log=None) -> int:
    """洗版收尾：删除目录里同一集旧版本的 strm，每组（归一化同名）保留最新。

    只删 mtime 严格早于 ``older_than``（通常为新任务创建时间）的文件——
    绝不碰洗版任务刚写入的新版本；早于该时间的同名旧版本全部清掉。
    单文件组、无法解析基准名的文件一律不动。返回删除数量，任何异常
    由调用方决定语义；这里只记日志不打断。
    """
    import logging
    from pathlib import Path

    logger = log or logging.getLogger("cms-tg-ingest")
    try:
        root = Path(dest_path)
        if not root.is_dir():
            return 0
        groups: dict[str, list[Path]] = {}
        for path in root.rglob("*.strm"):
            try:
                base = normalize_strm_base(path.name)
            except Exception:
                continue
            if base:
                groups.setdefault(base, []).append(path)
        removed = 0
        for paths in groups.values():
            if len(paths) < 2:
                continue
            ordered = sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)
            for old in ordered[1:]:
                try:
                    if old.stat().st_mtime >= older_than:
                        continue
                    old.unlink()
                    removed += 1
                    logger.warning("Wash supersede removed old-resolution STRM: %s", old)
                except OSError:
                    logger.warning("Wash supersede failed to remove %s", old, exc_info=True)
        return removed
    except Exception:
        logger.warning("Wash supersede cleanup failed for %s", dest_path, exc_info=True)
        return 0
