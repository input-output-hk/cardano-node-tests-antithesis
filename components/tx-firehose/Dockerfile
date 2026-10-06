# tx-firehose — the Leios load generator.
#
# Its own image, deliberately. The obvious shortcut is to reuse the
# gov-cli image, which already has this nix stage - and that is exactly
# what broke run 37252534099: Antithesis discovers test commands per
# container by the presence of /opt/antithesis/test/v1, so a second
# container running the gov-cli image became a second place the
# governance drivers were scheduled. They ran there against a read-only
# /gov-data and aborted, failing the run in both Conway and Leios. An
# image that hosts the drivers cannot be reused for a container that
# must not run them.
#
# So: no composer drivers, no clusterlib, no python. Just the generator
# and the cardano-cli its preflight needs.
FROM nixos/nix:2.24.9 AS leios-builder
RUN printf 'experimental-features = nix-command flakes\nextra-trusted-public-keys = hydra.iohk.io:f/Ea+s+dFdN+3Y/G+FDgSq+a5NEWhJGzdjvKNGv0/EQ=\nextra-substituters = https://cache.iog.io/\n' \
    >> /etc/nix/nix.conf

# Keep in sync with docker-compose.yaml's NODE_IMAGE Leios-mode pin and
# with components/gov-cli/Dockerfile - all must come from one commit, so
# the generator, the cli it queries with and the node it submits to can
# never drift apart.
ARG LEIOS_NODE_REV=6a540bd43a83e697fd662b973f7e99cee02a59fb

# One --requisites query over both roots, deduped: the two closures
# overlap almost entirely, and copying them in two passes would either
# nest a duplicate path under itself or fail outright on the 0555 store
# directory. The -e guard keeps the copy idempotent either way.
#
# --builders "" --max-jobs 0 (same as scripts/build-leios-node-image.sh)
# makes this substitution-only. Both roots are cache-warm on cache.iog.io,
# and if one ever isn't, failing in seconds is far better than starting a
# GHC build that silently eats rebuild-gov-images.yaml's 30-minute budget
# and leaves the image un-repinned.
RUN CLI_OUT=$(nix build --accept-flake-config --no-write-lock-file --builders "" --max-jobs 0 \
        --no-link --print-out-paths \
        "github:IntersectMBO/cardano-node?ref=${LEIOS_NODE_REV}#cardano-cli") && \
    FIREHOSE_OUT=$(nix build --accept-flake-config --no-write-lock-file --builders "" --max-jobs 0 \
        --no-link --print-out-paths \
        "github:IntersectMBO/cardano-node?ref=${LEIOS_NODE_REV}#tx-firehose") && \
    mkdir -p /out/nix/store && \
    nix-store --query --requisites "${CLI_OUT}" "${FIREHOSE_OUT}" | sort -u > /tmp/closure.txt && \
    while read -r p; do \
        [ -e "/out/nix/store/${p##*/}" ] || cp -a "$p" /out/nix/store/; \
    done < /tmp/closure.txt && \
    ln -s "${CLI_OUT}/bin/cardano-cli" /out/cardano-cli && \
    ln -s "${FIREHOSE_OUT}/bin/tx-firehose" /out/tx-firehose

FROM debian:bookworm-slim

ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# jq only: the entrypoint reads protocolVersion out of the generated
# genesis. No python, no clusterlib - this image runs no drivers.
RUN apt-get update && \
    apt-get install -y --no-install-recommends jq && \
    rm -rf /var/lib/apt/lists/*

COPY --from=leios-builder /out/nix/store /nix/store
COPY --from=leios-builder /out/cardano-cli /usr/local/bin/cardano-cli
COPY --from=leios-builder /out/tx-firehose /usr/local/bin/tx-firehose

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

# Defaults; the testnet compose overrides as needed.
ENV CARDANO_NODE_SOCKET_PATH=/state/node.socket \
    TESTNET_MAGIC=42 \
    GOV=/gov-data \
    GOV_STATE_DIR=/gov-state \
    TX_TPS=15

ENTRYPOINT ["/entrypoint.sh"]
