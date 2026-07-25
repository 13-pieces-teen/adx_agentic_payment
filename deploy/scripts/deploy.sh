#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib.sh
. "${script_dir}/lib.sh"

require_command docker
require_command stat
require_env_file

tls_mode="$(env_value ADX_TLS_MODE)"
public_host="$(env_value ADX_PUBLIC_HOST)"
build_connector_artifacts="$(env_value ADX_BUILD_CONNECTOR_ARTIFACTS)"
[ -n "${build_connector_artifacts}" ] || build_connector_artifacts=true
enable_hosted_runtime="$(env_value ADX_ENABLE_HOSTED_RUNTIME)"
[ -n "${enable_hosted_runtime}" ] || enable_hosted_runtime=false
hosted_agents_enabled="$(env_value ADX_HOSTED_AGENTS_ENABLED)"
[ -n "${hosted_agents_enabled}" ] || hosted_agents_enabled=false
hosted_secret_backend="$(env_value ADX_HOSTED_SECRET_BACKEND)"
[ -n "${hosted_secret_backend}" ] || hosted_secret_backend=tencent_ssm
enable_arena_worker="$(env_value ADX_ENABLE_ARENA_WORKER)"
[ -n "${enable_arena_worker}" ] || enable_arena_worker=false
enable_settlement_worker="$(env_value ADX_ENABLE_SETTLEMENT_WORKER)"
[ -n "${enable_settlement_worker}" ] || enable_settlement_worker=false
automatic_payments_enabled="$(env_value ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED)"
[ -n "${automatic_payments_enabled}" ] || automatic_payments_enabled=false
enable_testnet_signer="$(env_value ADX_ENABLE_TESTNET_SIGNER)"
[ -n "${enable_testnet_signer}" ] || enable_testnet_signer=false
enable_testnet_facilitator="$(env_value ADX_ENABLE_TESTNET_FACILITATOR)"
[ -n "${enable_testnet_facilitator}" ] || enable_testnet_facilitator=false

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
case "${enable_settlement_worker}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_SETTLEMENT_WORKER must be true or false." >&2
    exit 1
    ;;
esac
for payment_flag in \
  "${automatic_payments_enabled}" \
  "${enable_testnet_signer}" \
  "${enable_testnet_facilitator}"
do
  case "${payment_flag}" in
    true|false) ;;
    *)
      echo "Payment deployment flags must be true or false." >&2
      exit 1
      ;;
  esac
done

if [ "${enable_arena_worker}" = "true" ]; then
  if [ "$(env_value ADX_ARENA_CORE_ENABLED)" != "true" ]; then
    echo "Arena Worker requires ADX_ARENA_CORE_ENABLED=true." >&2
    exit 1
  fi
fi

if [ "${automatic_payments_enabled}" = "true" ]; then
  if [ "${enable_arena_worker}" != "true" ]; then
    echo "Automatic payments require ADX_ENABLE_ARENA_WORKER=true." >&2
    exit 1
  fi
  if [ "${enable_settlement_worker}" != "true" ]; then
    echo "Automatic payments require ADX_ENABLE_SETTLEMENT_WORKER=true." >&2
    exit 1
  fi
  if [ "${enable_testnet_signer}" != "true" ]; then
    echo "Automatic payments require ADX_ENABLE_TESTNET_SIGNER=true." >&2
    exit 1
  fi
fi

if [ "${enable_settlement_worker}" = "true" ] && \
   [ "${automatic_payments_enabled}" != "true" ]; then
  echo "Settlement Worker requires ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED=true." >&2
  exit 1
fi

if [ "${enable_testnet_signer}" = "true" ]; then
  wallet_secret_dir="$(env_value ADX_WALLET_SECRET_DIR_HOST_PATH)"
  [ -n "${wallet_secret_dir}" ] || wallet_secret_dir="${repo_dir}/deploy/secrets"
  wallet_key_file="${wallet_secret_dir}/wallet-master.key"
  if [ ! -f "${wallet_key_file}" ]; then
    echo "Missing wallet signer master key: ${wallet_key_file}" >&2
    exit 1
  fi
  if [ "$(stat -c %s "${wallet_key_file}")" != "32" ]; then
    echo "Wallet signer master key must contain exactly 32 raw bytes." >&2
    exit 1
  fi
  if find "${wallet_key_file}" -perm /077 -print | grep -q .; then
    echo "Wallet signer master key must have no group/world permissions." >&2
    exit 1
  fi
  if find "${wallet_key_file}" -perm /200 -print | grep -q .; then
    echo "Wallet signer master key must be mounted from a read-only host file." >&2
    exit 1
  fi
