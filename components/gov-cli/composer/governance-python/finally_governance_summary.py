#!/usr/bin/env python3
"""finally_governance_summary.py — end-of-run coverage marker.

Emits a single Sometimes/Reachable pair proving the gov-cli lifecycle
reached end-of-test. No node calls, no sleep: always exits 0 even if a
fault delivers SIGTERM mid-run.
"""

from __future__ import annotations

import sys

import helper_sdk as sdk


def main() -> int:
    sdk.sometimes(True, "governance_run_completed")
    sdk.reachable("finally_governance_summary entered")
    return 0


if __name__ == "__main__":
    try:
        main()
        sdk.always(True, "finally_governance_summary_exits_zero")
    except Exception as exc:  # noqa: BLE001
        print(f"finally_governance_summary aborted: {exc}", file=sys.stderr)
        sdk.unreachable("finally_governance_summary_aborted")
    finally:
        sys.exit(0)
