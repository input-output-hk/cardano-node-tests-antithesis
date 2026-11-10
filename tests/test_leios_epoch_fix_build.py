"""Exercise publication and pin guards with offline build/registry stand-ins."""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = pathlib.Path("scripts/build-leios-epoch-fix.sh")
WORKFLOW = pathlib.Path(".github/workflows/cardano-node-governance.yaml")
BASE = (
    "ghcr.io/saratomaz/cardano-node-tests-antithesis/cardano-node-leios@sha256:"
    "3a88c4544247839b23c258412bad0ea4ddfe21c30a43c93968a93e3e7cc15b17"
)
PUBLISHED = BASE.split("@", 1)[0] + "@sha256:" + "1" * 64


class EpochFixBuild(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        for relative in [SCRIPT, WORKFLOW]:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / relative, destination)
        shutil.copytree(ROOT / "components/leios-node", self.root / "components/leios-node")
        # Exercise the one-time upgrade from its expected baseline, even after
        # the real workflow has been updated to the published backport digest.
        self.original = re.sub(
            re.escape(BASE.split("@", 1)[0]) + r"@sha256:[0-9a-f]{64}",
            BASE,
            (self.root / WORKFLOW).read_text(),
        )
        (self.root / WORKFLOW).write_text(self.original)
        self.node = self.root / "fake-node"
        (self.node / "bin").mkdir(parents=True)
        (self.node / "bin/cardano-node").write_text("test node bytes")
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        stub = """#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'nix':
    pathlib.Path(args[args.index('--out-link') + 1]).symlink_to(os.environ['FAKE_NODE'])
elif name == 'nix-store':
    print(os.environ['FAKE_NODE'])
elif args[:2] == ['image', 'inspect']:
    print(json.dumps([os.environ['PUBLISHED']]))
"""
        for name in ["nix", "nix-store", "docker"]:
            path = self.bin / name
            path.write_text(stub)
            path.chmod(0o755)
        self.log = self.root / "calls.jsonl"

    def run_build(self, *args: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        result = subprocess.run(
            ["bash", str(self.root / SCRIPT), *args],
            env={
                **os.environ,
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "FAKE_NODE": str(self.node),
                "CALL_LOG": str(self.log),
                "PUBLISHED": PUBLISHED,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        calls = (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )
        return result, calls

    def test_default_build_does_not_publish_or_change_workflow(self) -> None:
        result, calls = self.run_build()
        assert result.returncode == 0, result.stderr
        assert (self.root / WORKFLOW).read_text() == self.original
        assert not any(call[:2] == ["docker", "push"] for call in calls)
        assert any(call[:2] == ["nix", "build"] for call in calls)

    def test_publish_uses_registry_digest_and_changes_only_the_leios_pin(self) -> None:
        result, calls = self.run_build("--push")
        assert result.returncode == 0, result.stderr
        assert any(call[:2] == ["docker", "push"] for call in calls)
        assert (self.root / WORKFLOW).read_text() == self.original.replace(BASE, PUBLISHED)

    def test_unexpected_workflow_pin_is_never_overwritten(self) -> None:
        text = self.original.replace(BASE, BASE.replace("3a88c454", "ffffffff"))
        (self.root / WORKFLOW).write_text(text)
        result, _calls = self.run_build("--push")
        assert result.returncode != 0
        assert (self.root / WORKFLOW).read_text() == text

    def test_modified_patch_stops_before_build_or_publication(self) -> None:
        patch = self.root / "components/leios-node/epoch-config/complete-ledger-config.patch"
        patch.write_text(patch.read_text() + "\n")
        result, calls = self.run_build("--push")
        assert result.returncode != 0
        assert calls == []
        assert (self.root / WORKFLOW).read_text() == self.original

    def test_read_only_store_copy_is_removed_without_changing_source(self) -> None:
        self.node.chmod(0o555)
        (self.node / "bin").chmod(0o555)
        self.addCleanup(lambda: self.node.chmod(0o755))
        self.addCleanup(lambda: (self.node / "bin").chmod(0o755))
        result, _calls = self.run_build()
        assert result.returncode == 0, result.stderr
        assert self.node.stat().st_mode & 511 == 365
        assert (self.node / "bin").stat().st_mode & 511 == 365


if __name__ == "__main__":
    unittest.main()
