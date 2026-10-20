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

Also registers a small dedicated pool of fresh stake addresses
(treasury_recv{i}) used only as treasury-withdrawal funds-receiving
targets by parallel_driver_create_treasury_withdrawal.py - kept separate
from vote_stake_addr{i} (already a deposit-return sink elsewhere) so a
withdrawal's reward-balance delta can be attributed unambiguously. The
same faucet-race reasoning as above is why this lives here instead of
its own first_ script.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time

import helper_gov as g
import helper_sdk as sdk
import helper_treasury as treasury
import parallel_driver_create_treasury_withdrawal as treasury_creator
import parallel_driver_vote_treasury_withdrawal as treasury_voter
from cardano_clusterlib import clusterlib

DREP_DELEGATED = 500_000_000_000
SPECIAL_DELEGATED = DREP_DELEGATED


def _leios_public_key(params: dict) -> str | None:
    # Pinned prototype schema, then the newer BLS key-with-epoch schema.
    legacy = (params.get("spsLeiosKey") or {}).get("leiosPubKey")
    modern = ((params.get("spsBlsKey") or {}).get("bksKey") or {}).get("blsPubKey")
    return legacy or modern


def _leios_pool_keys(cluster: clusterlib.ClusterLib, *, confirm: bool = False) -> bool:
    """Bootstrap node voting keys separately from all parallel workloads.

    Genesis pools have no registered Leios key. Pool updates become active at
    the next epoch; the committee's later snapshot picks them up naturally.
    """
    genesis = json.loads((g.GOV_STATE_DIR / "shelley/genesis.json").read_text())
    if int(genesis["protocolParams"]["protocolVersion"]["major"]) < 12:
        return True
    try:
        certificates, signing = [], []
        for index in range(1, g.NUM_POOLS + 1):
            pool = g.GOV / "pools" / f"node-pool{index}"
            vkey = bytes.fromhex(json.loads((pool / "bls.vkey").read_text())["cborHex"])
            # Pinned CLI: a 96-byte public key in a definite CBOR byte string.
            if len(vkey) != 98 or vkey[:2] != bytes.fromhex("5860"):
                msg = "unsupported BLS verification-key encoding"
                raise ValueError(msg)
            expected = vkey[2:].hex()
            pool_id = cluster.g_stake_pool.get_stake_pool_id(cold_vkey_file=pool / "cold.vkey")
            info = cluster.g_query.get_pool_state(pool_id)
            active = _leios_public_key(info.pool_params)
            pending = _leios_public_key(info.future_pool_params)
            if active == expected or (not confirm and pending == expected):
                continue
            if confirm:
                msg = f"pool {index} voting key is not active after the epoch boundary"
                raise ValueError(msg)
            params = info.pool_params
            # These genesis pools have no metadata or relays. Do not silently
            # erase those fields if the baseline is extended in future.
            if params["spsRelays"] or params["spsMetadata"]:
                msg = "Leios bootstrap needs registration support for pool relays/metadata"
                raise ValueError(msg)
            cert = g.WORK / f"leios_pool{index}.cert"
            cluster.cli(
                [
                    "stake-pool",
                    "registration-certificate",
                    "--cold-verification-key-file",
                    str(pool / "cold.vkey"),
                    "--vrf-verification-key-file",
                    str(pool / "vrf.vkey"),
                    "--bls-signing-key-file",
                    str(pool / "bls.skey"),
                    "--pool-pledge",
                    str(params["spsPledge"]),
                    "--pool-cost",
                    str(params["spsCost"]),
                    "--pool-margin",
                    str(params["spsMargin"]),
                    "--pool-reward-account-verification-key-file",
                    str(pool / "reward.vkey"),
                    "--pool-owner-stake-verification-key-file",
                    str(pool / "owner-stake.vkey"),
                    "--testnet-magic",
                    str(cluster.genesis["networkMagic"]),
                    "--out-file",
                    str(cert),
                ]
            )
            certificates.append(cert)
            signing.extend([pool / "cold.skey", pool / "owner-stake.skey"])
        if certificates:
            g.build_sign_submit(
                cluster,
                "setup_leios_pool_keys",
                certificate_files=certificates,
                signing_key_files=signing,
            )
        sdk.sometimes(True, "leios_pool_keys_active" if confirm else "leios_pool_keys_submitted")
    except Exception as exc:  # noqa: BLE001
        print(f"Leios voting-key setup failed: {exc}", file=sys.stderr)
        sdk.unreachable("leios_pool_keys_setup_failed")
        return False
    return True


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

        for name, deleg_kwargs, _expected in g.SPECIAL_DREP_TARGETS:
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

    for name, _deleg_kwargs, expected in g.SPECIAL_DREP_TARGETS:
        addr = addrs.get(name)
        if addr is None:
            continue
        try:
            matches, actual = g.check_vote_delegation(cluster, addr, expected)
            sdk.sometimes(matches, f"special_drep_{name}_confirmed", {"vote_delegation": actual})
        except Exception:  # noqa: BLE001
            pass

    g.SPECIAL_DREPS_MARKER.touch()
    sdk.sometimes(True, "special_dreps_setup_complete")
    print(f"special-DRep setup complete ({', '.join(addrs)})", file=sys.stderr)


