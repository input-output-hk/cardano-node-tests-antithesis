#!/usr/bin/env python3
"""Follow owned transactions through certified EBs into the immutable ledger."""

from __future__ import annotations

import helper_gov as g
import helper_leios_transactions as t
import helper_sdk as sdk


def observe(state: dict, tip: dict, utxos: dict) -> None:
    ancestors = t.immutable_ancestors(state, tip["hash"])
    for payment in state.get("payments", []):
        if f"{payment['txid']}#0" in utxos:
            payment["included"] = True
        proof = payment.get("proof") or t.certified_payment(state, payment, ancestors)
        if proof is None:
            continue
        details = {"txid": payment["txid"], "immutable_hash": tip["hash"], **proof}
        size = payment.get("ebs", {}).get(proof["eb_hash"])
        sdk.always(size == payment["size"], "leios_eb_tx_size_matches", details)
        output = utxos.get(f"{payment['txid']}#0")
        matches = (
            isinstance(output, dict)
            and output.get("address") == payment["recipient"]
            and output.get("value") == {"lovelace": payment["amount"]}
        )
        # Only an immutable certifying RB makes absence a safety failure.
        # Ordinary RB inclusion, orphaned certificates and query errors cannot.
        sdk.always(matches, "leios_certified_tx_payment_matches", details)
        sdk.sometimes(
            matches and size == payment["size"], "leios_tx_certified_and_immutable", details
        )
        payment["proof"] = proof
        if matches and size == payment["size"]:
            state["complete"] = True
    if not state.get("complete"):
        # Retire only after the validity bound is immutable. This is a bounded
        # attribution journal, not a claim that an unproven payment used an EB.
        # Fresh independent batches may then provide the missing witness.
        state["payments"] = [p for p in state.get("payments", []) if p["expiry"] > tip["slot"]]


def main() -> int:
    if not t.enabled():
        return 0
    sdk.reachable("leios_tx_tracking entered")
    sdk.sometimes(False, "leios_tx_certified_and_immutable", {"reason": "awaiting_evidence"})
    if not g.SETUP_MARKER.exists():
        return 0
    with t.locked() as state:
        if state is None:
            return 0
        t.ingest(state)
        payments = state.get("payments", [])
        if not payments:
            return 0
        try:
            tip, utxos = t.immutable_payments(g.make_cluster(), payments)
        except Exception as exc:  # noqa: BLE001
            sdk.sometimes(True, "leios_tx_query_unavailable", {"error": str(exc)}, must_hit=False)
            return 0
        observe(state, tip, utxos)
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_tx_tracking_aborted", "leios_tx_tracking_exits_zero" if t.enabled() else None
    )
