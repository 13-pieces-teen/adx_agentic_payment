#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib.sh
. "${script_dir}/lib.sh"

require_command docker
require_command stat
require_command awk
require_env_file

tls_mode="$(env_value ADX_TLS_MODE)"
public_host="$(env_value ADX_PUBLIC_HOST)"
build_connector_artifacts="$(env_value ADX_BUILD_CONNECTOR_ARTIFACTS)"
[ -n "${build_connector_artifacts}" ] || build_connector_artifacts=true
enable_hosted_runtime="$(env_value ADX_ENABLE_HOSTED_RUNTIME)"
[ -n "${enable_hosted_runtime}" ] || enable_hosted_runtime=false
enable_official_litellm="$(env_value ADX_ENABLE_OFFICIAL_LITELLM)"
[ -n "${enable_official_litellm}" ] || enable_official_litellm=false
hosted_worker_replicas="$(env_value ADX_HOSTED_WORKER_REPLICAS)"
[ -n "${hosted_worker_replicas}" ] || hosted_worker_replicas=4
hosted_agents_enabled="$(env_value ADX_HOSTED_AGENTS_ENABLED)"
[ -n "${hosted_agents_enabled}" ] || hosted_agents_enabled=false
hosted_secret_backend="$(env_value ADX_HOSTED_SECRET_BACKEND)"
[ -n "${hosted_secret_backend}" ] || hosted_secret_backend=tencent_ssm
enable_arena_worker="$(env_value ADX_ENABLE_ARENA_WORKER)"
[ -n "${enable_arena_worker}" ] || enable_arena_worker=false
enable_memorial_minter="$(env_value ADX_ENABLE_MEMORIAL_MINTER)"
[ -n "${enable_memorial_minter}" ] || enable_memorial_minter=false
enable_gamecoin_provisioner="$(env_value ADX_ENABLE_GAMECOIN_PROVISIONER)"
[ -n "${enable_gamecoin_provisioner}" ] || enable_gamecoin_provisioner=false
enable_settlement_worker="$(env_value ADX_ENABLE_SETTLEMENT_WORKER)"
[ -n "${enable_settlement_worker}" ] || enable_settlement_worker=false
automatic_payments_enabled="$(env_value ADX_ARENA_AUTOMATIC_PAYMENTS_ENABLED)"
[ -n "${automatic_payments_enabled}" ] || automatic_payments_enabled=false
enable_testnet_signer="$(env_value ADX_ENABLE_TESTNET_SIGNER)"
[ -n "${enable_testnet_signer}" ] || enable_testnet_signer=false
enable_testnet_facilitator="$(env_value ADX_ENABLE_TESTNET_FACILITATOR)"
[ -n "${enable_testnet_facilitator}" ] || enable_testnet_facilitator=false
facilitator_shard_count="$(env_value ADX_X402_FACILITATOR_SHARD_COUNT)"
[ -n "${facilitator_shard_count}" ] || facilitator_shard_count=4

case "${enable_hosted_runtime}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_HOSTED_RUNTIME must be true or false." >&2
    exit 1
    ;;
esac
case "${enable_official_litellm}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_OFFICIAL_LITELLM must be true or false." >&2
    exit 1
    ;;
esac
case "${hosted_worker_replicas}" in
  *[!0-9]*|"")
    echo "ADX_HOSTED_WORKER_REPLICAS must be an integer." >&2
    exit 1
    ;;
esac
if [ "${hosted_worker_replicas}" -lt 1 ] || \
   [ "${hosted_worker_replicas}" -gt 16 ]; then
  echo "ADX_HOSTED_WORKER_REPLICAS must be between 1 and 16." >&2
  exit 1
fi
case "${enable_arena_worker}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_ARENA_WORKER must be true or false." >&2
    exit 1
    ;;
esac
case "${enable_memorial_minter}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_MEMORIAL_MINTER must be true or false." >&2
    exit 1
    ;;
esac
case "${enable_gamecoin_provisioner}" in
  true|false) ;;
  *)
    echo "ADX_ENABLE_GAMECOIN_PROVISIONER must be true or false." >&2
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
case "${facilitator_shard_count}" in
  *[!0-9]*|"")
    echo "ADX_X402_FACILITATOR_SHARD_COUNT must be an integer." >&2
    exit 1
    ;;
esac
if [ "${facilitator_shard_count}" -lt 1 ] || \
   [ "${facilitator_shard_count}" -gt 64 ]; then
  echo "ADX_X402_FACILITATOR_SHARD_COUNT must be between 1 and 64." >&2
  exit 1
fi

if [ "${enable_arena_worker}" = "true" ]; then
  if [ "$(env_value ADX_ARENA_CORE_ENABLED)" != "true" ]; then
    echo "Arena Worker requires ADX_ARENA_CORE_ENABLED=true." >&2
    exit 1
  fi
fi

