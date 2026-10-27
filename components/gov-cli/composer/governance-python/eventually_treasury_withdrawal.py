#!/usr/bin/env python3
"""Verify the isolated guided withdrawal settles after faults stop.

Antithesis stops scheduling anytime commands during terminal recovery. Keep
observing here through the epoch boundary instead of relying on another
anytime tick after ratification. No transactions or votes are submitted.
"""

from __future__ import annotations

import math
import sys
import time

import anytime_treasury_withdrawal_enactment as checker
import helper_gov as g
import helper_sdk as sdk
import helper_treasury as treasury

POLL_SECONDS = 5
RECOVERY_EPOCHS = 3


def main() -> int:
    sdk.reachable("treasury_withdrawal_recovery entered")
    if not g.SETUP_MARKER.exists():
        return 0
    sdk.sometimes(False, "treasury_withdrawal_ratified")
    sdk.sometimes(False, "treasury_withdrawal_enacted_correctly")
    cluster = g.make_cluster()
    # A counter bounds elapsed sleep even if the node stops advancing slots.
    # Derive the budget from genesis; no change to k or production epoch timing.
    checks = math.ceil(RECOVERY_EPOCHS * cluster.epoch_length * cluster.slot_length / POLL_SECONDS)
    last_error = None
    for attempt in range(checks + 1):
        try:
            with (
                treasury.lock() as observer_locked,
                treasury.receiver_lock(treasury.GUIDED_RECEIVER) as receiver_locked,
            ):
                if observer_locked and receiver_locked:
                    record = dict(g.pending_recv_slots()).get(treasury.GUIDED_RECEIVER)
                    if record is None:
                        last_error = "guided recipient claim is missing"
                    elif checker.observe(
                        cluster, treasury.GUIDED_RECEIVER, record, treasury.gov_state(cluster)
                    ):
                        sdk.always(True, "treasury_withdrawal_recovered")
                        return 0
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            print(f"treasury recovery snapshot unavailable: {exc}", file=sys.stderr)
            sdk.sometimes(True, "treasury_withdrawal_query_unavailable")
        if attempt < checks:
            time.sleep(POLL_SECONDS)
    sdk.always(
        False,
        "treasury_withdrawal_recovered",
        {"checks": checks + 1, "last_error": last_error},
    )
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "treasury_withdrawal_recovery_aborted", "treasury_withdrawal_recovery_exits_zero"
    )
