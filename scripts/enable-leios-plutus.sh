#!/usr/bin/env bash
# Materialize only the checker flag in the single Compose file consumed by moog.
set -euo pipefail
cd "${1:?usage: enable-leios-plutus.sh TESTNET_DIRECTORY}"
temporary=$(mktemp ./plutus-compose.XXXXXX)
trap 'rm -f "$temporary"' EXIT
docker compose -f docker-compose.yaml config --format json | python3 -c '
import json, sys
s = json.load(sys.stdin)["services"]
assert s["gov-configurator"]["environment"]["PROTOCOL_VERSION"] == "12", "requires Leios"
assert s["gov-cli"]["environment"]["GOV_COMMAND_ERA"] == "dijkstra", "requires Dijkstra CLI"
assert "cardano-node-leios@sha256:" in s["relay1"]["image"], "requires pinned Leios, not latest"
'
docker compose -f docker-compose.yaml -f docker-compose.leios-plutus.yaml \
  config --no-path-resolution > "$temporary"
mv "$temporary" docker-compose.yaml
