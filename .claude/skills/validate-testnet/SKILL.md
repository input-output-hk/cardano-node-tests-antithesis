---
name: validate-testnet
description: "Validate the cardano_node_governance testnet locally before a paid Antithesis run, or after changing anything under components/. Use when asked to validate, smoke-test, or check the testnet locally; when verifying a change to generate.sh, a driver, or genesis parameters; or before dispatching an Antithesis run. Covers genesis generation, pool-only liveness, and running the drivers by hand."
---

# Validate the governance testnet locally

Work from `testnets/cardano_node_governance/`.

## Read this first — three traps

**A green image rebuild does not validate `generate.sh`.** The gov-configurator Dockerfile only
`COPY`s it and sets it as `ENTRYPOINT`, so it runs at container *start*. The CI rebuild proves the
script was packaged, nothing more. Only step 1 below actually runs it.

**`docker compose build` does nothing here** — no service has a `build:` section. `docker compose
up` therefore runs the pinned image, **not your working tree**. To test an uncommitted change to
`components/`, see "Testing a local change" below.

**A second `up` is a silent no-op.** `generate.sh` guards on a `/gov-data/.generated` marker, so it
skips regeneration and exits 0 without doing anything. Always `docker compose down -v` first when
you want a fresh genesis, or you will validate stale state and believe it passed.

## 1. Genesis generation

```bash
cd testnets/cardano_node_governance
docker compose down -v          # clear the .generated marker
docker compose up gov-configurator
```

Expect exit 0 and a line like `genesis spec: securityParam=150 epochLength=15000`.

**Don't trust that log line — read the result back.** It reports what the script intended, not what
landed in the genesis:

```bash
docker run --rm -v cardano_node_governance_gov-state:/gs alpine:3 \
  sh -c 'apk add -q jq; jq "{securityParam, epochLength, activeSlotsCoeff, slotLength}" /gs/shelley/genesis.json'
```

`epochLength` must be `100 * securityParam`, and `activeSlotsCoeff` must be `0.1` — `generate.sh`
derives epoch length from k assuming `f=0.1` and aborts if the spec disagrees.

## 2. Pool-only liveness — the assumption most worth checking

```bash
docker compose up p1 p2 p3 relay1
```

Confirm the chain produces blocks with no BFT node: `cardano-cli query tip` must advance. This is
the single biggest assumption in the setup. If it stalls, the fallback is adding a genesis-delegate
(BFT) producer.

## 3. Drivers by hand

```bash
docker compose exec gov-cli python3 /opt/antithesis/test/v1/governance/first_setup.py
# then a create/vote driver, e.g.
docker compose exec gov-cli python3 /opt/antithesis/test/v1/governance/parallel_driver_create_action.py
```

Then check proposals carry DRep/SPO/CC votes:

```bash
docker compose exec gov-cli cardano-cli conway query gov-state --testnet-magic 42 | jq '.proposals'
```

`first_setup.py` waits a full epoch (~50 min at k=150), so budget for that.

## 4. Tear down

```bash
docker compose down -v
```

Leaving volumes behind means the next run silently skips generation (see the third trap).

## Testing a local change before pushing

Because compose pins digests, you must build and override:

```bash
docker build --platform linux/amd64 -t gov-configurator:local ../../components/gov-configurator
cat > docker-compose.override.yaml <<'YAML'
services:
  gov-configurator:
    image: gov-configurator:local
YAML
docker compose config | grep -A1 'gov-configurator:' | grep image   # confirm the swap
```

Compose merges `docker-compose.override.yaml` automatically. **Delete it when done** — it is not
gitignored, and committing it would override the pinned digest for everyone, including Antithesis.

The alternative is to push and let CI rebuild and re-pin, then pull and run the new digest. That
works but costs a build per iteration, so prefer the override while iterating.

## If docker is permission-denied

On a machine where the docker group was added to an already-running shell, wrap calls:

```bash
newgrp docker <<'EOF'
docker compose up gov-configurator
EOF
```

Interactive `newgrp docker` alone hangs waiting for a TTY; the heredoc form works non-interactively.
