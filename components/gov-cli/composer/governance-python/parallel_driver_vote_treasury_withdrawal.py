#!/usr/bin/env python3
"""Drive one treasury proposal towards payout; retain random voting on others.

The guided proposal gets all missing DRep/CC YES votes in one transaction.
Random voting never overwrites those approvals. Headcount is participation
coverage only: the ledger's ratification state supplies the approval oracle.
"""

from __future__ import annotations

import sys

import helper_gov as g
import helper_sdk as sdk
import helper_treasury as treasury
from cardano_clusterlib import clusterlib


def build_voters(cluster: clusterlib.ClusterLib) -> list[g.Voter]:
    voters = []
    for i in range(1, g.NUM_DREPS + 1):
        vkey = g.GD / f"default_drep_{i}_drep.vkey"
        skey = g.GD / f"default_drep_{i}_drep.skey"
        if vkey.exists():
            voters.append(
                ("drep", cluster.g_governance.vote.create_drep, "drep_vkey_file", vkey, skey)
            )
    for i in range(1, g.NUM_CC + 1):
        vkey = g.GD / f"cc_member{i}_committee_hot.vkey"
        skey = g.GD / f"cc_member{i}_committee_hot.skey"
        if vkey.exists():
            voters.append(
                ("cc", cluster.g_governance.vote.create_committee, "cc_hot_vkey_file", vkey, skey)
            )
    return voters


def voter_hash(cluster: clusterlib.ClusterLib, voter: g.Voter) -> str:
    if voter[0] == "drep":
        return cluster.g_governance.drep.get_id(drep_vkey_file=voter[3], out_format="hex")
    return cluster.g_governance.committee.get_key_hash(vkey_file=voter[3])


def recorded_vote(prop: dict, kind: str, key_hash: str) -> str | None:
    votes = prop["dRepVotes" if kind == "drep" else "committeeVotes"]
    return next((v for k, v in votes.items() if k.removeprefix("keyHash-") == key_hash), None)


def cast(cluster: clusterlib.ClusterLib, addr: clusterlib.AddressRecord) -> int:
    state = treasury.gov_state(cluster)
    props = treasury.actions(state)
    sdk.sometimes(bool(props), "treasury_withdrawal_actions_live", {"live": len(props)})
    for prop in props:
        treasury.participation(prop)
    if not props:
        return 0

    voters = build_voters(cluster)
    keyed = [(voter, voter_hash(cluster, voter)) for voter in voters]
    guided_id = treasury.guided_id()
    expiry_id = treasury.receiver_id(treasury.EXPIRY_RECEIVER)
    guided = next((p for p in props if treasury.action_id(p) == guided_id), None)
    selected = []
    if guided:
        selected = [
            (voter, key)
            for voter, key in keyed
            if recorded_vote(guided, voter[0], key) != "VoteYes"
        ]
        sdk.sometimes(
            bool(keyed) and not selected,
            "treasury_withdrawal_guided_votes_recorded",
            {"txid": guided_id[0]},
        )
    if selected:
        pick = guided
        vote_enum, decision = clusterlib.Votes.YES, "yes"
    else:
        # Concentrate random votes; prefer an absent vote before replacing one.
        candidates = [p for p in props if treasury.action_id(p) not in (guided_id, expiry_id)]
        candidates.sort(key=lambda p: (p["expiresAfter"], treasury.action_id(p)))
        candidates = candidates[:3]
        if not candidates:
            return 0
        pick = candidates[g.rng_mod(len(candidates))]
        missing = [(v, key) for v, key in keyed if recorded_vote(pick, v[0], key) is None]
        pool = missing or keyed
        if not pool:
            return 0
        selected = [pool[g.rng_mod(len(pool))]]
        decisions = [
            (clusterlib.Votes.YES, "yes"),
            (clusterlib.Votes.NO, "no"),
            (clusterlib.Votes.ABSTAIN, "abstain"),
        ]
        vote_enum, decision = decisions[g.rng_mod(len(decisions))]

    txid, ix = treasury.action_id(pick)
    tok = g.unique_token()
    vote_files = []
    signing = []
    for n, (voter, _key) in enumerate(selected):
        kind, create_fn, vkey_kw, vkey_file, skey_file = voter
        vote = create_fn(
            vote_name=f"treasury_{kind}_{tok}_{n}",
            action_txid=txid,
            action_ix=ix,
            vote=vote_enum,
            destination_dir=str(g.WORK),
            **{vkey_kw: vkey_file},
        )
        vote_files.append(vote.vote_file)
        signing.append(skey_file)
    try:
        g.build_sign_submit(
            cluster,
            f"treasury_vote_{tok}",
            vote_files=vote_files,
            signing_key_files=signing,
            src_addr=addr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"treasury withdrawal votes interrupted: {exc} (will retry)", file=sys.stderr)
        sdk.sometimes(True, "treasury_withdrawal_vote_transient_failure")
        return 0

    sdk.reachable("treasury_withdrawal_vote_submitted")
    print(f"submitted {len(selected)} {decision} votes on treasury {txid}#{ix}", file=sys.stderr)
    prop = treasury.find_action(treasury.gov_state(cluster), txid, ix)
    for voter, key in selected:
        kind = voter[0]
        sdk.sometimes(True, f"treasury_withdrawal_vote_decision_{decision}")
        sdk.sometimes(
            True,
            f"treasury_withdrawal_vote_decision_{decision}_by_{kind}",
            {"voter": kind, "decision": decision},
        )
        if prop is not None:
            sdk.sometimes(
                recorded_vote(prop, kind, key) == f"Vote{decision.capitalize()}",
                f"treasury_withdrawal_vote_recorded_{kind}",
            )
    if prop is not None:
        treasury.participation(prop)
        if treasury.action_id(prop) == guided_id:
            sdk.sometimes(
                bool(keyed)
                and all(recorded_vote(prop, v[0], key) == "VoteYes" for v, key in keyed),
                "treasury_withdrawal_guided_votes_recorded",
                {"txid": txid},
            )
    if g.recent_stall(cluster):
        sdk.sometimes(True, "gov_op_under_perturbation", {"op": "vote_treasury_withdrawal"})
    return 0


def main() -> int:
    sdk.reachable("vote_treasury_withdrawal entered")
    g.ensure_dirs()
    if not g.SETUP_MARKER.exists():
        return 0
    sdk.sometimes(False, "treasury_withdrawal_majority_reached")
    sdk.sometimes(False, "treasury_withdrawal_guided_votes_recorded")
    idx = g.rng_mod(g.NUM_PAYMENT_ADDRS)
    addr, lock_fh = g.try_acquire_payment_addr(idx)
    if addr is None:
        return 0
    try:
        cluster = g.make_cluster()
        with treasury.submission_lock() as locked:
            if not locked:
                return 0
            try:
                return cast(cluster, addr)
            except Exception as exc:  # noqa: BLE001
                print(f"treasury vote state unavailable: {exc}", file=sys.stderr)
                sdk.sometimes(True, "treasury_withdrawal_query_unavailable")
                return 0
    finally:
        g.release_payment_addr(lock_fh)


if __name__ == "__main__":
    sdk.run_driver(main, "vote_treasury_withdrawal_aborted", "vote_treasury_withdrawal_exits_zero")
