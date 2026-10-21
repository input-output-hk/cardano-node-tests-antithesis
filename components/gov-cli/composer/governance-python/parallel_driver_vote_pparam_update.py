#!/usr/bin/env python3
"""parallel_driver_vote_pparam_update.py — batch missing DRep/CC votes on one
live ParameterChange action.

Same shape as parallel_driver_vote_treasury_withdrawal.py, restricted to
DRep and CC voters. SPO voting eligibility on a ParameterChange action
depends on whether the targeted parameters fall in Conway's
security-relevant group (fee/size params etc.) - confirmed by a real
ledger rejection (`ConwayGovFailure (DisallowedVoters (StakePoolVoter
...))`) when this driver used to include SPOs. helper_gov.PPARAM_ALLOWLIST
deliberately targets only non-security-group parameters (see its own
docstring), so an SPO vote on any action this driver casts against is
unconditionally rejected - SPOs are left out of the roster rather than
submitted and expected to fail every time.

Stateless: the votable set comes straight from the chain via an N2C
gov-state query (relay1 is fault-excluded, so the query answers even
under a block-production stall). There is NO local created/rejected
ledger - a ParameterChange action leaves the set once it ratifies+enacts
(or expires), which this driver only ever observes indirectly (the
action disappears from live gov-state).
"""

from __future__ import annotations

import fcntl
import sys

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib


def build_voters(cluster: clusterlib.ClusterLib) -> list[g.Voter]:
    """Build the voter roster: every DRep and CC member (no SPOs - see
    module docstring). Each entry is (kind, create_fn, vkey_kw, vkey_file, skey_file)."""
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


def recorded_vote(prop: dict, kind: str, key: str) -> str | None:
    field = "dRepVotes" if kind == "drep" else "committeeVotes"
    return next((v for k, v in prop[field].items() if k.removeprefix("keyHash-") == key), None)


def participation(prop: dict) -> None:
    drep_n, _spo_n, cc_n = g.vote_counts(prop)
    total = drep_n + cc_n
    target = (g.NUM_DREPS + g.NUM_CC + 1) // 2
    sdk.sometimes(
        drep_n >= 1 and cc_n >= 1,
        "pparam_update_voted_by_drep_and_cc",
        {"drep": drep_n, "cc": cc_n},
    )
    sdk.sometimes(
        total >= target,
        "pparam_update_majority_reached",
        {"txid": prop["actionId"]["txId"], "total": total, "majority": target},
    )


def cast(cluster: clusterlib.ClusterLib, addr: clusterlib.AddressRecord) -> int:
    try:
        state = cluster.g_query.get_gov_state()
        props = [
            p
            for p in state["proposals"]
            if p["proposalProcedure"]["govAction"]["tag"] == "ParameterChange"
        ]
        # Observe existing votes even when this tick cannot submit another tx.
        for prop in props:
            participation(prop)
        epoch = g.current_epoch(cluster)
    except Exception as exc:  # noqa: BLE001
        print(f"pparam vote query unavailable: {exc} (will retry)", file=sys.stderr)
        sdk.sometimes(True, "pparam_update_vote_query_unavailable")
        return 0
    # Expired actions can remain listed until the next ledger cleanup epoch.
    props = [p for p in props if p["expiresAfter"] >= epoch]
    sdk.sometimes(bool(props), "pparam_update_actions_live", {"live": len(props)})
    if not props:
        return 0
    props.sort(
        key=lambda p: (p["expiresAfter"], p["actionId"]["txId"], p["actionId"]["govActionIx"])
    )
    keyed = [(v, voter_hash(cluster, v)) for v in build_voters(cluster)]
    if not keyed:
        return 0
    selected = []
    for pick in props:
        selected = [(v, key) for v, key in keyed if recorded_vote(pick, v[0], key) is None]
        if selected:
            break
    if not selected:
        pick = props[g.rng_mod(min(3, len(props)))]
        selected = [keyed[g.rng_mod(len(keyed))]]
    txid, ix = pick["actionId"]["txId"], pick["actionId"]["govActionIx"]
    choices = [
        (clusterlib.Votes.YES, "yes"),
        (clusterlib.Votes.NO, "no"),
        (clusterlib.Votes.ABSTAIN, "abstain"),
    ]
    tok = g.unique_token()
    files, signing, decisions = [], [], []
    for n, (voter, key) in enumerate(selected):
        kind, create_fn, vkey_kw, vkey_file, skey_file = voter
        vote_enum, decision = choices[g.rng_mod(len(choices))]
        vote = create_fn(
            vote_name=f"pparam_{kind}_{tok}_{n}",
            action_txid=txid,
            action_ix=ix,
            vote=vote_enum,
            destination_dir=str(g.WORK),
            **{vkey_kw: vkey_file},
        )
        files.append(vote.vote_file)
        signing.append(skey_file)
        decisions.append((kind, key, decision))
    try:
        g.build_sign_submit(
            cluster,
            f"pparam_vote_{tok}",
            vote_files=files,
            signing_key_files=signing,
            src_addr=addr,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"pparam vote batch interrupted for {txid}: {exc} (will retry)", file=sys.stderr)
        sdk.sometimes(True, "pparam_update_vote_transient_failure")
        return 0
    sdk.reachable("pparam_update_vote_submitted")
    prop = None
    try:
        props = cluster.g_query.get_gov_state()["proposals"]
        prop = next((p for p in props if p["actionId"] == {"txId": txid, "govActionIx": ix}), None)
    except Exception as exc:  # noqa: BLE001
        print(f"post-vote pparam query unavailable: {exc} (will retry)", file=sys.stderr)
        sdk.sometimes(True, "pparam_update_vote_query_unavailable")
    for kind, key, decision in decisions:
        sdk.sometimes(True, f"pparam_update_vote_decision_{decision}")
        sdk.sometimes(
            True,
            f"pparam_update_vote_decision_{decision}_by_{kind}",
            {"voter": kind, "decision": decision},
        )
        if prop is not None:
            sdk.sometimes(
                recorded_vote(prop, kind, key) == f"Vote{decision.capitalize()}",
                f"pparam_update_vote_recorded_{kind}",
            )
    if prop is not None:
        participation(prop)
    if g.recent_stall(cluster):
        sdk.sometimes(True, "gov_op_under_perturbation", {"op": "vote_pparam_update"})
    print(f"submitted {len(selected)} pparam votes on {txid}#{ix}", file=sys.stderr)
    return 0


def main() -> int:
    sdk.reachable("vote_pparam_update entered")
    g.ensure_dirs()
    if not g.SETUP_MARKER.exists():
        return 0
    sdk.sometimes(False, "pparam_update_majority_reached")
    # Workload-local: neither the creator nor any observer takes this lock.
    with (g.STATE_DIR / "pparam_votes.lock").open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        idx = g.rng_mod(g.NUM_PAYMENT_ADDRS)
        addr, lock_fh = g.try_acquire_payment_addr(idx)
        if addr is None:
            return 0
        try:
            cluster = g.make_cluster()
            if not g.wait_for_node(cluster, tries=30):
                sdk.unreachable("vote_pparam_update_node_not_ready")
                return 0
            return cast(cluster, addr)
        finally:
            g.release_payment_addr(lock_fh)


if __name__ == "__main__":
    sdk.run_driver(main, "vote_pparam_update_aborted", "vote_pparam_update_exits_zero")
