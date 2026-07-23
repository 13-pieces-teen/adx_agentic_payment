#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib.sh
. "${script_dir}/lib.sh"

require_command docker
require_env_file

tls_mode="$(env_value ADX_TLS_MODE)"
public_host="$(env_value ADX_PUBLIC_HOST)"
build_connector_artifacts="$(env_value ADX_BUILD_CONNECTOR_ARTIFACTS)"
[ -n "${build_connector_artifacts}" ] || build_connector_artifacts=true

case "${tls_mode}" in
  domain)
    set_env_value ADX_CADDY_CONFIG Caddyfile.domain
    ;;
  ip)
    if ! is_ipv4 "${public_host}"; then
      echo "ADX_TLS_MODE=ip requires ADX_PUBLIC_HOST to be an IPv4 address." >&2
      exit 1
    fi
    if ! compose run --rm --no-deps certbot certificates \
      --cert-name "${public_host}" 2>/dev/null | grep -q "Certificate Name: ${public_host}"; then
      set_env_value ADX_CADDY_CONFIG Caddyfile.ip-bootstrap
    fi
    ;;
  http)
    set_env_value ADX_CADDY_CONFIG Caddyfile.ip-bootstrap
    ;;
  *)
    echo "Unsupported ADX_TLS_MODE: ${tls_mode}" >&2
    exit 1
    ;;
esac

if [ "${build_connector_artifacts}" = "true" ]; then
  sh "${script_dir}/build-connector-artifacts.sh"
else
  sh "${script_dir}/build-connector-artifacts.sh" --verify-only
fi

compose config --quiet
compose pull postgres caddy certbot
compose build --pull api web migrate
compose up -d postgres
compose run --rm migrate
compose up -d api web caddy

if [ "${tls_mode}" = "ip" ]; then
  sh "${script_dir}/ensure-ip-certificate.sh"
fi

compose ps
echo "Deployment converged at $(env_value ADX_PUBLIC_APP_URL)."
