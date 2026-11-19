#!/usr/bin/env python3
"""Observe nonempty EB propagation across invocations and log rotation."""

from __future__ import annotations

import helper_gov as g
import helper_leios_endorser as endorser
import helper_sdk as sdk


def main() -> int:
    if not endorser.enabled():
        return 0
    sdk.reachable("leios_endorser entered")
    sdk.sometimes(False, endorser.PROPERTY, {"reason": "awaiting_propagation"})
    if not g.SETUP_MARKER.exists():
        return 0
    try:
        with endorser.locked() as state:
            if state is not None:
                complete, details = endorser.observe(state)
                sdk.sometimes(complete, endorser.PROPERTY, details)
                if not complete:
                    endorser.supply(state)
    except Exception as exc:  # noqa: BLE001
        sdk.sometimes(True, "leios_endorser_unavailable", {"error": str(exc)}, must_hit=False)
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_endorser_aborted",
        "leios_endorser_exits_zero" if endorser.enabled() else None,
    )
