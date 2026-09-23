#!/usr/bin/env python3
"""anytime_govstate_invariant.py — gov-state well-formedness invariant.

May run at any time, including under fault injection. Whenever the node
answers, its governance state must parse and expose a proposals array.
A missing node (mid-fault) is absorbed, not flagged.
"""

from __future__ import annotations

import helper_gov as g
import helper_sdk as sdk


def main() -> int:
    sdk.reachable("govstate_invariant entered")
    cluster = g.make_cluster()
    try:
        gov_state = cluster.g_query.get_gov_state()
    except Exception:  # noqa: BLE001
        sdk.unreachable("govstate_unavailable")
        return 0

    sdk.always(isinstance(gov_state, dict) and "proposals" in gov_state, "govstate_well_formed")

    # Invariant: once setup has authorized the committee, its quorum must
    # survive fault injection (CC auth is on-chain state, not a container,
    # so killing producers must never drop authorized members below
    # minSize). Bash counts `committee-state --active`, i.e. members whose
    # status is Active; mirror that against the full committee-state.
    if g.SETUP_MARKER.exists():
        try:
            authorized = g.count_active_committee_members(cluster)
            # committeeMinSize is 2 in the seeded Conway genesis.
            sdk.always(
                authorized >= 2, "committee_quorum_maintained", {"authorized": authorized, "min": 2}
            )
        except Exception:  # noqa: BLE001
            pass

    # Invariant: the always-abstain / always-no-confidence vote-stake
    # delegations set up by first_setup.py must never drift
    # once on-chain — unlike a DRep delegation there's no key to re-submit
    # a certificate with under fault injection, so any loss would be a
    # ledger bug, not a driver retry opportunity.
    if g.SPECIAL_DREPS_MARKER.exists():
        for name, _kwargs, expected in g.SPECIAL_DREP_TARGETS:
            addr_file = g.SPECIAL_DREPS_DIR / f"special_{name}.addr"
            if not addr_file.exists():
                continue
            try:
                addr = addr_file.read_text().strip()
                matches, actual = g.check_vote_delegation(cluster, addr, expected)
                sdk.always(
                    matches, f"special_drep_{name}_delegation_stable", {"vote_delegation": actual}
                )
            except Exception:  # noqa: BLE001
                pass

    # Lifecycle coverage (stateless, derived from gov-state): a proposal
    # stays in `proposals` through epoch == expiresAfter and is gone by
    # expiresAfter + 1 (verified against cardano-node-tests, which waits
    # for epoch == action_epoch + govActionLifetime + 1 - the ledger's own
    # expiresAfter value - and confirms the action is STILL present there,
    # only gone one epoch later). So expiresAfter == ep is the last epoch
    # it's actually observable in.
    #
    # must_hit=False: with govActionLifetime=2 (cardonnay's default -
    # shared with TreasuryWithdrawals/ParameterChange, so not something to
    # shrink just for this) and moog's DURATION hard-capped at 3h, an
    # action created right after first_setup's 1-epoch wait only reaches
    # expiresAfter at epoch 4 - never reached within the ~3.6-epoch budget
    # a 3h run allows. This is a real timing-budget ceiling, not a driver
    # bug, so it's recorded as observational rather than a required hit.
    try:
        ep = cluster.g_query.get_epoch()
        near = sum(1 for p in (gov_state.get("proposals", []) or []) if p.get("expiresAfter") == ep)
        sdk.sometimes(near >= 1, "action_near_expiry", {"near": near, "epoch": ep}, must_hit=False)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sdk.run_driver(main, "govstate_invariant_aborted", "govstate_invariant_exits_zero")
