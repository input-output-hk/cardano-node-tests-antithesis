# Keep the existing environment and replace only the node with a patched build
# of the same upstream revision. The patch changes no ledger/network encoding.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG NODE_REV
ARG FIX_REV
ARG FIX_SHA256
LABEL io.cardano.leios.node-revision="${NODE_REV}" \
      io.cardano.leios.consensus-fix="${FIX_REV}" \
      io.cardano.leios.consensus-fix-sha256="${FIX_SHA256}"
COPY nix/store /nix/store
RUN rm -f /usr/local/bin/cardano-node
COPY cardano-node /usr/local/bin/cardano-node
