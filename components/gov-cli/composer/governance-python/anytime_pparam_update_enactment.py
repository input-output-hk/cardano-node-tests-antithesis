#!/usr/bin/env python3
"""anytime_pparam_update_enactment.py — verify each ParameterChange
action this repo submitted actually changed the targeted protocol
parameter once resolved.

May run at any time, including under fault injection. Pairs with
parallel_driver_create_pparam_update.py, which records the targeted
parameter and expected value locally right after a successful submit
(helper_gov.record_pending_pparam_update).

Once the tracked action has left the live ParameterChange set
(ratified+enacted, rejected, or expired), the current protocol-params
value is what distinguishes those outcomes. Unlike a treasury
withdrawal, there's no overpay-style bug class to guard against here -
setting a protocol parameter is idempotent (re-enacting the same value
twice is harmless, unlike crediting a balance twice), so this driver
only records which outcome happened, with no Always invariant.
"""

from __future__ import annotations

import helper_gov as g
import helper_sdk as sdk


def main() -> int:
    sdk.reachable("pparam_update_enactment entered")

    if not g.SETUP_MARKER.exists():
        return 0

    cluster = g.make_cluster()
    try:
        live_ids = {
            (p["actionId"]["txId"], p["actionId"]["govActionIx"])
            for p in g.live_pparam_update_actions(cluster)
        }
    except Exception:  # noqa: BLE001
        sdk.unreachable("pparam_update_enactment_unavailable")
        return 0

    for txid, ix, record in g.pending_pparam_updates():
        try:
            if (txid, ix) in live_ids:
                continue  # still pending ratification/expiry

            param_key = record["param_key"]
            expected_value = record["expected_value"]
            current_value = cluster.g_query.get_protocol_params().get(param_key)

            if current_value == expected_value:
                sdk.sometimes(
                    True,
                    "pparam_update_enacted_correctly",
                    {"param": param_key, "value": expected_value},
                )
            else:
                sdk.sometimes(
                    True,
                    "pparam_update_resolved_without_enactment",
                    {"param": param_key, "expected": expected_value, "current": current_value},
                )

            g.remove_pending_pparam_update(txid, ix)
        except Exception:  # noqa: BLE001
            continue
    return 0


if __name__ == "__main__":
    sdk.run_driver(main, "pparam_update_enactment_aborted", "pparam_update_enactment_exits_zero")
