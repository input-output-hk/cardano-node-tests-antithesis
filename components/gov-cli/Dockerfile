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
# Pinned to a digest, not :latest - ouroboros-leios doesn't cut stable
# releases yet and rebuilds :latest on every push to its main branch,
# which has already broken Leios mode twice (missing genesis fields,
# then a BLS TextEnvelope tag mismatch between cardano-cli and
# cardano-node built from different commits of that moving tag).
# Pinned digest is confirmed self-consistent (own cardano-cli's BLS key
# accepted by own cardano-node); bump deliberately, not automatically,
# once ouroboros-leios cuts an actual stable release.
ARG LEIOS_NODE_IMAGE=ghcr.io/input-output-hk/ouroboros-leios/cardano-node-leios@sha256:4fe269cfb2a7f61605ed4c77aea5a82af8dd89d1df4ee82e08d4f47d9e46a66e
FROM ${LEIOS_NODE_IMAGE} AS leios-cli

FROM debian:bookworm-slim

ARG NODE_VERSION=11.0.1

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

COPY --from=leios-cli /bin/cardano-cli /usr/local/bin/cardano-cli-leios

# cardano_clusterlib always invokes the literal name "cardano-cli" and
# always passes the era (conway/dijkstra) as its first argument - this
# wrapper dispatches to the right real binary based on that, so drivers
# need no code change to get Leios support.
RUN printf '#!/usr/bin/env bash\nif [ "$1" = "dijkstra" ]; then\n  exec /usr/local/bin/cardano-cli-leios "$@"\nelse\n  exec /usr/local/bin/cardano-cli-stable "$@"\nfi\n' \
    > /usr/local/bin/cardano-cli && \
    chmod 0755 /usr/local/bin/cardano-cli

# Standalone cardano-cli wrapper library used by the drivers.
RUN python3 -m pip install --no-cache-dir --break-system-packages cardano-clusterlib

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
