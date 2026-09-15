#!/usr/bin/env bash
# Build the Leios-mode node image ourselves via Nix, from a pinned
# IntersectMBO/cardano-node commit, and push it to ghcr.io - replaces
# pulling the third-party ghcr.io/input-output-hk/ouroboros-leios image,
# so the node and gov-cli's cardano-cli-leios (built from the same
# LEIOS_NODE_REV, see components/gov-cli/Dockerfile) always come from
# the exact same build. Bump LEIOS_NODE_REV below deliberately, not
# automatically - see components/gov-cli/Dockerfile's comment for why.
#
# Usage:
#   ./scripts/build-leios-node-image.sh [REV]
#
# Requires: nix (with an IOG-cache-trusting nix.conf, or --accept-flake-config
# to pick up the flake's own nixConfig), skopeo, and docker login to ghcr.io.

set -euo pipefail

REPO="ghcr.io/saratomaz/cardano-node-tests-antithesis/cardano-node-leios"
LEIOS_NODE_REV="${1:-6a540bd43a83e697fd662b973f7e99cee02a59fb}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT
cd "${WORKDIR}"

echo "==> Building dockerImage/node for ${LEIOS_NODE_REV}..."
nix build --accept-flake-config --no-write-lock-file --builders "" --max-jobs 0 \
    --out-link ./cardano-node-image \
    "github:IntersectMBO/cardano-node?ref=${LEIOS_NODE_REV}#packages.x86_64-linux.dockerImage/node"

TAG="${REPO}:${LEIOS_NODE_REV:0:8}"
echo "==> Pushing ${TAG}..."
skopeo copy --authfile ~/.docker/config.json \
    "docker-archive:./cardano-node-image" "docker://${TAG}"

DIGEST=$(skopeo inspect --authfile ~/.docker/config.json --no-tags "docker://${TAG}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["Digest"])')

echo ""
echo "Pushed: ${REPO}@${DIGEST}"
echo ""
echo "Update these to bump the pin:"
echo "  - .github/workflows/cardano-node-governance.yaml (leios-patch step's LEIOS_NODE_IMAGE)"
echo "  - components/gov-cli/Dockerfile (LEIOS_NODE_REV ARG default, if bumping the commit too)"
