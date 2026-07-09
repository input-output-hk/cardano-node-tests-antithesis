#!/usr/bin/env python3
"""first_setup.py — one-shot governance setup phase (Python).

`first_` runs after setup_complete, before any driver, with NO fault
injection. Brings the committee + DReps on-chain so the create/vote
drivers have real voters. Submits cardonnay's pre-generated certs via
cardano-clusterlib: CC hot-key authorizations, DRep registrations, and
vote-stake registration + delegation (funding each vote-stake address),
then waits one epoch for the DRep stake distribution to go live.

Also delegates two freshly generated vote-stake addresses to the
Conway ledger's predefined always-abstain / always-no-confidence
targets (as opposed to a real registered DRep, which is all the main
registration tx delegates). That stake then auto-counts in
ratification tallies with no vote transaction ever cast on its behalf,
exercising a ledger code path the create/vote drivers never touch.
This used to be a separate first_setup_special_dreps.py script that
polled for this script's SETUP_MARKER with its own 30-minute timeout —
since it shares the same faucet and can't run before this script's own
registration tx, the two clocks raced under load and it was folded in
here instead.
"""

from __future__ import annotations

import sys
import time

import helper_gov as g
import helper_sdk as sdk
from cardano_clusterlib import clusterlib

DREP_DELEGATED = 500_000_000_000
SPECIAL_DELEGATED = 500_000_000_000  # same weight as DREP_DELEGATED

SPECIAL_DREP_TARGETS = [
    ("always_abstain", {"always_abstain": True}, "alwaysAbstain"),
    ("always_no_confidence", {"always_no_confidence": True}, "alwaysNoConfidence"),
]


def _setup_special_dreps(cluster: clusterlib.ClusterLib) -> dict[str, str]:
    """One-shot always-abstain / always-no-confidence vote-stake delegation.

    Returns the {name: address} map for later delegation confirmation, or
    {} if setup was already done or the registration tx failed.
    """
    sdk.reachable("special_dreps_setup entered")

    if g.SPECIAL_DREPS_MARKER.exists():
        sdk.sometimes(True, "special_dreps_setup_already_done")
        return {}

    g.SPECIAL_DREPS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        deposit = cluster.g_query.get_address_deposit()

        cert_files: list = []
        signing: list = []
        txouts: list = []
        addrs: dict[str, str] = {}

        for name, deleg_kwargs, _expected in SPECIAL_DREP_TARGETS:
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
        return {}

    sdk.sometimes(True, "special_dreps_registration_submitted")
    return addrs


def _confirm_special_dreps(cluster: clusterlib.ClusterLib, addrs: dict[str, str]) -> None:
    """Check delegation landed and drop the completion marker. No-op if
    _setup_special_dreps was skipped or failed (addrs empty)."""
    if not addrs:
        return

    for name, _deleg_kwargs, expected in SPECIAL_DREP_TARGETS:
        addr = addrs.get(name)
        if addr is None:
            continue
        try:
            info = cluster.g_query.get_stake_addr_info(addr)
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


def main() -> int:
    sdk.reachable("first_setup entered")
    g.ensure_dirs()

    if g.SETUP_MARKER.exists():
        sdk.sometimes(True, "governance_setup_already_done")
        sdk.setup_complete()
        return 0

    cluster = g.make_cluster()
    if not g.wait_for_node(cluster):
        sdk.unreachable("setup_node_not_ready")
        return 1

    cert_files: list = []
    signing: list = []
    txouts: list = []

    for i in range(1, g.NUM_DREPS + 1):
        addr = (g.GD / f"vote_stake_addr{i}.addr").read_text().strip()
        cert_files += [
            g.GD / f"default_drep_{i}_drep_reg.cert",
            g.GD / f"vote_stake_addr{i}_stake.reg.cert",
            g.GD / f"vote_stake_addr{i}_stake.vote_deleg.cert",
        ]
        signing += [
            g.GD / f"default_drep_{i}_drep.skey",
            g.GD / f"vote_stake_addr{i}.skey",
            g.GD / f"vote_stake_addr{i}_stake.skey",
        ]
        txouts.append(clusterlib.TxOut(address=addr, amount=DREP_DELEGATED))

    for i in range(1, g.NUM_CC + 1):
        auth = g.GD / f"cc_member{i}_committee_hot_auth.cert"
        if not auth.exists():
            continue
        cert_files.append(auth)
        signing.append(g.GD / f"cc_member{i}_committee_cold.skey")

    try:
        txid = g.build_sign_submit(
            cluster,
            "setup_register",
            certificate_files=cert_files,
            signing_key_files=signing,
            txouts=txouts,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"registration tx failed: {exc}", file=sys.stderr)
        sdk.unreachable("setup_registration_failed")
        return 1

    print(f"registration tx submitted: {txid}", file=sys.stderr)
    sdk.sometimes(True, "governance_registration_submitted")

    special_drep_addrs = _setup_special_dreps(cluster)

    # DRep stake delegation takes effect at the next epoch boundary.
    try:
        start_epoch = g.current_epoch(cluster)
        # Epochs are ~16.7 min (epochLength 5000 × 0.2s), so allow > 1 epoch.
        g.wait_for_epoch(cluster, start_epoch + 1, 1800)
    except Exception:  # noqa: BLE001
        pass

    _confirm_special_dreps(cluster, special_drep_addrs)

    cc_active = 0
    try:
        cc_state = cluster.g_query.get_committee_state()
        members = (cc_state or {}).get("committee", {}) or {}
        cc_active = sum(
            1 for m in members.values() if (m or {}).get("status") == "Active"
        )
    except Exception:  # noqa: BLE001
        pass
    sdk.sometimes(cc_active >= 1, "committee_active_after_setup")

    dreps = 0
    try:
        dreps = len(cluster.g_query.get_drep_state() or [])
    except Exception:  # noqa: BLE001
        pass
    sdk.sometimes(dreps >= 1, "dreps_registered_after_setup")

    # Generate and fund a pool of payment addresses for the parallel drivers.
    pool_dir = g.PAYMENT_POOL
    pool_dir.mkdir(parents=True, exist_ok=True)
    pool_txouts = []
    for i in range(g.NUM_PAYMENT_ADDRS):
        keys = cluster.g_address.gen_payment_key_pair(
            key_name=f"addr_{i}",
            destination_dir=str(pool_dir),
        )
        addr = cluster.g_address.gen_payment_addr(
            addr_name=f"addr_{i}",
            payment_vkey_file=keys.vkey_file,
            destination_dir=str(pool_dir),
        )
        (pool_dir / f"addr_{i}.addr").write_text(addr + "\n")
        pool_txouts.append(clusterlib.TxOut(address=addr, amount=g.PAYMENT_ADDR_FUND))
    try:
        g.build_sign_submit(cluster, "fund_payment_pool", txouts=pool_txouts)
        sdk.sometimes(True, "payment_pool_funded")
        print(f"payment pool funded ({g.NUM_PAYMENT_ADDRS} addresses)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"payment pool funding failed: {exc}", file=sys.stderr)
        sdk.unreachable("payment_pool_funding_failed")
        return 1

    g.SETUP_MARKER.touch()
    sdk.sometimes(True, "governance_setup_complete")
    sdk.setup_complete()
    print(f"setup complete (cc_active={cc_active} dreps={dreps})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # Absorb fault-induced failures into a coverage signal, exit 0.
        print(f"setup aborted: {exc}", file=sys.stderr)
        sdk.unreachable("setup_aborted")
        sys.exit(0)
