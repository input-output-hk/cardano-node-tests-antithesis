# gov-cli — sleeping container holding cardano-cli plus the composer
# governance driver scripts. Antithesis `docker exec`s the drivers per
# tick to drive governance operations against relay1.
#
# Drivers use cardano-clusterlib (the standalone cardano-cli wrapper
# library) - composer/governance-python.

# Leios-patched cardano-cli, used ONLY for dijkstra-era commands (see
# the wrapper script below) - the stable release's "dijkstra" era
# support is a stub (just enough "node" subcommands for BLS key-gen),
# with no query/governance/transaction support at all under that era,
# which silently broke wait_for_node()'s tip query in Leios mode (every
# query threw, caught by its own except: pass, so it just retried for
# the full budget and timed out even though the node was healthy).
#
# Built ourselves via Nix from a pinned IntersectMBO/cardano-node commit,
# rather than extracted from a third-party image - the same commit
# NODE_IMAGE is built from (see docker-compose.yaml), so cli and node can
# never drift apart again (previously caused a BLS TextEnvelope tag
# mismatch between a cli/node pulled from different builds of a moving
# :latest tag). Neither cardano-cli nor tx-firehose has a dockerImage
# flake output, so their runtime closure (glibc, libsodium, gmp, ...) is
# copied alongside the binaries rather than just the binaries themselves.
#
# tx-firehose is the Leios load generator (see /tx-firehose.sh). It is
# built here, from this same commit, so the generator can never drift
# from the node and cli it submits to.
FROM nixos/nix:2.24.9 AS leios-builder
RUN printf 'experimental-features = nix-command flakes\nextra-trusted-public-keys = hydra.iohk.io:f/Ea+s+dFdN+3Y/G+FDgSq+a5NEWhJGzdjvKNGv0/EQ=\nextra-substituters = https://cache.iog.io/\n' \
    >> /etc/nix/nix.conf

# Keep in sync with docker-compose.yaml's NODE_IMAGE Leios-mode pin -
# both must come from the same commit.
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
    ln -s "${CLI_OUT}/bin/cardano-cli" /out/cardano-cli-leios && \
    ln -s "${FIREHOSE_OUT}/bin/tx-firehose" /out/tx-firehose

FROM debian:bookworm-slim

ARG NODE_VERSION=11.0.1
ARG CLUSTERLIB_VERSION=0.10.5

ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        python3 \
        python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Download cardano-cli from the official GitHub release.
RUN curl -fsSL \
    "https://github.com/IntersectMBO/cardano-node/releases/download/${NODE_VERSION}/cardano-node-${NODE_VERSION}-linux-amd64.tar.gz" \
    -o /tmp/cardano.tar.gz && \
    tar -xzf /tmp/cardano.tar.gz -C /tmp && \
    install -m 0755 /tmp/bin/cardano-cli /usr/local/bin/cardano-cli-stable && \
    rm -rf /tmp/cardano.tar.gz /tmp/bin

COPY --from=leios-builder /out/nix/store /nix/store
COPY --from=leios-builder /out/cardano-cli-leios /usr/local/bin/cardano-cli-leios
COPY --from=leios-builder /out/tx-firehose /usr/local/bin/tx-firehose

# cardano_clusterlib always invokes the literal name "cardano-cli" and
# always passes the era (conway/dijkstra) as its first argument - this
# wrapper dispatches to the right real binary based on that, so drivers
# need no code change to get Leios support.
RUN printf '#!/usr/bin/env bash\nif [ "$1" = "dijkstra" ]; then\n  exec /usr/local/bin/cardano-cli-leios "$@"\nelse\n  exec /usr/local/bin/cardano-cli-stable "$@"\nfi\n' \
    > /usr/local/bin/cardano-cli && \
    chmod 0755 /usr/local/bin/cardano-cli

# Standalone cardano-cli wrapper library used by the drivers. Pinned so a
# rebuild triggered by an unrelated components/ change can't silently bump
# it - bump this deliberately, like CARDONNAY_VERSION in gov-configurator.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        "cardano-clusterlib==${CLUSTERLIB_VERSION}"

# Becomes the live composer template at /opt/antithesis/test/v1/governance.
COPY composer/governance-python /opt/antithesis/test/v1/governance
RUN chmod 0644 /opt/antithesis/test/v1/governance/* && \
    chmod 0755 /opt/antithesis/test/v1/governance/first_* \
               /opt/antithesis/test/v1/governance/parallel_* \
               /opt/antithesis/test/v1/governance/anytime_* \
               /opt/antithesis/test/v1/governance/eventually_* \
               /opt/antithesis/test/v1/governance/finally_*

COPY sleep.sh /sleep.sh
RUN chmod 0755 /sleep.sh

# The tx-firehose service runs this image with entrypoint overridden to
# /tx-firehose.sh (see docker-compose.yaml); gov-cli itself never runs it.
COPY tx-firehose.sh /tx-firehose.sh
RUN chmod 0755 /tx-firehose.sh

# Defaults; the testnet compose overrides as needed.
ENV CARDANO_NODE_SOCKET_PATH=/state/node.socket \
    TESTNET_MAGIC=42 \
    GOV=/gov-data \
    GOV_STATE_DIR=/gov-state \
    WORK=/work \
    NUM_DREPS=5 \
    NUM_CC=5 \
    NUM_POOLS=2

ENTRYPOINT ["/sleep.sh"]
