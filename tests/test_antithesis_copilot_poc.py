"""POC provenance, context limits, and credential/tool isolation without AI calls."""

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("poc", ROOT / "scripts/antithesis-copilot-poc.py")
poc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(poc)


class CopilotPOC(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.output = self.root / "analysis"
        self.source = self.root / "tested"
        self.driver = self.source / poc.DRIVERS / "eventually_treasury_withdrawal.py"
        self.driver.parent.mkdir(parents=True)
        (self.source / ".git").mkdir()
        self.driver.write_text('sdk.always(False, "treasury_withdrawal_recovered")\n')
        self.evidence = self.root / "evidence.json"
        self.bundle = {
            "github": {"head_sha": "b" * 40, "run_id": "123"},
            "test": {"tested_commit": "a" * 40, "mode": "leios", "outcome": "failure"},
            "report": {"status": "partial"},
            "gaps": ["Restricted Tracer logs"],
            "properties": [
                {
                    "property_id": "treasury_withdrawal_recovered",
                    "passed": False,
                    "counterexamples": [
                        {"command": "governance/eventually_treasury_withdrawal.py"}
                    ],
                }
            ],
        }

    def prepare(self):
        self.evidence.write_text(json.dumps(self.bundle))
        with mock.patch.object(poc.subprocess, "check_output", return_value="a" * 40 + "\n"):
            poc.prepare(self.evidence, self.source, self.output)

    def test_uses_tested_commit_and_line_numbered_source_not_workflow_head(self):
        self.prepare()
        prompt = (self.output / "prompt.txt").read_text()
        self.assertIn(f"CANDIDATE SOURCE {'a' * 40}", prompt)
        self.assertIn('1: sdk.always(False, "treasury_withdrawal_recovered")', prompt)
        self.assertIn("runtime images are pinned", prompt)
        self.assertIn("Restricted Tracer logs", prompt)

    def test_missing_tested_commit_does_not_substitute_head_or_read_source(self):
        self.bundle["test"].pop("tested_commit")
        self.evidence.write_text(json.dumps(self.bundle))
        with mock.patch.object(poc.subprocess, "check_output") as git:
            poc.prepare(self.evidence, self.source, self.output)
        git.assert_not_called()
        self.assertIn(
            "no source checkout was substituted",
            (self.output / "prompt.txt").read_text(),
        )

    def test_mismatched_checkout_is_rejected(self):
        self.evidence.write_text(json.dumps(self.bundle))
        with mock.patch.object(poc.subprocess, "check_output", return_value="b" * 40):
            with self.assertRaisesRegex(ValueError, "does not match"):
                poc.prepare(self.evidence, self.source, self.output)

    def test_unavailable_checkout_is_an_explicit_gap(self):
        (self.source / ".git").rmdir()
        self.prepare()
        context = json.loads((self.output / "context.json").read_text())
        self.assertFalse(context["source_available"])
        self.assertEqual(context["source_files"], [])

    def test_source_symlink_cannot_attach_runner_files(self):
        outside = self.root / "outside.py"
        outside.write_text('sdk.always(False, "treasury_withdrawal_recovered"); PRIVATE_CONTENT')
        self.driver.unlink()
        self.driver.symlink_to(outside)
        self.prepare()
        self.assertNotIn("PRIVATE_CONTENT", (self.output / "prompt.txt").read_text())

    def test_large_context_is_bounded_and_at_file_expansion_is_disabled(self):
        self.bundle["gaps"] = ["@/proc/self/environ " + "x" * 100_000] * 50
        self.bundle["properties"] *= 1000
        self.prepare()
        prompt = (self.output / "prompt.txt").read_text()
        self.assertLessEqual(len(prompt), poc.MAX_PROMPT)
        self.assertNotIn("@", prompt)
        self.assertIn("TRUNCATED", prompt)
        self.assertIn("NONPASSING RECORDS", prompt)

    def test_token_missing_fails_without_invoking_cli(self):
        self.prepare()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(poc.subprocess, "run") as cli,
        ):
            with self.assertRaisesRegex(ValueError, "COPILOT_POC_TOKEN"):
                poc.run(self.output, "copilot")
        cli.assert_not_called()

    def test_one_tool_free_call_has_no_actions_or_byok_credential_and_sanitizes_output(
        self,
    ):
        self.prepare()

        def response(command, **kwargs):
            self.assertIn("--available-tools", command)
            self.assertIn("--deny-tool=shell", command)
            self.assertIn("--no-custom-instructions", command)
            self.assertIn("--sync-session=off", command)
            self.assertNotIn("--autopilot", command)
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertEqual(kwargs["env"]["COPILOT_GITHUB_TOKEN"], "personal-test-value")
            kwargs["stdout"].write(
                b"## Evidence gaps\nSee https://example.com/report?auth=hidden\n"
            )
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "COPILOT_GITHUB_TOKEN": "personal-test-value",
                    "GITHUB_TOKEN": "actions-test-value",
                    "OPENAI_API_KEY": "byok-test-value",
                },
            ),
            mock.patch.object(poc.subprocess, "run", side_effect=response) as cli,
        ):
            self.assertEqual(poc.run(self.output, "copilot"), 0)
        self.assertEqual(cli.call_count, 1)
        analysis = (self.output / "analysis.md").read_text()
        self.assertIn("unverified analysis", analysis)
        self.assertNotIn("auth=hidden", analysis)

    def test_failed_response_never_becomes_an_analysis(self):
        self.prepare()
        (self.output / "analysis.md").write_text("stale previous analysis")
        with (
            mock.patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "personal-test-value"}),
            mock.patch.object(
                poc.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
            ),
        ):
            self.assertEqual(poc.run(self.output, "copilot"), 1)
        self.assertFalse((self.output / "analysis.md").exists())
        self.assertIn("No diagnosis was accepted", (self.output / "status.md").read_text())

    def test_empty_response_is_rejected(self):
        self.prepare()
        with (
            mock.patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "personal-test-value"}),
            mock.patch.object(
                poc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ),
        ):
            with self.assertRaisesRegex(ValueError, "empty response"):
                poc.run(self.output, "copilot")
        self.assertFalse((self.output / "analysis.md").exists())

    def test_failure_usage_is_sanitized_before_archival(self):
        self.prepare()

        def failure(command, **kwargs):
            usage = pathlib.Path(command[command.index("--usage-output-file") + 1])
            usage.write_text(json.dumps({"credits": 1, "token": "hidden"}))
            return subprocess.CompletedProcess(command, 1)

        with (
            mock.patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "personal-test-value"}),
            mock.patch.object(poc.subprocess, "run", side_effect=failure),
        ):
            self.assertEqual(poc.run(self.output, "copilot"), 1)
        usage = json.loads((self.output / "usage.json").read_text())
        self.assertEqual(usage["credits"], 1)
        self.assertEqual(usage["token"], "[redacted]")
        self.assertFalse((self.output / "analysis.md").exists())


if __name__ == "__main__":
    unittest.main()
