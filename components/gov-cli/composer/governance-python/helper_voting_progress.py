"""Bounded Leios voting steps and independent post-fault participation retries.

No additional shared state: the existing workload lock and payment-address
lock still cover each transaction. Gov-state remains the proposal queue.
Conway keeps its existing clusterlib submission and scheduling paths.
"""

from __future__ import annotations

import importlib
import json
import math
import pathlib
import time

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

POLL_SECONDS = 5
SUBMISSION_CHECKS = 12
CLI_SECONDS = 20
MAX_STALLED_STEPS = 4


def enabled() -> bool:
    return g.GOV_COMMAND_ERA == "dijkstra"


def make_cluster() -> clusterlib.ClusterLib:
    cluster = g.make_cluster()
    if not enabled():
        return cluster
    original_cli = cluster.cli

    def cli(
        cli_args: list[str], timeout: float | None = None, add_default_args: bool = True
    ) -> clusterlib.CLIOut:
        # Clusterlib's Popen context waits for a child even if communicate's
        # timeout expires. GNU timeout kills the command's process group and
        # allows that context to finish, including for an unresponsive socket.
        seconds = CLI_SECONDS if timeout is None else min(CLI_SECONDS, timeout)
        command = ["cardano-cli", cluster.command_era, *cli_args] if add_default_args else cli_args
        return original_cli(
            ["timeout", "--kill-after=2s", f"{seconds}s", *command], add_default_args=False
        )

    cluster.cli = cli

    def submit(
        tx_file: str | pathlib.Path,
        txins: list[clusterlib.UTXOData],
        wait_blocks: int | None = None,  # noqa: ARG001 - clusterlib method signature
    ) -> str:
        # Do not wait for a specified number of blocks: during faults there
        # may be none. Keep the source lock while checking inclusion, then
        # yield to another scheduling opportunity if this step cannot finish.
        # Coverage still comes exclusively from subsequent gov-state queries.
        if not txins:
            msg = "voting submission requires an input to observe"
            raise ValueError(msg)
        txid = cluster.g_transaction.get_txid(tx_file=tx_file)
        cluster.cli(
            [
                "transaction",
                "submit",
                *cluster.magic_args,
                *cluster.socket_args,
                "--tx-file",
                str(tx_file),
            ]
        )
        for _ in range(SUBMISSION_CHECKS):
            time.sleep(POLL_SECONDS)
            if not cluster.g_query.get_utxo(utxo=txins[0]):
                return txid
        msg = f"voting submission {txid} was not observed within this step"
        raise TimeoutError(msg)

    cluster.g_transaction.submit_tx = submit
    return cluster


def recovery_limits() -> tuple[int, int]:
    genesis = json.loads((g.GOV_STATE_DIR / "shelley/genesis.json").read_text())
    seconds = max(
        120,
        4 * genesis["securityParam"] * genesis["slotLength"] / genesis["activeSlotsCoeff"],
    )
    return math.ceil(seconds / POLL_SECONDS), math.ceil(seconds / genesis["slotLength"])


def recover(kind: str) -> int:
    """Retry just this workload after faults, regardless of creator ordering."""
    if not enabled() or not g.SETUP_MARKER.exists():
        return 0
    voter = importlib.import_module(
        "parallel_driver_vote" if kind == "info" else "parallel_driver_vote_pparam_update"
    )
    prefix = "action" if kind == "info" else "pparam_update"
    live_id = "actions_live" if kind == "info" else "pparam_update_actions_live"
    sdk.sometimes(False, live_id)
    sdk.sometimes(False, f"{prefix}_majority_reached")
    role_id = (
        "action_voted_by_all_roles" if kind == "info" else "pparam_update_voted_by_drep_and_cc"
    )
    sdk.sometimes(False, role_id)
    checks, slot_span = recovery_limits()
    start_slot, last_slot, stalled = None, None, 0
    for attempt in range(checks + 1):
        if attempt:
            time.sleep(POLL_SECONDS)
        try:
            cluster = make_cluster()
            slot = cluster.g_query.get_slot_no()
            if type(slot) is not int:
                msg = "voting recovery tip did not contain a slot"
                raise TypeError(msg)
            if start_slot is None:
                start_slot = slot
            if slot - start_slot >= slot_span:
                return 0
            stalled = stalled + 1 if last_slot is not None and slot <= last_slot else 0
            last_slot = slot
            if stalled >= MAX_STALLED_STEPS:
                return 0
            # The slot budget includes time spent inside the voter, not only
            # outer sleeps. A fully stalled/unreadable chain also has a small
            # step bound; accepted-but-never-included txs cannot multiply the
            # nominal recovery budget into hundreds of confirmation waits.
            voter.main()
            if kind == "info":
                props = g.live_info_actions(cluster, strict=True)
                target = (g.NUM_DREPS + g.NUM_POOLS + g.NUM_CC + 1) // 2
            else:
                state = cluster.g_query.get_gov_state()
                epoch = cluster.g_query.get_epoch()
                if not isinstance(state.get("proposals"), list) or type(epoch) is not int:
                    msg = "ParameterChange recovery query returned an unknown schema"
                    raise TypeError(msg)
                props = [
                    p
                    for p in state["proposals"]
                    if p["proposalProcedure"]["govAction"]["tag"] == "ParameterChange"
                    and p["expiresAfter"] >= epoch
                ]
                target = (g.NUM_DREPS + g.NUM_CC + 1) // 2
            sdk.sometimes(bool(props), live_id, {"live": len(props)})
            for prop in props:
                voter.participation(prop)
                drep, spo, cc = g.vote_counts(prop)
                # Require all eligible roles as well as the majority on the
                # same action. Submission responses never satisfy recovery.
                if (
                    drep
                    and cc
                    and (kind != "info" or spo)
                    and drep + cc + (spo if kind == "info" else 0) >= target
                ):
                    return 0
        except Exception as exc:  # noqa: BLE001
            stalled += 1
            sdk.sometimes(
                True,
                f"leios_{kind}_voting_recovery_unavailable",
                {"error": str(exc)},
                must_hit=False,
            )
            if stalled >= MAX_STALLED_STEPS:
                return 0
    return 0
