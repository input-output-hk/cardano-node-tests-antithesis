"""Exercise the real Compose merge and Conway guard; no daemon is required."""

from __future__ import annotations

import copy
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTNET = ROOT / "testnets/cardano_node_governance"
SCRIPT = ROOT / "scripts/enable-leios-storage.sh"


@unittest.skipUnless(shutil.which("docker"), "requires Docker Compose CLI (not daemon)")
class StorageTopology(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = pathlib.Path(self.temporary.name)
        for name in ("docker-compose.yaml", "docker-compose.leios-storage.yaml", ".env"):
            shutil.copy(TESTNET / name, self.directory / name)
        self.compose = self.directory / "docker-compose.yaml"
        self.original = self.compose.read_text()

    def config(self):
        return json.loads(
            subprocess.check_output(
                ["docker", "compose", "-f", str(self.compose), "config", "--format", "json"],
                text=True,
            )
        )

    def enable(self):
        return subprocess.run(
            ["bash", str(SCRIPT), str(self.directory)], capture_output=True, text=True
        )

    def select_leios(self, image):
        # Apply the same load-bearing substitutions as the workflow.
        text = re.sub(r"\$\{NODE_IMAGE:-[^}]*\}", "${NODE_IMAGE:-" + image + "}", self.original)
        for old, new in [
            ("${PROTOCOL_VERSION:-10}", "${PROTOCOL_VERSION:-12}"),
            ("${GOV_COMMAND_ERA:-conway}", "${GOV_COMMAND_ERA:-dijkstra}"),
            (
                "${NODE_EXTRA_ARGS:-}",
                "${NODE_EXTRA_ARGS:---shelley-bls-key /configs/keys/bls.skey}",
            ),
            ("./tracer-config.yaml:", "./tracer-config-leios.yaml:"),
        ]:
            text = text.replace(old, new)
        self.compose.write_text(text)

    def test_conway_rejected_without_mutation(self):
        self.assertNotEqual(self.enable().returncode, 0)
        self.assertEqual(self.compose.read_text(), self.original)
        self.assertNotIn("storage-node", self.config()["services"])

    def test_latest_rejected_without_mutation(self):
        self.select_leios("ghcr.io/input-output-hk/ouroboros-leios/cardano-node-leios:latest")
        before = self.compose.read_text()
        self.assertNotEqual(self.enable().returncode, 0)
        self.assertEqual(self.compose.read_text(), before)

    def test_overlay_changes_only_checker_and_adds_own_faulted_node(self):
        workflow = (ROOT / ".github/workflows/cardano-node-governance.yaml").read_text()
        image = re.search(
            r"ghcr.io/saratomaz/[^'\s]+/cardano-node-leios@sha256:[0-9a-f]+", workflow
        )[0]
        self.select_leios(image)
        before = self.config()["services"]
        result = self.enable()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.config()["services"]
        for name in before.keys() - {"gov-cli"}:
            self.assertEqual(before[name], after[name], name)
        checker = copy.deepcopy(after["gov-cli"])
        self.assertEqual(checker["environment"].pop("LEIOS_STORAGE_CHECKS"), "true")
        mount = next(v for v in checker["volumes"] if v["target"] == "/leios-storage")
        checker["volumes"].remove(mount)
        self.assertTrue(mount["read_only"])
        self.assertEqual(checker, before["gov-cli"])
        node = after["storage-node"]
        self.assertEqual(node["image"], after["p1"]["image"])
        self.assertEqual(node["environment"]["CARDANO_BLOCK_PRODUCER"], "false")
        self.assertEqual(node["labels"]["com.antithesis.exclude_from_faults"], "")
        state = next(v for v in node["volumes"] if v["target"] == "/state")
        self.assertEqual(state["source"], mount["source"])
        self.assertNotEqual(state["source"], "relay1-state")
        self.assertNotIn("shelley-bls-key", " ".join(node["command"]))
        self.assertIn("./relay-topology.json", self.compose.read_text())


if __name__ == "__main__":
    unittest.main()
