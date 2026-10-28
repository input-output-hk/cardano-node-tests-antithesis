# Governance test properties

Every assertion the `gov-cli` drivers emit, so "what's actually tested" is
answerable by reading this file instead of grepping
`components/gov-cli/composer/governance-python/`.

Antithesis groups these the same way in a report's **Properties** panel:

- **Always** — must hold every time it's checked. A single failure is a
  real bug.
- **Sometimes** — must be true at least once across the whole run. Never
  being true is a coverage failure; individual false samples are expected under faults.
- **Reachable** / **Unreachable** — the driver got here at least once /
  must never get here. An `unreachable` id firing IS a failure.

Every driver also emits `<name>_exits_zero` (Always) and `<name>_aborted`
(Unreachable) via the shared `helper_sdk.run_driver` wrapper — omitted
below since they're identical scaffolding, not test-specific behavior.
`first_setup.py` doesn't emit `setup_exits_zero` (a pre-existing gap,
kept as-is — see the driver-simplification commit). The Leios endorser
probe suppresses all its SDK properties, including scaffolding, in Conway.

**Reading a red Leios report:** in Leios mode the `tx-firehose` service
puts sustained transaction load on `relay1`, which every driver queries.
That gives `chain_progress_relay_unreachable` and the various
`*_node_not_ready` assertions a load-related failure mode they don't have
in Conway — a saturated mempool during a stall is when a
`LocalStateQuery` is most likely to time out. Check whether the load was
running before reading one of those as a node bug; `TX_FIREHOSE=false`
re-runs without it.

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
| `leios_pool_keys_submitted` | Sometimes, Leios only | all generated producer BLS keys are active, pending, or submitted with proof of possession |
| `leios_pool_keys_active` | Sometimes, Leios only | every producer's active ledger voting key matches its staged BLS verification key after the setup epoch wait |
| `leios_pool_keys_setup_failed` | Unreachable, Leios only | voting-key registration or confirmation failed; setup returns 1 without enabling faults |

Leios bootstrap registers the BLS keys the producers already start with. Genesis
pools initially have no ledger voting key. The configurator stages the matching
VRF, reward and owner keys; setup submits a single pool-update transaction with
the BLS proofs, cold-key and owner witnesses. It preserves pledge, cost, margin,
VRF and reward account, and makes the generated owner stake key the pool owner
(the genesis pools initially have no owners). Existing active or pending matching
keys are not resubmitted. The normal setup epoch wait activates the updates;
the Leios committee picks them up through its subsequent stake snapshot. This
is baseline initialization, separate from the isolated payment workload.

## `parallel_driver_create_action.py` — submit one InfoAction

| Assertion | Type | Meaning |
|---|---|---|
| `create_action entered` | Reachable | driver ran |
| `create_action_node_not_ready` | Unreachable | node never answered within the wait budget |
| `info_action_created` | Sometimes (True and False variants) | coverage of both submit outcomes |
| `gov_op_under_perturbation` (`op: create`) | Sometimes | an InfoAction landed while the chain was recently stalled |

## `parallel_driver_vote.py` — batch missing DRep/SPO/CC votes

Missing credentials are batched onto one expiring InfoAction, with an
independent random YES/NO/ABSTAIN decision per voter. Completed actions yield
to the next incomplete action; replacements continue once all are complete.
A workload-local nonblocking lock prevents overlapping batches. Participation
is observed before submission as well as after, so a failed submit cannot hide
an already reached threshold. The majority is a count of recorded votes of any
decision, not a stake-weighted approval threshold. Coverage is seeded false
once setup completes.

