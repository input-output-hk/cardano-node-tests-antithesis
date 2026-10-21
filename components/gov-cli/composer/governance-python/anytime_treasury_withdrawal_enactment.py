#!/usr/bin/env python3
"""Observe ledger ratification and settled treasury payouts under faults.

Only a consistent immutable snapshot can resolve a claim. The guided receiver
stays reserved after payout, allowing later ticks to detect a delayed duplicate
payment. Unavailable queries leave claims untouched for another tick.
"""

from __future__ import annotations

import sys

import helper_gov as g
import helper_sdk as sdk
import helper_treasury as treasury
from cardano_clusterlib import clusterlib

STALE_CLAIM_EPOCHS = 2


def observe(cluster: clusterlib.ClusterLib, idx: int, record: dict, live: dict) -> None:
    if "txid" not in record:
        # No broadcast occurred: the action id is journalled before submission.
        tip = treasury.query(cluster, ["tip"], immutable=True)
        if tip["epoch"] - record["claimed_epoch"] >= STALE_CLAIM_EPOCHS:
            g.release_recv_slot(idx)
            sdk.sometimes(True, "treasury_withdrawal_slot_reclaimed_after_abandonment")
        return

    txid, ix = record["txid"], record["ix"]
    tip, state, balance = treasury.immutable_snapshot(cluster, record["recv_addr"])
    delta = balance - record["pre_balance"]
    expected = record["transfer_amt"]
    details = {"txid": txid, "delta": delta, "expected": expected, "immutable_slot": tip["slot"]}
    sdk.always(0 <= delta <= expected, "treasury_withdrawal_no_overpay", details)

    ratified = state["nextRatifyState"]["enactedGovActions"]
    if any(treasury.action_id(p) == (txid, ix) for p in ratified):
        # This is the ledger's stake-weighted DRep + CC decision, not a
        # headcount approximation. Immutable evidence survives rollbacks.
        g.complete_recv_slot(idx, ratified_epoch=tip["epoch"])
        record["ratified_epoch"] = tip["epoch"]
        sdk.sometimes(True, "treasury_withdrawal_ratified", {"txid": txid})

    immutable_action = treasury.find_action(state, txid, ix)
    if immutable_action is not None:
        # Persist inclusion evidence; absence alone could be a failed submit.
        expires_after = immutable_action["expiresAfter"]
        if record.get("expires_after") != expires_after:
            g.complete_recv_slot(idx, expires_after=expires_after)
            record["expires_after"] = expires_after
    if immutable_action or treasury.find_action(live, txid, ix):
        return
    # An uncertain submission is terminal only once a delayed inclusion is
    # impossible at the immutable tip.
    if tip["slot"] < record["valid_until"]:
        return
    if "ratified_epoch" in record:
        if tip["epoch"] <= record["ratified_epoch"]:
            return
        sdk.always(delta == expected, "treasury_withdrawal_payout_matches_ratification", details)

    paid = delta == expected
    sdk.sometimes(paid, "treasury_withdrawal_enacted_correctly", details)
    expired = (
        "expires_after" in record
        and tip["epoch"] > record["expires_after"]
        and "ratified_epoch" not in record
    )
    if expired:
        sdk.sometimes(
            delta == 0, "treasury_withdrawal_resolved_without_payout", details, must_hit=False
        )
        if idx == treasury.EXPIRY_RECEIVER:
            sdk.always(delta == 0, "treasury_withdrawal_expired_no_payout", details)
            # Keep the isolated receiver to detect a delayed erroneous payment.
            return
    if idx == treasury.GUIDED_RECEIVER and paid:
        # Keep checking this recipient for the rest of the run. Reusing it
        # would hide a late duplicate in the next claim's baseline.
        return
    g.release_recv_slot(idx)


def main() -> int:
    sdk.reachable("treasury_withdrawal_enactment entered")
    if not g.SETUP_MARKER.exists():
        return 0
    # Register even if no proposal ever reaches the corresponding phase.
    sdk.sometimes(False, "treasury_withdrawal_ratified")
    sdk.sometimes(False, "treasury_withdrawal_enacted_correctly")
    # Natural expiry can outlive the production three-hour budget. Keep the
    # observation visible without requiring an impossible coverage hit.
    sdk.sometimes(False, "treasury_withdrawal_resolved_without_payout", must_hit=False)
    cluster = g.make_cluster()
    with treasury.lock() as locked:
        if not locked:
            return 0
        try:
            live = treasury.gov_state(cluster)
        except Exception as exc:  # noqa: BLE001
            print(f"treasury governance query unavailable: {exc}", file=sys.stderr)
            sdk.sometimes(True, "treasury_withdrawal_query_unavailable")
            return 0
        for prop in treasury.actions(live):
            treasury.participation(prop)
        for idx, _record in g.pending_recv_slots():
            with treasury.receiver_lock(idx) as receiver_locked:
                if not receiver_locked:
                    continue
                # A creator may have replaced a claim since the first scan.
                record = dict(g.pending_recv_slots()).get(idx)
                if record is None:
                    continue
                try:
                    observe(cluster, idx, record, live)
                except Exception as exc:  # noqa: BLE001
                    print(f"treasury claim {idx} snapshot unavailable: {exc}", file=sys.stderr)
                    sdk.sometimes(True, "treasury_withdrawal_query_unavailable")
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "treasury_withdrawal_enactment_aborted", "treasury_withdrawal_enactment_exits_zero"
    )
