#!/bin/sh
set -eu

usage() {
  echo "Usage: $0 --archive PATH --commit SHA --checksum SHA256 --release-dir PATH --current-game-round-count N --current-game-market-protocol fcfs.v1|agent_a2a.v1 [--refresh-official-strategies true|false] [--expected-public-ip IPv4]" >&2
  exit 2
}

archive=
commit=
checksum=
release_dir=
expected_public_ip=
current_game_round_count=
current_game_market_protocol=
refresh_official_strategies=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --archive)
      [ "$#" -ge 2 ] || usage
      archive=$2
      shift 2
      ;;
    --commit)
      [ "$#" -ge 2 ] || usage
      commit=$2
      shift 2
      ;;
    --checksum)
      [ "$#" -ge 2 ] || usage
      checksum=$2
      shift 2
      ;;
    --release-dir)
      [ "$#" -ge 2 ] || usage
      release_dir=$2
      shift 2
      ;;
    --expected-public-ip)
      [ "$#" -ge 2 ] || usage
      expected_public_ip=$2
      shift 2
      ;;
    --current-game-round-count)
      [ "$#" -ge 2 ] || usage
      current_game_round_count=$2
      shift 2
      ;;
    --current-game-market-protocol)
      [ "$#" -ge 2 ] || usage
      current_game_market_protocol=$2
      shift 2
      ;;
    --refresh-official-strategies)
      [ "$#" -ge 2 ] || usage
      refresh_official_strategies=$2
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[ "${#commit}" -eq 40 ] || {
  echo "Commit must be a full 40-character SHA." >&2
  exit 1
}
case "${commit}" in
  *[!0-9a-f]*)
    echo "Commit must contain lowercase hexadecimal characters only." >&2
    exit 1
    ;;
esac
case "${current_game_round_count}" in
  ""|*[!0-9]*)
    echo "Current Game round count must be an integer." >&2
    exit 1
    ;;
esac
if [ "${current_game_round_count}" -lt 1 ] \
  || [ "${current_game_round_count}" -gt 10 ]; then
  echo "Current Game round count must be between 1 and 10." >&2
  exit 1
fi
case "${current_game_market_protocol}" in
  fcfs.v1|agent_a2a.v1) ;;
  *)
    echo "Current Game market protocol must be fcfs.v1 or agent_a2a.v1." >&2
    exit 1
    ;;
esac
case "${refresh_official_strategies}" in
  true|false) ;;
  *)
    echo "Official strategy refresh must be true or false." >&2
    exit 1
    ;;
esac
[ "${#checksum}" -eq 64 ] || {
  echo "Checksum must be a 64-character SHA-256 value." >&2
  exit 1
}
case "${checksum}" in
  *[!0-9a-f]*)
    echo "Checksum must contain lowercase hexadecimal characters only." >&2
    exit 1
    ;;
