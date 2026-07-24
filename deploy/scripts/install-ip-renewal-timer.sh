#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repo_dir="$(CDPATH='' cd -- "${script_dir}/../.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -- sh "$0" "$@"
fi

service_file=/etc/systemd/system/adx-ip-cert-renew.service
timer_file=/etc/systemd/system/adx-ip-cert-renew.timer

umask 022
{
  printf '[Unit]\n'
  printf 'Description=Renew ADX short-lived IP TLS certificate\n'
  printf 'After=docker.service network-online.target\n'
  printf 'Wants=network-online.target\n\n'
  printf '[Service]\n'
  printf 'Type=oneshot\n'
  printf 'WorkingDirectory=%s\n' "${repo_dir}"
  printf 'ExecStart=/bin/sh %s/deploy/scripts/renew-ip-certificate.sh\n' "${repo_dir}"
} > "${service_file}"

{
  printf '[Unit]\n'
  printf 'Description=Check ADX short-lived IP TLS certificate every six hours\n\n'
  printf '[Timer]\n'
  printf 'OnBootSec=15min\n'
  printf 'OnUnitActiveSec=6h\n'
  printf 'RandomizedDelaySec=20min\n'
  printf 'Persistent=true\n\n'
  printf '[Install]\n'
  printf 'WantedBy=timers.target\n'
} > "${timer_file}"

systemctl daemon-reload
systemctl enable --now adx-ip-cert-renew.timer
systemctl list-timers adx-ip-cert-renew.timer --no-pager
