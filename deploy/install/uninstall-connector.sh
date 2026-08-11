#!/bin/sh
set -eu

purge_credentials=false
if [ "${1:-}" = "--purge-credentials" ]; then
  purge_credentials=true
  shift
fi
[ "$#" -eq 0 ] || {
  printf '%s\n' "Usage: uninstall-connector.sh [--purge-credentials]" >&2
  exit 1
}

home=${HOME:?HOME is required}
install_directory="$home/.local/lib/adx-connector"
command_link="$home/.local/bin/adx-connector"
state_directory="${XDG_CONFIG_HOME:-$home/.config}/adx/connector"

case "$(uname -s)" in
  Linux)
    unit_path="${XDG_CONFIG_HOME:-$home/.config}/systemd/user/adx-connector.service"
    systemctl --user disable --now adx-connector.service >/dev/null 2>&1 || true
    case "$unit_path" in
      "$home"/*) rm -f -- "$unit_path" ;;
      *) printf '%s\n' "Refusing to remove a unit outside HOME." >&2; exit 1 ;;
    esac
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    ;;
  Darwin)
    uid=$(id -u)
    label="com.adx.local-connector"
    plist_path="$home/Library/LaunchAgents/${label}.plist"
    launchctl bootout "gui/${uid}/${label}" >/dev/null 2>&1 || true
    launchctl disable "gui/${uid}/${label}" >/dev/null 2>&1 || true
    case "$plist_path" in
      "$home"/*) rm -f -- "$plist_path" ;;
      *) printf '%s\n' "Refusing to remove a LaunchAgent outside HOME." >&2; exit 1 ;;
    esac
    ;;
  *)
    printf '%s\n' "This uninstaller supports Linux and macOS; use uninstall-connector.ps1 on Windows." >&2
    exit 1
    ;;
esac

case "$command_link" in
  "$home"/*) rm -f -- "$command_link" ;;
  *) printf '%s\n' "Refusing to remove a command outside HOME." >&2; exit 1 ;;
esac
case "$install_directory" in
  "$home"/*) rm -rf -- "$install_directory" ;;
  *) printf '%s\n' "Refusing to remove an installation outside HOME." >&2; exit 1 ;;
esac

if [ "$purge_credentials" = true ]; then
  case "$state_directory" in
    "$home"/*)
      rm -rf -- "$state_directory"
      printf '%s\n' "Removed local Connector credentials and event state. This cannot be recovered."
      ;;
    *) printf '%s\n' "Refusing to remove credentials outside HOME." >&2; exit 1 ;;
  esac
else
  printf '%s\n' "Local credentials were retained. Pass --purge-credentials to remove them."
fi

printf '%s\n' "ADX Connector was removed from startup."
printf '%s\n' "Device revocation is a separate action in ADX Arena."
