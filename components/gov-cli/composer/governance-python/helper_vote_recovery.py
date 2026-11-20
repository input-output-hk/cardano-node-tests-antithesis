"""Private post-fault InfoAction supply and immutable participation evidence."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import fcntl
import json
import math
import pathlib
import shutil

import helper_gov as g
import helper_sdk as sdk
import parallel_driver_vote as voting
from cardano_clusterlib import clusterlib

ROOT = g.GOV / "vote_recovery"
STATE = ROOT / "tracking.json"
POLL_SECONDS = 5
COVERAGE = "action_fully_voted_after_recovery"


def recovery_checks() -> int:
    genesis = json.loads((g.GOV_STATE_DIR / "shelley/genesis.json").read_text())
    # One immutable witness is required at the end. Reserve four windows for
    # fault recovery and uncertain-submission retries, without epoch waits.
    seconds = 4 * genesis["securityParam"] * genesis["slotLength"] / genesis["activeSlotsCoeff"]
    return max(24, math.ceil(seconds / POLL_SECONDS))


def save(state: dict) -> None:
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(STATE)


@contextlib.contextmanager
def locked() -> cabc.Iterator[dict | None]:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "tracking.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
            yield state
            save(state)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def immutable_query(cluster: clusterlib.ClusterLib, args: list[str]) -> tuple[dict, dict]:
    for _ in range(3):
        before = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
        result = json.loads(cluster.g_query.query_cli(args, cli_sub_args=["--immutable-tip"]))
        after = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
        if isinstance(result, dict) and before["hash"] == after["hash"]:
            return before, result
    msg = "immutable vote-recovery read point moved or returned an unknown schema"
    raise ValueError(msg)


def fixtures(cluster: clusterlib.ClusterLib) -> dict:
    keys = ROOT / "keys"
    if keys.exists():
        return json.loads((keys / "manifest.json").read_text())
    stage = ROOT / "keys.pending"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    source = cluster.g_address.gen_payment_addr_and_keys(name="source", destination_dir=stage)
    stake = cluster.g_stake_address.gen_stake_key_pair(key_name="return", destination_dir=stage)
    certificate = cluster.g_stake_address.gen_stake_addr_registration_cert(
        addr_name="return",
        deposit_amt=cluster.g_query.get_address_deposit(),
        stake_vkey_file=stake.vkey_file,
        destination_dir=stage,
    )
    manifest = {
        "address": source.address,
        "source_vkey": source.vkey_file.name,
        "source_skey": source.skey_file.name,
        "stake_vkey": stake.vkey_file.name,
        "stake_skey": stake.skey_file.name,
        "stake_cert": certificate.name,
    }
    (stage / "manifest.json").write_text(json.dumps(manifest))
    stage.rename(keys)
    return manifest


def expiry(cluster: clusterlib.ClusterLib) -> int:
    return cluster.g_query.get_slot_no() + min(cluster.epoch_length, 1500)


def journal(state: dict, kind: str, bound: int) -> cabc.Callable[[str], None]:
    def remember(txid: str) -> None:
        state[kind] = {"txid": txid, "expiry": bound, "signed": str(ROOT / f"{kind}.signed")}
        save(state)

    return remember


def rebroadcast(cluster: clusterlib.ClusterLib, pending: dict) -> None:
    cluster.g_transaction.submit_tx_bare(tx_file=pending["signed"])


def fund(cluster: clusterlib.ClusterLib, state: dict, accounts: dict) -> bool:
    if state.get("funded"):
        return True
    tip, utxos = immutable_query(
        cluster, ["utxo", "--address", accounts["address"], "--output-json"]
    )
    if utxos:
        # The registration and private funding were atomic in the same tx.
        state["funded"] = True
        save(state)
        return True
    pending = state.get("funding")
    if pending:
        # The account registration is atomic with the private output. The
        # CLI needs that registration on-chain before building a proposal,
        # but it does not need to be immutable to submit dependent work.
        current = cluster.g_query.get_utxo(address=accounts["address"])
        if any(u.utxo_hash == pending["txid"] for u in current):
            return True
        if tip["slot"] < pending["expiry"]:
            rebroadcast(cluster, pending)
            return False
    keys, bound = ROOT / "keys", expiry(cluster)
    g.build_sign_submit(
        cluster,
        "recovery_fund_" + g.unique_token(),
        certificate_files=[keys / accounts["stake_cert"]],
        signing_key_files=[keys / accounts["stake_skey"]],
        txouts=[
            clusterlib.TxOut(
                address=accounts["address"],
                amount=max(
                    g.PAYMENT_ADDR_FUND,
                    4 * cluster.g_query.get_gov_action_deposit() + 100_000_000,
                ),
            )
        ],
        invalid_hereafter=bound,
        submission_file=ROOT / "funding.signed",
        before_submit=journal(state, "funding", bound),
        wait_for_confirmation=False,
    )
    return False


def source(accounts: dict) -> clusterlib.AddressRecord:
    keys = ROOT / "keys"
    return clusterlib.AddressRecord(
        address=accounts["address"],
        vkey_file=keys / accounts["source_vkey"],
        skey_file=keys / accounts["source_skey"],
    )


def info_file(cluster: clusterlib.ClusterLib, accounts: dict, name: str) -> pathlib.Path:
    info = cluster.g_governance.action.create_info(
        action_name=name,
        deposit_amt=cluster.g_query.get_gov_action_deposit(),
        anchor_url=g.ANCHOR_URL,
        anchor_data_hash=cluster.g_governance.get_anchor_data_hash(text=g.ANCHOR_TEXT),
        deposit_return_stake_vkey_file=ROOT / "keys" / accounts["stake_vkey"],
        destination_dir=ROOT,
    )
    return info.action_file


def create(cluster: clusterlib.ClusterLib, state: dict, accounts: dict) -> None:
    token, bound = g.unique_token(), expiry(cluster)
    g.build_sign_submit(
        cluster,
        "recovery_create_" + token,
        proposal_files=[info_file(cluster, accounts, "recovery_info_" + token)],
        src_addr=source(accounts),
        invalid_hereafter=bound,
        submission_file=ROOT / "action.signed",
        before_submit=journal(state, "action", bound),
        wait_for_confirmation=False,
    )


def step(cluster: clusterlib.ClusterLib, state: dict) -> bool:
    sdk.sometimes(True, "vote_recovery_phase_query", must_hit=False)
    tip, gov = immutable_query(cluster, ["gov-state"])
    if not isinstance(gov.get("proposals"), list):
        msg = "immutable governance query did not contain a proposals list"
        raise TypeError(msg)
    state["last"] = {
        "immutable_slot": tip["slot"],
        "epoch": tip["epoch"],
        "live": len(gov["proposals"]),
    }
    accounts = fixtures(cluster)
    action = state.get("action")
    if action is None:
        sdk.sometimes(True, "vote_recovery_phase_prepare", state["last"], must_hit=False)
        if not fund(cluster, state, accounts):
            return False
        create(cluster, state, accounts)
        return False
    prop = g.lookup_proposal(gov, action["txid"])
    state["last"]["txid"] = action["txid"]
    roster = voting.build_voters(cluster)
    voters = [next(v for v in roster if v[0] == kind) for kind in ("drep", "spo")]
    selected = [(v, voting.voter_hash(cluster, v)) for v in voters]
    immutable_missing = [
        (v, key)
        for v, key in selected
        if prop is None
        or voting.recorded_vote(prop, v[0], key) not in ("VoteYes", "VoteNo", "VoteAbstain")
    ]
    state["last"]["recorded"] = {
        v[0]: voting.recorded_vote(prop, v[0], key) if prop else None for v, key in selected
    }
    if not immutable_missing:
        state["funded"] = True
        return True
    if prop is not None:
        state["funded"] = True
    if prop is not None and tip["epoch"] > prop["expiresAfter"]:
        # Expired actions remain in governance queries until a later cleanup
        # epoch, but the ledger already rejects new votes for them.
        state.pop("action")
        state.pop("votes", None)
        return False
    # Volatile inclusion is enough to submit a dependent vote, never enough
    # to satisfy coverage. This pipelines proposal+vote confirmation instead
    # of waiting a full k blocks between each transaction.
    live = cluster.g_query.get_gov_state()
    if not isinstance(live, dict) or not isinstance(live.get("proposals"), list):
        msg = "volatile recovery query did not contain a proposals list"
        raise TypeError(msg)
    candidate = g.lookup_proposal(live, action["txid"])
    if candidate is None:
        funding = state.get("funding")
        if not state.get("funded") and funding and tip["slot"] < funding["expiry"]:
            current = cluster.g_query.get_utxo(address=accounts["address"])
            if not any(u.utxo_hash == funding["txid"] for u in current):
                # The volatile funding block may have rolled back after the
                # dependent proposal was signed. Replay the parent first.
                # A duplicate-parent rejection must not prevent retrying the
                # proposal: it may simply mean the parent is already included.
                with contextlib.suppress(Exception):
                    rebroadcast(cluster, funding)
        if tip["slot"] < action["expiry"]:
            rebroadcast(cluster, action)
        elif prop is None:
            # Only successful queries plus immutable TTL expiry can replace
            # an absent action. An unknown query never retires a transaction.
            state.pop("action")
            state.pop("votes", None)
        return False
    missing = [
        (v, key)
        for v, key in selected
        if voting.recorded_vote(candidate, v[0], key) not in ("VoteYes", "VoteNo", "VoteAbstain")
    ]
    if not missing:
        sdk.sometimes(True, "vote_recovery_phase_wait_immutable", state["last"], must_hit=False)
        return False
    pending = state.get("votes")
    if pending and tip["slot"] < pending["expiry"]:
        rebroadcast(cluster, pending)
        return False
    token, bound = g.unique_token(), expiry(cluster)
    sdk.sometimes(True, "vote_recovery_phase_submit_votes", state["last"], must_hit=False)
    files, signing = [], []
    for voter, _key in missing:
        kind, factory, keyword, vkey, skey = voter
        vote = factory(
            vote_name=f"recovery_{kind}_{token}",
            action_txid=action["txid"],
            action_ix=0,
            vote=clusterlib.Votes.YES,
            destination_dir=ROOT,
            **{keyword: vkey},
        )
        files.append(vote.vote_file)
        signing.append(skey)
    g.build_sign_submit(
        cluster,
        "recovery_vote_" + token,
        vote_files=files,
        signing_key_files=signing,
        src_addr=source(accounts),
        invalid_hereafter=bound,
        submission_file=ROOT / "votes.signed",
        before_submit=journal(state, "votes", bound),
        wait_for_confirmation=False,
    )
    return False
