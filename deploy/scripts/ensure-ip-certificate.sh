#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib.sh
. "${script_dir}/lib.sh"

require_command docker
require_command python3
require_env_file

public_host="$(env_value ADX_PUBLIC_HOST)"
acme_email="$(env_value ADX_ACME_EMAIL)"

if ! is_ipv4 "${public_host}"; then
  echo "IP certificate mode requires ADX_PUBLIC_HOST to be an IPv4 address." >&2
  exit 1
fi

set_env_value ADX_CADDY_CONFIG Caddyfile.ip-bootstrap
compose up -d --force-recreate caddy

set -- certonly \
  --non-interactive \
  --agree-tos \
  --keep-until-expiring \
  --preferred-profile shortlived \
  --webroot \
  --webroot-path /var/www/certbot \
  --ip-address "${public_host}" \
  --cert-name "${public_host}"

if [ -n "${acme_email}" ]; then
  set -- "$@" --email "${acme_email}" --no-eff-email
else
  set -- "$@" --register-unsafely-without-email
fi

compose run --rm --no-deps certbot "$@"

set_env_value ADX_CADDY_CONFIG Caddyfile.ip
compose up -d --force-recreate caddy

if command -v systemctl >/dev/null 2>&1; then
  if [ "$(id -u)" -eq 0 ] || sudo -n true >/dev/null 2>&1; then
    sh "${script_dir}/install-ip-renewal-timer.sh"
  else
    echo "Run sudo sh deploy/scripts/install-ip-renewal-timer.sh to automate renewal." >&2
  fi
fi

echo "IP certificate is active. Verify the HTTPS endpoint before onboarding users."
