#!/usr/bin/env python3
"""Continue the independent propagation observer after faults stop."""

from __future__ import annotations

import time

import eventually_leios_transactions as timing
import helper_gov as g
import helper_leios_endorser as endorser
import helper_sdk as sdk


def main() -> int:
    if not endorser.enabled():
        return 0
    sdk.reachable("leios_endorser_recovery entered")
    sdk.sometimes(False, endorser.PROPERTY, {"reason": "awaiting_propagation"})
    if not g.SETUP_MARKER.exists():
        return 0
    for attempt in range(timing.recovery_checks() + 1):
        if attempt:
            time.sleep(timing.POLL_SECONDS)
        try:
            with endorser.locked() as state:
                if state is not None:
                    complete, details = endorser.observe(state)
                    sdk.sometimes(complete, endorser.PROPERTY, details)
                    if complete:
                        return 0
                    endorser.supply(state)
        except Exception as exc:  # noqa: BLE001
            sdk.sometimes(True, "leios_endorser_unavailable", {"error": str(exc)}, must_hit=False)
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_endorser_recovery_aborted",
        "leios_endorser_recovery_exits_zero" if endorser.enabled() else None,
    )
