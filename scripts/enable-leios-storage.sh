#!/usr/bin/env bash
# Materialize the optional topology for moog, which reads one committed file.
set -euo pipefail
cd "${1:?usage: enable-leios-storage.sh TESTNET_DIRECTORY}"
temporary=$(mktemp ./storage-compose.XXXXXX)
trap 'rm -f "$temporary"' EXIT
docker compose -f docker-compose.yaml config --format json | python3 -c '
import json, sys
s = json.load(sys.stdin)["services"]
assert s["gov-configurator"]["environment"]["PROTOCOL_VERSION"] == "12", "requires Leios"
assert s["gov-cli"]["environment"]["GOV_COMMAND_ERA"] == "dijkstra", "requires Dijkstra CLI"
image = s["relay1"]["image"]
assert "cardano-node-leios@sha256:" in image, "requires pinned Leios, not latest"
'
docker compose -f docker-compose.yaml -f docker-compose.leios-storage.yaml \
  config --no-path-resolution > "$temporary"
mv "$temporary" docker-compose.yaml
