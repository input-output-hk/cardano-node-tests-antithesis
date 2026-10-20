#!/usr/bin/env python3
"""parallel_driver_vote.py — batch missing DRep/SPO/CC votes on one live action.

Stateless: the votable set comes straight from the chain via an N2C
gov-state query (relay1 is fault-excluded, so the query answers even
under a block-production stall). There is NO local created/rejected
ledger — an action leaves the set only when the LEDGER expires or enacts
it, so a transient submit/lock failure is self-healing (the action is
simply picked again next tick). Submits missing DRep, SPO and committee votes
with independent random decisions (yes / no / abstain). Missing credentials
are batched onto one expiring InfoAction before replacements are attempted.
Live chain state remains the queue after interrupted submissions.
"""

from __future__ import annotations

import fcntl
import sys

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib


def build_voters(cluster: clusterlib.ClusterLib) -> list[g.Voter]:
    """Build the voter roster: every DRep, SPO and CC member. Each entry
    is (kind, create_fn, vkey_kw, vkey_file, skey_file)."""
    voters = []
    for i in range(1, g.NUM_DREPS + 1):
        vkey = g.GD / f"default_drep_{i}_drep.vkey"
        skey = g.GD / f"default_drep_{i}_drep.skey"
        if vkey.exists():
            voters.append(
                ("drep", cluster.g_governance.vote.create_drep, "drep_vkey_file", vkey, skey)
            )
    for i in range(1, g.NUM_POOLS + 1):
        vkey = g.GOV / "pools" / f"node-pool{i}" / "cold.vkey"
        skey = g.GOV / "pools" / f"node-pool{i}" / "cold.skey"
        if vkey.exists():
            voters.append(
                ("spo", cluster.g_governance.vote.create_spo, "cold_vkey_file", vkey, skey)
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
    if voter[0] == "cc":
        return cluster.g_governance.committee.get_key_hash(vkey_file=voter[3])
    return (
        cluster.cli(
            ["stake-pool", "id", "--cold-verification-key-file", str(voter[3]), "--output-hex"]
        )
        .stdout.decode()
        .strip()
    )


def recorded_vote(prop: dict, kind: str, key_hash: str) -> str | None:
    field = {"drep": "dRepVotes", "spo": "stakePoolVotes", "cc": "committeeVotes"}[kind]
    return next(
        (v for k, v in (prop.get(field) or {}).items() if k.removeprefix("keyHash-") == key_hash),
        None,
    )


def participation(prop: dict) -> None:
    drep_n, spo_n, cc_n = g.vote_counts(prop)
    total = drep_n + spo_n + cc_n
    majority = (g.NUM_DREPS + g.NUM_POOLS + g.NUM_CC + 1) // 2
    sdk.sometimes(
        drep_n >= 1 and spo_n >= 1 and cc_n >= 1,
        "action_voted_by_all_roles",
        {"drep": drep_n, "spo": spo_n, "cc": cc_n},
    )
    sdk.sometimes(
        total >= majority, "action_majority_reached", {"total": total, "majority": majority}
    )


def cast(cluster: clusterlib.ClusterLib, addr: clusterlib.AddressRecord) -> int:
    props = g.live_info_actions(cluster)
    sdk.sometimes(bool(props), "actions_live", {"live": len(props)})
    for prop in props:
        participation(prop)
    if not props:
        return 0
    # Concentrate a complete participation batch on one expiring proposal.
    # Random YES/NO/ABSTAIN decisions remain independent for every voter.
    props.sort(
        key=lambda p: (
            p.get("expiresAfter", 0),
            p["actionId"]["txId"],
            p["actionId"]["govActionIx"],
        )
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
        # All live actions are fully participated: keep exercising replacements.
        pick = props[g.rng_mod(min(3, len(props)))]
        selected = [keyed[g.rng_mod(len(keyed))]]
    txid = pick["actionId"]["txId"]
    ix = pick["actionId"]["govActionIx"]
    tok = g.unique_token()
    files, signing, decisions = [], [], []
    choices = [
        (clusterlib.Votes.YES, "yes"),
        (clusterlib.Votes.NO, "no"),
        (clusterlib.Votes.ABSTAIN, "abstain"),
    ]
    for n, (voter, key) in enumerate(selected):
        kind, create_fn, vkey_kw, vkey_file, skey_file = voter
        vote_enum, decision = choices[g.rng_mod(len(choices))]
        vote = create_fn(
            vote_name=f"{kind}_{tok}_{n}",
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
            cluster, f"vote_{tok}", vote_files=files, signing_key_files=signing, src_addr=addr
        )
    except Exception as exc:  # noqa: BLE001
        print(f"vote batch interrupted for {txid}: {exc} (will retry)", file=sys.stderr)
        sdk.sometimes(True, "vote_transient_failure", {"op": "vote"})
        return 0
    sdk.reachable("vote_submitted")
    try:
        prop = g.lookup_proposal(cluster.g_query.get_gov_state(), txid)
    except Exception as exc:  # noqa: BLE001
        print(f"post-vote query unavailable for {txid}: {exc} (will retry)", file=sys.stderr)
        prop = None
    for kind, key, decision in decisions:
        sdk.sometimes(True, f"vote_decision_{decision}")
        sdk.sometimes(
            True, f"vote_decision_{decision}_by_{kind}", {"voter": kind, "decision": decision}
        )
        if prop is not None:
            sdk.sometimes(
                recorded_vote(prop, kind, key) == f"Vote{decision.capitalize()}",
                f"vote_recorded_{kind}",
            )
    if prop is not None:
        participation(prop)
    if g.recent_stall(cluster):
        sdk.sometimes(True, "gov_op_under_perturbation", {"op": "vote"})
    print(f"submitted {len(selected)} InfoAction votes on {txid}#{ix}", file=sys.stderr)
    return 0


def main() -> int:
    sdk.reachable("vote entered")
    g.ensure_dirs()
    if not g.SETUP_MARKER.exists():
        return 0
    sdk.sometimes(False, "action_majority_reached")
    # This workload's lock prevents concurrent stale snapshots from replacing
    # the same credentials. It is independent of treasury coordination.
    with (g.STATE_DIR / "info_votes.lock").open("a") as fh:
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
                sdk.unreachable("vote_node_not_ready")
                return 0
            return cast(cluster, addr)
        finally:
            g.release_payment_addr(lock_fh)


if __name__ == "__main__":
    sdk.run_driver(main, "vote_aborted", "vote_exits_zero")
