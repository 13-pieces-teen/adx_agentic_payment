#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib.sh
. "${script_dir}/lib.sh"

usage() {
  echo "Usage: $0 [--http] [--app-url URL] [--invite-output-file PATH] <public-api-domain-or-ip> [acme-email]" >&2
  exit 2
}

http_only=false
app_url=
invite_output_file=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --http)
      http_only=true
      shift
      ;;
    --invite-output-file)
      [ "$#" -ge 2 ] || usage
      invite_output_file=$2
      shift 2
      ;;
    --app-url)
      [ "$#" -ge 2 ] || usage
      app_url=$2
      shift 2
      ;;
    --*)
      usage
      ;;
    *)
      break
      ;;
  esac
done

public_host="${1:-}"
acme_email="${2:-}"
[ -n "${public_host}" ] || usage

if [ -n "${invite_output_file}" ]; then
  invite_parent="$(dirname -- "${invite_output_file}")"
  [ -d "${invite_parent}" ] || {
    echo "Invite output parent directory does not exist: ${invite_parent}" >&2
    exit 1
  }
  invite_output_file="$(
    CDPATH='' cd -- "${invite_parent}" && printf '%s/%s\n' "$PWD" "$(basename -- "${invite_output_file}")"
  )"
  env_parent="$(dirname -- "${env_file}")"
  env_absolute="$(
    CDPATH='' cd -- "${env_parent}" && printf '%s/%s\n' "$PWD" "$(basename -- "${env_file}")"
  )"
  if [ "${invite_output_file}" = "${env_absolute}" ]; then
    echo "Invite output file must be different from ${env_file}." >&2
    exit 1
  fi
fi

if [ -e "${env_file}" ]; then
  echo "${env_file} already exists; refusing to overwrite secrets." >&2
  exit 1
fi
if [ -n "${invite_output_file}" ] && [ -e "${invite_output_file}" ]; then
  echo "${invite_output_file} already exists; refusing to overwrite the invite." >&2
  exit 1
fi

require_command openssl
require_command python3
require_command sha256sum

case "${public_host}" in
  *[!A-Za-z0-9.:-]*|"")
    echo "Public host contains unsupported characters." >&2
    exit 1
    ;;
esac

if is_ipv4 "${public_host}"; then
  tls_mode=ip
  caddy_config=Caddyfile.ip-bootstrap
elif [ "${public_host}" = "localhost" ]; then
  tls_mode=http
  caddy_config=Caddyfile.ip-bootstrap
elif printf '%s' "${public_host}" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$'; then
  tls_mode=domain
  caddy_config=Caddyfile.domain
else
  echo "Expected a public IPv4 address or fully-qualified domain name." >&2
  exit 1
fi

if [ "${http_only}" = "true" ]; then
  tls_mode=http
  caddy_config=Caddyfile.ip-bootstrap
fi

if [ "${tls_mode}" = "http" ]; then
  public_url="http://${public_host}"
  adx_environment=development
  cookie_secure=false
else
  public_url="https://${public_host}"
  adx_environment=production
  cookie_secure=true
fi
if [ -n "${app_url}" ]; then
  public_url="${app_url}"
fi
case "${public_url}" in
  https://*)
    app_host="${public_url#https://}"
    ;;
  http://*)
    if [ "${adx_environment}" = "production" ]; then
      echo "--app-url must use HTTPS in production." >&2
      exit 1
    fi
    app_host="${public_url#http://}"
    ;;
  *)
    echo "--app-url must be an absolute HTTP(S) URL without credentials." >&2
    exit 1
    ;;
