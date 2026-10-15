#!/usr/bin/env python3
"""Witness nonempty endorser-block propagation during Antithesis exploration.

A Sometimes, not a latency/SLA check: faults may stop forging or fetching
for any individual sample. Never seeing a matching pair means the run has
not demonstrated that it exercised this part of Leios under faults.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

import helper_gov as g
import helper_sdk as sdk

TRACER_LOGS = pathlib.Path(os.environ.get("LEIOS_TRACER_LOGS", "/tracer/logs"))
WINDOW = int(os.environ.get("LEIOS_ENDORSER_WINDOW", "20"))
MAX_READ_BYTES = 2 * 1024 * 1024
PROPERTY = "leios_nonempty_eb_propagated"


def _enabled() -> bool:
    """Both Leios modes use v12 genesis; Conway uses v10. No extra switch."""
    try:
        genesis = json.loads((g.GOV_STATE_DIR / "shelley" / "genesis.json").read_text())
        return int(genesis["protocolParams"]["protocolVersion"]["major"]) >= 12
    except Exception:  # noqa: BLE001
        return False


def _files() -> list[pathlib.Path]:
    # FileMode/ForMachine in tracer-config.yaml: one directory per node.
    # Include rotated files: a forge and its acquisition may straddle rotation.
    return sorted(TRACER_LOGS.glob("*/node-*.json"))


def _events(path: pathlib.Path, offset: int = 0) -> list[dict]:
    """Read a bounded tail, dropping partial JSON lines during concurrent writes."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            start = max(offset, size - MAX_READ_BYTES)
            if start:
                stream.seek(start - 1)
                if stream.read(1) != b"\n":
                    # May start inside an unfinished record at the previous
                    # sample's EOF. Do not mistake its suffix for fresh JSON.
                    stream.readline(MAX_READ_BYTES)
            else:
                stream.seek(0)
            data = stream.read(MAX_READ_BYTES)
    except OSError:
        # The tracer can rotate/remove a file while a probe is reading it.
        return []
    result = []
    # cardano-tracer prefixes batches with a newline, leaving the final
    # complete JSON record without one. Decode it too; partial writes fail
    # JSON decoding and are safely skipped below.
    for line in data.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict) and str(event.get("ns", "")).startswith(
            "Consensus.LeiosKernel."
        ):
            result.append(event)
    return result


def _producer(event: dict) -> str:
    host = event.get("host", "")
    return host if isinstance(host, str) and host.startswith("p") and host[1:].isdigit() else ""


def _witness(forging: list[dict], arrivals: list[dict]) -> dict | None:
    """Match a nonempty forge to a fresh acquisition on a different producer."""
    forged = {}
    for event in forging:
        data = event.get("data", {})
        if not isinstance(data, dict) or event.get("ns") != "Consensus.LeiosKernel.BlockForged":
            continue
        txs, eb_hash, host = data.get("numTxs"), data.get("hash"), _producer(event)
        if type(txs) is int and txs > 0 and isinstance(eb_hash, str) and eb_hash and host:
            forged[eb_hash] = (host, txs, data.get("slot"))
    for event in arrivals:
        data = event.get("data", {})
        if not isinstance(data, dict) or event.get("ns") != "Consensus.LeiosKernel.BlockAcquired":
            continue
        eb_hash, host = data.get("ebHash"), _producer(event)
        if not isinstance(eb_hash, str) or eb_hash not in forged or not host:
            continue
        source, txs, slot = forged[eb_hash]
        if host != source and data.get("ebSlot") == slot:
            return {
                "eb_hash": eb_hash,
                "num_txs": txs,
                "eb_slot": slot,
                "forger": source,
                "acquirer": host,
            }
    return None


def main() -> int:
    # Gate before emitting anything, including the reachability scaffolding.
    if not _enabled():
        return 0
    sdk.reachable("leios_endorser entered")
    if not g.SETUP_MARKER.exists():
        sdk.sometimes(False, PROPERTY, {"reason": "setup_pending"})
        return 0

    before = []
    positions = {}
    for path in _files():
        try:
            stat = path.stat()
        except OSError:
            continue
        positions[path] = (stat.st_ino, stat.st_size)
        before.extend(_events(path))
    time.sleep(WINDOW)
    arrivals = []
    for path in _files():
        try:
            stat = path.stat()
        except OSError:
            continue
        inode, offset = positions.get(path, (None, 0))
        if inode != stat.st_ino or stat.st_size < offset:
            offset = 0
        arrivals.extend(_events(path, offset))

    witness = _witness(before + arrivals, arrivals)
    sdk.sometimes(witness is not None, PROPERTY, {"window_s": WINDOW, **(witness or {})})
    return 0


if __name__ == "__main__":
    # run_driver normally emits exits_zero even for early returns. Suppress it
    # in Conway too, so this driver introduces no Conway SDK properties.
    sdk.run_driver(
        main,
        "leios_endorser_aborted",
        "leios_endorser_exits_zero" if _enabled() else None,
    )
