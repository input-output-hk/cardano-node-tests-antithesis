"""Exercise actual Compose merges and mode guards without a Docker daemon."""

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


@unittest.skipUnless(shutil.which("docker"), "requires Docker Compose CLI")
class BlsTopology(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = pathlib.Path(self.temporary.name)
        for name in [
            "docker-compose.yaml",
            "docker-compose.leios-bls.yaml",
            "docker-compose.leios-storage.yaml",
            ".env",
        ]:
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

    def enable(self, name="bls"):
        return subprocess.run(
            ["bash", str(ROOT / f"scripts/enable-leios-{name}.sh"), str(self.directory)],
            capture_output=True,
            text=True,
        )

    def leios(self, latest=False):
        workflow = (ROOT / ".github/workflows/cardano-node-governance.yaml").read_text()
        image = (
            "ghcr.io/input-output-hk/ouroboros-leios/cardano-node-leios:latest"
            if latest
            else re.search(
                r"ghcr.io/saratomaz/[^'\s]+/cardano-node-leios@sha256:[0-9a-f]+", workflow
            )[0]
        )
        text = re.sub(r"\$\{NODE_IMAGE:-[^}]*\}", "${NODE_IMAGE:-" + image + "}", self.original)
        self.compose.write_text(
            text.replace("${PROTOCOL_VERSION:-10}", "${PROTOCOL_VERSION:-12}").replace(
                "${GOV_COMMAND_ERA:-conway}", "${GOV_COMMAND_ERA:-dijkstra}"
            )
        )

    def test_conway_and_latest_rejected_without_mutation(self):
        for latest in [False, True]:
            if latest:
                self.leios(latest=True)
            before = self.compose.read_text()
            self.assertNotEqual(self.enable().returncode, 0)
            self.assertEqual(self.compose.read_text(), before)

    def test_only_checker_environment_changes(self):
        self.leios()
        before = self.config()
        result = self.enable()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.config()
        self.assertEqual(
            after["services"]["gov-cli"]["environment"].pop("LEIOS_BLS_CHECKS"), "true"
        )
        self.assertEqual(before, after)

    def test_storage_and_bls_options_compose_in_either_order(self):
        for order in [("storage", "bls"), ("bls", "storage")]:
            self.leios()
            original = copy.deepcopy(self.config()["services"]["p1"])
            for name in order:
                result = self.enable(name)
                self.assertEqual(result.returncode, 0, result.stderr)
            services = self.config()["services"]
            self.assertEqual(services["p1"], original)
            self.assertIn("storage-node", services)
            for flag in ["LEIOS_STORAGE_CHECKS", "LEIOS_BLS_CHECKS"]:
                self.assertEqual(services["gov-cli"]["environment"][flag], "true")


if __name__ == "__main__":
    unittest.main()