esac
case "${app_host}" in
  ""|*[!A-Za-z0-9.:-]*|*@*|*/*|*\?*|*\#*)
    echo "--app-url must contain only a host and optional port." >&2
    exit 1
    ;;
esac

postgres_password="$(openssl rand -hex 32)"
api_database_password="$(openssl rand -hex 32)"
hosted_worker_database_password="$(openssl rand -hex 32)"
arena_core_database_password="$(openssl rand -hex 32)"
credential_controller_database_password="$(openssl rand -hex 32)"
hosted_fingerprint_pepper_b64="$(openssl rand -base64 48 | tr -d '\n')"
session_secret="$(openssl rand -hex 48)"
bootstrap_invite="$(openssl rand -hex 20)"
bootstrap_invite_hash="$(
  printf '%s' "${bootstrap_invite}" | sha256sum | awk '{print $1}'
)"

umask 077
{
  printf 'ADX_TLS_MODE=%s\n' "${tls_mode}"
  printf 'ADX_PUBLIC_HOST=%s\n' "${public_host}"
  printf 'ADX_PUBLIC_APP_URL=%s\n' "${public_url}"
  printf 'ADX_CADDY_CONFIG=%s\n' "${caddy_config}"
  printf '\n'
  printf 'POSTGRES_DB=adx\n'
  printf 'POSTGRES_USER=adx\n'
  printf 'POSTGRES_PASSWORD=%s\n' "${postgres_password}"
  printf 'ADX_API_DATABASE_PASSWORD=%s\n' "${api_database_password}"
  printf 'ADX_HOSTED_WORKER_DATABASE_PASSWORD=%s\n' "${hosted_worker_database_password}"
  printf 'ADX_ARENA_CORE_DATABASE_PASSWORD=%s\n' "${arena_core_database_password}"
  printf 'ADX_CREDENTIAL_CONTROLLER_DATABASE_PASSWORD=%s\n' "${credential_controller_database_password}"
  printf '\n'
  printf 'ADX_CONNECTOR_SESSION_SECRET=%s\n' "${session_secret}"
  printf 'ADX_GITHUB_OAUTH_CLIENT_ID=\n'
  printf 'ADX_GITHUB_OAUTH_CLIENT_SECRET=\n'
  printf 'ADX_GITHUB_OAUTH_STATE_TTL_SECONDS=600\n'
  printf 'ADX_BOOTSTRAP_INVITE_HASH=%s\n' "${bootstrap_invite_hash}"
  printf 'ADX_CONNECTOR_SESSION_TTL_SECONDS=604800\n'
  printf 'ADX_CONNECTOR_COOKIE_NAME=adx_session\n'
  printf 'ADX_CONNECTOR_COOKIE_SECURE=%s\n' "${cookie_secure}"
  printf 'ADX_CONNECTOR_AUTH_RATE_LIMIT_ATTEMPTS=10\n'
  printf 'ADX_CONNECTOR_PAIRING_RATE_LIMIT_ATTEMPTS=60\n'
  printf 'ADX_CONNECTOR_RATE_LIMIT_WINDOW_SECONDS=60\n'
  printf 'ADX_CONNECTOR_MAX_PENDING_PAIRINGS=500\n'
  printf 'ADX_ENV=%s\n' "${adx_environment}"
  printf 'ADX_API_MAX_CONCURRENCY=64\n'
  printf '\n'
  printf 'ADX_HOSTED_AGENTS_ENABLED=false\n'
  printf 'ADX_ARENA_CORE_ENABLED=true\n'
  printf 'ADX_ENABLE_HOSTED_RUNTIME=false\n'
  printf 'ADX_HOSTED_WORKER_TASK_CONCURRENCY=5\n'
  printf 'ADX_HOSTED_FINGERPRINT_PEPPER_B64=%s\n' "${hosted_fingerprint_pepper_b64}"
  printf 'ADX_HOSTED_FINGERPRINT_PEPPER_VERSION=1\n'
  printf 'ADX_TENCENT_SSM_REGION=ap-guangzhou\n'
  printf 'ADX_TENCENT_SSM_RECOVERY_WINDOW_DAYS=0\n'
  printf 'ADX_TENCENT_SSM_IAM_VERIFIED=false\n'
  printf 'ADX_TENCENT_SSM_WRITER_SECRET_ID=\n'
  printf 'ADX_TENCENT_SSM_WRITER_SECRET_KEY=\n'
  printf 'ADX_TENCENT_SSM_WRITER_TOKEN=\n'
  printf 'ADX_TENCENT_SSM_READER_SECRET_ID=\n'
  printf 'ADX_TENCENT_SSM_READER_SECRET_KEY=\n'
  printf 'ADX_TENCENT_SSM_READER_TOKEN=\n'
  printf 'ADX_TENCENT_SSM_CONTROLLER_SECRET_ID=\n'
  printf 'ADX_TENCENT_SSM_CONTROLLER_SECRET_KEY=\n'
  printf 'ADX_TENCENT_SSM_CONTROLLER_TOKEN=\n'
  printf 'ADX_OPENAI_COMPATIBLE_ENDPOINT=\n'
  printf 'ADX_OPENAI_COMPATIBLE_MODELS=\n'
  printf '\n'
  printf 'ADX_ENABLE_ARENA_WORKER=true\n'
  printf 'ADX_ARENA_SETTLEMENT_RPC_URL=https://k8s.testnet.json-rpc.injective.network/\n'
  printf 'ADX_ARENA_SETTLEMENT_BLOCKSCOUT_URL=https://testnet.blockscout.injective.network/api/v2\n'
  printf 'ADX_ARENA_WORKER_LEASE_SECONDS=600\n'
  printf 'ADX_ARENA_WORKER_POLL_SECONDS=0.25\n'
  printf 'ADX_ARENA_FINALIZER_POLL_SECONDS=1\n'
  printf 'ADX_SETTLEMENT_RECOVERY_POLL_SECONDS=3\n'
  printf '\n'
  printf 'ADX_ACME_EMAIL=%s\n' "${acme_email}"
  printf 'TZ=Asia/Shanghai\n'
  printf 'ADX_BACKUP_DIR=/var/backups/adx\n'
  printf 'ADX_BACKUP_RETENTION_DAYS=14\n'
  printf 'ADX_BUILD_CONNECTOR_ARTIFACTS=true\n'
} > "${env_file}"
chmod 600 "${env_file}"

echo "Created ${env_file} with mode ${tls_mode}."
echo "Long-lived secrets were written only to that chmod 600 file."
if [ -n "${invite_output_file}" ]; then
  printf '%s\n' "${bootstrap_invite}" > "${invite_output_file}"
  chmod 600 "${invite_output_file}"
  echo "Bootstrap invite was written once to ${invite_output_file} with mode 600."
else
  echo "Bootstrap invite (shown once; store it in your password manager): ${bootstrap_invite}"
fi
