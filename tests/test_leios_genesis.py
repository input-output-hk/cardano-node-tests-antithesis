"""Genesis voting keys must match producer credentials before snapshots exist."""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import re
import tempfile
import unittest
from unittest import mock

import cbor2

PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "components/gov-configurator/seed_leios_genesis.py"
)
SPEC = importlib.util.spec_from_file_location("seed_leios_genesis", PATH)
genesis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(genesis)


class LeiosGenesis(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pool = self.root / "nodes/node-pool1"
        self.pool.mkdir(parents=True)
        self.pool_id = "11" * 28
        self.key, self.proof = bytes.fromhex("22" * 96), bytes.fromhex("33" * 48)
        self.cert = [
            3,
            bytes.fromhex(self.pool_id),
            b"v" * 32,
            [self.key, self.proof],
            123,
            456,
            [0, 1],
            b"account",
            [],
            [],
            None,
        ]
        self.vkey = self.pool / "bls.vkey"
        self.vkey.write_text(json.dumps({"cborHex": cbor2.dumps(self.key).hex()}))
        self.path = self.root / "genesis.json"
        self.original = {
            "protocolParams": {"protocolVersion": {"major": 12}},
            "staking": {
                "pools": {
                    self.pool_id: {
                        "pledge": 123,
                        "cost": 456,
                        "margin": 0,
                        "relays": [],
                        "metadata": None,
                    }
                },
                "stake": {"credential": self.pool_id},
            },
            "initialFunds": {"address": 1000000},
        }
        self.path.write_text(json.dumps(self.original))

    def seed(self):
        def certificate(command, **_kwargs):
            pathlib.Path(command[-1]).write_text(
                json.dumps({"cborHex": cbor2.dumps(self.cert).hex()})
            )

        with (
            mock.patch.object(genesis.subprocess, "check_output", return_value=self.pool_id + "\n"),
            mock.patch.object(genesis.subprocess, "run", side_effect=certificate),
        ):
            genesis.seed(self.path, self.root / "nodes", 1, 42)

    def test_only_voting_keys_change_and_both_ledger_schemas_agree(self):
        self.seed()
        result = json.loads(self.path.read_text())
        pool = result["staking"]["pools"][self.pool_id]
        self.assertEqual(
            pool.pop("leiosKey"),
            {
                "leiosPubKey": self.key.hex(),
                "leiosPossessionProof": self.proof.hex(),
            },
        )
        self.assertEqual(
            pool.pop("blsKey"),
            {
                "blsPubKey": self.key.hex(),
                "blsPossessionProof": self.proof.hex(),
            },
        )
        self.assertEqual(result, self.original)

    def test_wrong_producer_key_does_not_publish_genesis(self):
        self.cert[3][0] = b"x" * 96
        with self.assertRaisesRegex(ValueError, "producer voting key"):
            self.seed()
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_wrong_certificate_pool_does_not_publish_genesis(self):
        self.cert[1] = b"x" * 28
        with self.assertRaisesRegex(ValueError, "unexpected pool"):
            self.seed()
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_malformed_proof_does_not_publish_genesis(self):
        self.cert[3][1] = b"x" * 47
        with self.assertRaisesRegex(ValueError, "producer voting key"):
            self.seed()
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_conway_is_byte_identical_and_never_invokes_leios_cli(self):
        data = copy.deepcopy(self.original)
        data["protocolParams"]["protocolVersion"]["major"] = 10
        self.path.write_text(json.dumps(data, indent=2))
        before = self.path.read_bytes()
        with mock.patch.object(genesis, "subprocess") as commands:
            genesis.seed(self.path, self.root / "nodes", 1, 42)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(commands.mock_calls, [])

    def test_missing_genesis_pool_is_not_silently_skipped(self):
        with self.assertRaisesRegex(ValueError, "pool count"):
            genesis.seed(self.path, self.root / "nodes", 3, 42)

    def test_configurator_and_driver_build_the_same_pinned_cli(self):
        components = PATH.parents[1]
        revisions = []
        for component in ["gov-cli", "gov-configurator"]:
            dockerfile = (components / component / "Dockerfile").read_text()
            revisions.append(re.search(r"^ARG LEIOS_NODE_REV=([a-f0-9]{40})$", dockerfile, re.M)[1])
        self.assertEqual(revisions[0], revisions[1])


if __name__ == "__main__":
    unittest.main()
