#!/usr/bin/env python3
"""Continue private script submission and observation after faults stop."""

import time

import eventually_leios_transactions as timing
import helper_leios_plutus as plutus
import helper_sdk as sdk


def main() -> int:
    if not plutus.enabled():
        return 0
    sdk.reachable("leios_plutus_recovery entered")
    for attempt in range(timing.recovery_checks() + 1):
        if attempt:
            time.sleep(timing.POLL_SECONDS)
        if plutus.tick(submit=True):
            return 0
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_plutus_recovery_aborted",
        "leios_plutus_recovery_exits_zero" if plutus.enabled() else None,
    )
