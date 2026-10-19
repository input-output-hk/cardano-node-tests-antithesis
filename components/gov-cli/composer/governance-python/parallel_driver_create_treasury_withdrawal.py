#!/usr/bin/env python3
"""parallel_driver_create_treasury_withdrawal.py — submit one TreasuryWithdrawal action.

One logical cardano-cli governance operation = one driver, same shape as
parallel_driver_create_action.py. Unlike an InfoAction, a treasury
withdrawal DOES enact once it clears DRep+CC ratification (SPOs cannot
vote on it at all - see parallel_driver_vote_treasury_withdrawal.py), so
this exercises the enactment/treasury-debit code path the InfoAction
workload deliberately avoids. transfer_amt is kept small (1-5 ADA) so a
long fault-injection run can't meaningfully drain the treasury even if
many of these actions end up enacted.

Funds-receiving target comes from the dedicated treasury_recv{i} pool
first_setup.py registers (helper_gov.TREASURY_RECV_DIR) - NOT from
vote_stake_addr{i}, which is already a deposit-return sink elsewhere
(parallel_driver_create_action.py's InfoActions, and this driver's own
deposit-return pick below) and would contaminate the balance-delta
check in anytime_treasury_withdrawal_enactment.py with unrelated
refunds. The recv index is claimed (helper_gov.claim_recv_slot) rather
than picked by plain RNG: at most one pending withdrawal is ever
attached to a given treasury_recv{i} at a time, which is what lets that
checker later tell "this address's reward balance changed" apart from
"which of several withdrawals caused it." Deposit-return reuses the
existing vote_stake_addr{i} pool (already registered, already a
deposit-return sink, so no new contamination) and isn't claimed since
this driver doesn't track that payout.

The chain supplies live actions. Recipient claims retain attribution metadata,
including the signed action id before broadcast and a submission validity
bound. An interrupted submission therefore remains tracked until immutable
state proves it can be resolved. Receiver 1 is reserved for guided YES voting;
other receivers retain the random workload.
"""

from __future__ import annotations

import sys

import helper_gov as g
import helper_sdk as sdk
import helper_treasury as treasury
from cardano_clusterlib import clusterlib

MIN_TRANSFER = 1_000_000  # 1 ADA
MAX_TRANSFER = 5_000_000  # 5 ADA


def _claim_recv_slot(cluster: clusterlib.ClusterLib) -> tuple[int | None, str | None]:
    """Try the guided receiver first, then a randomly rotated remainder.

    Atomically claim the first free funds-receiving slot. Returns
    (idx, addr) or (None, None) if all NUM_DREPS slots are currently
    pending an unresolved withdrawal."""
    epoch = g.current_epoch(cluster)
    order = list(range(2, g.NUM_DREPS + 1))
    if order:
        start = g.rng_mod(len(order))
        order = order[start:] + order[:start]
    order = [treasury.GUIDED_RECEIVER, *order]
    claimed = {idx for idx, _record in g.pending_recv_slots()}
    for i in order:
        if i in claimed:
            continue
        addr_file = g.TREASURY_RECV_DIR / f"treasury_recv{i}_stake.addr"
        if not addr_file.exists():
            continue
        addr = addr_file.read_text().strip()
        _tip, _state, pre_balance = treasury.immutable_snapshot(cluster, addr)
        if g.claim_recv_slot(i, pre_balance=pre_balance, claimed_epoch=epoch):
            return i, addr
    return None, None


def create(cluster: clusterlib.ClusterLib, addr: clusterlib.AddressRecord) -> int:
    try:
        recv_idx, recv_addr = _claim_recv_slot(cluster)
    except Exception as exc:  # noqa: BLE001
        print(f"treasury recipient snapshot unavailable: {exc}", file=sys.stderr)
        sdk.sometimes(True, "treasury_withdrawal_query_unavailable")
        return 0
    if recv_idx is None:
        print("all funds-receiving slots pending, skipping tick", file=sys.stderr)
        return 0

    broadcast_intent = False

    def journal(txid: str) -> None:
        nonlocal broadcast_intent
        g.complete_recv_slot(recv_idx, txid=txid, ix=0)
        broadcast_intent = True

    try:
        tok = g.unique_token()
        ret_idx = g.rng_mod(g.NUM_DREPS) + 1
        ret_vkey = g.GD / f"vote_stake_addr{ret_idx}_stake.vkey"
        recv_vkey = g.TREASURY_RECV_DIR / f"treasury_recv{recv_idx}_stake.vkey"
        deposit = cluster.g_query.get_gov_action_deposit()
        anchor_hash = cluster.g_governance.get_anchor_data_hash(text=g.ANCHOR_TEXT)
        transfer_amt = MIN_TRANSFER + g.rng_mod(MAX_TRANSFER - MIN_TRANSFER + 1)
        # Bound an uncertain submission: after this slot becomes immutable,
        # an absent proposal cannot be introduced by a delayed retry.
        valid_until = cluster.g_query.get_slot_no() + cluster.epoch_length
        g.complete_recv_slot(
            recv_idx, recv_addr=recv_addr, transfer_amt=transfer_amt, valid_until=valid_until
        )
        withdrawal = cluster.g_governance.action.create_treasury_withdrawal(
            action_name=f"treasury_withdrawal_{tok}",
            transfer_amt=transfer_amt,
            deposit_amt=deposit,
            anchor_url=g.ANCHOR_URL,
            anchor_data_hash=anchor_hash,
            funds_receiving_stake_vkey_file=recv_vkey,
            deposit_return_stake_vkey_file=ret_vkey,
            destination_dir=str(g.WORK),
        )
        try:
            txid = g.build_sign_submit(
                cluster,
                f"treasury_withdrawal_{tok}",
                proposal_files=[withdrawal.action_file],
                src_addr=addr,
                invalid_hereafter=valid_until,
                before_submit=journal,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"treasury withdrawal submit interrupted: {exc} (will reconcile)", file=sys.stderr
            )
            sdk.sometimes(False, "treasury_withdrawal_created")
            return 0
        print(
            f"treasury withdrawal created: {txid} (transfer_amt={transfer_amt}, "
            f"guided={recv_idx == treasury.GUIDED_RECEIVER})",
            file=sys.stderr,
        )
        sdk.sometimes(True, "treasury_withdrawal_created", {"transfer_amt": transfer_amt})
        if g.recent_stall(cluster):
            sdk.sometimes(True, "gov_op_under_perturbation", {"op": "create_treasury_withdrawal"})
        return 0
    finally:
        if not broadcast_intent:
            g.release_recv_slot(recv_idx)


def main() -> int:
    sdk.reachable("create_treasury_withdrawal entered")
    g.ensure_dirs()
    if not g.SETUP_MARKER.exists():
        return 0
    idx = g.rng_mod(g.NUM_PAYMENT_ADDRS)
    addr, lock_fh = g.try_acquire_payment_addr(idx)
    if addr is None:
        return 0
    try:
        cluster = g.make_cluster()
        if not g.wait_for_node(cluster, tries=30):
            sdk.unreachable("create_treasury_withdrawal_node_not_ready")
            return 0
        with treasury.lock() as locked:
            if not locked:
                return 0
            return create(cluster, addr)
    finally:
        g.release_payment_addr(lock_fh)


if __name__ == "__main__":
    sdk.run_driver(
        main, "create_treasury_withdrawal_aborted", "create_treasury_withdrawal_exits_zero"
    )
