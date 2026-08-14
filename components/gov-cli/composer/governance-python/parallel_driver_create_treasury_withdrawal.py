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

Stateless w.r.t. the action itself: nothing about the action's
lifecycle is published locally - the vote driver finds it straight from
gov-state, so a transient submit failure is harmless (a later tick
creates another one). The one thing that IS recorded locally is the
funds-receiving claim, because reward-account balance deltas can only be
attributed across separate driver ticks that way.
"""

from __future__ import annotations

import sys

import helper_gov as g
import helper_sdk as sdk

MIN_TRANSFER = 1_000_000  # 1 ADA
MAX_TRANSFER = 5_000_000  # 5 ADA


def _claim_recv_slot(cluster):
    """Try every treasury_recv{i} index once (RNG-shuffled start) and
    atomically claim the first free funds-receiving slot. Returns
    (idx, addr) or (None, None) if all NUM_DREPS slots are currently
    pending an unresolved withdrawal."""
    epoch = g.current_epoch(cluster)
    order = list(range(1, g.NUM_DREPS + 1))
    start = g.rng_mod(g.NUM_DREPS)
    order = order[start:] + order[:start]
    for i in order:
        addr_file = g.TREASURY_RECV_DIR / f"treasury_recv{i}_stake.addr"
        if not addr_file.exists():
            continue
        addr = addr_file.read_text().strip()
        pre_balance = cluster.g_query.get_stake_addr_info(addr).reward_account_balance
        if g.claim_recv_slot(i, pre_balance=pre_balance, claimed_epoch=epoch):
            return i, addr
    return None, None


def main() -> int:
    sdk.reachable("create_treasury_withdrawal entered")
    g.ensure_dirs()

    if not g.SETUP_MARKER.exists():
        return 0

    idx = g.rng_mod(g.NUM_PAYMENT_ADDRS)
    addr, lock_fh = g.try_acquire_payment_addr(idx)
    if addr is None:
        return 0  # in use, antithesis retries next tick

    try:
        cluster = g.make_cluster()
        if not g.wait_for_node(cluster, tries=30):
            sdk.unreachable("create_treasury_withdrawal_node_not_ready")
            return 0

        recv_idx, recv_addr = _claim_recv_slot(cluster)
        if recv_idx is None:
            print("all funds-receiving slots pending, skipping tick", file=sys.stderr)
            return 0

        completed = False
        try:
            tok = g.unique_token()
            # Deposit-return reuses the (already-registered, already a
            # deposit-return sink elsewhere) vote_stake_addr pool - a
            # different pool from recv_idx's treasury_recv{i}, so no
            # index-collision handling is needed here.
            ret_idx = g.rng_mod(g.NUM_DREPS) + 1
            ret_vkey = g.GD / f"vote_stake_addr{ret_idx}_stake.vkey"
            recv_vkey = g.TREASURY_RECV_DIR / f"treasury_recv{recv_idx}_stake.vkey"

            deposit = cluster.g_query.get_gov_action_deposit()
            anchor_hash = cluster.g_governance.get_anchor_data_hash(text=g.ANCHOR_TEXT)
            transfer_amt = MIN_TRANSFER + g.rng_mod(MAX_TRANSFER - MIN_TRANSFER + 1)

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
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"treasury withdrawal submit failed transiently: {exc} (will retry)",
                    file=sys.stderr,
                )
                sdk.sometimes(False, "treasury_withdrawal_created")
                return 0

            g.complete_recv_slot(
                recv_idx, recv_addr=recv_addr, txid=txid, ix=0, transfer_amt=transfer_amt
            )
            completed = True

            print(
                f"treasury withdrawal created: {txid} (transfer_amt={transfer_amt})", file=sys.stderr
            )
            sdk.sometimes(True, "treasury_withdrawal_created", {"transfer_amt": transfer_amt})

            if g.recent_stall(cluster):
                sdk.sometimes(True, "gov_op_under_perturbation", {"op": "create_treasury_withdrawal"})
            return 0
        finally:
            if not completed:
                g.release_recv_slot(recv_idx)
    finally:
        g.release_payment_addr(lock_fh)


if __name__ == "__main__":
    sdk.run_driver(main, "create_treasury_withdrawal_aborted", "create_treasury_withdrawal_exits_zero")
