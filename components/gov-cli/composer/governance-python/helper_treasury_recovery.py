"""Position the Leios guided payout within a bounded recovery horizon."""

from __future__ import annotations

import json
import math
import sys
import time

import anytime_treasury_withdrawal_enactment as checker
import helper_gov as g
import helper_sdk as sdk
import helper_treasury as treasury
from cardano_clusterlib import clusterlib

POLL_SECONDS = 5


def leios_window() -> tuple[int, float] | None:
    genesis = json.loads((g.GOV_STATE_DIR / "shelley/genesis.json").read_text())
    if int(genesis["protocolParams"]["protocolVersion"]["major"]) < 12:
        return None
    window_slots = math.ceil(genesis["securityParam"] / genesis["activeSlotsCoeff"])
    # Start faults no more than two confirmation windows before enactment.
    # Four windows then cover the boundary, immutability, and retry overhead.
    return 2 * window_slots, max(120, 4 * window_slots * genesis["slotLength"])


def poll_seconds(cluster: clusterlib.ClusterLib, observation: dict) -> float:
    if observation.get("ratified_epoch") is None or observation.get("waiting_reason") not in (
        "immutable_proposal_live",
        "volatile_proposal_live",
        "next_immutable_epoch",
    ):
        return POLL_SECONDS
    remaining_slots = cluster.epoch_length - observation["immutable_slot"] % cluster.epoch_length
    return max(
        POLL_SECONDS,
        min(remaining_slots * cluster.slot_length, cluster.epoch_length * cluster.slot_length / 10),
    )


def prepare(cluster: clusterlib.ClusterLib) -> bool:
    window = leios_window()
    if window is None:
        return True
    sdk.sometimes(False, "leios_treasury_recovery_ready")
    margin, recovery_seconds = window
    remaining = 2 * cluster.epoch_length * cluster.slot_length
    last_error = None
    for _ in range(math.ceil(remaining / POLL_SECONDS) + 1):
        observation: dict = {}
        try:
            with (
                treasury.lock() as observer_locked,
                treasury.receiver_lock(treasury.GUIDED_RECEIVER) as receiver_locked,
            ):
                if observer_locked and receiver_locked:
                    record = dict(g.pending_recv_slots()).get(treasury.GUIDED_RECEIVER)
                    if record is None:
                        msg = "guided recipient claim is missing during Leios recovery preparation"
                        raise ValueError(msg)
                    paid = checker.observe(
                        cluster,
                        treasury.GUIDED_RECEIVER,
                        record,
                        treasury.gov_state(cluster),
                        observation=observation,
                    )
                    ratified_epoch = observation.get("ratified_epoch")
                    until_boundary = (
                        cluster.epoch_length - observation["immutable_slot"] % cluster.epoch_length
                    )
                    if paid or (
                        ratified_epoch == observation["immutable_epoch"]
                        and until_boundary <= margin
                    ):
                        sdk.sometimes(
                            True,
                            "leios_treasury_recovery_ready",
                            {**observation, "recovery_seconds": recovery_seconds},
                        )
                        return True
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            observation.clear()
            print(f"Leios treasury recovery preparation will retry: {exc}", file=sys.stderr)
        if remaining <= 0:
            break
        delay = min(remaining, poll_seconds(cluster, observation))
        time.sleep(delay)
        remaining -= delay
    sdk.unreachable("leios_treasury_recovery_setup_failed")
    print(f"Leios treasury recovery preparation failed: {last_error}", file=sys.stderr)
    return False
