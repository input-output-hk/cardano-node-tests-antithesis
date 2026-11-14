"""Private valid Plutus spends, correlated with immutable certified Leios EBs."""

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
import helper_leios_transactions as traces
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

ROOT = g.GOV / "leios_plutus"
STATE = ROOT / "tracking.json"
COVERAGE = "leios_plutus_certified_and_immutable"
BATCH_SIZE = 16
MAX_PAYMENTS = 64
SCRIPT_AMOUNT = 10_000_000
COLLATERAL_AMOUNT = 10_000_000
FUND_AMOUNT = 10_000_000_000
# Basic UPLC 1.1 always-succeed V3 script, without new builtins. Fixture:
# cardano-node-tests 900f58e6, tests/data/plutus/v3/alwaysSucceedPolicyScriptV3.plutus.
SCRIPT = {"type": "PlutusScriptV3", "description": "", "cborHex": "46450101002499"}


def enabled() -> bool:
    return os.environ.get("LEIOS_PLUTUS_CHECKS") == "true" and traces.enabled()


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


def fixtures(cluster: clusterlib.ClusterLib) -> dict:
    """Publish credentials atomically before any of their addresses are funded."""
    keys = ROOT / "keys"
    if keys.exists():
        return json.loads((keys / "manifest.json").read_text())
    pending = ROOT / "keys.pending"
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir()
    manifest = {}
    for name in ("source", "collateral", "recipient"):
        manifest[name] = cluster.g_address.gen_payment_addr_and_keys(
            name=name, destination_dir=pending
        ).address
    (pending / "script.plutus").write_text(json.dumps(SCRIPT))
    (pending / "datum.json").write_text('{"int":42}')
    (pending / "metadata.json").write_text(json.dumps({"0": ["e" * 64] * 200}))
    manifest["script"] = cluster.g_address.gen_payment_addr(
        addr_name="script", payment_script_file=pending / "script.plutus", destination_dir=pending
    )
    (pending / "manifest.json").write_text(json.dumps(manifest))
    pending.rename(keys)
    return manifest


def immutable_utxos(cluster: clusterlib.ClusterLib, args: list[str]) -> tuple[dict, dict]:
    for _ in range(3):
        before = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
        utxos = json.loads(
            cluster.g_query.query_cli(
                ["utxo", *args, "--output-json"], cli_sub_args=["--immutable-tip"]
            )
        )
        after = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
        if isinstance(utxos, dict) and before["hash"] == after["hash"]:
            return before, utxos
    msg = "immutable Plutus read point moved or returned an unknown UTxO schema"
    raise ValueError(msg)


def source_ready(cluster: clusterlib.ClusterLib, state: dict, accounts: dict) -> bool:
    if state.get("funded"):
        return True
    tip, utxos = immutable_utxos(cluster, ["--address", accounts["source"]])
    if utxos:
        state["funded"] = True
        state.pop("funding", None)
        return True
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
        "plutus_fund_" + g.unique_token(),
        txouts=[clusterlib.TxOut(address=accounts["source"], amount=FUND_AMOUNT)],
        invalid_hereafter=expiry,
        submission_file=ROOT / "funding.signed",
        before_submit=remember,
    )
    return False


def batch_ready(cluster: clusterlib.ClusterLib, state: dict) -> bool:
    if not cluster.g_query.get_protocol_params().get("costModels", {}).get("PlutusV3"):
        msg = "the pinned Plutus workload requires the existing PlutusV3 cost model"
        raise ValueError(msg)
    accounts = fixtures(cluster)
    if not source_ready(cluster, state, accounts):
        return False
    batch = state.get("batch")
    if batch:
        args = [part for i in range(2 * BATCH_SIZE) for part in ("--tx-in", f"{batch['txid']}#{i}")]
        tip, utxos = immutable_utxos(cluster, args)
        if len(utxos) == 2 * BATCH_SIZE:
            return True
        if tip["slot"] >= batch["expiry"]:
            del state["batch"]
        else:
            cluster.g_transaction.submit_tx_bare(tx_file=batch["signed"])
        return False
    keys = ROOT / "keys"
    expiry = cluster.g_query.get_slot_no() + min(cluster.epoch_length, 1500)
    name = "plutus_inputs_" + g.unique_token()
    raw = cluster.g_transaction.build_tx(
        src_address=accounts["source"],
        tx_name=name,
        txouts=[
            clusterlib.TxOut(
                address=accounts["script"],
                amount=SCRIPT_AMOUNT,
                inline_datum_file=keys / "datum.json",
            )
        ]
        * BATCH_SIZE
        + [clusterlib.TxOut(address=accounts["collateral"], amount=COLLATERAL_AMOUNT)] * BATCH_SIZE,
        tx_files=clusterlib.TxFiles(signing_key_files=[keys / "source.skey"]),
        join_txouts=False,
        invalid_hereafter=expiry,
        destination_dir=ROOT,
    )
    signed = cluster.g_transaction.sign_tx(
        tx_body_file=raw.out_file,
        signing_key_files=[keys / "source.skey"],
        tx_name=name,
        destination_dir=ROOT,
    )
    state["batch"] = {
        "txid": cluster.g_transaction.get_txid(tx_file=signed),
        "signed": str(signed),
        "expiry": expiry,
    }
    save(state)
    cluster.g_transaction.submit_tx_bare(tx_file=signed)
    return False


