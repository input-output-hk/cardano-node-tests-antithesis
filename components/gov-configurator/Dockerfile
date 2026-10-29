# gov-configurator — one-shot init container that uses cardonnay to
# generate the local_fast genesis (with the constitutional committee
# seeded) plus the full governance_data asset set, then distributes the
# artifacts to the per-node config volumes and the gov-cli volumes.
#
# Genesis + assets are produced by cardonnay; the on-chain governance
# operations (CC hot-key auth, DRep registration, voting) are performed
# later by the gov-cli composer drivers against relay1.

# Same pinned CLI/node pair as gov-cli. Only the Leios genesis path uses
# this binary; Conway keeps the stable release below.
FROM nixos/nix:2.24.9 AS leios-cli-builder
RUN printf 'experimental-features = nix-command flakes\nextra-trusted-public-keys = hydra.iohk.io:f/Ea+s+dFdN+3Y/G+FDgSq+a5NEWhJGzdjvKNGv0/EQ=\nextra-substituters = https://cache.iog.io/\n' \
    >> /etc/nix/nix.conf
ARG LEIOS_NODE_REV=6a540bd43a83e697fd662b973f7e99cee02a59fb
RUN CLI_OUT=$(nix build --accept-flake-config --no-write-lock-file --no-link --print-out-paths \
        "github:IntersectMBO/cardano-node?ref=${LEIOS_NODE_REV}#cardano-cli") && \
    mkdir -p /out/nix/store && \
    nix-store --query --requisites "${CLI_OUT}" > /tmp/closure.txt && \
    while read -r p; do cp -a "$p" /out/nix/store/; done < /tmp/closure.txt && \
    ln -s "${CLI_OUT}/bin/cardano-cli" /out/cardano-cli-leios

FROM debian:bookworm-slim

# Pin cardonnay (cardano-node-tests tracks >=0.4.4,<0.5.0). 0.4.6 fixes
# a Dijkstra-genesis gap (missing minPoolMargin + several other newly-
# required Leios-era pparams) that broke every Leios-mode node boot
# once ghcr.io/input-output-hk/ouroboros-leios/cardano-node-leios:latest
# picked up a leios-prototype update expecting them - see
# IntersectMBO/cardonnay#161, released as 0.4.6 on 2026-09-07.
ARG CARDONNAY_VERSION=0.4.6
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

# cardonnay ships both the CLI and the local_fast generation scripts.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        "cardonnay==${CARDONNAY_VERSION}" "cbor2==5.6.5"

COPY --from=leios-cli-builder /out/nix/store /nix/store
COPY --from=leios-cli-builder /out/cardano-cli-leios /usr/local/bin/cardano-cli-leios

COPY generate.sh /generate.sh
COPY seed_leios_genesis.py /seed_leios_genesis.py
RUN chmod 0755 /generate.sh

ENV USER=root \
    NUM_POOLS=3 \
    NUM_DREPS=5 \
    NUM_CC=5 \
    NETWORK_MAGIC=42 \
    PROTOCOL_VERSION=10

ENTRYPOINT ["/generate.sh"]
