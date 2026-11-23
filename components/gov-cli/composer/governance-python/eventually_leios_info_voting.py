#!/usr/bin/env python3
"""Retry InfoAction supply and all-role participation after Leios faults."""

import helper_sdk as sdk
import helper_voting_progress as progress


def main() -> int:
    sdk.reachable("leios_info_voting_recovery entered")
    return progress.recover("info")


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_info_voting_recovery_aborted", "leios_info_voting_recovery_exits_zero"
    )
