"""Seed voting keys before consensus builds its initial stake snapshots."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import cbor2

CLI = "cardano-cli-leios"


def certificate_key(
    certificate: pathlib.Path, verification_key: pathlib.Path, pool_id: str
) -> dict:
    cert = cbor2.loads(bytes.fromhex(json.loads(certificate.read_text())["cborHex"]))
    expected = cbor2.loads(bytes.fromhex(json.loads(verification_key.read_text())["cborHex"]))
    # Pinned Dijkstra pool-registration certificate: kind, cold hash, VRF
    # hash, [BLS public key, proof], then the ordinary pool parameters.
    if (
        not isinstance(cert, list)
        or len(cert) != 11
        or cert[0] != 3
        or cert[1] != bytes.fromhex(pool_id)
    ):
        msg = "unexpected pool registration certificate"
        raise ValueError(msg)
    pair = cert[3]
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or pair[0] != expected
        or not isinstance(expected, bytes)
        or len(expected) != 96
        or not isinstance(pair[1], bytes)
        or len(pair[1]) != 48
    ):
        msg = "registration proof does not match the producer voting key"
        raise ValueError(msg)
    return {"leiosPubKey": expected.hex(), "leiosPossessionProof": pair[1].hex()}


def seed(genesis_path: pathlib.Path, nodes: pathlib.Path, pool_count: int, magic: int) -> None:
    genesis = json.loads(genesis_path.read_text())
    if int(genesis["protocolParams"]["protocolVersion"]["major"]) < 12:
        return
    pools = genesis["staking"]["pools"]
    if len(pools) != pool_count:
        msg = "genesis pool count differs from the producer count"
        raise ValueError(msg)
    for index in range(1, pool_count + 1):
        pool = nodes / f"node-pool{index}"
        pool_id = subprocess.check_output(
            [
                CLI,
                "dijkstra",
                "stake-pool",
                "id",
                "--cold-verification-key-file",
                str(pool / "cold.vkey"),
                "--output-format",
                "hex",
            ],
            text=True,
        ).strip()
        params = pools[pool_id]
        if params["relays"] or params["metadata"]:
            msg = "genesis voting-key setup expects pools without relays or metadata"
            raise ValueError(msg)
        certificate = pool / "leios-genesis.cert"
        subprocess.run(
            [
                CLI,
                "dijkstra",
                "stake-pool",
                "registration-certificate",
                "--cold-verification-key-file",
                str(pool / "cold.vkey"),
                "--vrf-verification-key-file",
                str(pool / "vrf.vkey"),
                "--bls-signing-key-file",
                str(pool / "bls.skey"),
                "--pool-pledge",
                str(params["pledge"]),
                "--pool-cost",
                str(params["cost"]),
                "--pool-margin",
                str(params["margin"]),
                "--pool-reward-account-verification-key-file",
                str(pool / "reward.vkey"),
                "--pool-owner-stake-verification-key-file",
                str(pool / "owner-stake.vkey"),
                "--testnet-magic",
                str(magic),
                "--out-file",
                str(certificate),
            ],
            check=True,
        )
        params["leiosKey"] = certificate_key(certificate, pool / "bls.vkey", pool_id)
        # Pinned ledger uses leiosKey; newer Leios ledgers call it blsKey.
        # Each decoder ignores the other spelling. Both carry the same
        # CLI-generated key and proof, not two independently generated keys.
        params["blsKey"] = {
            "blsPubKey": params["leiosKey"]["leiosPubKey"],
            "blsPossessionProof": params["leiosKey"]["leiosPossessionProof"],
        }
    temporary = genesis_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(genesis))
    temporary.replace(genesis_path)


if __name__ == "__main__":
    seed(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
