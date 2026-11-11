#!/usr/bin/env python3
"""Allow the isolated storage node to recover after faults stop."""

from __future__ import annotations

import time

import anytime_leios_storage as observer
import eventually_leios_transactions as timing
import helper_gov as g
import helper_leios_storage as storage
import helper_sdk as sdk
import parallel_driver_leios_storage as submitter


def main() -> int:
    if not storage.enabled():
        return 0
    sdk.reachable("leios_storage_recovery entered")
    observer.main()
    if not g.SETUP_MARKER.exists():
        return 0
    for attempt in range(timing.recovery_checks() + 1):
        if attempt:
            time.sleep(timing.POLL_SECONDS)
        observer.main()
        with storage.locked() as state:
            if state is not None and storage.recovery_complete(state):
                return 0
        submitter.main()
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_storage_recovery_aborted",
        "leios_storage_recovery_exits_zero" if storage.enabled() else None,
    )
