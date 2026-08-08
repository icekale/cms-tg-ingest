#!/usr/bin/env bash
# update-cms.sh — 安全升级 CMS 容器（含 STRM 守卫验证与自动回滚）。
#
# 用法：
#   ./update-cms.sh <CMS_COMPOSE_DIR> [新版本]
#
#   <CMS_COMPOSE_DIR>  CMS 的 docker-compose 项目目录
#                     （Unraid 上通常是 /boot/config/plugins/compose.manager/projects/CMS）
#   [新版本]          可选。指定则把 image 标签改为 imaliang/cloud-media-sync:<新版本>
#                     后重建；不指定则保持当前标签（用于"当前版本重装验证守卫"）。
#
# 流程：预检守卫文件 → 记录当前标签 → 备份 compose → 切换标签 → pull → up -d
#       → 等容器 running → verify.sh 验证守卫 → 失败自动回滚旧标签并重建。
#
# 设计目标：CMS 固定基线版本（如 0.4.9.1），升级永远显式指定新版本；守卫没装上
# 就自动回滚，绝不把"守卫失效的 CMS"留在线上。与 verify.sh、doctor.py 的
# cms_strm_guard 检查形成"升级闭环 + 日常监控"。

set -eu

CMS_DIR="${1:-}"
NEW_VERSION="${2:-}"
COMPOSE_FILE="docker-compose.yml"
# STRM 守卫文件路径。默认在 CMS compose 目录下找；Unraid 上守卫文件实际位于
# /mnt/user/appdata/cloud-media-sync/config/patches/sitecustomize.py（挂载进容器
# /config/patches），与 compose 项目目录不同，请用 CMS_GUARD_FILE 环境变量指定。
GUARD_FILE="${CMS_GUARD_FILE:-$CMS_DIR/config/patches/sitecustomize.py}"
VERIFY_SCRIPT="$(cd "$(dirname "$0")" && pwd)/verify.sh"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

if [ -z "$CMS_DIR" ]; then
    echo "用法: $0 <CMS_COMPOSE_DIR> [新版本]" >&2
    echo "例如: $0 /boot/config/plugins/compose.manager/projects/CMS 0.4.9.2" >&2
    exit 1
fi
if [ ! -f "$CMS_DIR/$COMPOSE_FILE" ]; then
    fail "找不到 $CMS_DIR/$COMPOSE_FILE（确认传入的是 CMS 的 compose 项目目录）"
fi
if [ ! -f "$GUARD_FILE" ]; then
    fail "找不到 STRM 守卫文件 $GUARD_FILE —— 先部署 sitecustomize.py，或用 CMS_GUARD_FILE 指定路径（Unraid 上通常是 /mnt/user/appdata/cloud-media-sync/config/patches/sitecustomize.py）"
fi
if [ ! -f "$VERIFY_SCRIPT" ]; then
    fail "找不到 verify.sh: $VERIFY_SCRIPT"
fi

cd "$CMS_DIR"

# 提取目标服务名（services: 下的第一个顶层服务键）
SERVICE=$(awk '/^services:/{in_services=1; next} in_services && /^  [A-Za-z0-9_-]+:/{name=$1; sub(/:$/,"",name); print name; exit}' "$COMPOSE_FILE")
SERVICE="${SERVICE:-cloud-media-sync}"
echo "==> 目标服务: $SERVICE"

# 仅在目标服务块内替换 image 行（多服务 compose 时不影响其它服务）
set_image() {
    local new_image="$1"
    awk -v svc="$SERVICE" -v img="$new_image" '
        /^services:/ { in_services=1; print; next }
        in_services && /^[^ ]/ { in_services=0 }
        in_services && /^  [A-Za-z0-9_-]+:/ {
            name=$1; sub(/:$/, "", name)
            in_target = (name == svc)
            print; next
        }
        in_target && /^    image:/ { sub(/image:.*/, "image: " img) }
        { print }
    ' "$COMPOSE_FILE" > "$COMPOSE_FILE.tmp" && mv "$COMPOSE_FILE.tmp" "$COMPOSE_FILE"
}

# 记录当前 image 标签（回滚基线）
CURRENT_IMAGE=$(grep -E '^[[:space:]]+image:[[:space:]]+' "$COMPOSE_FILE" | head -1 | sed -E 's/^[[:space:]]+image:[[:space:]]*//' | tr -d '"'"'"' ')
if [ -z "$CURRENT_IMAGE" ]; then
    fail "无法从 $COMPOSE_FILE 读取当前 image 标签"
fi
echo "==> 当前 image: $CURRENT_IMAGE"

# 备份 compose
STAMP=$(date +%Y%m%d-%H%M%S)
cp "$COMPOSE_FILE" "$COMPOSE_FILE.bak-$STAMP"
echo "==> 已备份 compose: $COMPOSE_FILE.bak-$STAMP"

# 切换标签（若指定新版本）
TARGET_IMAGE="$CURRENT_IMAGE"
if [ -n "$NEW_VERSION" ]; then
    if echo "$NEW_VERSION" | grep -qE '^[A-Za-z0-9._-]+$'; then
        TARGET_IMAGE="imaliang/cloud-media-sync:$NEW_VERSION"
    else
        TARGET_IMAGE="$NEW_VERSION"
    fi
    set_image "$TARGET_IMAGE"
    echo "==> 切换 image 标签: $CURRENT_IMAGE -> $TARGET_IMAGE"
else
    echo "==> 未指定新版本，保持当前标签（重装验证守卫）"
fi

rollback() {
    echo "==> 回滚: 恢复 image 标签 $CURRENT_IMAGE"
    set_image "$CURRENT_IMAGE" 2>/dev/null || true
    docker compose up -d "$SERVICE" >/dev/null 2>&1 || echo "==> 注意: 回滚重建失败，请手动执行 docker compose up -d" >&2
}

# 拉取新镜像
echo "==> 拉取镜像: $TARGET_IMAGE"
if ! docker compose pull "$SERVICE" 2>&1; then
    rollback
    fail "拉取镜像失败，已回滚到 $CURRENT_IMAGE"
fi

# 重建容器
echo "==> 重建容器"
if ! docker compose up -d "$SERVICE" 2>&1; then
    rollback
    fail "容器重建失败，已回滚到 $CURRENT_IMAGE"
fi

# 等容器 running（最多 60s）
echo "==> 等待容器就绪..."
CONTAINER_NAME=$(grep -E '^[[:space:]]+container_name:[[:space:]]+' "$COMPOSE_FILE" | head -1 | sed -E 's/^[[:space:]]+container_name:[[:space:]]*//' | tr -d '"'"'"' ')
CONTAINER_NAME="${CONTAINER_NAME:-cloud-media-sync}"
for _ in $(seq 1 30); do
    if docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
        break
    fi
    sleep 2
done
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
    rollback
    fail "容器 $CONTAINER_NAME 未能在 60s 内进入 running，已回滚到 $CURRENT_IMAGE"
fi
echo "==> 容器 running"

# 验证守卫（本机模式，无 SSH 参数）
echo "==> 验证 STRM 守卫..."
if "$VERIFY_SCRIPT"; then
    echo "==> 完成: CMS 已更新到 $TARGET_IMAGE，STRM 守卫正常"
    exit 0
fi
rollback
fail "STRM 守卫验证失败（CMS 更新可能改变了内部结构），已自动回滚到 $CURRENT_IMAGE"
