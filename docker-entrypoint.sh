#!/usr/bin/env sh
set -e
# Cloud Run define PORT (ex.: 8080 ou 8000). Docker local sem PORT → 8000.
export PORT="${PORT:-8000}"
cd /app
echo "[pp-api entrypoint] PORT=$PORT cwd=$(pwd)"

# Opcional: importar app duas vezes atrasa o arranque no Cloud Run (CPU limitada).
# Para diagnosticar falhas de env: definir ENTRYPOINT_PREFLIGHT=1 na revisão.
if [ "${ENTRYPOINT_PREFLIGHT:-0}" = 1 ]; then
  if ! python -c "from app.main import app as _a; print('[pp-api entrypoint] app.main import OK')"; then
    echo "[pp-api entrypoint] FATAL: falha ao importar app.main — confirme env:"
    echo "  DATABASE_URL, JWT_SECRET (>=32), REFRESH_TOKEN_PEPPER (>=16), FIREBASE_*"
    exit 1
  fi
fi

# --proxy-headers: atrás do proxy do Google (X-Forwarded-Proto / Host).
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips='*'
