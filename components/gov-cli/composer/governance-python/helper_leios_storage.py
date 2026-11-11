"""Read-only recovery evidence for the pinned Leios prototype's SQLite schema.

The fault target is a dedicated non-producing node. No keys, submissions,
producer databases or another workload's journal are used here.
"""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import sqlite3

import helper_gov as g
import helper_leios_transactions as traces
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

ROOT = g.GOV / "leios_storage"
STATE = ROOT / "tracking.json"
NODE = pathlib.Path("/leios-storage")
DB = NODE / "leios.db"
SOCKET = NODE / "node.socket"
COVERAGE = (
    "leios_storage_baseline",
    "leios_storage_restart_observed",
    "leios_storage_recovered",
    "leios_storage_recovered_payment",
)


def enabled() -> bool:
    return os.environ.get("LEIOS_STORAGE_CHECKS") == "true" and traces.enabled()


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


def identity(path: pathlib.Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_ctime_ns]


def certified_ebs(trace: dict, tip: dict) -> list[dict]:
    ancestors = traces.immutable_ancestors(trace, tip["hash"])
    result = []
    for block, slot in trace.get("certificates", {}).items():
        header = trace.get("headers", {}).get(block)
        if block not in ancestors or header is None or header["slot"] != slot:
            continue
        announcement = trace.get("announcements", {}).get(header["previous"])
        if announcement:
            result.append({**announcement, "certifying_rb": block, "cert_slot": slot})
    return sorted(result, key=lambda p: p["cert_slot"], reverse=True)[:32]


def fingerprint(db: sqlite3.Connection, proof: dict) -> dict | None:
    """Recompute the EB body and closure from a single SQLite read snapshot."""
    eb = bytes.fromhex(proof["eb_hash"])
    complete = db.execute(
        "SELECT missingTxCount FROM ebs WHERE ebSlot=? AND ebHashBytes=?",
        (proof["eb_slot"], eb),
    ).fetchone()
    if complete is None or not isinstance(complete[0], int) or complete[0] not in (-1, 0):
        return None
    rows = db.execute(
        "SELECT e.txOffset,e.txHashBytes,e.txBytesSize,t.txBytes,t.txBytesSize "
        "FROM ebTxs e LEFT JOIN txs t ON t.txHashBytes=e.txHashBytes "
        "WHERE e.ebHashBytes=? ORDER BY e.txOffset",
        (eb,),
    ).fetchall()
    if not rows:
        return None
    refs = []
    closure = hashlib.blake2b(digest_size=32)
    for offset, (position, tx_hash, size, raw, stored_size) in enumerate(rows):
        if (
            position != offset
            or not isinstance(raw, bytes)
            or not isinstance(tx_hash, bytes)
            or len(tx_hash) != 32
            or not isinstance(size, int)
            or not 0 <= size < 1 << 32
            or size != stored_size
            or size != len(raw)
            or hashlib.blake2b(raw, digest_size=32).digest() != tx_hash
        ):
            return None
        refs.append((tx_hash.hex(), size))
        closure.update(len(raw).to_bytes(8, "big") + raw)
    if traces.eb_body_hash(refs) != proof["eb_hash"]:
        return None
    return {"body": proof["eb_hash"], "closure": closure.hexdigest(), "tx_count": len(refs)}


def read_snapshot(proofs: list[dict], check_integrity: bool) -> tuple[dict, list[str]]:
    # mode=ro avoids creating a missing DB. immutable=1 would ignore the live
    # WAL and produce false missing rows. The mount is also read-only.
    with contextlib.closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
        db.execute("PRAGMA query_only=ON")
        # Bound full integrity scans even as the DB grows (100M VM operations).
        budget = iter(range(100_000))
        db.set_progress_handler(lambda: int(next(budget, None) is None), 1000)
        db.execute("BEGIN")
        integrity = [row[0] for row in db.execute("PRAGMA quick_check")] if check_integrity else []
        snapshots = {p["eb_hash"]: fingerprint(db, p) for p in proofs}
        return snapshots, integrity


def assess(
    state: dict,
    tip: dict,
    head: dict,
    socket: list[int],
    proofs: list[dict],
    snapshots: dict,
    integrity: list[str],
) -> None:
    """Publish only after the caller has checked snapshot identities and tip."""
    if integrity:
        sdk.always(integrity == ["ok"], "leios_storage_integrity", {"result": integrity})
        if integrity != ["ok"]:
            return
    baseline = state.get("baseline")
    if baseline is None:
        for proof in proofs:
            value = snapshots[proof["eb_hash"]]
            if value:
                baseline = {"proof": proof, "fingerprint": value}
                state.update(baseline=baseline, socket=socket, observed_head=head["slot"])
                sdk.sometimes(True, "leios_storage_baseline", baseline)
                break
        return
    if socket != state["socket"]:
        # A restarted node can initially query behind the old immutable point.
        # Replaying pre-crash certificates must not count as fresh progress.
        boundary = max(head["slot"], state["observed_head"], baseline["proof"]["cert_slot"])
        state.update(socket=socket, restart_slot=boundary, recovered=False)
        sdk.sometimes(True, "leios_storage_restart_observed", {"head": head, "socket": socket})
    state["observed_head"] = max(state["observed_head"], head["slot"])
    if tip["slot"] < baseline["proof"]["cert_slot"]:
        return  # The reopened node has not caught up to the baseline yet.
    preserved = snapshots.get(baseline["proof"]["eb_hash"]) == baseline["fingerprint"]
    sdk.always(preserved, "leios_storage_certified_eb_preserved", baseline)
    if not preserved or "restart_slot" not in state:
        return
    for proof in proofs:
        if proof["cert_slot"] > state["restart_slot"] and snapshots[proof["eb_hash"]]:
            state["recovered"] = True
            sdk.sometimes(True, "leios_storage_recovered", proof)
            return


