#!/usr/bin/env python3
"""first_setup_special_dreps.py — one-shot special-DRep delegation (Python).

`first_` runs once, before any driver, with NO fault injection — same
lifecycle slot as first_setup.py, but kept as a separate script per
operation. Delegates two freshly generated vote-stake addresses to the
Conway ledger's predefined always-abstain / always-no-confidence
targets (as opposed to a real registered DRep, which is all
first_setup.py delegates). That stake then auto-counts in ratification
tallies with no vote transaction ever cast on its behalf, exercising a
ledger code path the create/vote drivers never touch.

Waits for first_setup.py's SETUP_MARKER before doing anything, since it
shares the same faucet and must not race the main registration tx.
"""

from __future__ import annotations

import sys
import time

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

SPECIAL_DELEGATED = 500_000_000_000  # same weight as first_setup.py's DREP_DELEGATED
WAIT_FOR_SETUP_SECONDS = 1800

TARGETS = [
    ("always_abstain", {"always_abstain": True}, "alwaysAbstain"),
    ("always_no_confidence", {"always_no_confidence": True}, "alwaysNoConfidence"),
]


def main() -> int:
    sdk.reachable("special_dreps_setup entered")
    g.ensure_dirs()

    if g.SPECIAL_DREPS_MARKER.exists():
        sdk.sometimes(True, "special_dreps_setup_already_done")
        return 0

    waited = 0
    while not g.SETUP_MARKER.exists():
        if waited >= WAIT_FOR_SETUP_SECONDS:
            sdk.unreachable("special_dreps_setup_timed_out")
            return 1
        time.sleep(5)
        waited += 5

    cluster = g.make_cluster()
    if not g.wait_for_node(cluster):
        sdk.unreachable("special_dreps_node_not_ready")
        return 1

    g.SPECIAL_DREPS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        deposit = cluster.g_query.get_address_deposit()

        cert_files: list = []
        signing: list = []
        txouts: list = []
        addrs: dict[str, str] = {}

        for name, deleg_kwargs, _expected in TARGETS:
            stake_keys = cluster.g_stake_address.gen_stake_key_pair(
                key_name=f"special_{name}",
                destination_dir=str(g.SPECIAL_DREPS_DIR),
            )
            addr_rec = cluster.g_address.gen_payment_addr_and_keys(
                name=f"special_{name}",
                stake_vkey_file=stake_keys.vkey_file,
                destination_dir=str(g.SPECIAL_DREPS_DIR),
            )
            reg_cert = cluster.g_stake_address.gen_stake_addr_registration_cert(
                addr_name=f"special_{name}",
                deposit_amt=deposit,
                stake_vkey_file=stake_keys.vkey_file,
                destination_dir=str(g.SPECIAL_DREPS_DIR),
            )
            deleg_cert = cluster.g_stake_address.gen_vote_delegation_cert(
                addr_name=f"special_{name}",
                stake_vkey_file=stake_keys.vkey_file,
                destination_dir=str(g.SPECIAL_DREPS_DIR),
                **deleg_kwargs,
            )
            cert_files += [reg_cert, deleg_cert]
            signing += [addr_rec.skey_file, stake_keys.skey_file]
            txouts.append(clusterlib.TxOut(address=addr_rec.address, amount=SPECIAL_DELEGATED))
            addrs[name] = addr_rec.address

        g.build_sign_submit(
            cluster,
            "setup_special_dreps",
            certificate_files=cert_files,
            signing_key_files=signing,
            txouts=txouts,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"special-DRep registration tx failed: {exc}", file=sys.stderr)
        sdk.unreachable("special_dreps_registration_failed")
        return 1

    sdk.sometimes(True, "special_dreps_registration_submitted")

    try:
        start_epoch = g.current_epoch(cluster)
        g.wait_for_epoch(cluster, start_epoch + 1, 1800)
    except Exception:  # noqa: BLE001
        pass

    for name, _deleg_kwargs, expected in TARGETS:
        try:
            info = cluster.g_query.get_stake_addr_info(addrs[name])
            sdk.sometimes(
                info.vote_delegation == expected,
                f"special_drep_{name}_confirmed",
                {"vote_delegation": info.vote_delegation},
            )
        except Exception:  # noqa: BLE001
            pass

    g.SPECIAL_DREPS_MARKER.touch()
    sdk.sometimes(True, "special_dreps_setup_complete")
    print(f"special-DRep setup complete ({', '.join(addrs)})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"special_dreps_setup aborted: {exc}", file=sys.stderr)
        sdk.unreachable("special_dreps_setup_aborted")
        sys.exit(0)
