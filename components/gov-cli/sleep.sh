#!/usr/bin/env bash
# Entrypoint for the gov-cli container: a do-nothing supervisor loop.
#
# The container holds cardano-cli plus the composer driver scripts under
# /opt/antithesis/test/v1/governance/. Antithesis `docker exec`s those
# scripts per tick; the container process itself just stays alive so
# `restart: always` has something to supervise. Idempotent: if
# Antithesis kills the container, a fresh one comes back with no lost
# state (all shared state lives on the gov-data volume).
#
# Also serves the governance anchor JSON on port 8080 so cardano-cli's
# transaction build can verify the anchor hash without external network
# access (required in Antithesis's isolated environment).

ANCHOR_DIR=/tmp/anchor
mkdir -p "$ANCHOR_DIR"
printf '{"body":{"title":"antithesis governance workload"}}' > "$ANCHOR_DIR/governance.json"

cd "$ANCHOR_DIR" && python3 -m http.server 8080 --bind 0.0.0.0 &

while true; do
    echo "gov-cli idle"
    sleep 60
done
