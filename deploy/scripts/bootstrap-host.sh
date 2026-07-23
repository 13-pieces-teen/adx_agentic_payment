#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -- sh "$0" "$@"
fi

if command -v docker >/dev/null 2>&1 \
  && docker compose version >/dev/null 2>&1; then
  systemctl enable --now docker
  echo "Docker Engine and Compose plugin are already installed."
  exit 0
fi

# shellcheck disable=SC1091
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ]; then
  echo "This bootstrap script supports Ubuntu only." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

architecture="$(dpkg --print-architecture)"
printf '%s\n' \
  "deb [arch=${architecture} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
systemctl enable --now docker

if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  usermod -aG docker "${SUDO_USER}"
  echo "Added ${SUDO_USER} to the docker group; a new login is required."
fi

docker version
docker compose version
