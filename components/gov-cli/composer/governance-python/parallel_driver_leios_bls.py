#!/usr/bin/env python3
"""Rebroadcast this scenario's exact valid registration during faults."""

import helper_leios_bls as bls
import helper_sdk as sdk


def main() -> int:
    if bls.enabled():
        sdk.reachable("leios_bls_submit entered")
        bls.tick(submit=True)
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_bls_submit_aborted", "leios_bls_submit_exits_zero" if bls.enabled() else None
    )
