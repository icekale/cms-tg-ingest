"""只对本次改动的行跑 pyright：存量类型债放行，新代码引入的必须拦住。

    python3 scripts/typecheck_diff.py <基准提交>

基准提交由 CI 事件给出（push 用 github.event.before，PR 用
github.event.pull_request.base.sha）。脚本会取它与 HEAD 的 merge-base，
再按统一 diff 算出"新增/修改行"，只在这些行上判 pyright 的 error。

为什么按行而不是按文件：self_share.py / bridge.py / app/web.py 这类核心文件
本来就带着存量报错（全仓约 400 条，多为流程精度与注解噪声，抽查确认非真 bug），
按文件判会让任何一次改动都失败；按行判则改一行不影响存量，同时新写的行不许
再欠债。`-U0` 让每个 hunk 只含真实改动行，行号取新文件一侧，删掉的行不参与。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PYRIGHT_VERSION = "1.1.414"
PYTHON_VERSION = "3.12"  # 与 Dockerfile 的 python:3.12-alpine 及 ci.yml 一致
# 测试文件不纳入：测试一旦有 None 这类问题会当场炸掉，pyright 带不来额外保障，
# 而本仓 fixture（如 find_by_id 返回 dict | None）会持续造噪声。
SKIPPED_PREFIXES = ("tests/",)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text: str) -> dict[str, set[int]]:
    """从统一 diff 文本取出「新文件一侧的行号」集合，按文件分组。

    `-U0` 下没有上下文行，hunk 里的 `+` 行就是改动行本身；纯删除的 hunk
    计数为 0（`range(start, start)` 为空），会被最后的过滤丢掉。
    """
    result: dict[str, set[int]] = {}
    path = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/"):]
            result.setdefault(path, set())
            continue
        match = _HUNK_RE.match(line)
        if match and path:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            result[path].update(range(start, start + count))
    return {
        name: lines
        for name, lines in result.items()
        if lines and not name.startswith(SKIPPED_PREFIXES)
    }


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def changed_lines(base: str) -> dict[str, set[int]]:
    """相对基准提交的新增/修改行；不传 HEAD 即包含尚未提交的改动。"""
    fork = ""
    try:
        fork = git("merge-base", base, "HEAD")
    except subprocess.CalledProcessError:
        return {}
    if not fork:
        return {}
    diff = subprocess.run(
        ["git", "diff", "-U0", "--diff-filter=ACMR", fork, "--", "*.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    return parse_hunks(diff)


def resolve_base(raw: str) -> str:
    """空/全零（新分支首次推送）时退回 HEAD^，都没有就返回空串。"""
    base = (raw or "").strip()
    if not base or not base.strip("0"):
        for candidate in ("HEAD^",):
            try:
                return git("rev-parse", "--verify", candidate)
            except subprocess.CalledProcessError:
                continue
        return ""
    return base


def run_pyright(paths: list[str]) -> dict:
    proc = subprocess.run(
        [
            "npx", "-y", f"pyright@{PYRIGHT_VERSION}",
            "--outputjson", "--pythonversion", PYTHON_VERSION, *paths,
        ],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write((proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-2000:] + "\n")
        raise SystemExit("pyright 输出无法解析（npx / 网络问题？）")


def relative(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), os.getcwd())


def main(argv: list[str]) -> int:
    base = resolve_base(argv[1] if len(argv) > 1 else "")
    if not base:
        print("没有可用的基准提交，跳过类型检查")
        return 0
    targets = changed_lines(base)
    if not targets:
        print("本次没有改动的 Python 行，跳过类型检查")
        return 0
    report = run_pyright(sorted(targets))
    hits = []
    for item in report.get("generalDiagnostics") or []:
        if str(item.get("severity")) != "error":
            continue
        name = relative(str(item.get("file") or ""))
        if name not in targets:
            continue
        start = ((item.get("range") or {}).get("start") or {})
        line = int(start.get("line", -1)) + 1
        if line in targets[name]:
            message = str(item.get("message") or "").splitlines()[0]
            rule = str(item.get("rule") or "")
            hits.append(f"{name}:{line}: {message} [{rule}]")
    if not hits:
        print(f"改动行类型检查通过（{len(targets)} 个文件，基准 {base[:12]}）")
        return 0
    print(f"改动的行上出现 {len(hits)} 条类型错误：")
    for hit in hits:
        print("  " + hit)
    print("\n存量报错（未改动的行）已放行；上面这些属于本次引入，请修掉后重跑。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
