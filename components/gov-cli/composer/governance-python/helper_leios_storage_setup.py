"""Prepare the opt-in baseline before the first command releases fault injection."""

from __future__ import annotations

import time

import eventually_leios_transactions as timing
import helper_leios_storage as storage
import helper_sdk as sdk
import parallel_driver_leios_storage as submitter


def prepare() -> bool:
    if not storage.enabled():
        return True
    checks = timing.recovery_checks()
    for attempt in range(checks + 1):
        if attempt:
            time.sleep(timing.POLL_SECONDS)
        with storage.locked() as state:
            if state is None:
                continue
            try:
                storage.observe(state)
                if state.get("baseline") and state.get("last_probe_ok"):
                    return True
                if attempt < checks:
                    submitter.supply(state)
            except Exception as exc:  # noqa: BLE001
                sdk.sometimes(
                    True,
                    "leios_storage_query_unavailable",
                    {"phase": "setup", "error": str(exc)},
                    must_hit=False,
                )
    sdk.unreachable("leios_storage_setup_failed")
    return False
