"""Bounded, restart-safe joins of producer forge and acquisition traces."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import fcntl
import json
import os
import pathlib

import helper_gov as g
import helper_leios_transactions as payments
from cardano_clusterlib import clusterlib
from parallel_driver_leios_transactions import submit_payments

ROOT = g.GOV / "leios_endorser"
TRACER_LOGS = pathlib.Path(os.environ.get("LEIOS_TRACER_LOGS", "/tracer/logs"))
PROPERTY = "leios_nonempty_eb_propagated"
MAX_READ_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_BLOCKS = 8192


def enabled() -> bool:
    try:
        genesis = json.loads((g.GOV_STATE_DIR / "shelley/genesis.json").read_text())
        return int(genesis["protocolParams"]["protocolVersion"]["major"]) >= 12
    except Exception:  # noqa: BLE001
        return False


def save(state: dict) -> None:
    path = ROOT / "tracking.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


class Traffic:
    """Reuse the stateless batch builder with this lane's own funds and journal."""

    BATCH_SIZE = payments.BATCH_SIZE
    INPUT_AMOUNT = payments.INPUT_AMOUNT
    PAYMENT_AMOUNT = payments.PAYMENT_AMOUNT
    FUND_AMOUNT = payments.FUND_AMOUNT
    MAX_PAYMENTS = payments.MAX_PAYMENTS
    tx_reference = staticmethod(payments.tx_reference)

    def __init__(self, parent: dict) -> None:
        self.parent = parent
        self.ROOT = ROOT / "payments"
        self.ROOT.mkdir(parents=True, exist_ok=True)

    def save(self, traffic: dict) -> None:
        self.parent["traffic"] = traffic
        save(self.parent)

    def account(self, cluster: clusterlib.ClusterLib, name: str) -> clusterlib.AddressRecord:
        path = self.ROOT / f"{name}.addr"
        if not path.exists():
            return cluster.g_address.gen_payment_addr_and_keys(name=name, destination_dir=self.ROOT)
        return clusterlib.AddressRecord(
            address=path.read_text().strip(),
            vkey_file=self.ROOT / f"{name}.vkey",
            skey_file=self.ROOT / f"{name}.skey",
        )


def supply(state: dict) -> None:
    cluster = g.make_cluster()
    immutable = json.loads(cluster.g_query.query_cli(["tip"], cli_sub_args=["--immutable-tip"]))
    journal = state.setdefault("traffic", {})
    journal["payments"] = [
        p for p in journal.get("payments", []) if p["expiry"] > immutable["slot"]
    ]
    slot = cluster.g_query.get_slot_no()
    for payment in journal["payments"]:
        if slot < payment["expiry"]:
            with contextlib.suppress(Exception):
                cluster.g_transaction.submit_tx_bare(tx_file=payment["signed"])
    if len(journal["payments"]) < Traffic.MAX_PAYMENTS:
        submit_payments(cluster, journal, workload=Traffic(state))


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


def producer(event: dict) -> str:
    host = event.get("host", "")
    if not isinstance(host, str):
        return ""
    host = host.removesuffix(".example")
    if host.startswith("p") and host[1:].isdigit() and 1 <= int(host[1:]) <= g.NUM_POOLS:
        return host
    return ""


def ingest(state: dict, event: dict) -> None:
    host, data = producer(event), event.get("data")
    if not host or not isinstance(data, dict):
        return
    namespace = event.get("ns")
    if namespace == "Consensus.LeiosKernel.BlockForged":
        block, slot, count = data.get("hash"), data.get("slot"), data.get("numTxs")
        if type(count) is not int or count <= 0:
            return
    elif namespace == "Consensus.LeiosKernel.BlockAcquired":
        block, slot, count = data.get("ebHash"), data.get("ebSlot"), None
    else:
        return
    if (
        not isinstance(block, str)
        or not block
        or type(slot) is not int
        or slot < state.get("start_slot", 0)
    ):
        return
    blocks = state.setdefault("blocks", {})
    record = blocks.setdefault(block, {"arrivals": {}})
    if count is not None:
        record.update(forger=host, slot=slot, num_txs=count)
    else:
        record["arrivals"][host] = slot
    blocks[block] = blocks.pop(block)
    if "forger" in record:
        for acquirer, acquired_slot in record["arrivals"].items():
            if acquirer != record["forger"] and acquired_slot == record["slot"]:
                state["witness"] = {
                    "eb_hash": block,
                    "eb_slot": record["slot"],
                    "num_txs": record["num_txs"],
                    "forger": record["forger"],
                    "acquirer": acquirer,
                }
    while len(blocks) > MAX_BLOCKS:
        blocks.pop(next(iter(blocks)))


def read_file(path: pathlib.Path, cursor: dict, limit: int) -> tuple[list[dict], dict, int, bool]:
    """Keep partial records and backlog for the next invocation, never tail-skip."""
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        offset = cursor.get("offset", 0)
        skipping = cursor.get("skipping", False)
        if cursor.get("inode") != stat.st_ino or stat.st_size < offset:
            offset = 0
            skipping = False
        stream.seek(offset)
        data = stream.read(limit)
    events = []
    consumed = 0
    for line in data.splitlines(keepends=True):
        if skipping:
            consumed += len(line)
            skipping = not line.endswith(b"\n")
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            if not line.endswith(b"\n"):
                if consumed == 0 and len(data) == limit and offset + len(data) < stat.st_size:
                    # An oversized unrelated trace must not block every later
                    # EB record. Discard it through its newline in bounded chunks.
                    consumed = len(data)
                    skipping = True
                break
        else:
            if isinstance(event, dict):
                events.append(event)
        consumed += len(line)
    position = offset + consumed
    return (
        events,
        {"inode": stat.st_ino, "offset": position, "skipping": skipping},
        len(data),
        position < stat.st_size,
    )


def observe(state: dict) -> tuple[bool, dict]:
    if "start_slot" not in state:
        # The first successful observer call is after the setup guard. Do not
        # let old startup records satisfy exploration/recovery coverage.
        slot = g.make_cluster().g_query.get_slot_no()
        if type(slot) is not int or slot < 0:
            msg = "could not establish a post-setup propagation watermark"
            raise ValueError(msg)
        state["start_slot"] = slot
    paths = sorted(TRACER_LOGS.glob("*/node-*.json"))
    files = state.setdefault("files", {})
    names = [str(path) for path in paths]
    start = names.index(state["next_file"]) if state.get("next_file") in names else 0
    paths = paths[start:] + paths[:start]
    budget, backlog = MAX_TOTAL_BYTES, False
    for index, path in enumerate(paths):
        try:
            events, cursor, size, pending = read_file(
                path, files.get(str(path), {}), min(MAX_READ_BYTES, budget)
            )
        except OSError:
            continue
        for event in events:
            ingest(state, event)
        files[str(path)] = cursor
        budget -= size
        backlog = backlog or pending
        state["next_file"] = str(paths[(index + 1) % len(paths)])
        if budget <= 0:
            backlog = True
            break
    state["files"] = {name: cursor for name, cursor in files.items() if name in names}
    witness = state.get("witness")
    return witness is not None, {
        "files": len(paths),
        "bytes_read": MAX_TOTAL_BYTES - budget,
        "pending_blocks": len(state.get("blocks", {})),
        "start_slot": state["start_slot"],
        "backlog": backlog,
        **(witness or {}),
    }
