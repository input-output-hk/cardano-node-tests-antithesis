# Governance test properties

Every assertion the `gov-cli` drivers emit, so "what's actually tested" is
answerable by reading this file instead of grepping
`components/gov-cli/composer/governance-python/`.

Antithesis groups these the same way in a report's **Properties** panel:

- **Always** — must hold every time it's checked. A single failure is a
  real bug.
- **Sometimes** — must happen at least once across the whole run. Never
  firing is a coverage gap, not a failure.
- **Reachable** / **Unreachable** — the driver got here at least once /
  must never get here. An `unreachable` id firing IS a failure.

Every driver also emits `<name>_exits_zero` (Always) and `<name>_aborted`
(Unreachable) via the shared `helper_sdk.run_driver` wrapper — omitted
below since they're identical scaffolding, not test-specific behavior.
`first_setup.py` is the one driver that doesn't emit `setup_exits_zero`
(a pre-existing gap, kept as-is — see the driver-simplification commit).

## `first_setup.py` — one-shot setup, no fault injection

| Assertion | Type | Meaning |
|---|---|---|
| `special_dreps_setup entered` | Reachable | special-DRep setup phase ran |
| `special_dreps_setup_already_done` | Sometimes | special-DRep setup was a no-op (idempotent restart) |
| `special_dreps_registration_failed` | Unreachable | special-DRep registration tx failed |
| `special_dreps_registration_submitted` | Sometimes | special-DRep registration tx submitted |
| `special_drep_<name>_confirmed` | Sometimes | a special DRep's delegation landed as expected |
| `special_dreps_setup_complete` | Sometimes | special-DRep phase finished |
| `first_setup entered` | Reachable | setup driver ran |
| `governance_setup_already_done` | Sometimes | setup was a no-op (idempotent restart) |
| `setup_node_not_ready` | Unreachable | node never answered within the wait budget |
| `setup_registration_failed` | Unreachable | main CC/DRep registration tx failed |
| `governance_registration_submitted` | Sometimes | main registration tx submitted |
| `committee_active_after_setup` | Sometimes | ≥1 CC member active right after setup |
| `dreps_registered_after_setup` | Sometimes | ≥1 DRep registered right after setup |
| `payment_pool_funded` | Sometimes | the shared payment-address pool got funded |
| `payment_pool_funding_failed` | Unreachable | funding the payment pool failed |
| `governance_setup_complete` | Sometimes | setup finished end to end |

## `parallel_driver_create_action.py` — submit one InfoAction

| Assertion | Type | Meaning |
|---|---|---|
| `create_action entered` | Reachable | driver ran |
| `create_action_node_not_ready` | Unreachable | node never answered within the wait budget |
| `info_action_created` | Sometimes (True and False variants) | coverage of both submit outcomes |
| `gov_op_under_perturbation` (`op: create`) | Sometimes | an InfoAction landed while the chain was recently stalled |

## `parallel_driver_vote.py` — cast one DRep/SPO/CC vote

| Assertion | Type | Meaning |
|---|---|---|
| `vote entered` | Reachable | driver ran |
| `vote_node_not_ready` | Unreachable | node never answered within the wait budget |
| `actions_live` | Sometimes | ≥1 votable action existed in gov-state |
| `vote_transient_failure` | Sometimes | a vote submit failed transiently (retried next tick) |
| `vote_submitted` | Reachable | a vote was actually submitted |
| `vote_recorded_<kind>` | Sometimes | that voter kind's (drep/spo/cc) vote was recorded |
| `vote_decision_<yes\|no\|abstain>` | Sometimes | that decision was cast at least once |
| `vote_decision_<decision>_by_<kind>` | Sometimes | that decision/voter-kind combination occurred |
| `action_voted_by_all_roles` | Sometimes | a single action got votes from all 3 roles |
| `action_majority_reached` | Sometimes | a single action's votes crossed the majority threshold |
| `gov_op_under_perturbation` (`op: vote`) | Sometimes | a vote landed while the chain was recently stalled |

