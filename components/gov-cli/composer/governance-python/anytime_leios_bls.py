#!/usr/bin/env python3
"""Check the private pool's BLS key at the immutable ledger tip."""

import helper_leios_bls as bls
import helper_sdk as sdk


def main() -> int:
    if bls.enabled():
        sdk.reachable("leios_bls_observe entered")
        bls.tick(submit=False)
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main, "leios_bls_observe_aborted", "leios_bls_observe_exits_zero" if bls.enabled() else None
    )
