# gov-cli — sleeping container holding cardano-cli plus the composer
# governance driver scripts. Antithesis `docker exec`s the drivers per
# tick to drive governance operations against relay1.
#
# Drivers use cardano-clusterlib (the standalone cardano-cli wrapper
# library) - composer/governance-python.

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
    install -m 0755 /tmp/bin/cardano-cli /usr/local/bin/cardano-cli && \
    rm -rf /tmp/cardano.tar.gz /tmp/bin

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
