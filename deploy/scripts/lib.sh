#!/bin/sh

set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repo_dir="$(CDPATH='' cd -- "${script_dir}/../.." && pwd)"
compose_file="${repo_dir}/docker-compose.production.yml"
env_file="${ADX_ENV_FILE:-${repo_dir}/deploy/.env}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command is missing: $1" >&2
    exit 1
  fi
}

require_env_file() {
  if [ ! -f "${env_file}" ]; then
    echo "Missing ${env_file}. Run deploy/scripts/generate-env.sh first." >&2
    exit 1
  fi
}

compose() {
  docker compose --env-file "${env_file}" -f "${compose_file}" "$@"
}

env_value() {
  key="$1"
  sed -n "s/^${key}=//p" "${env_file}" | tail -n 1
}

set_env_value() {
  key="$1"
  value="$2"
  temporary="${env_file}.tmp.$$"

  umask 077
  awk -v key="${key}" -v value="${value}" '
    BEGIN { replaced = 0 }
    index($0, key "=") == 1 {
      print key "=" value
      replaced = 1
      next
    }
    { print }
    END {
      if (!replaced) {
        print key "=" value
      }
    }
  ' "${env_file}" > "${temporary}"
  chmod 600 "${temporary}"
  mv "${temporary}" "${env_file}"
}

is_ipv4() {
  python3 - "$1" <<'PY'
import ipaddress
import sys

try:
    value = ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if value.version == 4 else 1)
PY
}
