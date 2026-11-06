#!/usr/bin/env python3
"""Supply isolated EB traffic until the storage node has recovered."""

from __future__ import annotations

import json

import helper_gov as g
import helper_leios_storage as storage
import helper_leios_storage_traffic as traffic
import helper_sdk as sdk
from parallel_driver_leios_transactions import submit_payments


def main() -> int:
    if not storage.enabled():
        return 0
    sdk.reachable("leios_storage_traffic entered")
    if not g.SETUP_MARKER.exists():
        return 0
    with storage.locked() as state:
        if state is None or state.get("recovered"):
            return 0
        try:
            cluster = g.make_cluster()
            tip = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
            journal = state.setdefault("traffic", {})
            # Only immutable expiry retires a batch; no other workload's
            # execution or completion flag can stop this traffic.
            journal["payments"] = [
                p for p in journal.get("payments", []) if p["expiry"] > tip["slot"]
            ]
            if len(journal["payments"]) < traffic.Traffic.MAX_PAYMENTS:
                submit_payments(cluster, journal, workload=traffic.Traffic(state))
        except Exception as exc:  # noqa: BLE001
            sdk.sometimes(
                True, "leios_storage_submission_unavailable", {"error": str(exc)}, must_hit=False
            )
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_storage_traffic_aborted",
        "leios_storage_traffic_exits_zero" if storage.enabled() else None,
    )
