#!/usr/bin/env python3
"""anytime_leios_load.py — is the Leios load generator actually submitting?

The tx-firehose service exists so Leios's endorser-block machinery has
transactions to certify (see testnets/.../README.md). Nothing else
reports whether it works: it is fault-excluded and never asserted on, so
a generator that crash-loops, idles on a misconfiguration, or was simply
never enabled produces a report exactly as green as a healthy one - and
the run then proves nothing about Leios under load while looking fine.

Signal: tx-firehose self-chains 1-in/1-out from its own funding address,
so while it is submitting, the UTxO set at that address churns
continuously. Sampling the set twice over a short window and comparing
is therefore a direct "it is submitting AND the txs are reaching the
chain" check - stronger than parsing its logs, which would only prove it
tried, and reachable without mounting the tracer volume into gov-cli.

  leios_load_observed  green => the generator submitted and the chain
                                accepted it at least once

Never going green across a Leios run means the load never materialized,
so any conclusion about Leios-under-faults from that run is void.

Conway runs assert nothing at all: the generator is inert there by
design, so an unfired Sometimes would be noise rather than signal.
"""

from __future__ import annotations

import json
import os
import time

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

WINDOW = int(os.environ.get("LEIOS_LOAD_WINDOW", "20"))

# Staged by gov-configurator, Leios only. Same path tx-firehose.sh reads.
ADDR_FILE = g.GOV / "tx-firehose" / "genesis-utxo2.addr"

# The era the load generator is meant for; below this it is inert.
LEIOS_PROTOCOL_MAJOR = 12


def _protocol_major() -> int | None:
    """Protocol version from the generated genesis - the same artifact
    tx-firehose.sh gates on, so the two can never disagree about whether
    load is expected."""
    try:
        genesis = json.loads((g.GOV_STATE_DIR / "shelley" / "genesis.json").read_text())
        return int(genesis["protocolParams"]["protocolVersion"]["major"])
    except Exception:  # noqa: BLE001
        return None


def _utxo_set(cluster: clusterlib.ClusterLib, addr: str) -> set[tuple[str, int]]:
    return {(u.utxo_hash, u.utxo_ix) for u in cluster.g_query.get_utxo(address=addr)}


def main() -> int:
    sdk.reachable("leios_load entered")

    # No ensure_dirs(): this driver only reads, so it needs none of the
    # shared state dirs - and not creating them keeps it working whether
    # gov-data is mounted rw or ro.
    major = _protocol_major()
    if major is None or major < LEIOS_PROTOCOL_MAJOR:
        # Conway, or the genesis is unreadable: nothing to observe.
        return 0

    # In Leios mode gov-configurator stages this from the same
    # PROTOCOL_VERSION that produced the genesis read above, so its
    # absence is a real misconfiguration - and the one the configurator
    # deliberately only warns about, since failing there would block
    # every other service and produce a silently green run.
    if not ADDR_FILE.exists():
        sdk.unreachable("leios_load_funding_key_missing")
        return 0

    addr = ADDR_FILE.read_text().strip()
    if not addr:
        sdk.unreachable("leios_load_funding_key_missing")
        return 0

    # make_cluster() is inside the try: it touches the socket and state
    # dir, so a node that is not answering yet raises here too, and that
    # is the same absorbable condition as a failed query below - not a
    # driver fault worth reporting as aborted.
    #
    # A failed query means the relay is busy or mid-fault, not that the
    # load is dead; relay reachability is anytime_chain_progress's
    # property, so absorb it here and let a later tick sample instead.
    try:
        cluster = g.make_cluster()
        before = _utxo_set(cluster, addr)
        time.sleep(WINDOW)
        after = _utxo_set(cluster, addr)
    except Exception:  # noqa: BLE001
        return 0

    # Only a positive observation is reported. The set legitimately holds
    # still while the chain is stalled under fault injection, which is a
    # normal and frequent state here - asserting on "no change" would
    # flag the fault injection working as intended.
    if before != after:
        sdk.sometimes(
            True,
            "leios_load_observed",
            {
                "window_s": WINDOW,
                "utxos_before": len(before),
                "utxos_after": len(after),
                "spent": len(before - after),
                "created": len(after - before),
            },
        )

    return 0


if __name__ == "__main__":
    sdk.run_driver(main, "leios_load_aborted", "leios_load_exits_zero")
