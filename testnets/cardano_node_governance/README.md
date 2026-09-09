# cardano_node_governance

Conway **governance** workload under Antithesis fault injection.

## Topology

| Service | Role | Faults | Notes |
|---|---|---|---|
| `p1`, `p2`, `p3` | block producers | **injected** | cardano-node 11.0.1 |
| `relay1` | relay | excluded | stable query/submit endpoint |
| `gov-cli` | cardano-cli driver host | excluded | sleeps; Antithesis execs the governance drivers |
| `gov-configurator` | one-shot init | n/a | cardonnay genesis + governance assets |
| `tracer`, `tracer-sidecar`, `log-tailer`, `sidecar` | support | excluded | reused from master |

Three block producers run under fault injection, so a network
partition leaves a 2-vs-1 majority to anchor the canonical chain; the
single relay is kept out of faults so the governance drivers always
have a reachable node.

## Genesis + assets (cardonnay)

`gov-configurator` runs cardonnay's `conway_fast` generator in a
generation-only mode (no nodes started) to produce:

- Byron/Shelley/Alonzo/**Conway genesis with the constitutional
  committee seeded** (`committee.members`, `threshold 0.6`,
  `committeeMinSize 2`);
- the full `governance_data/` asset set — DRep keys + registration
  certs, vote-stake keys + reg + **vote-delegation** certs, CC cold/hot
  keys + **hot-key authorization** certs;
- the faucet (`genesis-utxo`) and pool cold keys.

These are distributed to the per-node config volumes and to the gov-cli
volumes (`gov-data`, `gov-state`).

## Governance operations (gov-cli drivers)

Each logical cardano-cli operation is a separate composer driver
(`components/gov-cli/composer/governance-python/`):

1. `first_setup` — submits the CC hot-key authorizations, DRep
   registrations and vote-stake delegations, waits one epoch, and also
   delegates two freshly generated vote-stake addresses to the
   always-abstain / always-no-confidence targets (auto-counted by the
   ledger, no vote tx needed). Also registers a small dedicated pool of
   fresh stake addresses used only as treasury-withdrawal
   funds-receiving targets (never anyone's deposit-return target, so
   their reward balance is unambiguously attributable — see below).
2. `parallel_driver_create_action` — submits an InfoAction.
3. `parallel_driver_vote` — casts DRep + SPO + CC votes on a pending
   action.
4. `parallel_driver_create_treasury_withdrawal` — submits a
   TreasuryWithdrawals action (small, bounded transfer amount).
5. `parallel_driver_vote_treasury_withdrawal` — casts DRep + CC votes
   (SPOs can't vote on this action type) on a pending withdrawal.
6. `anytime_treasury_withdrawal_enactment` — confirms a resolved
   withdrawal's funds-receiving reward account actually settled
   correctly (and never paid out twice - see PROPERTIES.md).
7. `parallel_driver_create_pparam_update` — submits a ParameterChange
   action, chained off the ledger's own previous-action reference, only
   ever targeting a small allowlist of parameters confirmed safe to
   toggle repeatedly.
8. `parallel_driver_vote_pparam_update` — casts DRep + CC votes (SPO
   eligibility on ParameterChange depends on whether the targeted
   parameters fall in Conway's security-relevant group; the allowlist
   here deliberately doesn't, and a real ledger rejection confirmed SPO
   votes are unconditionally disallowed for it) on a pending pparam
   update.
9. `anytime_pparam_update_enactment` — confirms whether a resolved
   pparam update actually changed the targeted protocol parameter.
10. Remaining `anytime_` / `eventually_` / `finally_` validators.

InfoActions never enact, so the create/vote workload is unbounded and
chain state never drifts — ideal under continuous fault injection.
Treasury withdrawals are the opposite on purpose: they DO ratify and
enact once approved, exercising the enactment/treasury-debit path the
InfoAction workload skips, kept safe by a small (1-5 ADA) transfer
amount per action. ParameterChange actions similarly enact, exercising
the protocol-parameter-update path instead - a genuinely different
enactment mechanism (the ledger's live parameter set, not a one-off
balance transfer), kept safe by a small allowlist of parameters
confirmed not to affect fee/size math or this testnet's own hardcoded
genesis assumptions.

The drivers use the standalone `cardano-clusterlib` library.

See [`PROPERTIES.md`](PROPERTIES.md) for every assertion these drivers
emit and what it means - the reference for "what's actually tested" and
which failures represent real invariant violations versus coverage gaps.

## Validation status

Syntax-checked only in this environment (no cardano-cli / cardonnay /
node runtime available). Before a paid Antithesis run, validate in
order:

1. `docker compose build` (builds gov-configurator + gov-cli).
2. `docker compose up gov-configurator` — confirm genesis +
   `governance_data` land in the volumes.
3. `docker compose up p1 p2 p3 relay1` — **confirm pool-only liveness**
   (the chain produces blocks with no BFT node; `cardano-cli query tip`
   advances). This is the #1 assumption to verify. Fallback if it
   stalls: add a genesis-delegate (BFT) producer.
4. `docker compose exec gov-cli python3 /opt/antithesis/test/v1/governance/first_setup.py`
   then the create/vote drivers; check `cardano-cli conway query
   gov-state` shows proposals with DRep/SPO/CC votes.
5. Wire the published image digests and run a 1h Antithesis validation.
