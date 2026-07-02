#!/usr/bin/env bash
# Push gov-cli and/or gov-configurator to ghcr.io and pin the new digest
# in docker-compose.yaml.
#
# Usage:
#   ./scripts/push-gov-images.sh              # rebuild + push both images
#   ./scripts/push-gov-images.sh gov-cli      # gov-cli only
#   ./scripts/push-gov-images.sh gov-configurator
#
# Requires: docker login to ghcr.io (gh auth token or docker login manually)

set -euo pipefail

REPO="ghcr.io/saratomaz/cardano-node-tests-antithesis"
COMPOSE="testnets/cardano_node_governance/docker-compose.yaml"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT}"

# Default: build both; optional positional args restrict to a subset.
TARGETS=("gov-cli" "gov-configurator")
if [[ $# -gt 0 ]]; then
    TARGETS=("$@")
fi

push_image() {
    local name="$1"
    local tag="${REPO}/${name}:latest"

    echo "==> Building ${name}..."
    local build_args=()
    if [[ "${name}" == "gov-cli" ]]; then
        build_args=(--build-arg DRIVER_LANG=python)
    fi

    docker build "${build_args[@]}" \
        --platform linux/amd64 \
        -t "${tag}" \
        "components/${name}/"

    echo "==> Pushing ${name}..."
    local push_output
    push_output=$(docker push "${tag}" 2>&1)
    echo "${push_output}"

    local digest
    digest=$(echo "${push_output}" | grep -oP '(?<=digest: )sha256:[a-f0-9]+')
    if [[ -z "${digest}" ]]; then
        echo "ERROR: could not extract digest from push output for ${name}" >&2
        return 1
    fi

    echo "==> Pinning ${name} → ${digest}"
    sed -i "s|${name}@sha256:[a-f0-9]*|${name}@${digest}|g" "${COMPOSE}"
    echo "    Updated ${COMPOSE}"
}

for target in "${TARGETS[@]}"; do
    push_image "${target}"
done

echo ""
echo "Done. Review the diff before committing:"
echo "  git diff ${COMPOSE}"
