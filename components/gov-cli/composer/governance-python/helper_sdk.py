#!/usr/bin/env python3
"""helper_sdk.py — Antithesis Fallback SDK emitter (Python).

`helper_`-prefixed: ignored by the Antithesis composer scheduler.
Sibling driver scripts import it. Emits the same sdk.jsonl format as
the bash helper_sdk.sh so reports are identical regardless of which
driver language a run uses.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


def _emit(obj: dict) -> None:
    try:
        out_dir = pathlib.Path(os.environ.get("ANTITHESIS_OUTPUT_DIR", "/tmp"))
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "sdk.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj) + "\n")
    except Exception:
        # An SDK emit must never crash a driver.
        pass


def _assert(
    assert_id: str,
    display_type: str,
    assert_type: str,
    condition: bool,
    details: dict | None = None,
) -> None:
    _emit(
        {
            "antithesis_assert": {
                "id": assert_id,
                "message": assert_id,
                "condition": bool(condition),
                "display_type": display_type,
                "hit": True,
                "must_hit": True,
                "assert_type": assert_type,
                "location": {
                    "file": "",
                    "function": "",
                    "class": "",
                    "begin_line": 0,
                    "begin_column": 0,
                },
                "details": details,
            }
        }
    )


def reachable(assert_id: str) -> None:
    _assert(assert_id, "Reachable", "reachability", True)


def unreachable(assert_id: str) -> None:
    _assert(assert_id, "AlwaysOrUnreachable", "always", False)


def sometimes(condition: bool, assert_id: str, details: dict | None = None) -> None:
    _assert(assert_id, "Sometimes", "sometimes", condition, details)


def always(condition: bool, assert_id: str, details: dict | None = None) -> None:
    _assert(assert_id, "Always", "always", condition, details)


def setup_complete(details: dict | None = None) -> None:
    """Emit the Antithesis lifecycle "setup complete" signal: tells the
    hypervisor the system is healthy and it may START injecting faults."""
    _emit({"antithesis_setup": {"status": "complete", "details": details}})


def run_driver(main_fn, aborted_id: str, exits_zero_id: str | None = None) -> None:
    """Shared `__main__` entrypoint for a driver script: run main_fn(),
    exit with its return code, and emit the standard exits_zero/aborted
    coverage signals under the caller's own assert IDs. Any exception is
    absorbed into the aborted signal (exit 0) instead of crashing the
    Antithesis scheduler.

    exits_zero_id is optional since not every driver asserts it.
    """
    try:
        rc = main_fn()
        if exits_zero_id is not None:
            always(rc == 0, exits_zero_id)
        sys.exit(rc)
    except Exception as exc:  # noqa: BLE001
        label = aborted_id.removesuffix("_aborted")
        print(f"{label} aborted: {exc}", file=sys.stderr)
        unreachable(aborted_id)
        sys.exit(0)
