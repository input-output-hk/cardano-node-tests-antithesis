#!/usr/bin/env python3
"""Format governance drivers changed by a Codex apply_patch call."""

import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
DRIVERS = ROOT / "components/gov-cli/composer/governance-python"


def edited_drivers(event):
    tool_input = event.get("tool_input", {})
    # Codex hook events use command; direct apply_patch arguments can use input.
    patch = tool_input.get("command") or tool_input.get("input", "")
    cwd = Path(event.get("cwd") or ROOT)
    files = set()
    for line in patch.splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Move to: "):
            if line.startswith(prefix):
                path = (cwd / line.removeprefix(prefix)).resolve()
                if path.is_relative_to(DRIVERS) and path.suffix == ".py" and path.is_file():
                    files.add(path)
                break
    return sorted(files)


def main():
    files = edited_drivers(json.load(sys.stdin))
    if not files:
        return
    if shutil.which("ruff"):
        command = ["ruff", "format"]
    elif shutil.which("nix"):
        command = ["nix", "shell", "nixpkgs#ruff", "-c", "ruff", "format"]
    else:
        print(json.dumps({"systemMessage": "Driver formatting skipped: install ruff or Nix."}))
        return
    try:
        result = subprocess.run(
            [*command, *map(str, files)], cwd=ROOT, capture_output=True, text=True, timeout=50
        )
        if result.returncode:
            print(json.dumps({"systemMessage": "Driver formatting failed: " + result.stderr.strip()}))
    except (OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"systemMessage": f"Driver formatting failed: {error}"}))


if __name__ == "__main__":
    main()
