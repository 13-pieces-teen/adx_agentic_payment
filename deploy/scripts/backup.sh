#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib.sh
. "${script_dir}/lib.sh"

require_command docker
require_command gzip
require_env_file
umask 077

backup_dir="$(env_value ADX_BACKUP_DIR)"
retention_days="$(env_value ADX_BACKUP_RETENTION_DAYS)"
[ -n "${backup_dir}" ] || backup_dir=/var/backups/adx
[ -n "${retention_days}" ] || retention_days=14

case "${backup_dir}" in
  /|/home|/root|"")
    echo "Refusing unsafe backup directory: ${backup_dir}" >&2
    exit 1
    ;;
esac

case "${retention_days}" in
  *[!0-9]*|"")
    echo "ADX_BACKUP_RETENTION_DAYS must be a non-negative integer." >&2
    exit 1
    ;;
esac

mkdir -p "${backup_dir}"
chmod 700 "${backup_dir}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${backup_dir}/adx_${timestamp}.sql.gz"
temporary="${target}.tmp"
raw_temporary="${backup_dir}/.adx_${timestamp}.dump.tmp"

cleanup() {
  rm -f -- "${temporary}" "${raw_temporary}"
}
trap cleanup 0 HUP INT TERM

# Expansion is intentionally performed inside the PostgreSQL container.
# shellcheck disable=SC2016
if ! compose exec -T postgres sh -eu -c \
  'pg_dump --format=custom --no-owner --no-privileges --username="$POSTGRES_USER" "$POSTGRES_DB"' \
  > "${raw_temporary}"; then
  echo "pg_dump failed; no backup was published." >&2
  exit 1
fi
if [ ! -s "${raw_temporary}" ]; then
  echo "pg_dump produced an empty file; no backup was published." >&2
  exit 1
fi
gzip -9 -c "${raw_temporary}" > "${temporary}"
chmod 600 "${temporary}"
mv "${temporary}" "${target}"
rm -f -- "${raw_temporary}"
trap - 0 HUP INT TERM

find "${backup_dir}" -maxdepth 1 -type f -name 'adx_*.sql.gz' \
  -mtime "+${retention_days}" -delete

echo "Backup written to ${target}. Copy it to encrypted off-host storage."