def payment_candidate(state: dict, tip: dict) -> dict | None:
    """Use only this scenario's payments and certificates at this node's immutable tip."""
    saved = state.get("payment")
    if saved:
        # This recipient is never spent from. A payment already observed at an
        # immutable point remains an obligation after restarting and catching up.
        return saved if tip["slot"] >= saved["cert_slot"] else None
    trace = state["trace"]
    ancestors = traces.immutable_ancestors(trace, tip["hash"])
    for payment in state.get("traffic", {}).get("payments", []):
        proof = traces.certified_payment(trace, payment, ancestors)
        if proof:
            slot = trace["certificates"][proof["certifying_rb"]]
            return {
                **payment,
                "proof": {**proof, "cert_slot": slot},
                "cert_slot": slot,
            }
    return None


def assess_payment(state: dict, payment: dict, tip: dict, utxos: dict) -> None:
    """Check a known certified payment only after the caller validates the read point."""
    output = utxos.get(f"{payment['txid']}#0")
    matches = (
        isinstance(output, dict)
        and output.get("address") == payment["recipient"]
        and output.get("value") == {"lovelace": payment["amount"]}
    )
    details = {"txid": payment["txid"], "immutable_hash": tip["hash"], **payment["proof"]}
    sdk.always(matches, "leios_storage_payment_matches", details)
    state["last_payment_ok"] = matches
    if matches:
        state["payment"] = payment
    if state.get("recovered"):
        sdk.sometimes(matches, "leios_storage_recovered_payment", details)


def recovery_complete(state: dict) -> bool:
    return bool(
        state.get("recovered") and state.get("last_probe_ok") and state.get("last_payment_ok")
    )


def observe(state: dict) -> None:
    state["last_probe_ok"] = False
    state["last_payment_ok"] = False
    # Unlike fresh owned payments, storage can use already-immutable history.
    trace = state.setdefault("trace", {"tracking_started": True})
    # Reuse the trace parser with our own payment records, never the payment
    # workload's journal. Do not persist a second copy of this bounded list.
    trace["payments"] = state.get("traffic", {}).get("payments", [])
    try:
        traces.ingest(trace)
    finally:
        trace.pop("payments", None)
    cluster = clusterlib.ClusterLib(
        state_dir=g.GOV_STATE_DIR, socket_path=SOCKET, command_era="dijkstra"
    )
    socket = identity(SOCKET)
    # DB ctime changes on writes; compare only device/inode for the DB.
    database = identity(DB)[:2]
    tip = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
    head = cluster.g_query.get_tip()
    proofs = certified_ebs(trace, tip)
    baseline = state.get("baseline")
    if baseline:
        proofs = [*proofs, baseline["proof"]]
    payment = payment_candidate(state, tip)
    if payment:
        proofs = [*proofs, payment["proof"]]
    try:
        snapshots, integrity = read_snapshot(proofs, baseline is None or socket != state["socket"])
    except sqlite3.DatabaseError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in (
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB,
        ):
            raise
        snapshots, integrity = {}, [str(exc)]
    utxos = None
    if payment and snapshots.get(payment["proof"]["eb_hash"]):
        utxos = json.loads(
            cluster.g_query.query_cli(
                ["utxo", "--tx-in", f"{payment['txid']}#0", "--output-json"],
                cli_sub_args=["--immutable-tip"],
            )
        )
        if not isinstance(utxos, dict):
            msg = "unknown storage-node immutable UTxO response"
            raise TypeError(msg)
    after = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
    if tip["hash"] != after["hash"] or socket != identity(SOCKET) or database != identity(DB)[:2]:
        return
    assess(state, tip, head, socket, proofs, snapshots, integrity)
    if payment and utxos is not None and integrity in ([], ["ok"]):
        assess_payment(state, payment, tip, utxos)
    sdk.sometimes(
        True,
        "leios_storage_observation",
        {
            "socket": socket,
            "head": head,
            "immutable_tip": tip,
            "baseline": state.get("baseline"),
            "restart_slot": state.get("restart_slot"),
            "recovered": state.get("recovered", False),
            "payment_verified": state["last_payment_ok"],
        },
        must_hit=False,
    )
    state["last_probe_ok"] = integrity in ([], ["ok"])