def _setup_treasury_recv_pool(cluster: clusterlib.ClusterLib) -> None:
    """One-shot: register NUM_DREPS fresh, dedicated stake addresses used
    ONLY as treasury-withdrawal funds-receiving targets - never as
    anyone's deposit-return target. parallel_driver_create_treasury_
    withdrawal.py claims one at a time and anytime_treasury_withdrawal_
    enactment.py diffs its reward balance across ticks to confirm a
    withdrawal paid out exactly once; that comparison only holds if
    nothing else can ever move these addresses' balances, which ruled
    out reusing vote_stake_addr{i} (see helper_gov.py). No epoch wait
    needed: a stake registration is valid for governance-action
    targeting as soon as it's on-chain, unlike DRep delegation.
    """
    sdk.reachable("treasury_recv_pool_setup entered")

    if g.TREASURY_RECV_MARKER.exists():
        sdk.sometimes(True, "treasury_recv_pool_already_done")
        return

    g.TREASURY_RECV_DIR.mkdir(parents=True, exist_ok=True)

    try:
        deposit = cluster.g_query.get_address_deposit()
        cert_files: list = []
        signing: list = []

        for i in range(1, g.NUM_DREPS + 1):
            name = f"treasury_recv{i}"
            stake_keys = cluster.g_stake_address.gen_stake_key_pair(
                key_name=name, destination_dir=str(g.TREASURY_RECV_DIR)
            )
            reg_cert = cluster.g_stake_address.gen_stake_addr_registration_cert(
                addr_name=name,
                deposit_amt=deposit,
                stake_vkey_file=stake_keys.vkey_file,
                destination_dir=str(g.TREASURY_RECV_DIR),
            )
            # Derive+persist the address once here so downstream ticks
            # never need another cardano-cli call just to read it back.
            cluster.g_stake_address.gen_stake_addr(
                addr_name=name,
                stake_vkey_file=stake_keys.vkey_file,
                destination_dir=str(g.TREASURY_RECV_DIR),
            )
            cert_files.append(reg_cert)
            signing.append(stake_keys.skey_file)

        g.build_sign_submit(
            cluster,
            "setup_treasury_recv_pool",
            certificate_files=cert_files,
            signing_key_files=signing,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"treasury-recv pool registration failed: {exc}", file=sys.stderr)
        sdk.unreachable("treasury_recv_pool_registration_failed")
        return

    g.TREASURY_RECV_MARKER.touch()
    sdk.sometimes(True, "treasury_recv_pool_registered")
    print(f"treasury-recv pool registered ({g.NUM_DREPS} addresses)", file=sys.stderr)


