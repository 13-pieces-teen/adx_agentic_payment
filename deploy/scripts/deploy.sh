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
enable_hosted_runtime="$(env_value ADX_ENABLE_HOSTED_RUNTIME)"
[ -n "${enable_hosted_runtime}" ] || enable_hosted_runtime=false
enable_arena_worker="$(env_value ADX_ENABLE_ARENA_WORKER)"
[ -n "${enable_arena_worker}" ] || enable_arena_worker=false

case "${enable_hosted_runtime}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_HOSTED_RUNTIME must be true or false." >&2
    exit 1
    ;;
esac
case "${enable_arena_worker}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_ARENA_WORKER must be true or false." >&2
    exit 1
    ;;
esac

if [ "${enable_hosted_runtime}" = "true" ]; then
  if [ "$(env_value ADX_HOSTED_AGENTS_ENABLED)" != "true" ]; then
    echo "Hosted runtime requires ADX_HOSTED_AGENTS_ENABLED=true." >&2
    exit 1
  fi
  if [ "$(env_value ADX_TENCENT_SSM_IAM_VERIFIED)" != "true" ]; then
    echo "Hosted runtime requires verified writer/reader/controller SSM IAM." >&2
    exit 1
  fi
fi

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
compose build --pull api web migrate provision-db-roles
if [ "${enable_hosted_runtime}" = "true" ]; then
  compose --profile hosted build --pull hosted-worker credential-controller
fi
if [ "${enable_arena_worker}" = "true" ]; then
  compose --profile arena build --pull arena-worker
fi
compose up -d postgres
compose run --rm migrate
compose up -d api web caddy
if [ "${enable_hosted_runtime}" = "true" ]; then
  compose --profile hosted up -d hosted-worker credential-controller
fi
if [ "${enable_arena_worker}" = "true" ]; then
  compose --profile arena up -d arena-worker
fi

if [ "${tls_mode}" = "ip" ]; then
  sh "${script_dir}/ensure-ip-certificate.sh"
fi

compose ps
echo "Deployment converged at $(env_value ADX_PUBLIC_APP_URL)."
