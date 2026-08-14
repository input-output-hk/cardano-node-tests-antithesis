#!/usr/bin/env python3
"""parallel_driver_vote_treasury_withdrawal.py — cast one DRep/CC vote on
one live TreasuryWithdrawals action.

Same shape as parallel_driver_vote.py, restricted to TreasuryWithdrawals
proposals and to DRep/CC voters only: the ledger rejects an SPO vote on
this action type outright (cardano-node-tests' test_enact_treasury_withdrawals
confirms the CLI raises with "StakePoolVoter" if a stake-pool vote is
attempted), so SPOs are deliberately left out of the roster rather than
submitted and expected to fail.

Stateless: the votable set comes straight from the chain via an N2C
gov-state query (relay1 is fault-excluded, so the query answers even
under a block-production stall). There is NO local created/rejected
ledger - unlike an InfoAction, a treasury withdrawal actually leaves the
set once it ratifies+enacts (or expires), which this driver only ever
observes indirectly (the action disappears from live gov-state).
"""

from __future__ import annotations

import sys

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib


def build_voters(cluster: clusterlib.ClusterLib):
    """Build the voter roster: every DRep and CC member (no SPOs - see
    module docstring). Each entry is (kind, create_fn, vkey_kw, vkey_file, skey_file)."""
    voters = []
    for i in range(1, g.NUM_DREPS + 1):
        vkey = g.GD / f"default_drep_{i}_drep.vkey"
        skey = g.GD / f"default_drep_{i}_drep.skey"
        if vkey.exists():
            voters.append(("drep", cluster.g_governance.vote.create_drep, "drep_vkey_file", vkey, skey))
    for i in range(1, g.NUM_CC + 1):
        vkey = g.GD / f"cc_member{i}_committee_hot.vkey"
        skey = g.GD / f"cc_member{i}_committee_hot.skey"
        if vkey.exists():
            voters.append(("cc", cluster.g_governance.vote.create_committee, "cc_hot_vkey_file", vkey, skey))
    return voters


def main() -> int:
    sdk.reachable("vote_treasury_withdrawal entered")
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
            sdk.unreachable("vote_treasury_withdrawal_node_not_ready")
            return 0

        # Pull the live TreasuryWithdrawals set from gov-state and RNG-select one.
        props = g.live_treasury_withdrawal_actions(cluster)
        sdk.sometimes(len(props) >= 1, "treasury_withdrawal_actions_live", {"live": len(props)})
        if not props:
            print("no live treasury withdrawal actions in gov-state", file=sys.stderr)
            return 0

        pick = props[g.rng_mod(len(props))]
        txid = pick["actionId"]["txId"]
        ix = pick["actionId"]["govActionIx"]

        voters = build_voters(cluster)
        if not voters:
            return 0

        kind, create_fn, vkey_kw, vkey_file, skey_file = voters[g.rng_mod(len(voters))]

        decisions = [
            (clusterlib.Votes.YES, "yes"),
            (clusterlib.Votes.NO, "no"),
            (clusterlib.Votes.ABSTAIN, "abstain"),
        ]
        vote_enum, decision = decisions[g.rng_mod(3)]

        tok = g.unique_token()
        print(f"voting {decision} as {kind} on treasury withdrawal {txid}#{ix}", file=sys.stderr)

        vote = create_fn(
            vote_name=f"treasury_{kind}_{tok}",
            action_txid=txid,
            action_ix=ix,
            vote=vote_enum,
            destination_dir=str(g.WORK),
            **{vkey_kw: vkey_file},
        )

        try:
            g.build_sign_submit(
                cluster,
                f"treasury_vote_{tok}",
                vote_files=[vote.vote_file],
                signing_key_files=[skey_file],
                src_addr=addr,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"treasury withdrawal vote submit failed transiently for {txid}: {exc} (will retry)",
                file=sys.stderr,
            )
            sdk.sometimes(True, "treasury_withdrawal_vote_transient_failure", {"voter": kind})
            return 0
        sdk.reachable("treasury_withdrawal_vote_submitted")

        drep_n = cc_n = 0
        try:
            prop = g.lookup_proposal(cluster.g_query.get_gov_state(), txid) or {}
            drep_n, _spo_n, cc_n = g.vote_counts(prop)
        except Exception:  # noqa: BLE001
            pass
        total = drep_n + cc_n
        majority = (g.NUM_DREPS + g.NUM_CC + 1) // 2

        sdk.sometimes(total >= 1, f"treasury_withdrawal_vote_recorded_{kind}")
        sdk.sometimes(True, f"treasury_withdrawal_vote_decision_{decision}")
        sdk.sometimes(
            True,
            f"treasury_withdrawal_vote_decision_{decision}_by_{kind}",
            {"voter": kind, "decision": decision},
        )

        all_roles = drep_n >= 1 and cc_n >= 1
        sdk.sometimes(
            all_roles,
            "treasury_withdrawal_voted_by_drep_and_cc",
            {"drep": drep_n, "cc": cc_n},
        )
        sdk.sometimes(
            total >= majority,
            "treasury_withdrawal_majority_reached",
            {"total": total, "majority": majority},
        )

        if g.recent_stall(cluster):
            sdk.sometimes(True, "gov_op_under_perturbation", {"op": "vote_treasury_withdrawal", "voter": kind})

        print(
            f"treasury withdrawal vote submitted ({kind} {decision}; action now has {total} votes)",
            file=sys.stderr,
        )
        return 0
    finally:
        g.release_payment_addr(lock_fh)


if __name__ == "__main__":
    sdk.run_driver(main, "vote_treasury_withdrawal_aborted", "vote_treasury_withdrawal_exits_zero")
