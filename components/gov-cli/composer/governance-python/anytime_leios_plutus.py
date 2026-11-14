#!/usr/bin/env python3
"""Check private script spends only after immutable EB certification."""

import helper_leios_plutus as plutus
import helper_sdk as sdk


def main() -> int:
    if plutus.enabled():
        sdk.reachable("leios_plutus_observe entered")
        plutus.tick(submit=False)
    return 0


if __name__ == "__main__":
    sdk.run_driver(
        main,
        "leios_plutus_observe_aborted",
        "leios_plutus_observe_exits_zero" if plutus.enabled() else None,
    )
