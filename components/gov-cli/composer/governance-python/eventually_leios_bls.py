#!/usr/bin/env python3
"""Submit and observe after faults, even if the parallel driver never ran."""

import time

import eventually_leios_transactions as timing
import helper_leios_bls as bls
import helper_sdk as sdk


def main() -> int:
    if not bls.enabled():
        return 0
    sdk.reachable("leios_bls_recovery entered")
    for attempt in range(timing.recovery_checks() + 1):
        if attempt:
            time.sleep(timing.POLL_SECONDS)
        if bls.tick(submit=True):
            break
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_bls_recovery_aborted",
        "leios_bls_recovery_exits_zero" if bls.enabled() else None,
    )
