#!/usr/bin/env python3
"""Supply a private InfoAction and observe immutable votes after faults stop."""

from __future__ import annotations

import time

import helper_gov as g
import helper_sdk as sdk
import helper_vote_recovery as recovery


def main() -> int:
    sdk.reachable("eventually_votes entered")
    sdk.sometimes(False, recovery.COVERAGE, {"reason": "awaiting_private_immutable_votes"})
    if not g.SETUP_MARKER.exists():
        return 0
    g.ensure_dirs()
    readable = False
    for attempt in range(recovery.recovery_checks() + 1):
        if attempt:
            time.sleep(recovery.POLL_SECONDS)
        try:
            with recovery.locked() as state:
                if state is None:
                    continue
                state.pop("last", None)
                try:
                    complete = recovery.step(g.make_cluster(), state)
                finally:
                    readable = readable or bool(state.get("last"))
                sdk.sometimes(complete, recovery.COVERAGE, state.get("last"))
                if complete:
                    return 0
        except Exception as exc:  # noqa: BLE001
            sdk.sometimes(True, "eventually_votes_unavailable", {"error": str(exc)}, must_hit=False)
    if not readable:
        sdk.unreachable("eventually_cold_start")
    return 0


if __name__ == "__main__":
    sdk.run_driver(main, "eventually_aborted", "eventually_votes_exits_zero")
