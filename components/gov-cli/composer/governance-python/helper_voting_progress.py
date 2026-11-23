"""Bounded CLI and transaction inclusion waits for Leios voting.

No additional shared state: the existing workload lock and payment-address
lock still cover each transaction. Gov-state remains the proposal queue.
Conway keeps its existing clusterlib submission and scheduling paths.
"""

from __future__ import annotations

import pathlib
import time

import helper_gov as g
from cardano_clusterlib import clusterlib

POLL_SECONDS = 5
SUBMISSION_CHECKS = 12
CLI_SECONDS = 20


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
