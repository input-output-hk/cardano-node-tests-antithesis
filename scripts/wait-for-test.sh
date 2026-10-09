#!/usr/bin/env bash

# Usage:
# wait-for-test.sh 175672783fab56ace66e5463d8b159a9411264847095b4d50b8dae2bb620cb3b
# exits with 0 when finished or 1 if rejected
set -euo pipefail

ID="$1"

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

# How many consecutive unreadable responses to tolerate before giving
# up. A single bad read must not discard hours of completed test time
# (see query_field), but a permanently broken moog should still end the
# job rather than spin to the caller's timeout.
MAX_QUERY_FAILURES="${MAX_QUERY_FAILURES:-30}"
QUERY_FAILS=0
FIELD=""

# Read one field of the test-run fact, or fail.
#
# Two things make this fragile enough to need its own guard:
#
#   - moog returns the facts as a bare object instead of a one-element
#     array, so `.[0]` raises "Cannot index object with number" and jq
#     exits 5. Under `set -e` that killed the whole wait: run
#     37919452208 lost 2h52m of a completed Leios test that way, while
#     Antithesis carried on running it. The workflow's own parse of the
#     same data already normalises this (see the "Submit test" step);
#     this one never did.
#   - moog's output has occasionally contained raw, unescaped control
#     characters inside a JSON string value, which jq rejects outright.
#
# Returns non-zero on anything unreadable; the callers treat that as
# "unknown, keep waiting" rather than as a verdict.
query_field() {
    local field="$1" raw out
    raw=$(moog facts test-runs --test-run-id "$ID" 2>/dev/null) || return 1
    out=$(printf '%s' "$raw" | tr -d '\000-\037' \
        | jq -r "(if type == \"array\" then . else [.] end) | .[0] | ${field}" 2>/dev/null) || return 1
    [ -n "$out" ] && [ "$out" != "null" ] || return 1
    printf '%s' "$out"
}

# Sets FIELD, and keeps QUERY_FAILS in the parent shell - so it cannot
# live inside the command substitution that reads the value.
poll() {
    if FIELD=$(query_field "$1"); then
        QUERY_FAILS=0
        return 0
    fi
    FIELD=""
    QUERY_FAILS=$((QUERY_FAILS + 1))
    if [ "$QUERY_FAILS" -ge "$MAX_QUERY_FAILURES" ]; then
        echo "moog facts unreadable ${QUERY_FAILS} times in a row - giving up." >&2
        echo "The test may still be running; check the Antithesis dashboard." >&2
        exit 1
    fi
    echo "moog facts unreadable (${QUERY_FAILS}/${MAX_QUERY_FAILURES}); retrying" >&2
    return 1
}

echo "waiting to be accepted (giving up after ${PENDING_TIMEOUT}s)..."
while true; do
    if poll .value.phase; then
        case $FIELD in
        accepted)
            echo "accepted"
            break
            ;;
        rejected)
            echo "rejected"
            exit 1
            ;;
        finished)
            echo "already finished"
            break
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
            echo "unknown status: $FIELD"
            ;;
        esac
    fi
    sleep 10
    WAITED=$((WAITED + 10))
    echo "... (${WAITED}s)"
done

echo "waiting to be finished..."
while true; do
    if poll .value.phase; then
        case $FIELD in
        finished)
            echo "finished"
            break
            ;;
        accepted) ;;
        *)
            echo "unknown status: $FIELD"
            ;;
        esac
    fi
    sleep 60
    echo "..."
done

if ! poll .value.outcome; then
    echo "finished, but the outcome could not be read" >&2
    exit 1
fi

case $FIELD in
success)
    exit 0
    ;;
failure)
    echo "failed"
    exit 1
    ;;
*) # includes "unknown"
    echo "unknown outcome: $FIELD"
    exit 1
    ;;
esac
