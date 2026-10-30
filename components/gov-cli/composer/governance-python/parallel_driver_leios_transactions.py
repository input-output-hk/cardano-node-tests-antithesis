#!/usr/bin/env python3
"""Submit bounded, independent payments for certified-EB tracking."""

from __future__ import annotations

import contextlib
import json

import helper_gov as g
import helper_leios_transactions as t
import helper_sdk as sdk
from cardano_clusterlib import clusterlib


def submit_payments(cluster: clusterlib.ClusterLib, state: dict) -> None:
    source, recipient = t.account(cluster, "source"), t.account(cluster, "recipient")
    tip = cluster.g_query.get_tip()
    expiry = tip["slot"] + min(cluster.epoch_length, 1500)
    utxos = cluster.g_query.get_utxo(address=source.address)
    if not utxos:
        funding = state.get("funding")
        if funding:
            immutable = json.loads(
                cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"])
            )
            if immutable["slot"] < funding["expiry"]:
                return

        def journal(txid: str) -> None:
            state["funding"] = {"txid": txid, "expiry": expiry}
            t.save(state)

        g.build_sign_submit(
            cluster,
            f"leios_fund_{g.unique_token()}",
            txouts=[clusterlib.TxOut(address=source.address, amount=t.FUND_AMOUNT)],
            invalid_hereafter=expiry,
            before_submit=journal,
        )
        return
    if not state.get("fanout"):
        large = next(
            (u for u in utxos if u.amount > t.BATCH_SIZE * t.INPUT_AMOUNT + 2_000_000), None
        )
        if large is None:
            return
        name = f"leios_fanout_{g.unique_token()}"
        raw = cluster.g_transaction.build_tx(
            src_address=source.address,
            tx_name=name,
            txins=[large],
            txouts=[clusterlib.TxOut(address=source.address, amount=t.INPUT_AMOUNT)] * t.BATCH_SIZE,
            join_txouts=False,
            tx_files=clusterlib.TxFiles(signing_key_files=[source.skey_file]),
            invalid_hereafter=expiry,
            destination_dir=t.ROOT,
        )
        signed = cluster.g_transaction.sign_tx(
            tx_body_file=raw.out_file,
            signing_key_files=[source.skey_file],
            tx_name=name,
            destination_dir=t.ROOT,
        )
        state["fanout"] = {
            "txid": cluster.g_transaction.get_txid(tx_file=signed),
            "expiry": expiry,
            "signed": str(signed),
        }
        t.save(state)
        cluster.g_transaction.submit_tx_bare(tx_file=signed)
        return
    fanout = state["fanout"]
    inputs = [u for u in utxos if u.utxo_hash == fanout["txid"] and u.amount == t.INPUT_AMOUNT]
    if len(inputs) < t.BATCH_SIZE:
        if tip["slot"] < fanout["expiry"]:
            with contextlib.suppress(Exception):
                cluster.g_transaction.submit_tx_bare(tx_file=fanout["signed"])
        immutable = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
        if immutable["slot"] >= fanout["expiry"]:
            del state["fanout"]
        return
    # Build all first, then journal every signed tx before any broadcast.
    metadata = t.ROOT / "metadata.json"
    metadata.write_text(json.dumps({"0": ["e" * 64] * 200}))
    prepared = []
    count = min(t.BATCH_SIZE, t.MAX_PAYMENTS - len(state.get("payments", [])))
    for utxo in inputs[:count]:
        name = f"leios_payment_{g.unique_token()}"
        raw = cluster.g_transaction.build_tx(
            src_address=source.address,
            tx_name=name,
            txins=[utxo],
            txouts=[clusterlib.TxOut(address=recipient.address, amount=t.PAYMENT_AMOUNT)],
            tx_files=clusterlib.TxFiles(
                signing_key_files=[source.skey_file], metadata_json_files=[metadata]
            ),
            invalid_hereafter=expiry,
            destination_dir=t.ROOT,
        )
        signed = cluster.g_transaction.sign_tx(
            tx_body_file=raw.out_file,
            signing_key_files=[source.skey_file],
            tx_name=name,
            destination_dir=t.ROOT,
        )
        reference, size = t.tx_reference(signed)
        prepared.append(
            {
                "txid": cluster.g_transaction.get_txid(tx_file=signed),
                "reference": reference,
                "size": size,
                "recipient": recipient.address,
                "amount": t.PAYMENT_AMOUNT,
                "signed": str(signed),
                "expiry": expiry,
            }
        )
    state.setdefault("payments", []).extend(prepared)
    del state["fanout"]
    t.save(state)
    for payment in prepared:
        # Other independent transactions can still land. Keep all records.
        with contextlib.suppress(Exception):
            cluster.g_transaction.submit_tx_bare(tx_file=payment["signed"])


def main() -> int:
    if not t.enabled():
        return 0
    sdk.reachable("leios_transactions entered")
    sdk.sometimes(False, "leios_tx_certified_and_immutable", {"reason": "awaiting_evidence"})
    if not g.SETUP_MARKER.exists():
        return 0
    with t.locked() as state:
        if state is None or state.get("complete"):
            return 0
        t.start_tracking(state)
        try:
            cluster = g.make_cluster()
            slot = cluster.g_query.get_slot_no()
            for payment in state.get("payments", []):
                if not payment.get("included") and slot < payment["expiry"]:
                    with contextlib.suppress(Exception):
                        cluster.g_transaction.submit_tx_bare(tx_file=payment["signed"])
            if len(state.get("payments", [])) < t.MAX_PAYMENTS:
                submit_payments(cluster, state)
        except Exception as exc:  # noqa: BLE001
            sdk.sometimes(
                True, "leios_tx_submission_unavailable", {"error": str(exc)}, must_hit=False
            )
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_transactions_aborted", "leios_transactions_exits_zero" if t.enabled() else None
    )
