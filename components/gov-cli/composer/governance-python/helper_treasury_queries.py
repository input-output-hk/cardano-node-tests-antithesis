"""Finite Leios observer queries; no submissions, shared journal or clock reads."""

from __future__ import annotations

import collections.abc as cabc
import subprocess

from cardano_clusterlib import clusterlib
from cardano_clusterlib import consts
from cardano_clusterlib import exceptions

QUERY_SECONDS = 5.0
KILL_GRACE_SECONDS = 2
PROCESS_GRACE_SECONDS = 1


class QueryBudget:
    """Charge the maximum CLI duration before each query.

    This deliberately reserves worst-case time, rather than reading a host
    clock that Antithesis cannot reliably steer. A fast query does not earn
    its reservation back. Settlement sleeps have their own finite allowance.
    Small local genesis windows use shorter deadlines.
    """

    def __init__(self, seconds: float) -> None:
        self.remaining = seconds
        self.query_seconds = min(QUERY_SECONDS, max(0.5, seconds / 100))

    def reserve(self, seconds: float) -> None:
        if seconds > self.remaining:
            msg = "treasury query recovery budget exhausted"
            raise TimeoutError(msg)
        self.remaining -= seconds


def bounded(
    cluster: clusterlib.ClusterLib,
    *,
    budget: QueryBudget | None = None,
    progress: cabc.Callable[[dict], None] | None = None,
) -> clusterlib.ClusterLib:
    """Wrap only this observer's cluster; transaction callers keep their own."""

    def cli(
        cli_args: list[str], timeout: float | None = None, add_default_args: bool = True
    ) -> clusterlib.CLIOut:
        seconds = budget.query_seconds if budget is not None else QUERY_SECONDS
        if timeout is not None:
            seconds = min(seconds, max(0.1, timeout))
        maximum = seconds + KILL_GRACE_SECONDS + PROCESS_GRACE_SECONDS
        if budget is not None:
            budget.reserve(maximum)
        cli_args = [str(arg) for arg in cli_args if arg != consts.SUBCOMMAND_MARK]
        details = {
            "query": str(cli_args[1]) if len(cli_args) > 1 else "unknown",
            "immutable": "--immutable-tip" in cli_args,
            "remaining_seconds": budget.remaining if budget is not None else None,
        }
        if progress is not None:
            progress({**details, "phase": "started"})
        command = ["cardano-cli", cluster.command_era, *cli_args] if add_default_args else cli_args
        # One attempt only: clusterlib.cli's internal retry loop would multiply
        # the reserved duration. The observer's outer loop owns retries.
        command = [str(arg) for arg in command]
        try:
            result = subprocess.run(
                ["timeout", f"--kill-after={KILL_GRACE_SECONDS}s", f"{seconds}s", *command],
                capture_output=True,
                timeout=maximum,
                check=False,
            )
        except subprocess.TimeoutExpired:
            msg = "treasury CLI query exceeded its process deadline"
            raise TimeoutError(msg) from None
        if result.returncode:
            error = result.stderr.decode(errors="replace")[:2000]
            msg = f"treasury CLI query failed ({result.returncode}): {error}"
            raise exceptions.CLIError(msg)
        if progress is not None:
            progress({**details, "phase": "finished"})
        return clusterlib.CLIOut(result.stdout, result.stderr)

    cluster.cli = cli
    return cluster
