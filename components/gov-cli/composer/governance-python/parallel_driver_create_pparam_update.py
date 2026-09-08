#!/usr/bin/env python3
"""parallel_driver_create_pparam_update.py — submit one ParameterChange
action.

Unlike an InfoAction or a TreasuryWithdrawal, this action type is
*chained*: the ledger tracks one "current" ParameterChange lineage at a
time, and every new proposal must reference the previous one's
txid/index via --prev-governance-action-tx-id/--prev-governance-action-
index (helper_gov.get_prev_pparam_action queries this live off gov-state
each tick) or the ledger rejects it outright. This DOES ratify and enact
once DRep+SPO+CC approval clears (unlike a treasury withdrawal, SPOs CAN
vote on this action type - see the vote driver), exercising the
enactment path for a real protocol-parameter change rather than a
one-off balance transfer.

Only ever targets a small allowlist of parameters confirmed safe to
toggle repeatedly: neither affects fee/size math other drivers rely on,
nor anything this testnet's own genesis/config hardcodes (epoch length,
security param - those aren't governance-updatable pparams at all).
Values alternate between two fixed endpoints each tick, so the change is
always visible and always reversible.
"""

from __future__ import annotations

import sys

import helper_gov as g
import helper_sdk as sdk


def main() -> int:
    sdk.reachable("create_pparam_update entered")
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
            sdk.unreachable("create_pparam_update_node_not_ready")
            return 0

        param_key, param_arg, values = g.PPARAM_ALLOWLIST[g.rng_mod(len(g.PPARAM_ALLOWLIST))]
        value = values[g.rng_mod(2)]

        try:
            prev_txid, prev_ix = g.get_prev_pparam_action(cluster)
        except Exception as exc:  # noqa: BLE001
            print(f"could not query prev ParameterChange action: {exc} (will retry)", file=sys.stderr)
            return 0

        tok = g.unique_token()

        ret_idx = g.rng_mod(g.NUM_DREPS) + 1
        ret_vkey = g.GD / f"vote_stake_addr{ret_idx}_stake.vkey"

        deposit = cluster.g_query.get_gov_action_deposit()
        anchor_hash = cluster.g_governance.get_anchor_data_hash(text=g.ANCHOR_TEXT)

        pparam_update = cluster.g_governance.action.create_pparams_update(
            action_name=f"pparam_update_{tok}",
            deposit_amt=deposit,
            anchor_url=g.ANCHOR_URL,
            anchor_data_hash=anchor_hash,
            cli_args=[param_arg, str(value)],
            prev_action_txid=prev_txid,
            prev_action_ix=prev_ix,
            deposit_return_stake_vkey_file=ret_vkey,
            destination_dir=str(g.WORK),
        )

        try:
            txid = g.build_sign_submit(
                cluster,
                f"pparam_update_{tok}",
                proposal_files=[pparam_update.action_file],
                src_addr=addr,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"pparam update submit failed transiently: {exc} (will retry)",
                file=sys.stderr,
            )
            sdk.sometimes(False, "pparam_update_created")
            return 0

        g.record_pending_pparam_update(txid, 0, param_key, value)

        print(
            f"pparam update created: {txid} ({param_key}={value})", file=sys.stderr
        )
        sdk.sometimes(True, "pparam_update_created", {"param": param_key, "value": value})

        if g.recent_stall(cluster):
            sdk.sometimes(True, "gov_op_under_perturbation", {"op": "create_pparam_update"})
        return 0
    finally:
        g.release_payment_addr(lock_fh)


if __name__ == "__main__":
    sdk.run_driver(main, "create_pparam_update_aborted", "create_pparam_update_exits_zero")