def unavailable(exc: Exception, phase: str) -> None:
    sdk.sometimes(
        True, "leios_plutus_unavailable", {"phase": phase, "error": str(exc)}, must_hit=False
    )


def prepare() -> bool:
    """Prepare immutable private inputs/collateral; do not submit any script spends."""
    if not enabled():
        return True
    for attempt in range(timing.recovery_checks() + 1):
        if attempt:
            time.sleep(timing.POLL_SECONDS)
        try:
            with locked() as state:
                if state is not None and (
                    state.get("payments") or batch_ready(g.make_cluster(), state)
                ):
                    return True
        except Exception as exc:  # noqa: BLE001
            unavailable(exc, "setup")
    sdk.unreachable("leios_plutus_setup_failed")
    return False


def build_spends(cluster: clusterlib.ClusterLib, state: dict) -> None:
    """Journal a whole independent batch before broadcasting any spend."""
    accounts = fixtures(cluster)
    keys = ROOT / "keys"
    batch = state["batch"]
    funded = {
        u.utxo_ix: u
        for u in cluster.g_query.get_utxo(
            txin=[f"{batch['txid']}#{i}" for i in range(2 * BATCH_SIZE)]
        )
        if u.coin == "lovelace"
    }
    if len(funded) != 2 * BATCH_SIZE:
        return
    params = cluster.g_query.get_protocol_params()
    fee = params["txFeePerByte"] * params["maxTxSize"] + params["txFeeFixed"] + 1_000_000
    if fee >= SCRIPT_AMOUNT or fee * params["collateralPercentage"] > COLLATERAL_AMOUNT * 100:
        msg = "private script inputs/collateral cannot cover the current fee bounds"
        raise ValueError(msg)
    expiry = cluster.g_query.get_slot_no() + min(cluster.epoch_length, 1500)
    prepared = []
    for i in range(BATCH_SIZE):
        script_input, collateral = funded[i], funded[BATCH_SIZE + i]
        if (
            script_input.address != accounts["script"]
            or script_input.amount != SCRIPT_AMOUNT
            or script_input.inline_datum is None
            or collateral.address != accounts["collateral"]
            or collateral.amount != COLLATERAL_AMOUNT
        ):
            msg = "unexpected private script funding outputs"
            raise ValueError(msg)
        name = "plutus_spend_" + g.unique_token()
        raw = cluster.g_transaction.build_raw_tx_bare(
            out_file=ROOT / f"{name}.body",
            txouts=[clusterlib.TxOut(address=accounts["recipient"], amount=SCRIPT_AMOUNT - fee)],
            tx_files=clusterlib.TxFiles(
                signing_key_files=[keys / "collateral.skey"],
                metadata_json_files=[keys / "metadata.json"],
            ),
            script_txins=[
                clusterlib.ScriptTxIn(
                    txins=[script_input],
                    script_file=keys / "script.plutus",
                    inline_datum_present=True,
                    redeemer_value="42",
                    execution_units=(2_000_000, 10_000),
                    collaterals=[collateral],
                )
            ],
            fee=fee,
            invalid_hereafter=expiry,
        )
        signed = cluster.g_transaction.sign_tx(
            tx_body_file=raw.out_file,
            signing_key_files=[keys / "collateral.skey"],
            tx_name=name,
            destination_dir=ROOT,
        )
        reference, size = traces.tx_reference(signed)
        prepared.append(
            {
                "txid": cluster.g_transaction.get_txid(tx_file=signed),
                "reference": reference,
                "size": size,
                "recipient": accounts["recipient"],
                "amount": SCRIPT_AMOUNT - fee,
                "script_input": f"{batch['txid']}#{i}",
                "collateral": f"{batch['txid']}#{BATCH_SIZE + i}",
                "collateral_address": accounts["collateral"],
                "signed": str(signed),
                "expiry": expiry,
            }
        )
    # All source/collateral/recipient keys and journals belong to this scenario.
    # The collateral address is never used as an ordinary spending source.
    state.setdefault("payments", []).extend(prepared)
    del state["batch"]
    save(state)