def _prepare_treasury_payout(cluster: clusterlib.ClusterLib) -> bool:
    """Seed the isolated payout lane before the delegation epoch wait.

    Genesis starts with an empty treasury; waiting for natural rewards adds
    another epoch before ratification is possible. Fund it with an explicit
    donation and prepare the proposal before the delegation epoch wait.
    Submission remains fault-tested by the parallel lanes; this lane starts
    with confirmed votes so faults can exercise ratification and settlement.
    """
    sdk.sometimes(False, "treasury_withdrawal_setup_ready")
    try:
        name = "guided_payment"
        address_file = g.TREASURY_RECV_DIR / f"{name}.addr"
        if not address_file.exists():
            keys = cluster.g_address.gen_payment_key_pair(
                key_name=name, destination_dir=str(g.TREASURY_RECV_DIR)
            )
            address = cluster.g_address.gen_payment_addr(
                addr_name=name,
                payment_vkey_file=keys.vkey_file,
                destination_dir=str(g.TREASURY_RECV_DIR),
            )
            address_file.write_text(address + "\n")
        source = clusterlib.AddressRecord(
            address=address_file.read_text().strip(),
            vkey_file=g.TREASURY_RECV_DIR / f"{name}.vkey",
            skey_file=g.TREASURY_RECV_DIR / f"{name}.skey",
        )
        if not cluster.g_query.get_utxo(address=source.address):
            g.build_sign_submit(
                cluster,
                "fund_guided_treasury",
                txouts=[clusterlib.TxOut(address=source.address, amount=g.PAYMENT_ADDR_FUND)],
                treasury_donation=treasury_creator.MAX_TRANSFER * g.NUM_DREPS,
            )
        # Keep the exact signed proposal for retries, including a setup restart.
        submission_file = g.TREASURY_RECV_DIR / "guided_proposal.signed"
        # The new recipient must reach the immutable tip before its baseline
        # can be taken. Retry transient snapshots and uncertain submissions.
        for _ in range(120):
            try:
                if treasury.guided_id() is None:
                    treasury_creator.create(cluster, source, submission_file=submission_file)
                action_id = treasury.guided_id()
                if action_id is not None:
                    prop = treasury.find_action(treasury.gov_state(cluster), *action_id)
                    if prop is None and submission_file.exists():
                        signed_id = cluster.g_transaction.get_txid(tx_file=submission_file)
                        if signed_id != action_id[0]:
                            msg = "saved treasury proposal does not match pending claim"
                            raise ValueError(msg)
                        cluster.g_transaction.submit_tx_bare(tx_file=submission_file)
                    if prop is not None:
                        voters = treasury_voter.build_voters(cluster)
                        treasury_voter.cast(cluster, source)
                        prop = treasury.find_action(treasury.gov_state(cluster), *action_id)
                        if (
                            prop is not None
                            and len(voters) == g.NUM_DREPS + g.NUM_CC
                            and all(
                                treasury_voter.recorded_vote(
                                    prop, voter[0], treasury_voter.voter_hash(cluster, voter)
                                )
                                == "VoteYes"
                                for voter in voters
                            )
                        ):
                            sdk.sometimes(
                                True,
                                "treasury_withdrawal_setup_ready",
                                {"txid": action_id[0], "epoch": g.current_epoch(cluster)},
                            )
                            return True
            except Exception as exc:  # noqa: BLE001
                print(f"guided treasury setup will retry: {exc}", file=sys.stderr)
            time.sleep(5)
    except Exception as exc:  # noqa: BLE001
        print(f"guided treasury setup failed: {exc}", file=sys.stderr)
    sdk.unreachable("treasury_withdrawal_setup_failed")
    return False


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

    if not _leios_pool_keys(cluster):
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
    _setup_treasury_recv_pool(cluster)

    if not _prepare_treasury_payout(cluster):
        return 1

    # DRep stake delegation takes effect at the next epoch boundary.
    try:
        start_epoch = g.current_epoch(cluster)
        # ~50 min/epoch at k=150 (epochLength is 100*k = 15000 slots x
        # 0.2s), so allow > 1 epoch. Not derived from k - revisit if k moves.
        g.wait_for_epoch(cluster, start_epoch + 1, 5400)
    except Exception:  # noqa: BLE001
        pass

    _confirm_special_dreps(cluster, special_drep_addrs)

    if not _leios_pool_keys(cluster, confirm=True):
        return 1

    cc_active = 0
    with contextlib.suppress(Exception):
        cc_active = g.count_active_committee_members(cluster)
    sdk.sometimes(cc_active >= 1, "committee_active_after_setup")

    dreps = 0
    with contextlib.suppress(Exception):
        dreps = len(cluster.g_query.get_drep_state() or [])
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
    sdk.run_driver(main, "setup_aborted")
