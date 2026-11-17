#!/usr/bin/env python3
"""Read public wallet information locally, then query its preprod ADA balance."""

import base64
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ENDPOINT = "https://preprod.koios.rest/api/v1/address_info"


class CheckError(Exception):
    pass


def public_address():
    # Never echo raw CLI output: even a malformed wallet must not expose its
    # contents through an error, traceback, artifact, or Actions log.
    try:
        encoded = "".join(os.environ["MOOG_REQUESTER_WALLET"].split())
        wallet = base64.b64decode(encoded, validate=True)
        with tempfile.TemporaryDirectory(prefix="moog-wallet-") as directory:
            path = pathlib.Path(directory) / "wallet.json"
            path.touch(mode=0o600)
            path.write_bytes(wallet)
            env = dict(os.environ)
            env.pop("MOOG_REQUESTER_WALLET", None)
            result = subprocess.run(
                ["moog", "wallet", "info", "--wallet", str(path)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        if result.returncode:
            raise ValueError
        address = json.loads(result.stdout)["address"]
        if not isinstance(address, str) or not re.fullmatch(r"addr_test1[a-z0-9]{20,120}", address):
            raise ValueError
        return address
    except Exception:
        raise CheckError(
            "Unable to read the wallet. Check the configured wallet and passphrase secrets."
        ) from None


def parse_balance(data, address):
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise CheckError("Balance unavailable: the public API returned no unique address record.")
    row = data[0]
    value = row.get("balance")
    if row.get("address") != address or isinstance(value, bool):
        raise CheckError("Balance unavailable: the public API returned an unexpected record.")
    if not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise CheckError("Balance unavailable: the public API returned an invalid balance.")
    return int(value)


def balance(address):
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"_addresses": [address]}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
            return parse_balance(data, address)
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError):
            reason = "network request failed"
        except (ValueError, UnicodeError):
            raise CheckError("Balance unavailable: the public API returned invalid JSON.") from None
        if attempt == 0:
            time.sleep(2)
    raise CheckError(f"Balance unavailable: {reason}. No balance was inferred.")


def report(line):
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with pathlib.Path(summary).open("a") as stream:
            stream.write(line + "\n\n")


def main():
    try:
        address = public_address()
        report(f"Moog requester address (preprod): `{address}`")
        lovelace = balance(address)
        whole, fraction = divmod(lovelace, 1_000_000)
        report(f"Balance: **{whole:,}.{fraction:06d} tADA** ({lovelace} lovelace).")
        if not lovelace:
            print("::warning::The preprod requester wallet has zero ADA.")
        return 0
    except CheckError as exc:
        report(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
