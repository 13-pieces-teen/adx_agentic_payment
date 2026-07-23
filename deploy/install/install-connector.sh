#!/bin/sh
set -eu

umask 077

server=${ADX_SERVER:-}
binary_url=
expected_sha=
allow_root=${HOME:?HOME is required}
enable_codex=false
force_reauthorize=false

fail() {
  printf '%s\n' "adx-connector installer: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  install-connector.sh --server https://arena.example [options]

Options:
  --binary-url URL       Override the platform-hosted Connector binary.
  --sha256 HEX           Override the downloaded .sha256 checksum.
  --allow-root PATH      Workspace root managed sessions may access.
  --enable-codex-tasks   Allow Connector-owned Codex task execution.
  --force-reauthorize    Pair a new device even if local credentials exist.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server)
      [ "$#" -ge 2 ] || fail "--server needs a value"
      server=$2
      shift 2
      ;;
    --binary-url)
      [ "$#" -ge 2 ] || fail "--binary-url needs a value"
      binary_url=$2
      shift 2
      ;;
    --sha256)
      [ "$#" -ge 2 ] || fail "--sha256 needs a value"
      expected_sha=$2
      shift 2
      ;;
    --allow-root)
      [ "$#" -ge 2 ] || fail "--allow-root needs a value"
      allow_root=$2
      shift 2
      ;;
    --enable-codex-tasks)
      enable_codex=true
      shift
      ;;
    --force-reauthorize)
      force_reauthorize=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

[ -n "$server" ] || fail "--server is required"
server=${server%/}
case "$server" in
  *" "*|*"	"*|*"?"*|*"#"*|*"@"*|*\"*|*\'*) fail "server must be a URL origin without credentials, query or fragment" ;;
esac
case "$server" in
  https://*) ;;
  http://localhost|http://localhost:*|http://127.0.0.1|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]:*) ;;
  *) fail "server must use HTTPS; HTTP is allowed only for localhost" ;;
esac
case "$server" in
  *://*/*) fail "server must be an origin such as https://arena.example" ;;
esac

case "$(uname -s)" in
  Linux) platform=linux ;;
  *) fail "this installer supports Linux; use install-connector.ps1 on Windows" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) architecture=amd64 ;;
  aarch64|arm64) architecture=arm64 ;;
  *) fail "ADX Connector supports 64-bit AMD64 and ARM64 Linux" ;;
esac

if [ -z "$binary_url" ]; then
  binary_url="$server/downloads/adx-connector-$platform-$architecture"
fi
case "$binary_url" in
  https://*) ;;
  http://localhost/*|http://localhost:*/*|http://127.0.0.1/*|http://127.0.0.1:*/*) ;;
  *) fail "binary URL must use HTTPS; HTTP is allowed only for localhost" ;;
esac
case "$binary_url" in
  *" "*|*"	"*|*"?"*|*"#"*|*"@"*) fail "binary URL must not contain credentials, query or fragment" ;;
esac

command -v systemctl >/dev/null 2>&1 || fail "systemd is required"
if command -v curl >/dev/null 2>&1; then
  fetch() {
    case "$1" in
      https://*) curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "$1" --output "$2" ;;
      http://*) curl --proto '=http' --fail --silent --show-error --location "$1" --output "$2" ;;
    esac
  }
elif command -v wget >/dev/null 2>&1; then
  fetch() {
    case "$1" in
      https://*) wget --https-only --quiet --output-document="$2" "$1" ;;
      http://*) wget --quiet --output-document="$2" "$1" ;;
    esac
  }
else
  fail "curl or wget is required"
fi

temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/adx-connector.XXXXXX")
cleanup() {
  case "$temporary_directory" in
    "${TMPDIR:-/tmp}"/adx-connector.*) rm -rf -- "$temporary_directory" ;;
  esac
}
trap cleanup EXIT HUP INT TERM

download="$temporary_directory/adx-connector"
checksum_file="$temporary_directory/adx-connector.sha256"
printf '%s\n' "Downloading ADX Connector..."
fetch "$binary_url" "$download"

