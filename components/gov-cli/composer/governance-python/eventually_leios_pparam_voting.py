#!/usr/bin/env python3
"""Retry ParameterChange supply and participation after Leios faults."""

import helper_sdk as sdk
import helper_voting_progress as progress


def main() -> int:
    sdk.reachable("leios_pparam_voting_recovery entered")
    return progress.recover("pparam")


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_pparam_voting_recovery_aborted", "leios_pparam_voting_recovery_exits_zero"
    )
