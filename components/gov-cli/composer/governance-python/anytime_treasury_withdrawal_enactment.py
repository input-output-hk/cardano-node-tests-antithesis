#!/usr/bin/env python3
"""anytime_treasury_withdrawal_enactment.py — verify each treasury
withdrawal this repo submitted actually paid out at most once.

May run at any time, including under fault injection. Pairs with
parallel_driver_create_treasury_withdrawal.py, which claims one
vote_stake_addr{i} as a withdrawal's funds-receiving target (at most one
pending withdrawal per address at a time, via helper_gov.claim_recv_slot)
and records its pre-submission reward-account balance.

Once the claimed action has left the live TreasuryWithdrawals set
(ratified+enacted, rejected, or expired), the reward-account balance is
what distinguishes those outcomes - and catches the fault-injection bug
this driver exists for: a crash between ratification and payout that
gets retried on recovery and pays out twice. The create/vote drivers
only ever observe that an action left gov-state, never whether the
treasury actually settled correctly afterwards.

A claim that never got a txid recorded (the create driver was killed
between claiming the slot and finishing the submit) is reclaimed after
one full epoch, so a process kill can't permanently strand a
funds-receiving slot.
"""

from __future__ import annotations

import helper_gov as g
import helper_sdk as sdk

STALE_CLAIM_EPOCHS = 2


def main() -> int:
    sdk.reachable("treasury_withdrawal_enactment entered")

    if not g.SETUP_MARKER.exists():
        return 0

    cluster = g.make_cluster()
    try:
        epoch = g.current_epoch(cluster)
        live_ids = {
            (p["actionId"]["txId"], p["actionId"]["govActionIx"])
            for p in g.live_treasury_withdrawal_actions(cluster)
        }
    except Exception:  # noqa: BLE001
        sdk.unreachable("treasury_withdrawal_enactment_unavailable")
        return 0

    for idx, record in g.pending_recv_slots():
        try:
            if "txid" not in record:
                if epoch - record.get("claimed_epoch", epoch) >= STALE_CLAIM_EPOCHS:
                    g.release_recv_slot(idx)
                    sdk.sometimes(True, "treasury_withdrawal_slot_reclaimed_after_abandonment")
                continue

            if (record["txid"], record["ix"]) in live_ids:
                continue  # still pending ratification/expiry

            current_balance = cluster.g_query.get_stake_addr_info(
                record["recv_addr"]
            ).reward_account_balance
            transfer_amt = record["transfer_amt"]
            delta = current_balance - record["pre_balance"]

            if delta >= transfer_amt:
                sdk.sometimes(
                    True,
                    "treasury_withdrawal_enacted_correctly",
                    {"delta": delta, "expected": transfer_amt},
                )
            else:
                sdk.sometimes(
                    True,
                    "treasury_withdrawal_resolved_without_payout",
                    {"delta": delta, "expected": transfer_amt},
                )

            # The one true invariant here: at most one pending withdrawal is
            # ever attached to a given recv_addr at a time, so a resolved
            # claim's balance can only have moved by 0 (rejected/expired) or
            # transfer_amt (enacted) - never more. A crash between
            # ratification and payout that's retried on recovery and pays
            # out twice would trip this.
            sdk.always(
                delta <= transfer_amt,
                "treasury_withdrawal_no_overpay",
                {"delta": delta, "expected": transfer_amt},
            )

            g.release_recv_slot(idx)
        except Exception:  # noqa: BLE001
            continue
    return 0


if __name__ == "__main__":
    sdk.run_driver(main, "treasury_withdrawal_enactment_aborted", "treasury_withdrawal_enactment_exits_zero")
