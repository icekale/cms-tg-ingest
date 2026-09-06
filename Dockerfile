FROM node:22-alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# AI 助手以 pi coding agent 为基座：从前端构建阶段复用 Node 运行时（同为
# alpine/musl），并固定 pi 版本保证发布可复现。凭据放在数据卷：
# /data/pi/agent/auth.json（PI_CODING_AGENT_DIR 即 agent 目录本身）。
COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-build /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
ENV PI_CODING_AGENT_DIR=/data/pi/agent
RUN apk add --no-cache libstdc++ libgcc \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g --silent @earendil-works/pi-coding-agent@0.83.0 \
    && npm cache clean --force \
    && pi --version

WORKDIR /app
COPY bridge.py doctor.py /app/
COPY app/ /app/app/
COPY scripts/ /app/scripts/
COPY --from=frontend-build /frontend/dist /app/frontend/dist

VOLUME ["/data"]
CMD ["python", "/app/bridge.py"]
