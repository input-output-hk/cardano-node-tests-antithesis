#!/usr/bin/env bash
# Build locally by default. --push publishes and pins the verified digest.
set -euo pipefail
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--push" ) ]]; then
  echo "usage: $0 [--push]" >&2
  exit 2
fi
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
NODE_REV=6a540bd43a83e697fd662b973f7e99cee02a59fb
FIX_REV=e1aa48f000b30cafb7bc96f2fdb3ae91898eb32a
FIX_SHA256=f676115afcccde3fefaca1ba3d5b15b8ce8df6d02f9c35b29ef97da81f83535d
BASE_IMAGE=ghcr.io/saratomaz/cardano-node-tests-antithesis/cardano-node-leios@sha256:3a88c4544247839b23c258412bad0ea4ddfe21c30a43c93968a93e3e7cc15b17
REPO=ghcr.io/saratomaz/cardano-node-tests-antithesis/cardano-node-leios
LOCAL_TAG="cardano-node-leios:${NODE_REV:0:8}-${FIX_REV:0:8}"
REMOTE_TAG="${REPO}:${NODE_REV:0:8}-${FIX_REV:0:8}"
PATCH_CONFIG="$ROOT/components/leios-node/epoch-config"
WORKDIR=$(mktemp -d)
cleanup() {
  # Nix store directories copied below are read-only. Do not follow the node
  # symlinks back into the real store when making the temporary copy removable.
  find "$WORKDIR" -type d -exec chmod u+w '{}' +
  rm -rf "$WORKDIR"
}
trap cleanup EXIT
echo "$FIX_SHA256  $PATCH_CONFIG/complete-ledger-config.patch" | sha256sum --check

# Only consensus source is patched. Preserve the node's complete upstream lock
# file, ledger, cryptography and CLI versions; no moving branches are involved.
# A local backport is not in Hydra's cache, so bounded compilation is deliberate.
nix build --accept-flake-config --no-write-lock-file --builders "" \
  --max-jobs 2 --cores 4 --override-input customConfig "path:$PATCH_CONFIG" \
  --out-link "$WORKDIR/node" \
  "github:IntersectMBO/cardano-node/${NODE_REV}#cardano-node"
NODE_OUT=$(readlink -f "$WORKDIR/node")
mkdir -p "$WORKDIR/context/nix/store"
docker run --rm --network none --entrypoint /bin/sh "$BASE_IMAGE" \
  -c 'printf "%s\n" /nix/store/*' > "$WORKDIR/base-paths"
nix-store --query --requisites "$NODE_OUT" > "$WORKDIR/runtime-paths"
while IFS= read -r path; do
  if ! grep -Fxq "$path" "$WORKDIR/base-paths"; then
    cp -a "$path" "$WORKDIR/context/nix/store/"
  fi
done < "$WORKDIR/runtime-paths"
ln -s "$NODE_OUT/bin/cardano-node" "$WORKDIR/context/cardano-node"
docker build --platform linux/amd64 --build-arg "BASE_IMAGE=$BASE_IMAGE" \
  --build-arg "NODE_REV=$NODE_REV" --build-arg "FIX_REV=$FIX_REV" \
  --build-arg "FIX_SHA256=$FIX_SHA256" --tag "$LOCAL_TAG" \
  --file "$ROOT/components/leios-node/Dockerfile" "$WORKDIR/context"
docker run --rm --network none --entrypoint cardano-node "$LOCAL_TAG" --version
echo "Local patched image: $LOCAL_TAG"

if [[ ${1:-} == "--push" ]]; then
  docker tag "$LOCAL_TAG" "$REMOTE_TAG"
  docker push "$REMOTE_TAG"
  docker pull "$REMOTE_TAG"
  docker image inspect "$REMOTE_TAG" --format '{{json .RepoDigests}}' \
    > "$WORKDIR/published-digests.json"
  python3 - "$ROOT/.github/workflows/cardano-node-governance.yaml" \
    "$WORKDIR/published-digests.json" "$REPO" "$BASE_IMAGE" <<'PY'
import json, pathlib, re, sys
workflow, digests, repository, previous = sys.argv[1:]
matches = [d for d in json.loads(pathlib.Path(digests).read_text())
           if re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", d)]
if len(matches) != 1:
    raise SystemExit("could not identify one published node digest")
path = pathlib.Path(workflow)
text = path.read_text()
if text.count(previous) != 1:
    raise SystemExit("workflow pin changed; refusing to overwrite another node selection")
path.write_text(text.replace(previous, matches[0]))
print("Pinned published Leios image:", matches[0])
PY
fi
