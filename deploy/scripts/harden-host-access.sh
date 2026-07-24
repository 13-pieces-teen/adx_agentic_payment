#!/bin/sh
set -eu

usage() {
  echo "Usage: $0 --cloud-firewall-verified --i-have-tested-key-login [--reset-ufw]" >&2
  exit 2
}

cloud_verified=false
key_verified=false
reset_ufw=false
for argument in "$@"; do
  case "${argument}" in
    --cloud-firewall-verified) cloud_verified=true ;;
    --i-have-tested-key-login) key_verified=true ;;
    --reset-ufw) reset_ufw=true ;;
    *) usage ;;
  esac
done

[ "${cloud_verified}" = "true" ] || {
  echo "Verify the cloud firewall allows SSH, TCP 80/443 and UDP 443 first." >&2
  exit 1
}
[ "${key_verified}" = "true" ] || {
  echo "Open a second SSH session using the key, then rerun with the confirmation flag." >&2
  exit 1
}

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -- sh "$0" "$@"
fi

target_user="${SUDO_USER:-}"
if [ -z "${target_user}" ] || [ "${target_user}" = "root" ]; then
  echo "Run this script via sudo from the non-root SSH account." >&2
  exit 1
fi

target_home="$(getent passwd "${target_user}" | cut -d: -f6)"
authorized_keys="${target_home}/.ssh/authorized_keys"
if [ ! -s "${authorized_keys}" ] \
  || ! grep -Eq '^[[:space:]]*(ssh-|ecdsa-|sk-)' "${authorized_keys}"; then
  echo "No usable public key found for ${target_user}; refusing to disable passwords." >&2
  exit 1
fi

sshd_binary="$(command -v sshd || true)"
[ -n "${sshd_binary}" ] || sshd_binary=/usr/sbin/sshd
[ -x "${sshd_binary}" ] || {
  echo "sshd executable not found." >&2
  exit 1
}

drop_in_dir=/etc/ssh/sshd_config.d
drop_in="${drop_in_dir}/99-adx-hardening.conf"
backup="${drop_in}.previous.$$"
mkdir -p "${drop_in_dir}"
if [ -e "${drop_in}" ]; then
  cp -p "${drop_in}" "${backup}"
fi

{
  printf '# Managed by ADX deploy/scripts/harden-host-access.sh\n'
  printf 'PubkeyAuthentication yes\n'
  printf 'PasswordAuthentication no\n'
  printf 'KbdInteractiveAuthentication no\n'
  printf 'ChallengeResponseAuthentication no\n'
  printf 'PermitRootLogin prohibit-password\n'
} > "${drop_in}"
chmod 644 "${drop_in}"

if ! "${sshd_binary}" -t; then
  if [ -e "${backup}" ]; then
    mv "${backup}" "${drop_in}"
  else
    rm -f "${drop_in}"
  fi
  echo "sshd validation failed; the prior configuration was restored." >&2
  exit 1
fi
rm -f "${backup}"

export DEBIAN_FRONTEND=noninteractive
if ! command -v ufw >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ufw
fi

if [ "${reset_ufw}" = "true" ]; then
  ufw --force reset
else
  echo "Preserving existing UFW rules. Review the final rule list for unwanted ports."
fi

ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

if systemctl reload ssh 2>/dev/null; then
  :
elif systemctl reload sshd 2>/dev/null; then
  :
else
  echo "Could not reload SSH via systemd; configuration is valid but reload manually." >&2
  exit 1
fi

ufw status verbose
echo "Password and keyboard-interactive SSH authentication are disabled."
echo "The current SSH session was not terminated; keep it open until a new key login succeeds."
