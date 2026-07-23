#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib.sh
. "${script_dir}/lib.sh"

require_command docker
require_env_file

if [ "$(env_value ADX_TLS_MODE)" != "ip" ]; then
  echo "ADX_TLS_MODE is not ip; nothing to renew."
  exit 0
fi

lock_file="/var/lock/adx-ip-cert-renew.lock"
exec 9>"${lock_file}"
if ! flock -n 9; then
  echo "Another IP certificate renewal is already running."
  exit 0
fi

compose run --rm --no-deps certbot renew --quiet
compose exec -T caddy caddy reload \
  --config /etc/caddy/Caddyfile \
  --adapter caddyfile