esac
case "${release_dir}" in
  /|/home|/root|/tmp|"")
    echo "Refusing unsafe release directory: ${release_dir}" >&2
    exit 1
    ;;
  /*) ;;
  *)
    echo "Release directory must be absolute." >&2
    exit 1
    ;;
esac
case "${release_dir}" in
  *".."*|*[!A-Za-z0-9_./-]*)
    echo "Release directory contains unsupported characters." >&2
    exit 1
    ;;
esac

expected_archive="/tmp/arena402-${commit}.tar"
if [ "${archive}" != "${expected_archive}" ]; then
  echo "Archive path must be ${expected_archive}." >&2
  exit 1
fi
if [ ! -f "${archive}" ] || [ -L "${archive}" ]; then
  echo "Release archive is missing or is a symbolic link." >&2
  exit 1
fi
if [ ! -d "${release_dir}" ] || [ -L "${release_dir}" ]; then
  echo "Active release directory is missing or is a symbolic link." >&2
  exit 1
fi

for command_name in \
  awk \
  curl \
  date \
  docker \
  flock \
  grep \
  python3 \
  sha256sum \
  tar
do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command is missing: ${command_name}" >&2
    exit 1
  fi
done
if [ -n "${expected_public_ip}" ] \
  && ! python3 -c \
    'import ipaddress,sys; value=ipaddress.ip_address(sys.argv[1]); raise SystemExit(0 if value.version == 4 else 1)' \
    "${expected_public_ip}"; then
  echo "Expected public IP must be an IPv4 address." >&2
  exit 1
fi

parent_dir="$(dirname -- "${release_dir}")"
release_name="$(basename -- "${release_dir}")"
parent_dir="$(CDPATH='' cd -- "${parent_dir}" && pwd -P)"
release_dir="${parent_dir}/${release_name}"
short_commit="$(printf '%s' "${commit}" | cut -c1-12)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging_dir="${parent_dir}/${release_name}.incoming-${short_commit}-${timestamp}"
rollback_dir="${parent_dir}/${release_name}.pre-${short_commit}-${timestamp}"
archive_list="/tmp/arena402-archive-${commit}.$$.txt"
health_body="/tmp/arena402-health-${commit}.$$.json"
current_body="/tmp/arena402-current-${commit}.$$.json"
sse_headers="/tmp/arena402-sse-${commit}.$$.headers"
lock_file="/tmp/arena402-production-deploy.lock"
staging_created=false

cleanup() {
  rm -f -- "${archive_list}" "${health_body}" "${current_body}" "${sse_headers}"
  if [ "${staging_created}" = "true" ] && [ -d "${staging_dir}" ]; then
    case "${staging_dir}" in
      "${parent_dir}/${release_name}.incoming-"*)
        rm -rf -- "${staging_dir}"
        ;;
    esac
  fi
}
trap cleanup 0 HUP INT TERM

exec 9>"${lock_file}"
if ! flock -n 9; then
  echo "Another production deployment is already running." >&2
  exit 1
fi

actual_checksum="$(sha256sum "${archive}" | awk '{print $1}')"
if [ "${actual_checksum}" != "${checksum}" ]; then
  echo "Remote archive checksum does not match the CI release checksum." >&2
  exit 1
fi

tar -tf "${archive}" > "${archive_list}"
if ! awk '
  /^\// || /(^|\/)\.\.($|\/)/ { exit 1 }
  /(^|\/)\.env($|\.)/ \
    && $0 != ".env.example" \
    && $0 != "agent-arena/settlement/.env.example" { exit 1 }
  /\.pem$/ || /\.key$/ { exit 1 }
  /^(\.\/)?deploy\/secrets\/.+/ \
    && $0 != "deploy/secrets/README.md" \
    && $0 != "./deploy/secrets/README.md" { exit 1 }
' "${archive_list}"; then
  echo "Release archive contains an unsafe or secret-shaped path." >&2
  exit 1
fi
grep -Fx 'deploy/scripts/deploy.sh' "${archive_list}" >/dev/null
grep -Fx 'deploy/scripts/backup.sh' "${archive_list}" >/dev/null
grep -Fx 'docker-compose.production.yml' "${archive_list}" >/dev/null

if [ -e "${staging_dir}" ] || [ -e "${rollback_dir}" ]; then
  echo "Release staging or rollback path already exists." >&2
  exit 1
fi
if [ ! -f "${release_dir}/deploy/.env" ]; then
  echo "Active release is missing its server-only deploy/.env." >&2
  exit 1
fi
if [ ! -x "${release_dir}/deploy/scripts/backup.sh" ] \
  && [ ! -f "${release_dir}/deploy/scripts/backup.sh" ]; then
  echo "Active release is missing deploy/scripts/backup.sh." >&2
  exit 1
fi

echo "Release target resolved to ${release_dir}."
echo "Rollback directory reserved at ${rollback_dir}."

mkdir -m 0700 -- "${staging_dir}"
staging_created=true
tar -xf "${archive}" -C "${staging_dir}"
test -f "${staging_dir}/deploy/scripts/deploy.sh"
test -f "${staging_dir}/docker-compose.production.yml"

cp -p -- "${release_dir}/deploy/.env" "${staging_dir}/deploy/.env"
(
  ADX_ENV_FILE="${staging_dir}/deploy/.env"
  export ADX_ENV_FILE
  # shellcheck source=deploy/scripts/lib.sh
  . "${staging_dir}/deploy/scripts/lib.sh"
  set_env_value ADX_CURRENT_GAME_ROUND_COUNT "${current_game_round_count}"
  set_env_value ADX_CURRENT_GAME_MARKET_PROTOCOL "${current_game_market_protocol}"
)
for runtime_dir in artifacts secrets official-litellm; do
  if [ -d "${release_dir}/deploy/${runtime_dir}" ]; then
    mkdir -p -- "${staging_dir}/deploy/${runtime_dir}"
    cp -a -- \
      "${release_dir}/deploy/${runtime_dir}/." \
      "${staging_dir}/deploy/${runtime_dir}/"
  fi
done
for runtime_dir in artifacts secrets; do
  if [ -d "${release_dir}/${runtime_dir}" ]; then
    mkdir -p -- "${staging_dir}/${runtime_dir}"
    cp -a -- \
      "${release_dir}/${runtime_dir}/." \
      "${staging_dir}/${runtime_dir}/"
  fi
done

backup_output="$(
  CDPATH='' cd -- "${release_dir}" \
    && sh deploy/scripts/backup.sh
)"
echo "${backup_output}"

mv -- "${release_dir}" "${rollback_dir}"
if ! mv -- "${staging_dir}" "${release_dir}"; then
  mv -- "${rollback_dir}" "${release_dir}"
  echo "Could not activate the staged release; the prior directory was restored." >&2
  exit 1
fi
staging_created=false

if ! (
  CDPATH='' cd -- "${release_dir}" \
    && sh deploy/scripts/deploy.sh
); then
  echo "Deployment failed after release activation." >&2
  echo "The database backup and rollback directory were retained." >&2
  echo "Automatic rollback is disabled because migrations may have started." >&2
  exit 1
fi

script_dir="${release_dir}/deploy/scripts"
repo_dir="${release_dir}"
compose_file="${repo_dir}/docker-compose.production.yml"
env_file="${repo_dir}/deploy/.env"

compose() {
  docker compose --env-file "${env_file}" -f "${compose_file}" "$@"
}

if [ "${refresh_official_strategies}" = "true" ]; then
  compose --profile ops build official-agent-strategy-refresh
  compose --profile ops run --rm --no-deps \
    official-agent-strategy-refresh
  echo "Official Agent strategies refreshed."
fi

env_value() {
  key=$1
  sed -n "s/^${key}=//p" "${env_file}" | tail -n 1
}

require_running_service() {
  profile=$1
  service=$2
  service_attempt=1
  while [ "${service_attempt}" -le 20 ]; do
    if [ -n "${profile}" ]; then
      service_ids="$(compose --profile "${profile}" ps --status running --quiet "${service}")"
    else
      service_ids="$(compose ps --status running --quiet "${service}")"
    fi
    service_ready=true
    if [ -z "${service_ids}" ]; then
      service_ready=false
    else
      for container_id in ${service_ids}; do
        container_health="$(
          docker inspect \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "${container_id}"
        )"
        case "${container_health}" in
          healthy|none) ;;
          *) service_ready=false ;;
        esac
      done
    fi
    if [ "${service_ready}" = "true" ]; then
      return
    fi
    sleep 3
    service_attempt=$((service_attempt + 1))
  done
  echo "Expected service is not running and healthy: ${service}" >&2
  exit 1
}

require_running_service "" postgres
require_running_service "" api
require_running_service "" connector-api
require_running_service "" caddy

if [ "$(env_value ADX_ENABLE_OFFICIAL_LITELLM)" = "true" ]; then
  require_running_service official-agents official-litellm
fi
if [ "$(env_value ADX_ENABLE_HOSTED_RUNTIME)" = "true" ]; then
  require_running_service hosted hosted-worker
  require_running_service hosted credential-controller
  expected_replicas="$(env_value ADX_HOSTED_WORKER_REPLICAS)"
  [ -n "${expected_replicas}" ] || expected_replicas=4
  actual_replicas="$(
    compose --profile hosted ps --status running --quiet hosted-worker \
      | wc -l \
      | tr -d ' '
  )"
  if [ "${actual_replicas}" -ne "${expected_replicas}" ]; then
    echo "Hosted Worker replica count does not match deploy/.env." >&2
    exit 1
  fi
fi
if [ "$(env_value ADX_ENABLE_ARENA_WORKER)" = "true" ]; then
  require_running_service arena arena-worker
fi
if [ "$(env_value ADX_ENABLE_SETTLEMENT_WORKER)" = "true" ]; then
  require_running_service settlement settlement-worker
fi
if [ "$(env_value ADX_ENABLE_TESTNET_SIGNER)" = "true" ]; then
  require_running_service testnet-signer wallet-signer
fi
if [ "$(env_value ADX_ENABLE_TESTNET_FACILITATOR)" = "true" ]; then
  facilitator_index=1
  while [ "${facilitator_index}" -le 4 ]; do
    require_running_service \
      testnet-facilitator \
      "arena-facilitator-${facilitator_index}"
    facilitator_index=$((facilitator_index + 1))
  done
fi
if [ "$(env_value ADX_ENABLE_MEMORIAL_MINTER)" = "true" ]; then
  require_running_service memorial memorial-minter
fi
if [ "$(env_value ADX_ENABLE_GAMECOIN_PROVISIONER)" = "true" ]; then
  require_running_service gamecoin gamecoin-provisioner
fi

# A second migration pass is a checksum verification pass: every migration
# must now be present with the exact source checksum and no DDL is reapplied.
compose run --rm migrate
sh "${script_dir}/build-connector-artifacts.sh" --verify-only

public_api_url="$(env_value ADX_PUBLIC_API_URL)"
public_host="$(env_value ADX_PUBLIC_HOST)"
case "${public_api_url}" in
  https://*|http://*) ;;
  *)
    echo "ADX_PUBLIC_API_URL must be an absolute HTTP(S) URL." >&2
    exit 1
    ;;
esac
health_url="${public_api_url%/}/api/health"
health_status=000
health_attempt=1
while [ "${health_attempt}" -le 12 ]; do
  health_status="$(
    curl \
      --silent \
      --show-error \
      --max-time 10 \
      --output "${health_body}" \
      --write-out '%{http_code}' \
      "${health_url}" \
      || true
  )"
  if [ "${health_status}" = "200" ]; then
    break
  fi
  sleep 5
  health_attempt=$((health_attempt + 1))
done
if [ "${health_status}" != "200" ]; then
  echo "Public health check returned HTTP ${health_status}." >&2
  exit 1
fi
echo "Public health check returned HTTP 200."

protected_status="$(
  curl \
    --silent \
    --show-error \
    --max-time 10 \
    --output /dev/null \
    --write-out '%{http_code}' \
    "${public_api_url%/}/api/connectors/devices"
)"
if [ "${protected_status}" != "401" ]; then
  echo "Protected API smoke check returned HTTP ${protected_status}, expected 401." >&2
  exit 1
fi
echo "Protected API smoke check returned the expected HTTP 401."

current_status="$(
  curl \
    --silent \
    --show-error \
    --max-time 10 \
    --output "${current_body}" \
    --write-out '%{http_code}' \
    "${public_api_url%/}/api/v1/games/current"
)"
case "${current_status}" in
  200)
    current_game_id="$(
      python3 -c \
        'import json,sys; body=json.load(open(sys.argv[1], encoding="utf-8")); game=body.get("game"); print(body.get("gameId") or (game.get("gameId") if isinstance(game, dict) else ""))' \
        "${current_body}"
    )"
    case "${current_game_id}" in
      ""|*[!A-Za-z0-9._:-]*)
        echo "Current Game response contains an unsafe gameId." >&2
        exit 1
        ;;
    esac
    curl \
      --silent \
      --show-error \
      --max-time 5 \
      --dump-header "${sse_headers}" \
      --output /dev/null \
      "${public_api_url%/}/api/v1/pawnhouse/games/${current_game_id}/events" \
      || true
    if ! grep -Eqi '^content-type:[[:space:]]*text/event-stream' "${sse_headers}"; then
      echo "Current Game SSE smoke check did not return text/event-stream." >&2
      exit 1
    fi
    echo "Current Game and SSE content type were verified."
    ;;
  404)
    if ! python3 -c \
      'import json,sys; body=json.load(open(sys.argv[1], encoding="utf-8")); raise SystemExit(0 if body.get("detail", {}).get("code") == "current_game_not_found" else 1)' \
      "${current_body}"; then
      echo "Current Game returned an unexpected HTTP 404 body." >&2
      exit 1
    fi
    echo "Current Game returned the accepted empty-state HTTP 404."
    ;;
  *)
    echo "Current Game smoke check returned HTTP ${current_status}." >&2
    exit 1
    ;;
esac

if [ -n "${expected_public_ip}" ]; then
  if ! command -v getent >/dev/null 2>&1; then
    echo "getent is required for the configured DNS identity check." >&2
    exit 1
  fi
  if ! getent ahostsv4 "${public_host}" \
    | awk '{print $1}' \
    | grep -Fx "${expected_public_ip}" >/dev/null; then
    echo "Public DNS does not resolve to the expected production IPv4 address." >&2
    exit 1
  fi
  echo "Public DNS resolves to the expected production IPv4 address."
fi

write_marker() {
  marker_path=$1
  marker_value=$2
  marker_temporary="${marker_path}.tmp.$$"
  umask 022
  printf '%s\n' "${marker_value}" > "${marker_temporary}"
  chmod 0644 "${marker_temporary}"
  mv -- "${marker_temporary}" "${marker_path}"
}

write_marker "${release_dir}/DEPLOYED_GIT_SHA" "${commit}"
write_marker "${release_dir}/DEPLOYED_ARCHIVE_SHA256" "${checksum}"
test "$(cat "${release_dir}/DEPLOYED_GIT_SHA")" = "${commit}"
test "$(cat "${release_dir}/DEPLOYED_ARCHIVE_SHA256")" = "${checksum}"

rm -f -- "${archive}"
case "$0" in
  "/tmp/arena402-release-${commit}.sh")
    rm -f -- "$0"
    ;;
esac

echo "Arena 402 production release completed."
echo "Deployed commit: ${commit}"
echo "Archive SHA-256: ${checksum}"
echo "Rollback directory: ${rollback_dir}"
echo "Database backup: ${backup_output#Backup written to }"
