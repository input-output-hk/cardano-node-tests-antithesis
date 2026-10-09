#!/usr/bin/env bash
# Entrypoint for the tx-firehose container: the Leios load generator.
#
# Its own image (components/tx-firehose/), not gov-cli's - reusing that
# one made this container a second host for the governance drivers, which
# is what broke run 37252534099. Its own container too, so it gets an
# independent restart policy, its own logs and its own fault-exclusion
# label. The load has to be a *stable backdrop*: if Antithesis could
# pause or kill it, an EB-certification failure under a network fault
# would be indistinguishable from "the load stopped, so there was
# nothing to certify".
#
# Leios's endorser blocks only have something to do when the mempool has
# transactions to certify, and the governance drivers submit roughly one
# tx per composer tick - far too little. This keeps the pipeline fed.

set -uo pipefail

SOCKET="${CARDANO_NODE_SOCKET_PATH:-/state/node.socket}"
GENESIS="${GOV_STATE_DIR:-/gov-state}/shelley/genesis.json"
KEY_DIR="${GOV:-/gov-data}/tx-firehose"
SKEY="${KEY_DIR}/genesis-utxo2.skey"
ADDR_FILE="${KEY_DIR}/genesis-utxo2.addr"
MAGIC="${TESTNET_MAGIC:-42}"
TPS="${TX_TPS:-15}"

# tx-firehose's own defaults, spelled out so a change is visible here.
INPUTS_PER_TX="${TX_INPUTS_PER_TX:-1}"
OUTPUTS_PER_TX="${TX_OUTPUTS_PER_TX:-1}"
FEE="${TX_FEE:-200000}"
MAX_ERRORS="${TX_MAX_CONSECUTIVE_ERRORS:-50}"

# tx-firehose emits one JSON trace line per submitted transaction. At 15
# tps that is ~160k lines per timeline, but Antithesis explores many
# branches: run 37796283946 produced 19,319,280 TxFirehose.Submit.Success
# events and came back Incomplete, with no findings verdict at all. That
# is the same failure mode generate.sh's TraceOptions map exists to
# prevent - an output firehose Antithesis cannot materialize. So the
# per-tx success lines are dropped by default and everything else
# (rejects, errors, exits) is kept. Nothing depends on them:
# anytime_leios_load proves the load ran from UTxO churn on chain, not
# from logs. Set TX_FIREHOSE_LOG_SUBMITS=true to keep them when
# debugging locally.
case "${TX_FIREHOSE_LOG_SUBMITS:-}" in
    true|True|TRUE|1|yes) NOISE='' ;;
    *) NOISE='TxFirehose.Submit.Success' ;;
esac

log() { echo "tx-firehose: $*"; }

# Permanent misconfiguration. Exiting non-zero here would not be a
# single loud failure: `restart: always` turns it into a restart loop,
# which scrolls the reason away and shows up only as restart churn. So
# stay up and keep repeating the reason instead - the container is then
# visibly alive-but-useless, and the message is still on screen.
fatal() {
    while true; do
        echo "tx-firehose: FATAL: $* (idling; fix and restart)" >&2
        sleep 60
    done
}

idle() {
    # Mirrors sleep.sh: stay alive so `restart: always` has something to
    # supervise, without submitting anything.
    log "$1"
    while true; do
        sleep 60
    done
}

# ---------------------------------------------------------------------
# Gate: follow the generated genesis, not a separate switch.
# ---------------------------------------------------------------------
# Reading the artifact the nodes actually boot from means this can never
# disagree with them. A second env-var switch, flipped by the workflow's
# sed alongside PROTOCOL_VERSION, could: v12 with load off silently loses
# the coverage, and v10 with load on silently changes the Conway
# baseline and every `sometimes` property in it.
[ -f "$GENESIS" ] || fatal "$GENESIS not found; is gov-state mounted?"

MAJOR=$(jq -r '.protocolParams.protocolVersion.major // empty' "$GENESIS")
case "$MAJOR" in
    ''|*[!0-9]*) fatal "could not read protocolVersion.major from $GENESIS (got '${MAJOR}')" ;;
esac

if [ "$MAJOR" -ge 12 ]; then
    ERA=dijkstra
else
    ERA=conway
fi

# An explicit override exists only to A/B whether the load caused a
# regression; unset (the normal case) follows the genesis.
case "${TX_FIREHOSE:-}" in
    true|True|TRUE|1|yes) ENABLED=true ;;
    false|False|FALSE|0|no) ENABLED=false ;;
    '') [ "$MAJOR" -ge 12 ] && ENABLED=true || ENABLED=false ;;
    *) fatal "TX_FIREHOSE must be true/false, got '${TX_FIREHOSE}'" ;;
esac

if [ "$ENABLED" != true ]; then
    idle "disabled (protocolVersion=${MAJOR}); not submitting"
fi

log "enabled (protocolVersion=${MAJOR}, era=${ERA}, tps=${TPS})"

