#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

app_module="${ADX_ASGI_APP:-web.api:create_app}"
host="${ADX_API_HOST:-0.0.0.0}"
port="${ADX_API_PORT:-8000}"
forwarded_allow_ips="${ADX_FORWARDED_ALLOW_IPS:-*}"
workers="${ADX_API_WORKERS:-1}"

# Connector control-plane services stay at one worker. The dedicated WSS app
# may use multiple workers because shared leases and fencing own each Device.
exec python -m uvicorn "${app_module}" \
  --factory \
  --host "${host}" \
  --port "${port}" \
  --workers "${workers}" \
  --proxy-headers \
  --forwarded-allow-ips "${forwarded_allow_ips}" \
  --timeout-keep-alive 10 \
  --limit-concurrency "${ADX_API_MAX_CONCURRENCY:-256}" \
  --no-access-log