## `parallel_driver_create_treasury_withdrawal.py` — submit one TreasuryWithdrawals action

Unlike an InfoAction, this DOES ratify and enact once DRep+CC approval
clears (SPOs can't vote on it at all - see the vote driver below), so it
exercises the enactment/treasury-debit path the InfoAction workload
deliberately skips. `transfer_amt` is kept small (1-5 ADA) so a long run
can't meaningfully drain the treasury even with many enactments.

| Assertion | Type | Meaning |
|---|---|---|
| `create_treasury_withdrawal entered` | Reachable | driver ran |
| `create_treasury_withdrawal_node_not_ready` | Unreachable | node never answered within the wait budget |
| `treasury_withdrawal_created` | Sometimes (True and False variants) | coverage of both submit outcomes |
| `gov_op_under_perturbation` (`op: create_treasury_withdrawal`) | Sometimes | a treasury withdrawal action landed while the chain was recently stalled |

## `parallel_driver_vote_treasury_withdrawal.py` — cast one DRep/CC vote

Restricted to DRep and CC voters — cardano-node-tests' own
`test_enact_treasury_withdrawals` confirms the CLI rejects an SPO vote on
this action type with a `StakePoolVoter` error, so SPOs are left out of
the roster rather than submitted and expected to fail.

| Assertion | Type | Meaning |
|---|---|---|
| `vote_treasury_withdrawal entered` | Reachable | driver ran |
| `vote_treasury_withdrawal_node_not_ready` | Unreachable | node never answered within the wait budget |
| `treasury_withdrawal_actions_live` | Sometimes | ≥1 votable treasury withdrawal action existed in gov-state |
| `treasury_withdrawal_vote_transient_failure` | Sometimes | a vote submit failed transiently (retried next tick) |
| `treasury_withdrawal_vote_submitted` | Reachable | a vote was actually submitted |
| `treasury_withdrawal_vote_recorded_<kind>` | Sometimes | that voter kind's (drep/cc) vote was recorded |
| `treasury_withdrawal_vote_decision_<yes\|no\|abstain>` | Sometimes | that decision was cast at least once |
| `treasury_withdrawal_vote_decision_<decision>_by_<kind>` | Sometimes | that decision/voter-kind combination occurred |
| `treasury_withdrawal_voted_by_drep_and_cc` | Sometimes | a single action got votes from both roles |
| `treasury_withdrawal_majority_reached` | Sometimes | a single action's votes crossed the majority threshold |
| `gov_op_under_perturbation` (`op: vote_treasury_withdrawal`) | Sometimes | a vote landed while the chain was recently stalled |

## `anytime_treasury_withdrawal_enactment.py` — runs continuously, including under faults

Pairs with the create driver above: confirms every treasury withdrawal
this repo submitted, once it leaves the live set, actually settled its
funds-receiving reward account correctly - not just that the action
disappeared from gov-state. `treasury_withdrawal_no_overpay` is the
reason this driver exists: it's what would catch a crash between
ratification and payout getting retried on recovery and paying out
twice.

| Assertion | Type | Meaning |
|---|---|---|
| `treasury_withdrawal_enactment entered` | Reachable | checker ran |
| `treasury_withdrawal_enactment_unavailable` | Unreachable | node didn't answer a gov-state query (absorbed, not flagged) |
| `treasury_withdrawal_slot_reclaimed_after_abandonment` | Sometimes | a funds-receiving claim outlived a create-driver process that died mid-submit, and was self-healed |
| `treasury_withdrawal_enacted_correctly` | Sometimes | a resolved withdrawal's reward balance increased by at least the transferred amount |
| `treasury_withdrawal_resolved_without_payout` | Sometimes | a withdrawal left the live set (rejected/expired) without a payout landing |
| `treasury_withdrawal_no_overpay` | **Always** | a resolved withdrawal's reward-balance delta never exceeds the amount it transferred - catches a double payout on fault-recovery |

## `anytime_govstate_invariant.py` — runs continuously, including under faults

| Assertion | Type | Meaning |
|---|---|---|
| `govstate_invariant entered` | Reachable | invariant check ran |
| `govstate_unavailable` | Unreachable | node didn't answer a gov-state query (absorbed, not flagged) |
| `govstate_well_formed` | **Always** | gov-state always parses and exposes a `proposals` array |
| `committee_quorum_maintained` | **Always** | authorized CC members never drop below `committeeMinSize` (2), even under fault injection |
| `special_drep_<name>_delegation_stable` | **Always** | the always-abstain/always-no-confidence delegations never drift once on-chain |
| `action_near_expiry` | Sometimes | an action reached its final epoch before expiry (lifecycle coverage) |

## `anytime_chain_progress.py` — the perturbation witness, runs continuously

| Assertion | Type | Meaning |
|---|---|---|
| `chain_progress entered` | Reachable | probe ran |
| `chain_progress_relay_unreachable` | Unreachable | relay1 (fault-excluded) didn't answer — should be rare |
| `chain_stalled_under_fault` | Sometimes | faults actually halted block production at least once (proves perturbation is real) |
| `chain_producing` | Sometimes | the chain produced blocks during a sample window |
| `relay_reachable_under_fault` | **Always** | relay1 must always answer, since it's excluded from faults |

## `eventually_votes_recorded.py` — post-fault recovery check

| Assertion | Type | Meaning |
|---|---|---|
| `eventually_votes entered` | Reachable | recovery check ran |
| `eventually_cold_start` | Unreachable | node/gov-state wasn't available after the settle window |
| `action_fully_voted_after_recovery` | Sometimes | ≥1 action had both DRep and SPO votes after faults stopped |

## `finally_governance_summary.py` — end-of-run marker, no node calls

| Assertion | Type | Meaning |
|---|---|---|
| `governance_run_completed` | Sometimes | the gov-cli lifecycle reached end-of-test |
| `finally_governance_summary entered` | Reachable | marker ran |

## Always vs Sometimes: two different questions, both useful

`always` and `sometimes` aren't a strong/weak pair — they check different
things. `always(cond, id)` fails the moment `cond` is false on any single
check: it's for invariants that must hold every time, and a failure means
a real bug. `sometimes(cond, id)` never fails from `cond` being false on
some checks — it only fails if `cond` is true on *none* of them across the
whole run: it's proof that your test workload actually reaches a given
scenario at all.

That second question has teeth of its own. If a code change silently broke
vote submission so it never actually landed on-chain, no exception would
fire and no `always` would trip — `vote_recorded_drep` would just quietly
stop firing. A `sometimes` that goes from reliably green to never-hit
across several runs is exactly the signal that catches that kind of
regression, even though it never "fails" in the traditional sense.

Most of the `sometimes` properties above are `sometimes` by design, not by
oversight - either they record both sides of a branch on purpose
(`info_action_created`'s True/False pair, `chain_stalled_under_fault` vs
`chain_producing`), describe states that are only sometimes expected to be
true (`actions_live`, `action_majority_reached`, `action_near_expiry`), or
are checked with enough propagation lag under fault injection that
demanding they hold on every single check would produce false-positive
failures on healthy runs (`vote_recorded_<kind>`). None of these should
become `always`.

The properties that genuinely are invariants - must hold on every check,
no exceptions - are the **Always** ones: `govstate_well_formed`,
`committee_quorum_maintained`, `special_drep_<name>_delegation_stable`,
`relay_reachable_under_fault`, and `treasury_withdrawal_no_overpay` —
plus any `unreachable` id that fires at all.

Note: this testnet also inherits generic consensus-safety properties
from the shared tracer-sidecar tooling (reused from the `master`
testnet), not defined in this repo — e.g. `cluster fork depth < k`. Those
show up in the Antithesis report's Properties panel too, under a
different source (`tracer-sidecar.example`'s own SDK), not in this file.