# ---------------------------------------------------------------------
# Preflight. These are permanent misconfigurations, so they are reported
# and then idled on - deliberately not swallowed by the retry loop below,
# which would make a broken setup and the intended steady state look
# identical.
# ---------------------------------------------------------------------
command -v tx-firehose >/dev/null 2>&1 || fatal "tx-firehose not on PATH"
[ -f "$SKEY" ] || fatal "$SKEY missing; gov-configurator stages it only when PROTOCOL_VERSION>=12"
[ -f "$ADDR_FILE" ] || fatal "$ADDR_FILE missing"

ADDR=$(tr -d '[:space:]' < "$ADDR_FILE")
[ -n "$ADDR" ] || fatal "$ADDR_FILE is empty"

# Deliberately not a test for the socket file. relay1-state is a named
# volume, so /state/node.socket survives a `down` without -v: a stale
# inode passes `[ -S ]` instantly while nothing is listening. Wait for an
# *answer* instead, like helper_gov.wait_for_node.
log "waiting for the node to answer on ${SOCKET}"
SLOT=
for _ in $(seq 1 120); do
    SLOT=$(cardano-cli "$ERA" query tip --testnet-magic "$MAGIC" 2>/dev/null | jq -r '.slot // empty')
    case "$SLOT" in
        ''|*[!0-9]*) ;;
        *) [ "$SLOT" -gt 0 ] && break ;;
    esac
    SLOT=
    sleep 5
done
[ -n "$SLOT" ] || fatal "node did not answer with a slot > 0 within the wait budget"
log "node is answering (slot ${SLOT})"

# Genesis initialFunds are in the ledger from slot 0, so once the node
# serves queries the funds are there. tx-firehose exits non-zero if the
# address has no UTxO, so check it here to get a clear message instead.
log "checking funds at ${ADDR}"
FUNDED=false
for _ in $(seq 1 60); do
    if cardano-cli "$ERA" query utxo --address "$ADDR" --testnet-magic "$MAGIC" --output-json 2>/dev/null \
        | jq -e 'length > 0' >/dev/null 2>&1; then
        FUNDED=true
        break
    fi
    sleep 5
done
[ "$FUNDED" = true ] || fatal "no UTxO at ${ADDR}; the funding key is unfunded, nothing downstream will work"
log "funds confirmed"

# ---------------------------------------------------------------------
# Supervisor: unbounded, with bounded backoff.
# ---------------------------------------------------------------------
# tx-firehose caches its fund set at startup and self-chains, so *every*
# reorg invalidates it: it then rejects until --max-consecutive-errors is
# reached, exits, and needs restarting with a freshly queried fund set.
# Inducing reorgs is this testnet's entire purpose, so that cycle is the
# designed steady state here, not an error path - which is why this loop
# never gives up. (Upstream's own wrapper stops after 4 consecutive
# failures; one 150s partition would burn through that and silently end
# the load for the rest of the run.)
# The backoff resets on evidence the child did work, NOT on its exit
# status. tx-firehose documents no clean exit - an empty UTxO set, a
# drained fund set and hitting --max-consecutive-errors are all non-zero
# - so an `if tx-firehose; then ... fi` reset would never fire, the
# delay would ratchet to the cap after about three reorgs, and the
# "stable backdrop" would decay into a few seconds of load per 30s of
# silence. That would quietly gut the coverage this service exists for.
#
# $SECONDS is a flap heuristic only, never a deadline or an assertion
# input, so whether Antithesis virtualises the clock doesn't matter.
BACKOFF=5
BACKOFF_MIN=5
BACKOFF_MAX=30
HEALTHY_RUN=10

while true; do
    STARTED=$SECONDS
    # 2>&1 | grep, with the status recovered from PIPESTATUS[0] rather
    # than $? - $? would be grep's. A pipe rather than process
    # substitution so no output can be lost to the shell not waiting on
    # the substituted process.
    tx-firehose \
        --socket-path "$SOCKET" \
        --testnet-magic "$MAGIC" \
        --signing-key-file "$SKEY" \
        --tps "$TPS" \
        --inputs-per-tx "$INPUTS_PER_TX" \
        --outputs-per-tx "$OUTPUTS_PER_TX" \
        --fee "$FEE" \
        --max-consecutive-errors "$MAX_ERRORS" 2>&1 \
        | { if [ -n "$NOISE" ]; then grep --line-buffered -v "$NOISE"; else cat; fi; }
    RC=${PIPESTATUS[0]}
    RAN=$((SECONDS - STARTED))

    if [ "$RAN" -ge "$HEALTHY_RUN" ]; then
        log "submitted for ${RAN}s then exited (rc=${RC}); this is the expected" \
            "reorg cycle - restarting with a fresh fund set"
        BACKOFF="$BACKOFF_MIN"
        sleep 1
    else
        log "exited after only ${RAN}s (rc=${RC}); retrying in ${BACKOFF}s" >&2
        sleep "$BACKOFF"
        BACKOFF=$((BACKOFF * 2))
        [ "$BACKOFF" -gt "$BACKOFF_MAX" ] && BACKOFF="$BACKOFF_MAX"
    fi
done