fi

if [ "${enable_testnet_facilitator}" = "true" ]; then
  facilitator_csv="$(env_value ADX_FACILITATOR_CSV_HOST_PATH)"
  if [ ! -f "${facilitator_csv}" ]; then
    echo "Missing facilitator CSV: ${facilitator_csv}" >&2
    exit 1
  fi
  if find "${facilitator_csv}" -perm /077 -print | grep -q .; then
    echo "Facilitator CSV must have no group/world permissions." >&2
    exit 1
  fi
fi

if [ "${enable_hosted_runtime}" = "true" ]; then
  if [ "${hosted_agents_enabled}" != "true" ]; then
    echo "Hosted runtime requires ADX_HOSTED_AGENTS_ENABLED=true." >&2
    exit 1
  fi
fi

if [ "${hosted_agents_enabled}" = "true" ]; then
  case "${hosted_secret_backend}" in
    postgres_aesgcm)
      if [ "$(env_value ADX_HOSTED_CREDENTIAL_BACKEND_VERIFIED)" != "true" ]; then
        echo "PostgreSQL AES-GCM Hosted credentials require explicit verification." >&2
        exit 1
      fi
      hosted_secret_dir="$(env_value ADX_HOSTED_SECRET_DIR_HOST_PATH)"
      [ -n "${hosted_secret_dir}" ] || hosted_secret_dir="${repo_dir}/deploy/secrets"
      hosted_key_file="${hosted_secret_dir}/hosted-master.key"
      if [ ! -f "${hosted_key_file}" ]; then
        echo "Missing Hosted master key file: ${hosted_key_file}" >&2
        exit 1
      fi
      if [ "$(stat -c %s "${hosted_key_file}")" != "32" ]; then
        echo "Hosted master key must contain exactly 32 raw bytes." >&2
        exit 1
      fi
      if find "${hosted_key_file}" -perm /077 -print | grep -q .; then
        echo "Hosted master key must have no group/world permissions." >&2
        exit 1
      fi
      if find "${hosted_key_file}" -perm /200 -print | grep -q .; then
        echo "Hosted master key must be mounted from a read-only host file." >&2
        exit 1
      fi
      ;;
    tencent_ssm)
      if [ "$(env_value ADX_TENCENT_SSM_IAM_VERIFIED)" != "true" ]; then
        echo "Tencent SSM Hosted credentials require verified role IAM." >&2
        exit 1
      fi
      ;;
    *)
      echo "Unsupported ADX_HOSTED_SECRET_BACKEND: ${hosted_secret_backend}" >&2
      exit 1
      ;;
  esac
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
compose build --pull api migrate provision-db-roles
if [ "${enable_hosted_runtime}" = "true" ]; then
  compose --profile hosted build --pull hosted-worker credential-controller
fi
if [ "${enable_arena_worker}" = "true" ]; then
  compose --profile arena build --pull arena-worker
fi
if [ "${enable_settlement_worker}" = "true" ]; then
  compose --profile settlement build --pull settlement-worker
fi
if [ "${enable_testnet_signer}" = "true" ]; then
  compose --profile testnet-signer build --pull wallet-signer
fi
if [ "${enable_testnet_facilitator}" = "true" ]; then
  compose --profile testnet-facilitator build --pull arena-facilitator
fi
compose up -d postgres
compose run --rm migrate
compose up -d api caddy
if [ "${enable_hosted_runtime}" = "true" ]; then
  compose --profile hosted up -d hosted-worker credential-controller
fi
if [ "${enable_arena_worker}" = "true" ]; then
  compose --profile arena up -d arena-worker
fi
if [ "${enable_testnet_signer}" = "true" ]; then
  compose --profile testnet-signer up -d wallet-signer
fi
if [ "${enable_testnet_facilitator}" = "true" ]; then
  compose --profile testnet-facilitator up -d arena-facilitator
fi
if [ "${enable_settlement_worker}" = "true" ]; then
  compose --profile settlement up -d settlement-worker
fi

if [ "${tls_mode}" = "ip" ]; then
  sh "${script_dir}/ensure-ip-certificate.sh"
fi

compose ps
echo "Deployment converged at $(env_value ADX_PUBLIC_APP_URL)."
