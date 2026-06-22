# gov-configurator — one-shot init container that uses cardonnay to
# generate the conway_fast genesis (with the constitutional committee
# seeded) plus the full governance_data asset set, then distributes the
# artifacts to the per-node config volumes and the gov-cli volumes.
#
# Genesis + assets are produced by cardonnay; the on-chain governance
# operations (CC hot-key auth, DRep registration, voting) are performed
# later by the gov-cli composer drivers against relay1.

FROM debian:bookworm-slim

# Pin cardonnay (cardano-node-tests tracks >=0.3.6,<0.4.0).
ARG CARDONNAY_VERSION=0.3.6
ARG NODE_VERSION=11.0.1

ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        curl \
        jq \
        python3 \
        python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Download cardano-cli and cardano-node from the official GitHub release.
RUN curl -fsSL \
    "https://github.com/IntersectMBO/cardano-node/releases/download/${NODE_VERSION}/cardano-node-${NODE_VERSION}-linux-amd64.tar.gz" \
    -o /tmp/cardano.tar.gz && \
    tar -xzf /tmp/cardano.tar.gz -C /tmp && \
    install -m 0755 /tmp/bin/cardano-cli  /usr/local/bin/cardano-cli && \
    install -m 0755 /tmp/bin/cardano-node /usr/local/bin/cardano-node && \
    rm -rf /tmp/cardano.tar.gz /tmp/bin

# cardonnay ships both the CLI and the conway_fast generation scripts.
RUN python3 -m pip install --no-cache-dir --break-system-packages "cardonnay==${CARDONNAY_VERSION}"

COPY generate.sh /generate.sh
RUN chmod 0755 /generate.sh

ENV USER=root \
    NUM_POOLS=3 \
    NUM_DREPS=5 \
    NUM_CC=5 \
    NETWORK_MAGIC=42 \
    PROTOCOL_VERSION=10 \
    SECURITY_PARAM=50 \
    EPOCH_LENGTH=5000

ENTRYPOINT ["/generate.sh"]
