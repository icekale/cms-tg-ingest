#!/bin/sh
set -eu

redact_env() {
  env | sort | sed -E 's/^([^=]*(TOKEN|PASSWORD|KEY|COOKIE|SECRET|JCT|SESSDATA)[^=]*)=.*/\1=<redacted>/I'
}

echo "# cms-tg-ingest diagnostics"
echo "## time"
date -Iseconds 2>/dev/null || date

echo "## python"
python --version 2>&1 || true

echo "## doctor"
python /app/doctor.py || true

echo "## env-redacted"
redact_env

echo "## data-files"
find /data -maxdepth 1 -type f -printf '%f %s bytes\n' 2>/dev/null || true
