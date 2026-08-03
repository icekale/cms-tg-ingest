#!/usr/bin/env sh
# 宿主机侧 CMS 容器更新脚本：拉取新镜像并重建容器。
# 用法: ./scripts/update-cms-container.sh <CMS_COMPOSE_DIR>
# 例如: ./scripts/update-cms-container.sh /mnt/user/appdata/cloud-media-sync
set -eu

CMS_DIR="${1:-}"
if [ -z "$CMS_DIR" ]; then
  echo "用法: $0 <CMS_COMPOSE_DIR>" >&2
  exit 1
fi

cd "$CMS_DIR"
docker compose pull
docker compose up -d
docker compose ps
