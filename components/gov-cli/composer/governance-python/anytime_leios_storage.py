#!/usr/bin/env python3
"""Observe the isolated faulted Leios node's SQLite recovery."""

from __future__ import annotations

import helper_gov as g
import helper_leios_storage as storage
import helper_sdk as sdk


def main() -> int:
    if not storage.enabled():
        return 0
    sdk.reachable("leios_storage entered")
    for property_id in storage.COVERAGE:
        sdk.sometimes(False, property_id, {"reason": "awaiting_evidence"})
    if not g.SETUP_MARKER.exists():
        return 0
    with storage.locked() as state:
        if state is None:
            return 0
        try:
            storage.observe(state)
        except Exception as exc:  # noqa: BLE001
            # File/socket/SQLite reads race fault injection. Missing evidence
            # cannot count as recovery; the mandatory coverage stays pending.
            sdk.sometimes(
                True, "leios_storage_query_unavailable", {"error": str(exc)}, must_hit=False
            )
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_storage_aborted", "leios_storage_exits_zero" if storage.enabled() else None
    )