| Assertion | Type | Meaning |
|---|---|---|
| `vote entered` | Reachable | driver ran |
| `vote_node_not_ready` | Unreachable | node never answered within the wait budget |
| `actions_live` | Sometimes | ≥1 votable action existed in gov-state |
| `vote_transient_failure` | Sometimes | a vote submit failed transiently (retried next tick) |
| `vote_submitted` | Reachable | a vote was actually submitted |
| `vote_recorded_<kind>` | Sometimes | the submitted credential's exact decision was recorded for that voter kind |
| `vote_decision_<yes\|no\|abstain>` | Sometimes | that decision was cast at least once |
| `vote_decision_<decision>_by_<kind>` | Sometimes | that decision/voter-kind combination occurred |
| `action_voted_by_all_roles` | Sometimes | a single action got votes from all 3 roles |
| `action_majority_reached` | Sometimes | a single action has at least ceil((NUM_DREPS + NUM_POOLS + NUM_CC)/2) recorded votes of any decision |
| `gov_op_under_perturbation` (`op: vote`) | Sometimes | a vote landed while the chain was recently stalled |

## `parallel_driver_create_treasury_withdrawal.py` — submit one TreasuryWithdrawals action

Unlike an InfoAction, this DOES ratify and enact once DRep+CC approval
clears (SPOs can't vote on it at all - see the vote driver below), so it
exercises the enactment/treasury-debit path the InfoAction workload
deliberately skips. `transfer_amt` is kept small (1-5 ADA) so a long run
can't meaningfully drain the treasury even with many enactments.

Receiver 1 is reserved for guided payout, and receiver 2 for an unvoted
proposal that expires. Both are prioritised before the remaining random
receivers. Creators and voters share a nonblocking submission lock; the
observer has its own lock and continues during blocked submissions. Each
receiver has a separate nonblocking lock for preparing and updating its claim.
The creator
journals the signed transaction's id before broadcasting and bounds its
validity to one epoch, so an interrupted submission remains attributable and
cannot arrive after its claim is reclaimed. It releases the receiver lock after
journalling, before broadcasting or waiting for confirmation. Before that
point, only the receiver being prepared is excluded from observation. A killed
creator releases its kernel lock, allowing stale-claim reclamation.
Journal updates are atomic; the observer rereads each claim under its receiver
lock before applying observations, so a reused receiver cannot inherit stale
attribution.

| Assertion | Type | Meaning |
|---|---|---|
| `create_treasury_withdrawal entered` | Reachable | driver ran |
| `create_treasury_withdrawal_node_not_ready` | Unreachable | node never answered within the wait budget |
| `treasury_withdrawal_created` | Sometimes (True and False variants) | coverage of both submit outcomes |
| `treasury_withdrawal_query_unavailable` | Sometimes | a treasury query failed, returned an unusable schema, or could not acquire a consistent snapshot; claims remain pending |
| `gov_op_under_perturbation` (`op: create_treasury_withdrawal`) | Sometimes | a treasury withdrawal action landed while the chain was recently stalled |

## `parallel_driver_vote_treasury_withdrawal.py` — guided approval and random DRep/CC votes

Restricted to DRep and CC voters — cardano-node-tests' own
`test_enact_treasury_withdrawals` confirms the CLI rejects an SPO vote on
this action type with a `StakePoolVoter` error, so SPOs are left out of
the roster rather than submitted and expected to fail.

The guided proposal gets all missing YES votes in one transaction, using the
recorded vote for each specific credential. Its votes are never overwritten by
the random lane. Receiver 2 never receives votes from this driver, keeping
expiry independent of the payout path. Other proposals retain YES/NO/ABSTAIN
selection, concentrated on at most three expiring actions and preferentially choosing an unrecorded
voter. All live proposals are checked for participation, independently of a
new submission succeeding. Headcounts are coverage, not governance approval:
actual stake-weighted DRep and committee thresholds are checked by the ledger,
whose ratification result is observed below.

| Assertion | Type | Meaning |
|---|---|---|
| `vote_treasury_withdrawal entered` | Reachable | driver ran |
| `treasury_withdrawal_actions_live` | Sometimes | ≥1 votable treasury withdrawal action existed in gov-state |
| `treasury_withdrawal_vote_transient_failure` | Sometimes | a vote submit failed transiently (retried next tick) |
| `treasury_withdrawal_vote_submitted` | Reachable | a vote was actually submitted |
| `treasury_withdrawal_vote_recorded_<kind>` | Sometimes | the specific submitted credential's decision was recorded |
| `treasury_withdrawal_vote_decision_<yes\|no\|abstain>` | Sometimes | that decision was cast at least once |
| `treasury_withdrawal_vote_decision_<decision>_by_<kind>` | Sometimes | that decision/voter-kind combination occurred |
| `treasury_withdrawal_voted_by_drep_and_cc` | Sometimes | a single action got votes from both roles |
| `treasury_withdrawal_majority_reached` | Sometimes | a single action has at least ceil((NUM_DREPS + NUM_CC)/2) recorded votes of any decision; participation only, not ratification |
| `treasury_withdrawal_guided_votes_recorded` | Sometimes | all available guided DRep/CC credentials have recorded YES votes |
| `gov_op_under_perturbation` (`op: vote_treasury_withdrawal`) | Sometimes | a vote landed while the chain was recently stalled |

## `anytime_treasury_withdrawal_enactment.py` — runs continuously, including under faults

Pairs with the creator above. Governance and recipient balance are read at
the immutable tip, with matching tip hashes before and after the queries.
An unavailable query, unregistered recipient or advancing snapshot is unknown,
not an empty proposal set or zero balance. A claim is resolved only when its
action is absent from both volatile and immutable governance and the immutable
slot has passed its submission validity bound. Claims abandoned before the
pre-broadcast journal are reclaimable after two immutable epochs.

`nextRatifyState.enactedGovActions` supplies the ratification witness. Once
that immutable approval is followed by a terminal action in a later epoch,
the recipient must have received exactly the requested amount. A terminal
action without observed ratification may have expired or never been included.
Expiry coverage requires an earlier immutable inclusion witness and its ledger
`expiresAfter` epoch, followed by immutable disappearance beyond that epoch and
an unchanged recipient balance. Never-included submissions do not count.
The payout recipient and successfully expired receiver 2 remain reserved and
are checked throughout the run for delayed or duplicate payments.

Expiry is observational (`must_hit=False`): at production k=150 each epoch
lasts 50 minutes, and a proposal created after setup may remain live until
epoch 5, beyond the three-hour moog budget. It stays visible when unexercised;
immutable expiry evidence and the Always zero-payout check are still required
whenever the outcome occurs. The isolated expiry lane does not shorten the
shared governance lifetime or change production timing.

The majority, guided-YES, ratification and payout Sometimes properties are
emitted false before their success paths, so an unexercised phase stays visible
and fails coverage. The treasury workload applies to both Conway and Dijkstra;
it does not change the Leios-only gates of the load and EB probes.

Before the existing delegation epoch wait, setup funds a dedicated payment
credential, donates enough treasury funds for one maximum withdrawal per
recipient, and submits the guided withdrawal with confirmed YES votes from all
DReps and committee members. Genesis starts with an empty treasury; the donation
becomes available at the first epoch boundary, allowing ratification without
waiting for natural reward funding. Fault exploration starts with the approved
proposal pending settlement. Parallel lanes still exercise submission under
faults. Ratification, exact payout and overpayment checks remain mandatory.
Setup saves the exact signed proposal before journalling its action id and
rebroadcasts that same transaction if submission was interrupted. Failed
preparation prevents setup from completing. Protocol parameters and production
epoch timing are unchanged.

Treasury snapshot assertions include the receiver and action id, immutable slot
and epoch, observed ratification epoch, submission validity bound, both proposal
presence flags, and `waiting_reason`. The reason identifies a proposal still
live at the immutable or volatile tip, a validity bound not yet immutable, or a
wait for the next immutable epoch. It is null when terminal accounting is
eligible. Ratification observations carry the same details. These diagnostics
do not add queries or change the accounting predicates or coverage requirements.

| Assertion | Type | Meaning |
|---|---|---|
| `treasury_withdrawal_setup_ready` | Sometimes | setup confirmed the isolated guided proposal and every expected DRep/CC YES vote before its epoch wait |
| `treasury_withdrawal_setup_failed` | **AlwaysOrUnreachable** | guided payout preparation exhausted retries or failed; setup cannot complete |
| `treasury_withdrawal_enactment entered` | Reachable | checker ran |
| `treasury_withdrawal_query_unavailable` | Sometimes | retryable query/snapshot failure; no claim is resolved from unavailable state |
| `treasury_withdrawal_slot_reclaimed_after_abandonment` | Sometimes | a funds-receiving claim outlived a create-driver process that died mid-submit, and was self-healed |
| `treasury_withdrawal_ratified` | Sometimes | the ledger reports a tracked treasury action ratified at the immutable tip |
| `treasury_withdrawal_enacted_correctly` | Sometimes | an immutable, terminal withdrawal's recipient balance increased by exactly the transferred amount |
| `treasury_withdrawal_resolved_without_payout` | Sometimes (`must_hit=False`) | a withdrawal previously observed at the immutable tip expired and left both live and immutable sets with zero recipient balance delta |
| `treasury_withdrawal_no_overpay` | **Always** | a pending or retained guided claim's immutable balance delta is between zero and its transfer amount |
| `treasury_withdrawal_expired_no_payout` | **Always** | the isolated unvoted receiver remains unpaid after witnessed expiry, including on later checks |
| `treasury_withdrawal_payout_matches_ratification` | **Always** | a terminal action with immutable ratification evidence paid exactly its transfer amount in a later epoch |

## `eventually_treasury_withdrawal.py` — treasury settlement after fault recovery

Applies to both Conway and Leios. Antithesis stops scheduling `anytime_`
commands while terminal `eventually_` or `finally_` commands run, so the
continuous observer alone cannot witness a later epoch during recovery.
This dedicated recovery command polls the already-prepared guided recipient
through ratification and settlement, using the same immutable accounting checks
as the continuous observer. It submits no transactions or votes and does not
depend on another workload's recovery command running first.

The existing mandatory ratification and payout Sometimes assertions remain
enabled. A successful recovery requires an exact terminal payout; missing
claims, unavailable queries and stalled slots cannot count as success. Retry
sleep is bounded by three genesis-derived epoch durations using a loop counter;
exhaustion fails an Always assertion. Production k and epoch timing are unchanged.
Other terminal commands may be selected on other timelines; this command is a
separate treasury recovery path, not a guarantee that every timeline settles.

| Assertion | Type | Meaning |
|---|---|---|
| `treasury_withdrawal_recovery entered` | Reachable | treasury recovery command ran |
| `treasury_withdrawal_recovered` | **Always** | the guided withdrawal settled for exactly its requested amount within the recovery budget |
| `treasury_withdrawal_recovery_aborted` | **AlwaysOrUnreachable** | an unexpected exception escaped the recovery logic |
| `treasury_withdrawal_recovery_exits_zero` | **Always** | recovery command returned zero; failures are assertions |

## `parallel_driver_create_pparam_update.py` — submit one ParameterChange action

Unlike the other two action types, this one is *chained*: the ledger
tracks one "current" ParameterChange lineage at a time, and every new
proposal must reference the previous one's txid/index
(`helper_gov.get_prev_pparam_action`, queried live off gov-state each
tick) or the ledger rejects it outright. This DOES ratify and enact once
DRep+CC approval clears, exercising the enactment path for a real
protocol-parameter change. SPOs are not in the roster: their eligibility
depends on whether the targeted parameters fall in Conway's
security-relevant group, and the allowlist below stays out of it, so an
SPO vote here is rejected outright (see the vote driver). Only ever
targets a small allowlist of parameters
(`helper_gov.PPARAM_ALLOWLIST`) confirmed safe to toggle repeatedly -
neither affects fee/size math other drivers rely on, nor anything this
testnet's own genesis/config hardcodes.

| Assertion | Type | Meaning |
|---|---|---|
| `create_pparam_update entered` | Reachable | driver ran |
| `create_pparam_update_node_not_ready` | Unreachable | node never answered within the wait budget |
| `pparam_update_created` | Sometimes (True and False variants) | coverage of both submit outcomes |
| `gov_op_under_perturbation` (`op: create_pparam_update`) | Sometimes | a pparam update action landed while the chain was recently stalled |

## `parallel_driver_vote_pparam_update.py` — batch missing DRep/CC votes

Restricted to DRep and CC voters, same as
`parallel_driver_vote_treasury_withdrawal.py` — but for a different
reason. SPO voting eligibility on a ParameterChange action depends on
whether the targeted parameters fall in Conway's security-relevant
group; a real ledger rejection (`ConwayGovFailure (DisallowedVoters
(StakePoolVoter ...))`) confirmed this driver's SPO votes were
unconditionally rejected, since `helper_gov.PPARAM_ALLOWLIST`
deliberately targets only non-security-group parameters. SPOs are left
out of the roster rather than submitted and expected to fail every
time.

One transaction fills missing voter credentials on the earliest-expiring
votable proposal, with an independent random YES/NO/ABSTAIN choice per voter.
Completed proposals yield to incomplete ones before existing votes are replaced.
Expired actions still awaiting ledger removal are excluded from submission.
A workload-local nonblocking lock serializes vote batches; neither creators
nor observers acquire it. Participation is checked before and after submission,
and the majority coverage goal is registered false after setup even if no vote
can be submitted. A query failure remains unknown and is retried later.

| Assertion | Type | Meaning |
|---|---|---|
| `vote_pparam_update entered` | Reachable | driver ran |
| `vote_pparam_update_node_not_ready` | Unreachable | node never answered within the wait budget |
| `pparam_update_actions_live` | Sometimes | ≥1 votable pparam update action existed in gov-state |
| `pparam_update_vote_transient_failure` | Sometimes | a vote submit failed transiently (retried next tick) |
| `pparam_update_vote_submitted` | Reachable | a vote was actually submitted |
| `pparam_update_vote_recorded_<kind>` | Sometimes | the specific submitted DRep/CC credential has the requested decision recorded |
| `pparam_update_vote_query_unavailable` | Sometimes | a governance/epoch query failed; no recorded vote or empty ledger state is invented |
| `pparam_update_vote_decision_<yes\|no\|abstain>` | Sometimes | that decision was cast at least once |
| `pparam_update_vote_decision_<decision>_by_<kind>` | Sometimes | that decision/voter-kind combination occurred |
| `pparam_update_voted_by_drep_and_cc` | Sometimes | a single action got votes from both roles |
| `pparam_update_majority_reached` | Sometimes | a single action has at least ceil((NUM_DREPS + NUM_CC)/2) recorded votes of any decision; participation, not ratification |
| `gov_op_under_perturbation` (`op: vote_pparam_update`) | Sometimes | a vote landed while the chain was recently stalled |

## `anytime_pparam_update_enactment.py` — runs continuously, including under faults

Pairs with the create driver above: confirms whether each tracked
ParameterChange action, once it leaves the live set, actually changed
the targeted protocol parameter. Unlike a treasury withdrawal, there's
no overpay-style bug class here - setting a protocol parameter is
idempotent (re-enacting the same value twice is harmless, unlike
crediting a balance twice), so this driver only records which outcome
happened, with no Always invariant.

| Assertion | Type | Meaning |
|---|---|---|
| `pparam_update_enactment entered` | Reachable | checker ran |
| `pparam_update_enactment_unavailable` | Unreachable | node didn't answer a gov-state query (absorbed, not flagged) |
| `pparam_update_enacted_correctly` | Sometimes | a resolved action's targeted parameter matched the expected value |
| `pparam_update_resolved_without_enactment` | Sometimes | an action left the live set (rejected/expired) without the parameter changing |

## `anytime_govstate_invariant.py` — runs continuously, including under faults

| Assertion | Type | Meaning |
|---|---|---|
| `govstate_invariant entered` | Reachable | invariant check ran |
| `govstate_unavailable` | Unreachable | node didn't answer a gov-state query (absorbed, not flagged) |
| `govstate_well_formed` | **Always** | gov-state always parses and exposes a `proposals` array |
| `committee_quorum_maintained` | **Always** | authorized CC members never drop below `committeeMinSize` (2), even under fault injection |
| `special_drep_<name>_delegation_stable` | **Always** | the always-abstain/always-no-confidence delegations never drift once on-chain |
| `action_near_expiry` | Sometimes (not required) | an action reached its final epoch before expiry (lifecycle coverage) - see note below |

## `anytime_chain_progress.py` — the perturbation witness, runs continuously

| Assertion | Type | Meaning |
|---|---|---|
| `chain_progress entered` | Reachable | probe ran |
| `chain_progress_relay_unreachable` | Unreachable | relay1 (fault-excluded) didn't answer — should be rare |
| `chain_stalled_under_fault` | Sometimes | faults actually halted block production at least once (proves perturbation is real) |
| `chain_producing` | Sometimes | the chain produced blocks during a sample window |
| `relay_reachable_under_fault` | **Always** | relay1 must always answer, since it's excluded from faults |

## `anytime_leios_load.py` — Leios load witness, runs continuously

Leios-only: in Conway the load generator is inert by design, so this
driver asserts nothing at all there rather than leaving an unfired
Sometimes as noise. It gates on `protocolVersion.major` in the generated
genesis — the same artifact the generator's entrypoint gates on, so they cannot
disagree about whether load is expected.

| Assertion | Type | Meaning |
|---|---|---|
| `leios_load entered` | Reachable | probe ran |
| `leios_load_funding_key_missing` | Unreachable | Leios mode but gov-configurator never staged the funding key — the one case it deliberately only warns about, since failing there would block every service and produce a silently green run |
| `leios_load_observed` | Sometimes | tx-firehose submitted and the chain accepted it — the UTxO set at its funding address changed within a sample window |

`leios_load_observed` is the answer to "did the load actually happen".
Never going green across a Leios run means the generator was dead,
idling or disabled, so nothing in that run says anything about Leios
under fault injection — however green the rest of the report looks.

It is emitted on every sample with the real condition, not only when
true. A Sometimes only has to hold once per run, so the False samples
during a stall cost nothing, and registering it every tick is what makes
a dead generator show up as a never-satisfied must-hit rather than as an
absent property nobody notices.

## `anytime_leios_endorser.py`: endorser-block propagation coverage

Only `leios` and `leios-latest`: the generated genesis protocol major
must be at least 12. Conway emits none of this driver's SDK properties.

| Assertion | Type | Meaning |
|---|---|---|
| `leios_endorser entered` | Reachable | Leios endorser probe ran |
| `leios_nonempty_eb_propagated` | Sometimes | A producer forged an EB with `numTxs > 0` and another producer freshly acquired the same EB hash and slot during a probe window after setup |
| `leios_endorser_exits_zero` | Always | Leios probe completed normally |
| `leios_endorser_aborted` | Unreachable | Leios probe raised an unexpected exception |

This is an Antithesis coverage witness, not a throughput target or a
deadline on fault recovery. A partition can stop propagation for any
individual sample. The property is emitted with False on samples with
no match (including missing traces), so absent evidence cannot silently
remove it from a Leios report. False samples during setup do not count
startup traffic as evidence of exploration.

The driver reads the shared tracer volume read-only, matching
`Consensus.LeiosKernel.BlockForged` to `BlockAcquired` across producers.
It samples new acquisition records over 20 seconds, with up to 2 MiB
read per log file per pass. Old acquisition records, self-acquisition,
empty EBs, warnings and vote-scheduling events cannot satisfy it.
Matching hashes across a fork is still coverage, not evidence of
canonical-chain inclusion or successful certification. No shared cursor
or persistent observation state is needed between driver invocations.

## Isolated certified-EB payments — `parallel_driver_leios_transactions.py` / `anytime_leios_transactions.py` / `eventually_leios_transactions.py`

Enabled only by the v12 genesis used by `leios` and `leios-latest`; all three drivers
emit no SDK properties in Conway, including their wrapper scaffolding. The
Leios workflow selects `tracer-config-leios.yaml`, preserving machine logs and
adding human logs under `/tracer/eb-human`. The pinned node exposes EB
transaction references only in the human format.

The workload owns `/gov-data/leios_transactions`, a dedicated payment source
and recipient, and an `fcntl` lock shared only by its creator and observer.
The source receives a one-time faucet transfer under the existing faucet lock;
subsequent fan-outs and payments use only those private funds. It neither uses
governance payment addresses nor changes pools, committee membership or protocol
parameters. The recipient's outputs are never spent.

Payments are built in batches of up to 16, with about 13 KiB of metadata each,
so a burst can exceed a ranking block's capacity. At most 64 payment records are
pending. Every signed payment is journaled before the first broadcast; uncertain
submissions are retried while valid. Unproven records are retired only after
their validity bound (at most 1500 slots, or one epoch if shorter) is immutable,
allowing fresh attempts without an unbounded journal. After the first complete
witness, submission stops and the retained outputs continue to be checked.

EB references hash the whole signed transaction, not its transaction id. The
observer reconstructs the complete ordered EB body's CBOR and hashes it,
linking membership to the exact EB hash without guessing from timestamps.
It requires `LeiosCertifiedAndAnnounced`, which carries the actual constructed
header hash with its certificate flag set, rather than the pre-forge
`BlockCertified` trace. The hash-bound announcement in that block's immediate
parent identifies the certified EB. This deliberately witnesses certifying RBs
that also announce a new EB, a subset of all certifications. A hash-linked path
of forged block headers must connect that ranking block to the immutable tip. Missing headers,
orphaned certificates, ordinary-block payments and partial traces cannot satisfy
coverage. UTxOs are queried between two matching immutable tip hashes.

Reads are incremental and bounded to 8 MiB per file per invocation; partial
records are retried after append. Up to 20,000 block headers are retained.
Rotated or missing evidence can delay coverage, never establish inclusion.
The checks impose no transaction latency or recovery deadline during faults.

During terminal recovery, Antithesis no longer schedules the normal payment
submitter and anytime observer. The eventual driver continues both operations
with the same funds, lock and journal. It permits five genesis-derived epochs
of polling sleeps for the BLS stake snapshot pipeline, fresh payments,
certification and immutability, bounded by a counter if slots stop advancing.
Expired or ordinary-ranking-block payments do not satisfy coverage; fresh
recovery payments can provide the witness. Such a witness establishes recovery
of certification, not that a newly submitted payment experienced faults.
The same mandatory certification property and exact-output assertions apply;
exhausting the polling budget does not manufacture successful coverage.

| Assertion | Type | Meaning |
|---|---|---|
| `leios_transactions entered` | Reachable | isolated payment driver ran |
| `leios_tx_tracking entered` | Reachable | certified-EB observer ran |
| `leios_tx_recovery entered` | Reachable | Leios-only post-fault payment submission and observation ran |
| `leios_tx_recovery_aborted` | Unreachable | unexpected failure escaped the Leios recovery driver |
| `leios_tx_recovery_exits_zero` | Always | Leios recovery driver completed through the SDK wrapper |
| `leios_tx_certified_and_immutable` | Sometimes | a tracked payment belongs to an EB certified by an immutable ranking block, has the expected serialized size, and has the exact expected immutable output; emitted false before evidence is available |
| `leios_eb_tx_size_matches` | Always | the EB's size for a tracked signed transaction matches its serialized byte length |
| `leios_certified_tx_payment_matches` | Always | after immutable certification is proven, the unspent output exists at the private recipient with exactly the expected lovelace and no additional assets |
| `leios_tx_submission_unavailable` | Sometimes, optional | a retryable build/query/submit failure interrupted this invocation |
| `leios_tx_query_unavailable` | Sometimes, optional | the immutable query failed or could not acquire a consistent point; no payment invariant is evaluated |

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

`action_near_expiry` is marked `must_hit=False` (recorded, but never fails
a run on its own): with `govActionLifetime=2` (cardonnay's default, shared
with `TreasuryWithdrawals`/`ParameterChange` so not something to shrink
just for this one property) and moog's `DURATION` hard-capped at 3h, an
action created right after `first_setup`'s 1-epoch wait only reaches its
`expiresAfter` epoch at epoch 4 - never reached within the ~3.6-epoch
budget a 3h run allows. That's a real timing-budget ceiling, not a driver
bug, so treating it as a required hit would just fail every run forever
regardless of what the drivers do.

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