if [ -z "$expected_sha" ]; then
  fetch "$binary_url.sha256" "$checksum_file"
  expected_sha=$(sed -n 's/^\([0-9A-Fa-f]\{64\}\).*$/\1/p' "$checksum_file" | sed -n '1p')
fi
case "$expected_sha" in
  *[!0-9A-Fa-f]*|"") fail "published SHA-256 checksum is invalid" ;;
esac
[ "${#expected_sha}" -eq 64 ] || fail "SHA-256 checksum must contain 64 hexadecimal characters"

if command -v sha256sum >/dev/null 2>&1; then
  actual_sha=$(sha256sum "$download" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  actual_sha=$(shasum -a 256 "$download" | awk '{print $1}')
else
  fail "sha256sum or shasum is required"
fi
[ "$(printf '%s' "$actual_sha" | tr 'A-F' 'a-f')" = "$(printf '%s' "$expected_sha" | tr 'A-F' 'a-f')" ] ||
  fail "ADX Connector checksum verification failed"

systemctl --user stop adx-connector.service >/dev/null 2>&1 || true

install_directory="$HOME/.local/lib/adx-connector"
install_target="$install_directory/adx-connector"
command_link="$HOME/.local/bin/adx-connector"
state_path="${XDG_CONFIG_HOME:-$HOME/.config}/adx/connector/state.json"
state_directory=$(dirname "$state_path")
unit_directory="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_path="$unit_directory/adx-connector.service"

[ -d "$allow_root" ] || fail "allow-root must be an existing directory"
allow_root=$(CDPATH= cd -- "$allow_root" && pwd -P)

mkdir -p "$install_directory" "$HOME/.local/bin" "$state_directory" "$unit_directory"
chmod 700 "$install_directory" "$HOME/.local/bin" "$state_directory" "$unit_directory"
install -m 700 "$download" "$install_target"
if [ -e "$command_link" ] && [ ! -L "$command_link" ]; then
  fail "$command_link already exists and is not a symlink"
fi
ln -sfn "$install_target" "$command_link"

if [ "$force_reauthorize" = true ] || [ ! -f "$state_path" ]; then
  printf '%s\n' "Your browser will open for one-time authorization."
  "$install_target" pair --server "$server" --state "$state_path"
else
  printf '%s\n' "Existing device authorization found; keeping it."
fi

systemd_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/%/%%/g'
}

escaped_target=$(systemd_escape "$install_target")
escaped_server=$(systemd_escape "$server")
escaped_state=$(systemd_escape "$state_path")
escaped_root=$(systemd_escape "$allow_root")
codex_argument=
if [ "$enable_codex" = true ]; then
  codex_argument=" --enable-codex-tasks"
fi

cat >"$unit_path" <<EOF
[Unit]
Description=ADX Local Connector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart="$escaped_target" run --server "$escaped_server" --state "$escaped_state" --auto-pair=false --allow-root "$escaped_root"$codex_argument
Restart=on-failure
RestartSec=5s
RestartPreventExitStatus=78
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
UMask=0077
Environment=PATH=%h/.local/bin:%h/.npm-global/bin:%h/.local/share/pnpm:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF
chmod 600 "$unit_path"

systemctl --user daemon-reload
systemctl --user enable --now adx-connector.service

if command -v loginctl >/dev/null 2>&1; then
  linger=$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || true)
  if [ "$linger" != "yes" ]; then
    if loginctl enable-linger "$USER" 2>/dev/null; then
      printf '%s\n' "Enabled systemd user lingering for boot-time startup."
    else
      printf '%s\n' "To start before login, an administrator can run: sudo loginctl enable-linger $USER"
    fi
  fi
fi

printf '\n%s\n' "ADX Connector is installed and starting."
printf '%s\n' "Run: $command_link doctor"
printf '%s\n' "Revoke the device in ADX Arena before uninstalling if this computer is no longer trusted."
