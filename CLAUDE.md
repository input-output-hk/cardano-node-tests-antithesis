# cardano-node-tests-antithesis

Antithesis fault-injection testnets for cardano-node. `README.md` and
`testnets/cardano_node_governance/README.md` cover architecture and the drivers;
`testnets/cardano_node_governance/PROPERTIES.md` is the reference for what is actually tested.

## Drivers (`components/gov-cli/composer/governance-python/`)

- Filename prefixes are load-bearing. `first_`, `parallel_driver_`, `anytime_`, `eventually_`,
  `finally_` are scheduled; `helper_` is ignored. A wrong prefix drops the entire workload.
- End every driver with `sdk.run_driver(...)`. Never hand-roll try/except/exit.
- Failure is signalled by assertions, not exit codes. Only `first_setup.py` returns 1.
- No `time.time()` — it isn't virtualised. Use loop counters and slot numbers.
- Randomness goes through `helper_gov.rng_mod()`, never `random.*`.
- Parallel drivers share the `gov-data` volume; new shared state needs an `fcntl` lock or a claim
  slot (see `faucet_lock`, `claim_recv_slot`).
- Broad `except Exception:  # noqa: BLE001` is intentional — a node failing mid-fault is absorbed.
- New or renamed assertion ids go in `PROPERTIES.md`.

## Images and config

- A change under `components/gov-cli/**` or `components/gov-configurator/**` rebuilds and re-pins
  the image automatically, but only when pushed to `main`. Don't hand-edit the digests.
- `docker-compose.yaml`'s literal `${VAR:-default}` text is load-bearing — the Leios workflow
  `sed`s it. Reflowing that file breaks mode selection silently.
- Protocol constants are duplicated across `generate.sh`, both Dockerfile `ENV` blocks,
  `helper_gov.py` and `docker-compose.yaml` (which wins). `EPOCH_LENGTH == 100 * SECURITY_PARAM`.

## Working here

- Nothing runs on `pull_request`. Run the `code-reviewer` agent on a diff before pushing.
- `cluster fork depth < k` comes from the prebuilt tracer-sidecar image, not this repo.
