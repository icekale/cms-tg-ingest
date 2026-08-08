#!/usr/bin/env bash
# verify.sh — 检查 CMS 容器的 STRM 删除守卫是否安装并生效。
#
# 用法：
#   ./verify.sh                          # 本机 docker（Unraid 上直接跑）
#   ./verify.sh <ssh-host>               # 通过 SSH 检查（如 root@<unraid-ip>）
#
# 输出：
#   找到 "STRM-GUARD installed on MediaSync.delete_local_file" 日志行 → 通过
#   找不到 → 失败（CMS 可能更新过、模块/方法名变化，守卫静默失效）
#
# 守卫失效时请检查 sitecustomize.py 里的 _SELF_SHARE_URL_RE 和
# _install_guard 的模块/方法名是否与当前 CMS 版本一致。

set -euo pipefail

CONTAINER="${CMS_CONTAINER:-cloud-media-sync}"
# 注意：此 marker 与 sitecustomize.py / doctor.py / web_api.py 保持一致；
# 若自定义 marker，需同步修改这 4 处。
MARKER="STRM-GUARD installed on MediaSync.delete_local_file"
SSH_TARGET="${1:-}"

run_on_host() {
    if [[ -n "$SSH_TARGET" ]]; then
        ssh -o BatchMode=yes -o ConnectTimeout=5 "$SSH_TARGET" "$1"
    else
        bash -c "$1"
    fi
}

echo "==> 检查容器: $CONTAINER"
echo "==> 目标: ${SSH_TARGET:-本机}"

if ! run_on_host "docker ps --format '{{.Names}}' | grep -qx '$CONTAINER'"; then
    echo "FAIL: 容器 $CONTAINER 未运行（检查容器名或 CMS_CONTAINER 环境变量）"
    exit 1
fi

if run_on_host "docker logs $CONTAINER 2>&1 | grep -qF '$MARKER'"; then
    echo "PASS: STRM 删除守卫已安装"
    run_on_host "docker logs $CONTAINER 2>&1 | grep -F '$MARKER' | tail -1"
    exit 0
fi

# 兜底：容器刚重启，守卫 worker 可能还在等模块加载（最多 10 分钟）
# 等待 90 秒覆盖 CMS 冷启动 + sitecustomize worker 安装窗口，避免边缘情况下误报。
echo "WARN: 日志中暂无守卫标记，等待 worker 轮询（最多 90 秒）..."
if run_on_host "timeout 90 sh -c 'until docker logs $CONTAINER 2>&1 | grep -qF \"$MARKER\"; do sleep 2; done'"; then
    echo "PASS: STRM 删除守卫已安装（延迟确认）"
    exit 0
fi

echo "FAIL: 未找到守卫标记。CMS 可能已更新（模块/方法名变化），或 sitecustomize.py 未被 PYTHONPATH 加载。"
echo "      检查: docker logs $CONTAINER | grep -i strm-guard"
exit 1
