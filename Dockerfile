FROM node:22-alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY bridge.py doctor.py /app/
COPY app/ /app/app/
COPY scripts/ /app/scripts/
COPY --from=frontend-build /frontend/dist /app/frontend/dist

VOLUME ["/data"]
CMD ["python", "/app/bridge.py"]
