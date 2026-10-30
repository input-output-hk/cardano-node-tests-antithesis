#!/usr/bin/env python3
"""Continue isolated certified-EB payments after fault scheduling stops."""

from __future__ import annotations

import math
import time

import anytime_leios_transactions as observer
import helper_gov as g
import helper_leios_transactions as t
import helper_sdk as sdk
import parallel_driver_leios_transactions as submitter

POLL_SECONDS = 5
# Allow fresh terminal payments to reach immutability, including a replacement
# batch if an earlier batch landed in ordinary ranking blocks. Keep a finite
# counter even when slots stop progressing.
MIN_RECOVERY_CHECKS = 24


def recovery_checks() -> int:
    cluster = g.make_cluster()
    genesis = cluster.genesis
    immutable_seconds = (
        int(genesis["securityParam"]) * cluster.slot_length / float(genesis["activeSlotsCoeff"])
    )
    return max(MIN_RECOVERY_CHECKS, math.ceil(4 * immutable_seconds / POLL_SECONDS))


def main() -> int:
    if not t.enabled():
        return 0
    sdk.reachable("leios_tx_recovery entered")
    sdk.sometimes(False, "leios_tx_certified_and_immutable", {"reason": "awaiting_recovery"})
    if not g.SETUP_MARKER.exists():
        return 0
    checks = recovery_checks()
    for attempt in range(checks + 1):
        # The normal observer retires expired records at the immutable bound.
        # Reuse its exact certificate/output checks, even after an earlier hit.
        observer.main()
        with t.locked() as state:
            if state is not None and state.get("complete"):
                return 0
        # Earlier payments may have expired or landed in ordinary ranking
        # blocks before the committee was ready. Observation alone is not enough.
        if attempt < checks:
            submitter.main()
            time.sleep(POLL_SECONDS)
    # Missing certification still fails the mandatory Sometimes property;
    # there is no new latency invariant imposed on transactions during faults.
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_tx_recovery_aborted", "leios_tx_recovery_exits_zero" if t.enabled() else None
    )
