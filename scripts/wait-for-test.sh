#!/usr/bin/env bash

# Usage:
# wait-for-test.sh 175672783fab56ace66e5463d8b159a9411264847095b4d50b8dae2bb620cb3b
# exits with 0 when finished or 1 if rejected
set -euo pipefail

ID="$1"

# moog's output has occasionally contained raw, unescaped control
# characters inside a JSON string value (e.g. a literal newline instead
# of \n in a log/report field), which jq rejects outright. Strip C0
# control characters before parsing.
function query_run() { moog facts test-runs --test-run-id "$ID" | tr -d '\000-\037' | jq '.[0]'; }

# A run that is never picked up sits in "pending" indefinitely, and the
# commonest cause by far is the requester wallet being out of tAda -
# create-test succeeds, so nothing upstream complains. Unbounded, this
# loop then burns the caller's whole (DURATION + 2)h budget before the
# job is killed, with the reason buried in "..." output. Bound it and
# name the likely cause instead.
# 30min: deliberately far longer than acceptance is believed to take,
# since failing a legitimately queued run wastes a dispatch, while still
# saving ~4.5h of the old unbounded behaviour. Lower it once there is
# data on real acceptance latency.
PENDING_TIMEOUT="${PENDING_TIMEOUT:-1800}"
WAITED=0

echo "waiting to be accepted (giving up after ${PENDING_TIMEOUT}s)..."
while true; do
  STATUS=$(query_run | jq -r .value.phase)
  case $STATUS in
    accepted)
      echo "accepted"
      break;
      ;;
    rejected)
      echo "rejected"
      exit 1
      ;;
    finished)
      echo "already finished"
      break;
      ;;
    pending)
      if [ "$WAITED" -ge "$PENDING_TIMEOUT" ]; then
        echo "still pending after ${WAITED}s - giving up." >&2
        echo "The usual cause is the requester wallet being out of tAda:" >&2
        echo "create-test is accepted, but no agent ever picks the run up." >&2
        echo "Check the balance of the address from \`moog wallet info\` and" >&2
        echo "top it up from the Cardano preprod faucet, then re-dispatch." >&2
        exit 1
      fi
      ;;
    *)
      echo "unknown status: $STATUS"
      ;;
  esac
  sleep 10
  WAITED=$((WAITED + 10))
  echo "... (${WAITED}s)"
done

echo "waiting to be finished..."
while true; do
  STATUS=$(query_run | jq -r .value.phase)
  case $STATUS in
    finished)
      echo "finished"
      break;
      ;;
    accepted)
      ;;
    *)
      echo "unknown status: $STATUS"
      ;;
  esac
  sleep 60
  echo "..."
done

case $(query_run | jq -r .value.outcome) in
  success)
    exit 0;
    ;;
  failure)
    echo "failed"
    exit 1
    ;;
  *) # includes "unknown"
    echo "unknown outcome"
    exit 1
esac
