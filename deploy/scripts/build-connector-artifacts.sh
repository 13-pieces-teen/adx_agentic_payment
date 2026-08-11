#!/bin/sh
set -eu

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repo_dir="$(CDPATH='' cd -- "${script_dir}/../.." && pwd)"
connector_dir="${repo_dir}/connector"
artifact_dir="${repo_dir}/deploy/artifacts"
connector_package="./cmd/adx-connector"
installer_dir="${repo_dir}/deploy/install"

targets="
windows amd64 .exe
windows arm64 .exe
linux amd64
linux arm64
darwin amd64
darwin arm64
"

verify_artifacts() {
  missing=0
  while read -r goos goarch suffix; do
    [ -n "${goos}" ] || continue
    artifact="${artifact_dir}/adx-connector-${goos}-${goarch}${suffix}"
    if [ ! -s "${artifact}" ] || [ ! -s "${artifact}.sha256" ]; then
      echo "Missing Connector artifact or checksum: ${artifact}" >&2
      missing=1
    elif ! (
      cd "${artifact_dir}"
      sha256sum -c "$(basename "${artifact}.sha256")" >/dev/null
    ); then
      echo "Connector artifact checksum does not match: ${artifact}" >&2
      missing=1
    fi
  done <<EOF
${targets}
EOF
  for installer in install.sh install.ps1; do
    if [ ! -s "${artifact_dir}/${installer}" ]; then
      echo "Missing Connector installer: ${artifact_dir}/${installer}" >&2
      missing=1
    fi
  done
  [ "${missing}" -eq 0 ]
}

if [ "${1:-}" = "--verify-only" ]; then
  verify_artifacts
  echo "Connector release artifacts and checksums match."
  exit 0
fi

mkdir -p "${artifact_dir}"
cp "${installer_dir}/install-connector.sh" "${artifact_dir}/install.sh"
cp "${installer_dir}/install-connector.ps1" "${artifact_dir}/install.ps1"
chmod 644 "${artifact_dir}/install.sh" "${artifact_dir}/install.ps1"

build_native() {
  goos="$1"
  goarch="$2"
  output="$3"
  (
    cd "${connector_dir}"
    CGO_ENABLED=0 GOOS="${goos}" GOARCH="${goarch}" \
      go build -buildvcs=false -trimpath -ldflags="-s -w" \
      -o "${output}" "${connector_package}"
  )
}

build_in_docker() {
  docker_goos="$1"
  docker_goarch="$2"
  docker_output_name="$3"
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    --env CGO_ENABLED=0 \
    --env GOOS="${docker_goos}" \
    --env GOARCH="${docker_goarch}" \
    --env GOCACHE=/tmp/go-build \
    --env GOMODCACHE=/tmp/go-mod \
    --volume "${connector_dir}:/src:ro" \
    --volume "${artifact_dir}:/out" \
    --workdir /src \
    golang:1.26-alpine@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 \
    go build -buildvcs=false -trimpath -ldflags="-s -w" \
      -o "/out/${docker_output_name}" "${connector_package}"
}

while read -r goos goarch suffix; do
  [ -n "${goos}" ] || continue
  output_name="adx-connector-${goos}-${goarch}${suffix}"
  output="${artifact_dir}/${output_name}"
  temporary="${output}.tmp"
  rm -f "${temporary}"

  if command -v go >/dev/null 2>&1; then
    build_native "${goos}" "${goarch}" "${temporary}"
  else
    command -v docker >/dev/null 2>&1 || {
      echo "Go or Docker is required to build Connector artifacts." >&2
      exit 1
    }
    build_in_docker "${goos}" "${goarch}" "$(basename "${temporary}")"
  fi

  chmod 755 "${temporary}"
  mv "${temporary}" "${output}"
  checksum="$(sha256sum "${output}" | awk '{print $1}')"
  printf '%s  %s\n' "${checksum}" "${output_name}" > "${output}.sha256"
  chmod 644 "${output}.sha256"
  echo "Built ${output_name}"
done <<EOF
${targets}
EOF

verify_artifacts
