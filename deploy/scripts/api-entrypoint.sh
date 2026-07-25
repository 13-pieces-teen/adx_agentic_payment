#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

app_module="${ADX_ASGI_APP:-web.api:create_app}"
host="${ADX_API_HOST:-0.0.0.0}"
port="${ADX_API_PORT:-8000}"
forwarded_allow_ips="${ADX_FORWARDED_ALLOW_IPS:-*}"

# Keep a single worker: the current WebSocket connection registry is
# process-local while durable state lives in PostgreSQL.
exec python -m uvicorn "${app_module}" \
  --factory \
  --host "${host}" \
  --port "${port}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips "${forwarded_allow_ips}" \
  --timeout-keep-alive 10 \
  --limit-concurrency "${ADX_API_MAX_CONCURRENCY:-64}" \
  --no-access-log
