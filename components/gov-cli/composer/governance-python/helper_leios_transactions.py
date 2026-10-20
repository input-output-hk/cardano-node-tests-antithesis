"""Isolated Leios payments and trace evidence; never scheduled as a driver."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re

import helper_gov as g
from cardano_clusterlib import clusterlib

ROOT = g.GOV / "leios_transactions"
STATE = ROOT / "tracking.json"
LOGS = pathlib.Path(os.environ.get("LEIOS_TRACER_LOGS", "/tracer/logs"))
HUMAN_LOGS = pathlib.Path(os.environ.get("LEIOS_TX_HUMAN_LOGS", "/tracer/eb-human"))
MAX_READ = 8 * 1024 * 1024
MAX_HEADERS = 20_000
MAX_PAYMENTS = 64
BATCH_SIZE = 16
INPUT_AMOUNT = 5_000_000
PAYMENT_AMOUNT = 2_000_000
FUND_AMOUNT = 10_000_000_000
HASH = r"[0-9a-f]{64}"
EB_BODY = re.compile(r"EB forged at slot (?:SlotNo )?\d+: MkLeiosEb \{leiosEbTxs = \[(.*)\]\}")
TX_REF = re.compile(rf"\(({HASH}),(\d+)\)")


def enabled() -> bool:
    try:
        genesis = json.loads((g.GOV_STATE_DIR / "shelley/genesis.json").read_text())
        return int(genesis["protocolParams"]["protocolVersion"]["major"]) >= 12
    except Exception:  # noqa: BLE001
        return False


def save(state: dict) -> None:
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(STATE)


@contextlib.contextmanager
def locked() -> cabc.Iterator[dict | None]:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "tracking.lock").open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            state = json.loads(STATE.read_text()) if STATE.exists() else {}
            yield state
            save(state)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def account(cluster: clusterlib.ClusterLib, name: str) -> clusterlib.AddressRecord:
    """These payment credentials are used by no other workload."""
    path = ROOT / f"{name}.addr"
    if not path.exists():
        return cluster.g_address.gen_payment_addr_and_keys(name=name, destination_dir=ROOT)
    return clusterlib.AddressRecord(
        address=path.read_text().strip(),
        vkey_file=ROOT / f"{name}.vkey",
        skey_file=ROOT / f"{name}.skey",
    )


def tx_reference(signed: pathlib.Path) -> tuple[str, int]:
    raw = bytes.fromhex(json.loads(signed.read_text())["cborHex"])
    return hashlib.blake2b(raw, digest_size=32).hexdigest(), len(raw)


def _cbor_head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    for tag, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if value < 1 << (8 * width):
            return bytes([(major << 5) | tag]) + value.to_bytes(width, "big")
    msg = "CBOR integer out of range"
    raise ValueError(msg)


def eb_references(line: str) -> tuple[str, dict[str, int]] | None:
    """Recompute the EB hash from its complete ordered body, not host/slot guesses.

    The pinned node's encodeLeiosEb encodes a definite CBOR map of tx hash
    bytes to Word32 sizes, in vector order. Partial/truncated lines never count.
    """
    match = EB_BODY.search(line)
    if not match:
        return None
    body = match[1]
    refs = TX_REF.findall(body)
    if not refs or TX_REF.sub("", body).replace(",", "").strip():
        return None
    encoded = _cbor_head(5, len(refs))
    for tx_hash, size in refs:
        if int(size) >= 1 << 32:
            return None
        encoded += _cbor_head(2, 32) + bytes.fromhex(tx_hash) + _cbor_head(0, int(size))
    return hashlib.blake2b(encoded, digest_size=32).hexdigest(), {
        tx_hash: int(size) for tx_hash, size in refs
    }


def _lines(path: pathlib.Path, cursors: dict) -> list[str]:
    """Bound each read; resume partial records next tick and tolerate rotation."""
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            key = str(path)
            inode, offset = cursors.get(key, [None, 0])
            if inode != stat.st_ino or offset > stat.st_size:
                offset = 0
            stream.seek(offset)
            data = stream.read(MAX_READ)
            # A final machine record may lack a newline; retry it on append.
            end = data.rfind(b"\n") + 1
            cursors[key] = [stat.st_ino, offset + end]
            return data[:end].decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def ingest(state: dict) -> None:
    cursors = state.setdefault("cursors", {})
    headers = state.setdefault("headers", {})
    announcements = state.setdefault("announcements", {})
    certificates = state.setdefault("certificates", {})
    payments = state.setdefault("payments", [])
    for path in sorted(LOGS.glob("*/node-*.json")):
        for line in _lines(path, cursors):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            data, host = event.get("data", {}), event.get("host", "")
            if not re.fullmatch(r"p\d+", host) or not isinstance(data, dict):
                continue
            if data.get("kind") == "TraceForgedBlock":
                block, previous = data.get("block", ""), data.get("blockPrev", "")
                if re.fullmatch(HASH, block) and re.fullmatch(HASH, previous):
                    headers[block] = {"previous": previous, "slot": data["slot"]}
            elif data.get("kind") == "LeiosBlockAnnounced":
                announcements[data["rbHash"]] = {
                    "eb_hash": data["ebHash"],
                    "eb_slot": data["ebSlot"],
                }
            elif data.get("kind") == "LeiosCertifiedAndAnnounced":
                # Emitted after constructing the actual header with containsCert.
                # BlockCertified is emitted before forging and cannot bind an RB.
                certificates[data["rbHash"]] = data["slotNo"]
    for path in sorted(HUMAN_LOGS.glob("*/node-*.log")):
        for line in _lines(path, cursors):
            parsed = eb_references(line)
            if parsed is None:
                continue
            eb_hash, refs = parsed
            for payment in payments:
                if payment["reference"] in refs:
                    payment.setdefault("ebs", {})[eb_hash] = refs[payment["reference"]]
    # Missing old evidence means no witness, never permission to infer ancestry.
    if len(headers) > MAX_HEADERS:
        keep = sorted(headers, key=lambda h: headers[h]["slot"])[-MAX_HEADERS:]
        state["headers"] = {h: headers[h] for h in keep}
        oldest = min(headers[h]["slot"] for h in keep)
        state["announcements"] = {k: v for k, v in announcements.items() if v["eb_slot"] >= oldest}
        state["certificates"] = {k: v for k, v in certificates.items() if v >= oldest}
    existing = {str(p) for root in (LOGS, HUMAN_LOGS) for p in root.glob("*/node-*")}
    state["cursors"] = {k: v for k, v in cursors.items() if k in existing}


def immutable_ancestors(state: dict, tip_hash: str) -> set[str]:
    ancestors = set()
    while tip_hash not in ancestors:
        ancestors.add(tip_hash)
        header = state.get("headers", {}).get(tip_hash)
        if header is None:
            break
        tip_hash = header["previous"]
    return ancestors


def certified_payment(state: dict, payment: dict, ancestors: set[str]) -> dict | None:
    for block, slot in state.get("certificates", {}).items():
        header = state.get("headers", {}).get(block)
        if block not in ancestors or header is None or header["slot"] != slot:
            continue
        announcement = state.get("announcements", {}).get(header["previous"])
        if announcement and announcement["eb_hash"] in payment.get("ebs", {}):
            return {**announcement, "certifying_rb": block}
    return None


def immutable_payments(cluster: clusterlib.ClusterLib, payments: list[dict]) -> tuple[dict, dict]:
    args = ["utxo", "--output-json"]
    for payment in payments:
        args += ["--tx-in", f"{payment['txid']}#0"]
    for _ in range(3):
        before = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
        utxos = json.loads(cluster.g_query.query_cli(args, cli_sub_args=["--immutable-tip"]))
        after = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
        if isinstance(utxos, dict) and before["hash"] == after["hash"]:
            return before, utxos
    msg = "immutable payment point moved during queries"
    raise ValueError(msg)