if [ "${enable_memorial_minter}" = "true" ]; then
  if [ "$(env_value ADX_ARENA_MEMORIAL_ENABLED)" != "true" ]; then
    echo "Memorial minter requires ADX_ARENA_MEMORIAL_ENABLED=true." >&2
    exit 1
  fi
  if [ "${enable_testnet_facilitator}" != "true" ]; then
    echo "Memorial minter requires the reviewed facilitator wallet mount." >&2
    exit 1
  fi
fi

if [ "${enable_gamecoin_provisioner}" = "true" ]; then
  if [ "${enable_testnet_facilitator}" != "true" ]; then
    echo "Game-coin provisioner requires the reviewed facilitator wallet mount." >&2
    exit 1
  fi
  if [ "$(env_value ADX_CURRENT_GAME_TOKEN_SYMBOL)" != "arena402-g" ]; then
    echo "Game-coin provisioner requires ADX_CURRENT_GAME_TOKEN_SYMBOL=arena402-g." >&2
    exit 1
  fi
fi

if [ "${enable_memorial_minter}" = "true" ] && \
   [ "${enable_gamecoin_provisioner}" = "true" ]; then
  echo "Memorial minter and game-coin provisioner share one owner nonce; enable only one." >&2
  exit 1
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
  if [ "${enable_testnet_facilitator}" != "true" ]; then
    echo "Automatic payments require ADX_ENABLE_TESTNET_FACILITATOR=true." >&2
    exit 1
  fi
  if [ "$(env_value ADX_CURRENT_GAME_TOKEN_SYMBOL)" = "arena402-g" ] && \
     [ "${enable_gamecoin_provisioner}" != "true" ]; then
    echo "arena402-g automatic payments require ADX_ENABLE_GAMECOIN_PROVISIONER=true." >&2
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
  facilitator_tokens="|"
  facilitator_wallet_indices="|"
  facilitator_eoas="|"
  facilitator_index=1
  while [ "${facilitator_index}" -le 4 ]; do
    facilitator_token="$(
      env_value "ADX_X402_FACILITATOR_${facilitator_index}_BEARER_TOKEN"
    )"
    facilitator_authorization="$(
      env_value "ADX_X402_FACILITATOR_${facilitator_index}_AUTHORIZATION"
    )"
    facilitator_wallet_index="$(
      env_value "ADX_FACILITATOR_${facilitator_index}_WALLET_INDEX"
    )"
    if [ "${#facilitator_token}" -lt 32 ]; then
      echo "Each Facilitator bearer token must have at least 32 characters." >&2
      exit 1
    fi
    if [ "${facilitator_authorization}" != "Bearer ${facilitator_token}" ]; then
      echo "Each Facilitator authorization must match its bearer token." >&2
      exit 1
    fi
    case "${facilitator_tokens}" in
      *"|${facilitator_token}|"*)
        echo "Facilitator bearer tokens must be unique." >&2
        exit 1
        ;;
    esac
    facilitator_tokens="${facilitator_tokens}${facilitator_token}|"
    case "${facilitator_wallet_index}" in
      ""|*[!0-9]*)
        echo "Facilitator wallet indices must be positive integers." >&2
        exit 1
        ;;
    esac
    if [ "${facilitator_wallet_index}" -lt 1 ]; then
      echo "Facilitator wallet indices must be positive integers." >&2
      exit 1
    fi
    case "${facilitator_wallet_indices}" in
      *"|${facilitator_wallet_index}|"*)
        echo "Facilitator wallet indices must be unique." >&2
        exit 1
        ;;
    esac
    facilitator_wallet_indices="${facilitator_wallet_indices}${facilitator_wallet_index}|"
    if ! awk -F, -v target="${facilitator_wallet_index}" '
      NR > 1 && $1 == target { count += 1 }
      END { exit count == 1 ? 0 : 1 }
    ' "${facilitator_csv}"; then
      echo "Facilitator CSV must contain exactly one row for wallet index ${facilitator_wallet_index}." >&2
      exit 1
    fi
    facilitator_eoa="$(
      awk -F, -v target="${facilitator_wallet_index}" '
        NR == 1 {
          for (column = 1; column <= NF; column += 1) {
            gsub(/\r$/, "", $column)
            if ($column == "facilitator_index") index_column = column
            if ($column == "ethereum_address") address_column = column
          }
          next
        }
        index_column && address_column && $index_column == target {
          value = $address_column
          gsub(/\r$/, "", value)
          print value
        }
      ' "${facilitator_csv}"
    )"
    if ! printf '%s\n' "${facilitator_eoa}" | \
      grep -Eq '^0x[0-9a-fA-F]{40}$'; then
      echo "Facilitator CSV contains an invalid Ethereum address." >&2
      exit 1
    fi
    facilitator_eoa_normalized="$(
      printf '%s' "${facilitator_eoa}" | tr '[:upper:]' '[:lower:]'
    )"
    case "${facilitator_eoas}" in
      *"|${facilitator_eoa_normalized}|"*)
        echo "Facilitator EOA addresses must be unique." >&2
        exit 1
        ;;
    esac
    facilitator_eoas="${facilitator_eoas}${facilitator_eoa_normalized}|"
    set_env_value "ADX_X402_FACILITATOR_${facilitator_index}_EOA" "${facilitator_eoa_normalized}"
    facilitator_index=$((facilitator_index + 1))
  done
