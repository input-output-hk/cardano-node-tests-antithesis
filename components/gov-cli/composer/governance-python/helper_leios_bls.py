"""Opt-in valid BLS registration, with private zero-stake pool credentials."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import fcntl
import json
import os
import shutil
import time

import eventually_leios_transactions as timing
import helper_gov as g
import helper_leios_transactions as leios
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

ROOT = g.GOV / "leios_bls"
COVERAGE = "leios_bls_registered_immutable"


def enabled() -> bool:
    return os.environ.get("LEIOS_BLS_CHECKS") == "true" and leios.enabled()


def save(state: dict) -> None:
    temporary = ROOT / "tracking.tmp"
    temporary.write_text(json.dumps(state))
    temporary.replace(ROOT / "tracking.json")


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
            path = ROOT / "tracking.json"
            state = json.loads(path.read_text()) if path.exists() else {}
            yield state
            save(state)
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def fixtures(cluster: clusterlib.ClusterLib) -> dict:
    """Publish all credentials atomically, before their address can be funded."""
    directory = ROOT / "keys"
    if directory.exists():
        return json.loads((directory / "manifest.json").read_text())
    stage = ROOT / "keys.pending"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    source = cluster.g_address.gen_payment_addr_and_keys(name="source", destination_dir=stage)
    owner = cluster.g_stake_address.gen_stake_key_pair(key_name="owner", destination_dir=stage)
    cold = cluster.g_node.gen_cold_key_pair_and_counter(node_name="pool", destination_dir=stage)
    vrf = cluster.g_node.gen_vrf_key_pair(node_name="pool", destination_dir=stage)
    cluster.cli(
        [
            "node",
            "key-gen-BLS",
            "--verification-key-file",
            str(stage / "bls.vkey"),
            "--signing-key-file",
            str(stage / "bls.skey"),
        ]
    )
    key = bytes.fromhex(json.loads((stage / "bls.vkey").read_text())["cborHex"])
    if len(key) != 98 or key[:2] != b"\x58\x60":
        msg = "unexpected pinned BLS verification key encoding"
        raise ValueError(msg)
    # Governance can raise minPoolCost while this signed transaction waits.
    costs = [
        v for name, _flag, values in g.PPARAM_ALLOWLIST if name == "minPoolCost" for v in values
    ]
    cost = max(int(cluster.g_query.get_protocol_params()["minPoolCost"]), *costs)
    cluster.cli(
        [
            "stake-pool",
            "registration-certificate",
            "--cold-verification-key-file",
            str(cold.vkey_file),
            "--vrf-verification-key-file",
            str(vrf.vkey_file),
            "--bls-signing-key-file",
            str(stage / "bls.skey"),
            "--pool-pledge",
            "0",
            "--pool-cost",
            str(cost),
            "--pool-margin",
            "0",
            "--pool-reward-account-verification-key-file",
            str(owner.vkey_file),
            "--pool-owner-stake-verification-key-file",
            str(owner.vkey_file),
            "--testnet-magic",
            os.environ.get("TESTNET_MAGIC", "42"),
            "--out-file",
            str(stage / "pool.cert"),
        ]
    )
    deposit = cluster.g_query.get_address_deposit()
    registration = cluster.g_stake_address.gen_stake_addr_registration_cert(
        addr_name="owner",
        deposit_amt=deposit,
        stake_vkey_file=owner.vkey_file,
        destination_dir=stage,
    )
    manifest = {
        "address": source.address,
        "source_vkey": source.vkey_file.name,
        "source_skey": source.skey_file.name,
        "owner_skey": owner.skey_file.name,
        "cold_skey": cold.skey_file.name,
        "stake_cert": registration.name,
        "pool_id": cluster.g_stake_pool.get_stake_pool_id(cold_vkey_file=cold.vkey_file),
        "public_key": key[2:].hex(),
        "cost": cost,
        "deposit": deposit + cluster.g_query.get_pool_deposit(),
    }
    (stage / "manifest.json").write_text(json.dumps(manifest))
    stage.rename(directory)
    return manifest


def prepare_step(cluster: clusterlib.ClusterLib, state: dict) -> bool:
    if state.get("registration"):
        return True
    manifest = fixtures(cluster)
    # Read the expiry bound before the UTxO set. If funding expires between
    # queries, never replace it using an older snapshot that missed inclusion.
    tip = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
    immutable = json.loads(
        cluster.g_query.query_cli(
            ["utxo", "--address", manifest["address"], "--output-json"],
            cli_sub_args=["--immutable-tip"],
        )
    )
    if not isinstance(immutable, dict):
        msg = "unknown immutable UTxO response"
        raise TypeError(msg)
    funds = [
        u
        for u in cluster.g_query.get_utxo(address=manifest["address"])
        if f"{u.utxo_hash}#{u.utxo_ix}" in immutable and u.coin == "lovelace"
    ]
    if not funds:
        funding = state.get("funding")
        if funding and tip["slot"] < funding["expiry"]:
            cluster.g_transaction.submit_tx_bare(tx_file=ROOT / "funding.signed")
            return False
        expiry = cluster.g_query.get_slot_no() + min(cluster.epoch_length, 1500)

        def remember(txid: str) -> None:
            state["funding"] = {"txid": txid, "expiry": expiry}
            save(state)

        g.build_sign_submit(
            cluster,
            "bls_fund_" + g.unique_token(),
            txouts=[
                clusterlib.TxOut(
                    address=manifest["address"], amount=manifest["deposit"] + 100_000_000
                )
            ],
            invalid_hereafter=expiry,
            submission_file=ROOT / "funding.signed",
            before_submit=remember,
        )
        return False
    keys = ROOT / "keys"
    tx_files = clusterlib.TxFiles(
        certificate_files=[keys / manifest["stake_cert"], keys / "pool.cert"],
        signing_key_files=[keys / manifest[k] for k in ("source_skey", "owner_skey", "cold_skey")],
    )
    # This single private registration has no expiry: virtual campaign time
    # must not strand an otherwise valid transaction before recovery submits it.
    args = {
        "src_address": manifest["address"],
        "txins": funds,
        "tx_files": tx_files,
        "deposit": manifest["deposit"],
        "destination_dir": ROOT,
    }
    name = "bls_register_" + g.unique_token()
    fee = cluster.g_transaction.calculate_tx_fee(tx_name=name + "_fee", **args)
    out = cluster.g_transaction.build_raw_tx(tx_name=name, fee=fee, **args)
    signed = cluster.g_transaction.sign_tx(
        tx_body_file=out.out_file,
        signing_key_files=tx_files.signing_key_files,
        tx_name=name,
        destination_dir=ROOT,
    )
    state["registration"] = {
        **manifest,
        "signed": str(signed),
        "txid": cluster.g_transaction.get_txid(tx_body_file=out.out_file),
    }
    save(state)
    return True


def prepare() -> bool:
    if not enabled():
        return True
    for attempt in range(timing.recovery_checks() + 1):
        if attempt:
            time.sleep(timing.POLL_SECONDS)
        try:
            with locked() as state:
                if state is not None and prepare_step(g.make_cluster(), state):
                    return True
        except Exception as exc:  # noqa: BLE001
            unavailable(exc, "setup")
    sdk.unreachable("leios_bls_setup_failed")
    return False


def observe(cluster: clusterlib.ClusterLib, state: dict) -> bool:
    registration = state["registration"]
    pools = json.loads(
        cluster.g_query.query_cli(
            ["pool-state", "--stake-pool-id", registration["pool_id"]],
            cli_sub_args=["--immutable-tip"],
        )
    )
    if not isinstance(pools, dict) or len(pools) > 1:
        msg = "unknown immutable pool state"
        raise ValueError(msg)
    if not pools:
        return False
    params = next(iter(pools.values()))["poolParams"]
    key = params["spsLeiosKey"]["leiosPubKey"]
    if not isinstance(key, str) or len(bytes.fromhex(key)) != 96:
        msg = "unknown immutable BLS key encoding"
        raise ValueError(msg)
    matches = key == registration["public_key"]
    details = {"pool_id": registration["pool_id"], "txid": registration["txid"]}
    sdk.always(matches, "leios_bls_registered_key_matches", details)
    sdk.sometimes(matches, COVERAGE, details)
    return matches


def unavailable(exc: Exception, phase: str) -> None:
    sdk.sometimes(
        True, "leios_bls_unavailable", {"phase": phase, "error": str(exc)}, must_hit=False
    )


def tick(*, submit: bool) -> bool:
    sdk.sometimes(False, COVERAGE, {"reason": "awaiting_immutable_registration"})
    if not g.SETUP_MARKER.exists():
        return False
    try:
        with locked() as state:
            if state is None or not state.get("registration"):
                return False
            cluster = g.make_cluster()
            if observe(cluster, state):
                return True
            if submit:
                cluster.g_transaction.submit_tx_bare(tx_file=state["registration"]["signed"])
    except Exception as exc:  # noqa: BLE001
        unavailable(exc, "submit" if submit else "observe")
    return False
