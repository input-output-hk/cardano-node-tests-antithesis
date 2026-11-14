#!/usr/bin/env python3
"""Supply bounded private Plutus transactions during fault exploration."""

import helper_leios_plutus as plutus
import helper_sdk as sdk


def main() -> int:
    if plutus.enabled():
        sdk.reachable("leios_plutus_submit entered")
        plutus.tick(submit=True)
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_plutus_submit_aborted",
        "leios_plutus_submit_exits_zero" if plutus.enabled() else None,
    )
