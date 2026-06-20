FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY bridge.py doctor.py /app/
COPY scripts/ /app/scripts/

VOLUME ["/data"]
CMD ["python", "/app/bridge.py"]
