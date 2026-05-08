#!/usr/bin/env sh
set -e
# Cloud Run define PORT (ex.: 8080 ou 8000). Docker local sem PORT → 8000.
export PORT="${PORT:-8000}"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
