"""Treasury workload coordination and strict chain observations.

Receiver 1 is reserved for payout, receiver 2 for unvoted expiry. Remaining
receivers keep the random voting workload. The chain remains the source of
live actions; claim records only coordinate recipients and attribute payouts.
"""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import fcntl
import json

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

GUIDED_RECEIVER = 1
EXPIRY_RECEIVER = 2


@contextlib.contextmanager
def _lock(name: str) -> cabc.Iterator[bool]:
    g.ensure_dirs()
    with (g.STATE_DIR / name).open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def lock() -> contextlib.AbstractContextManager[bool]:
    """Serialize observers only; a stalled submit must not block observation."""
    return _lock("treasury-observer.lock")


def submission_lock() -> contextlib.AbstractContextManager[bool]:
    """Serialize creators and voters, independently of the observer."""
    return _lock("treasury-submit.lock")


def receiver_lock(idx: int) -> contextlib.AbstractContextManager[bool]:
    """Protect one claim's preparation, mutation and release."""
    return _lock(f"treasury-receiver-{idx}.lock")


def query(cluster: clusterlib.ClusterLib, args: list[str], *, immutable: bool = False) -> object:
    target = ["--immutable-tip"] if immutable else []
    return json.loads(cluster.g_query.query_cli(args, cli_sub_args=target))


def gov_state(cluster: clusterlib.ClusterLib, *, immutable: bool = False) -> dict:
    state = query(cluster, ["gov-state"], immutable=immutable)
    if not isinstance(state, dict) or not isinstance(state.get("proposals"), list):
        msg = "governance query did not return a proposals list"
        raise TypeError(msg)
    return state


def actions(state: dict) -> list[dict]:
    return [
        p
        for p in state["proposals"]
        if p["proposalProcedure"]["govAction"]["tag"] == "TreasuryWithdrawals"
    ]


def action_id(prop: dict) -> tuple[str, int]:
    aid = prop["actionId"]
    return aid["txId"], aid["govActionIx"]


def find_action(state: dict, txid: str, ix: int) -> dict | None:
    return next((p for p in actions(state) if action_id(p) == (txid, ix)), None)


def receiver_id(receiver: int) -> tuple[str, int] | None:
    for idx, record in g.pending_recv_slots():
        if idx == receiver and "txid" in record:
            return record["txid"], record["ix"]
    return None


def guided_id() -> tuple[str, int] | None:
    return receiver_id(GUIDED_RECEIVER)


def participation(prop: dict) -> None:
    """Observe participation, without confusing headcounts with ratification."""
    drep_n, _spo_n, cc_n = g.vote_counts(prop)
    total = drep_n + cc_n
    target = (g.NUM_DREPS + g.NUM_CC + 1) // 2
    details = {"txid": action_id(prop)[0], "total": total, "majority": target}
    sdk.sometimes(total >= target, "treasury_withdrawal_majority_reached", details)
    sdk.sometimes(
        drep_n >= 1 and cc_n >= 1,
        "treasury_withdrawal_voted_by_drep_and_cc",
        {"drep": drep_n, "cc": cc_n},
    )


def immutable_snapshot(cluster: clusterlib.ClusterLib, address: str) -> tuple[dict, dict, int]:
    """Read governance and reward balance at one confirmed immutable point.

    Separate CLI queries may otherwise straddle an epoch boundary. If the
    immutable point advances during the queries, retry or leave the claim alone.
    Missing/unregistered accounts are unknown, never an invented zero balance.
    """
    for _ in range(3):
        before = query(cluster, ["tip"], immutable=True)
        if not isinstance(before, dict) or "hash" not in before:
            continue
        state = gov_state(cluster, immutable=True)
        accounts = query(cluster, ["stake-address-info", "--address", address], immutable=True)
        after = query(cluster, ["tip"], immutable=True)
        if before["hash"] != after.get("hash"):
            continue
        if not isinstance(accounts, list) or len(accounts) != 1:
            msg = "recipient is not registered at the immutable point"
            raise ValueError(msg)
        balance = accounts[0]["rewardAccountBalance"]
        if not isinstance(balance, int):
            msg = "recipient query did not return an integer balance"
            raise TypeError(msg)
        return before, state, balance
    msg = "could not acquire a consistent immutable treasury snapshot"
    raise ValueError(msg)