def observe(cluster: clusterlib.ClusterLib, state: dict) -> bool:
    records = state.get("payments", [])
    if not records:
        return False
    args = [
        part
        for payment in records
        for ref in (f"{payment['txid']}#0", payment["script_input"], payment["collateral"])
        for part in ("--tx-in", ref)
    ]
    tip, utxos = immutable_utxos(cluster, args)
    ancestors = traces.immutable_ancestors(state, tip["hash"])
    found, valid = False, True
    for payment in records:
        proof = payment.get("proof") or traces.certified_payment(state, payment, ancestors)
        if proof is None:
            continue
        if "cert_slot" not in proof:
            proof = {**proof, "cert_slot": state["certificates"][proof["certifying_rb"]]}
        # A relay replaying its ledger after a restart can temporarily be behind
        # an already observed certificate. That read point cannot check it yet.
        if tip["slot"] < proof["cert_slot"]:
            continue
        found = True
        details = {"txid": payment["txid"], "immutable_hash": tip["hash"], **proof}
        output = utxos.get(f"{payment['txid']}#0", {})
        collateral = utxos.get(payment["collateral"], {})
        checks = {
            "leios_plutus_tx_size_matches": payment.get("ebs", {}).get(proof["eb_hash"])
            == payment["size"],
            "leios_plutus_output_matches": output.get("address") == payment["recipient"]
            and output.get("value") == {"lovelace": payment["amount"]},
            "leios_plutus_input_consumed": payment["script_input"] not in utxos,
            "leios_plutus_collateral_preserved": collateral.get("address")
            == payment["collateral_address"]
            and collateral.get("value") == {"lovelace": COLLATERAL_AMOUNT},
        }
        for property_id, condition in checks.items():
            sdk.always(condition, property_id, details)
        valid = valid and all(checks.values())
        payment["proof"] = proof
    complete = found and valid
    sdk.sometimes(complete, COVERAGE, traces.progress(state))
    state["complete"] = complete
    if not complete:
        # A batch landing only in ordinary RBs is not a witness. Retire only at
        # immutable expiry and keep preparing independent replacement batches.
        state["payments"] = [p for p in records if p.get("proof") or p["expiry"] > tip["slot"]]
    return complete


def tick(*, submit: bool) -> bool:
    sdk.sometimes(False, COVERAGE, {"reason": "awaiting_immutable_script_certificate"})
    if not g.SETUP_MARKER.exists():
        return False
    try:
        with locked() as state:
            if state is None:
                return False
            traces.start_tracking(state)
            traces.ingest(state)
            if observe(g.make_cluster(), state):
                return True
            if not submit:
                return False
            cluster = g.make_cluster()
            slot = cluster.g_query.get_slot_no()
            for payment in state.get("payments", []):
                if slot < payment["expiry"]:
                    with contextlib.suppress(Exception):
                        cluster.g_transaction.submit_tx_bare(tx_file=payment["signed"])
            if len(state.get("payments", [])) <= MAX_PAYMENTS - BATCH_SIZE and batch_ready(
                cluster, state
            ):
                build_spends(cluster, state)
                # Journalling precedes every broadcast, including the first one.
                for payment in state["payments"][-BATCH_SIZE:]:
                    with contextlib.suppress(Exception):
                        cluster.g_transaction.submit_tx_bare(tx_file=payment["signed"])
    except Exception as exc:  # noqa: BLE001
        unavailable(exc, "submit" if submit else "observe")
    return False
