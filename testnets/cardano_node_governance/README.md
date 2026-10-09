# cardano_node_governance

Conway **governance** workload under Antithesis fault injection.

## Topology

| Service | Role | Faults | Notes |
|---|---|---|---|
| `p1`, `p2`, `p3` | block producers | **injected** | cardano-node 11.0.1 |
| `relay1` | relay | excluded | stable query/submit endpoint |
| `gov-cli` | cardano-cli driver host | excluded | sleeps; Antithesis execs the governance drivers |
| `gov-configurator` | one-shot init | n/a | cardonnay genesis + governance assets |
| `tx-firehose` | Leios load generator | excluded | own image; idles unless `protocolVersion >= 12` |
| `tracer`, `tracer-sidecar`, `log-tailer`, `sidecar` | support | excluded | reused from master |

Three block producers run under fault injection, so a network
partition leaves a 2-vs-1 majority to anchor the canonical chain; the
single relay is kept out of faults so the governance drivers always
have a reachable node.

## Leios load (`tx-firehose`)

Leios's endorser blocks only certify transactions that are already in
the mempool, and the governance drivers submit roughly one transaction
per composer tick. Without sustained load a Leios run only proves the
Dijkstra node boots and survives faults, not that Leios's throughput
mechanism does - upstream's `test_leios_blocks.py` skips itself outright
when no generator is enabled, for the same reason.

The `tx-firehose` service has **its own image**
(`components/tx-firehose/`), built from `LEIOS_NODE_REV` so the
generator, the cli it queries with and the node it submits to all come
from one commit.

It deliberately does not reuse the `gov-cli` image, even though that one
already has the same nix stage. Antithesis discovers test commands per
container by the presence of `/opt/antithesis/test/v1`, so a second
container running the `gov-cli` image became a second place the
governance drivers were scheduled - against this service's read-only
`/gov-data`, where they aborted. That failed run `37252534099`. An image
that hosts the drivers cannot be reused for a container that must not
run them.

It follows the generated genesis rather than a switch of its own:
its `/entrypoint.sh` reads `protocolVersion.major` out of
`gov-state/shelley/genesis.json` and submits only when it is `>= 12`,
idling in Conway. That is deliberate - a second env switch flipped
alongside `PROTOCOL_VERSION` could disagree with it, silently either
losing the Leios coverage or putting load on the Conway baseline.

| Knob | Default | Effect |
|---|---|---|
| `TX_FIREHOSE` | unset | `true`/`false` forces the gate, overriding the genesis. Passed through from the environment, so `TX_FIREHOSE=false docker compose up` works locally |
| `TX_TPS` | `15` | submission rate ceiling (upstream's Leios rate) |
| `TX_INPUTS_PER_TX`, `TX_OUTPUTS_PER_TX` | `1` | tx shape |
| `TX_FEE` | `200000` | flat fee; `TxFirehose.Build.Fail` in the log means it is too low |
| `TX_MAX_CONSECUTIVE_ERRORS` | `50` | rejects tolerated before it exits and is restarted |
| `TX_FIREHOSE_LOG_SUBMITS` | unset | `true` keeps the per-tx success trace lines, which are dropped by default |

The per-tx success lines are filtered out on purpose. tx-firehose emits
one JSON trace per submitted transaction, and Antithesis explores many
timelines: run `37796283946` produced 19,319,280
`TxFirehose.Submit.Success` events and came back `Incomplete` with no
findings verdict - the same output-volume problem `generate.sh`'s
`TraceOptions` map exists to prevent. Rejects, errors and startup lines
are kept. Nothing depends on the dropped lines, since
`anytime_leios_load` proves the load ran from UTxO churn on chain rather
than from logs.

Only `TX_FIREHOSE` is wired as a passthrough; the rest are read from the
environment by `/entrypoint.sh` but not listed in the service, so
changing one means editing the `tx-firehose` service's `environment:`
block. A run dispatched through moog gets the committed values either
way - moog passes no environment of its own.

Its funding key is cardonnay's second genesis UTxO key
(`genesis-utxo2`), staged by `gov-configurator` into
`/gov-data/tx-firehose/` only in Leios mode. It is pre-funded from slot
0, so no funding transaction is needed, and it keeps the generator off
the shared faucet, whose spends are serialized on a lock. Enabling
cardonnay's own `tx-generator` or `tx-centrifuge` would race it for that
same key.

Expect restarts: the generator caches its fund set at startup, so every
reorg invalidates it and it exits once `--max-consecutive-errors` is
hit. `/entrypoint.sh` restarts it with a fresh fund set and never gives
up, so that cycle is normal here.

In Conway the container still starts and idles - compose has no
conditional services, and `profiles:` is not usable because moog selects
none, so a profiled service would never start in either mode. It submits
nothing there, but it is not invisible to Conway runs: it is a container
in the topology, which is precisely how the driver-scheduling problem
above reached Conway.

## Genesis + assets (cardonnay)

`gov-configurator` runs cardonnay's `local_fast` generator in a
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