fi
if [ "${enable_memorial_minter}" = "true" ]; then
  compose --profile memorial build --pull memorial-minter
fi
if [ "${enable_gamecoin_provisioner}" = "true" ]; then
  compose --profile gamecoin build --pull gamecoin-provisioner
fi

if [ "${enable_hosted_runtime}" = "true" ]; then
  if [ "${hosted_agents_enabled}" != "true" ]; then
    echo "Hosted runtime requires ADX_HOSTED_AGENTS_ENABLED=true." >&2
    exit 1
  fi
fi

if [ "${enable_official_litellm}" = "true" ]; then
  if [ "${enable_hosted_runtime}" != "true" ]; then
    echo "Official LiteLLM requires ADX_ENABLE_HOSTED_RUNTIME=true." >&2
    exit 1
  fi
  official_litellm_config="$(
    env_value ADX_OFFICIAL_LITELLM_CONFIG_HOST_PATH
  )"
  [ -n "${official_litellm_config}" ] || \
    official_litellm_config="${repo_dir}/deploy/official-litellm"
  if [ ! -f "${official_litellm_config}/manifest.json" ]; then
    echo "Missing Official LiteLLM manifest." >&2
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
compose build --pull api connector-api migrate provision-db-roles
if [ "${enable_hosted_runtime}" = "true" ]; then
  compose --profile hosted build --pull hosted-worker credential-controller
fi
if [ "${enable_official_litellm}" = "true" ]; then
  compose --profile official-agents build --pull official-litellm
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
  compose --profile testnet-facilitator build --pull \
    arena-facilitator-1 arena-facilitator-2 \
    arena-facilitator-3 arena-facilitator-4
fi
if [ "${enable_memorial_minter}" = "true" ]; then
  compose --profile memorial build --pull memorial-minter
fi
if [ "${enable_gamecoin_provisioner}" = "true" ]; then
  compose --profile gamecoin build --pull gamecoin-provisioner
fi

# Recovery migrations may requeue durable work. Stop every service that can
# claim or advance that work before the migration becomes visible, otherwise
# an old image can consume the recovered row during a rolling deployment.
stop_background_workers() {
  if [ "${enable_hosted_runtime}" = "true" ]; then
    compose --profile hosted stop -t 30 \
      hosted-worker credential-controller
  fi
  if [ "${enable_arena_worker}" = "true" ]; then
    compose --profile arena stop -t 30 arena-worker
  fi
  if [ "${enable_settlement_worker}" = "true" ]; then
    compose --profile settlement stop -t 30 settlement-worker
  fi
  if [ "${enable_gamecoin_provisioner}" = "true" ]; then
    compose --profile gamecoin stop -t 30 gamecoin-provisioner
  fi
  if [ "${enable_memorial_minter}" = "true" ]; then
    compose --profile memorial stop -t 30 memorial-minter
  fi
}

stop_background_workers
compose up -d postgres
compose run --rm migrate
compose up -d api connector-api
compose up -d --force-recreate caddy
if [ "${enable_official_litellm}" = "true" ]; then
  compose --profile official-agents up -d --force-recreate official-litellm
fi
if [ "${enable_hosted_runtime}" = "true" ]; then
  compose --profile hosted up -d --scale hosted-worker="${hosted_worker_replicas}" hosted-worker credential-controller
fi
if [ "${enable_arena_worker}" = "true" ]; then
  compose --profile arena up -d arena-worker
fi
if [ "${enable_testnet_signer}" = "true" ]; then
  compose --profile testnet-signer up -d wallet-signer
fi
if [ "${enable_testnet_facilitator}" = "true" ]; then
  compose --profile testnet-facilitator up -d arena-facilitator-1 arena-facilitator-2 arena-facilitator-3 arena-facilitator-4
fi
if [ "${enable_memorial_minter}" = "true" ]; then
  compose --profile memorial up -d memorial-minter
fi
if [ "${enable_gamecoin_provisioner}" = "true" ]; then
  compose --profile gamecoin up -d gamecoin-provisioner
fi
if [ "${enable_settlement_worker}" = "true" ]; then
  compose --profile settlement up -d settlement-worker
fi

if [ "${tls_mode}" = "ip" ]; then
  sh "${script_dir}/ensure-ip-certificate.sh"
fi

compose ps
echo "Deployment converged at $(env_value ADX_PUBLIC_APP_URL)."
