"""Evidence provenance, unavailable data, bounded parsing and credential handling."""

import argparse
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evidence", ROOT / "scripts/collect-antithesis-evidence.py"
)
e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(e)
CONTEXT = {
    "github": {"repository": "org/repo", "run_id": "123", "run_attempt": 2, "head_sha": "a" * 40},
    "test": {"request_id": "b" * 64, "tested_commit": "c" * 40, "mode": "leios"},
}
RUN = {
    "id": 123,
    "run_attempt": 2,
    "repository": {"full_name": "org/repo"},
    "head_repository": {"full_name": "org/repo"},
    "path": e.WORKFLOW,
    "head_branch": "main",
    "event": "workflow_dispatch",
    "status": "completed",
    "head_sha": "a" * 40,
    "conclusion": "failure",
    "html_url": "https://github.com/org/repo/actions/runs/123",
}


def record(request="b" * 64, commit="c" * 40, repository="org/repo", run="current", **overrides):
    org, repo = repository.split("/")
    value = {
        "run_id": run,
        "property_id": "Sometimes: Sometimes",
        "group_id": "actions_live",
        "passed": False,
        "example_count": 0,
        "counterexample_count": 1,
        "examples": [],
        "counterexamples": [json.dumps({"command": "governance/vote.py"})],
        "description": json.dumps(
            {
                "testRunId": request,
                "testRun": {
                    "commitId": commit,
                    "repository": {"organization": org, "repo": repo},
                },
            }
        ),
    }
    return value | overrides


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for name, value in files.items():
            z.writestr(name, value)
    return output.getvalue()


def shell_step(script, output="", env=""):
    return f"##[group]Run test\n{script}\nshell: /usr/bin/bash -e {{0}}\nenv:\n{env}\n##[endgroup]\n{output}\n"


def legacy_logs(
    mode="leios", commit="c" * 40, request_id="b" * 64, outcome="failure", submitted=True
):
    response = json.dumps(
        {"txHash": "e" * 64, "value": {"testRunId": request_id}, "wallet": "PRIVATE_MARKER"}
    )
    return (
        shell_step(
            'echo "mode=$MODE" >> "$GITHUB_OUTPUT"',
            env=f"  MODE: {mode}\n  API_KEY: PRIVATE_MARKER",
        )
        + shell_step(
            f'COMMIT="{commit}"\nRESULT=$(moog requester create-test -d "$TESTNET" -c "$COMMIT")',
            output="TRY=1 for testnets/cardano_node_governance\n" + (response if submitted else ""),
        )
        + shell_step(
            f'RESULT=$(moog facts test-runs --test-run-id "{request_id}")\necho "outcome: $OUTCOME"',
            output=f"outcome: {outcome}",
        )
    )


class ReportEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.context = self.root / "context.json"
        self.context.write_text(json.dumps(CONTEXT))

    def args(self, **kwargs):
        return (
            argparse.Namespace(
                context=self.context,
                facts=None,
                compose=None,
                report_url_file=None,
                offline_report=self.root,
                max_chunks=3,
            ).__dict__
            | kwargs
        )

    def collect(self, records, **kwargs):
        (self.root / "manifest.json").write_text(
            json.dumps({"chunk_id_to_chunk_location": {"0": "chunk.bin"}})
        )
        (self.root / "chunk0.bin").write_bytes(
            b"\xff\x00protobuf framing" + json.dumps({"body": json.dumps(records)}).encode()
        )
        return e.collect_report(argparse.Namespace(**self.args(**kwargs)))

    def test_same_commit_in_historical_run_is_not_current_evidence(self):
        bundle = self.collect([record(request="old-request", passed=True), record()])
        self.assertEqual(len(bundle["properties"]), 1)
        self.assertFalse(bundle["properties"][0]["passed"])
        self.assertEqual(
            bundle["properties"][0]["counterexamples"][0]["command"], "governance/vote.py"
        )
        self.assertFalse(bundle["report"]["inventory_complete"])

    def test_other_repo_and_other_tested_commit_cannot_supply_evidence(self):
        bundle = self.collect([record(repository="other/repo"), record(commit="d" * 40)])
        self.assertEqual(bundle["properties"], [])
        self.assertEqual(bundle["report"]["status"], "unsupported_or_no_matching_properties")

    def test_missing_request_identity_cannot_fall_back_to_latest_or_same_sha(self):
        context = json.loads(json.dumps(CONTEXT))
        context["test"]["request_id"] = ""
        self.context.write_text(json.dumps(context))
        bundle = self.collect([record()])
        self.assertEqual(bundle["properties"], [])
        self.assertIn("No Moog request identity", bundle["gaps"][0])

    def test_ambiguous_platform_runs_are_not_merged(self):
        bundle = self.collect([record(run="one"), record(run="two")])
        self.assertEqual(bundle["properties"], [])
        self.assertIn("Multiple platform runs", bundle["gaps"][0])

    def test_unsupported_chunk_format_remains_unknown(self):
        bundle = self.collect([])
        self.assertEqual(bundle["properties"], [])
        self.assertFalse(bundle["report"]["inventory_complete"])
        self.assertNotEqual(bundle["test"].get("outcome"), "success")

    def test_latest_snapshot_wins_without_duplicate_properties(self):
        selected = e.select_properties(
            [
                record(passed=False, insert_time="2026-01-02"),
                record(passed=True, insert_time="2026-01-01"),
            ],
            CONTEXT,
        )
        self.assertEqual(len(selected), 1)
        self.assertFalse(selected[0]["passed"])

    def test_moog_object_and_array_responses_keep_outcome_separate_from_properties(self):
        facts = self.root / "facts.json"
        for data in [
            {"value": {"outcome": "failure", "phase": "finished"}},
            [{"value": {"outcome": "failure"}}],
        ]:
            facts.write_text(json.dumps(data))
            result = self.collect([record(passed=True)], facts=facts)
            self.assertEqual(result["test"]["outcome"], "failure")
            self.assertTrue(result["properties"][0]["passed"])

    def test_malformed_or_wrong_request_fact_cannot_supply_an_outcome(self):
        facts = self.root / "facts.json"
        for value in [
            None,
            "not-a-fact",
            [{"value": {"testRunId": "other", "outcome": "success"}}],
        ]:
            with self.subTest(value=value):
                facts.write_text(json.dumps(value))
                result = self.collect([record()], facts=facts)
                self.assertNotEqual(result["test"].get("outcome"), "success")
                self.assertEqual(result["properties"], [])
                self.assertTrue(result["gaps"])

    def test_inaccessible_report_is_recorded_without_signed_url_or_exception_leak(self):
        url = self.root / "private-url"
        url.write_text("https://tenant.antithesis.com/report/abc.html?auth=private-marker")
        with mock.patch.object(
            e.Downloader, "get", side_effect=e.EvidenceError("Download unavailable (HTTP 403)")
        ):
            result = e.collect_report(
                argparse.Namespace(**self.args(offline_report=None, report_url_file=url))
            )
        e.write_bundle(result, self.root / "out")
        saved = (self.root / "out/evidence.json").read_text()
        self.assertIn("403", saved)
        self.assertNotIn("private-marker", saved)
        self.assertEqual(result["properties"], [])

    def test_script_sources_and_manifest_are_read_as_data_only(self):
        self.assertEqual(
            e.manifest('const m = {"chunk_id_to_chunk_location":{"1":"file.bin"}}; evil();')[
                "chunk_id_to_chunk_location"
            ],
            {"1": "file.bin"},
        )
        with self.assertRaises(e.EvidenceError):
            e.report_asset(
                "https://tenant.antithesis.com/report/a.html", "https://attacker.example/file.js"
            )

    def test_compose_images_and_options_come_from_resolved_services(self):
        path = self.root / "compose.json"
        services = {
            name: {"image": name + "@sha256:abc"}
            for name in ["p1", "gov-cli", "gov-configurator", "tx-firehose", "tracer-sidecar"]
        }
        services["gov-cli"]["environment"] = {
            "GOV_COMMAND_ERA": "dijkstra",
            "LEIOS_STORAGE_CHECKS": "true",
        }
        services["gov-configurator"]["environment"] = {"PROTOCOL_VERSION": "12"}
        path.write_text(json.dumps({"services": services}))
        result = self.collect([record()], compose=path)
        self.assertEqual(result["images"]["gov-cli"], "gov-cli@sha256:abc")
        self.assertEqual(result["test"]["options"]["LEIOS_STORAGE_CHECKS"], "true")


class SanitizationAndDownloads(unittest.TestCase):
    def test_embedded_credential_assignments_and_key_envelopes_are_not_uploaded(self):
        cases = [
            "Error: api_key=PRIVATE_MARKER",
            "Error: MOOG_REQUESTER_WALLET=PRIVATE_MARKER",
            'Error: {"nested":{"auth":"PRIVATE_MARKER"}}',
            r"Error: {\"signing_key\":\"PRIVATE_MARKER\"}",
            'Error: {"type":"PaymentSigningKeyShelley_ed25519","cborHex":"PRIVATE_MARKER"}',
            {"nested": {"type": "PaymentSigningKeyShelley_ed25519", "cborHex": "PRIVATE_MARKER"}},
            {"auth": "PRIVATE_MARKER"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            for case in cases:
                with self.subTest(case=case):
                    bundle = {
                        "schema_version": 1,
                        **CONTEXT,
                        "report": {"status": "extracted_partial"},
                        "properties": [record(counterexamples=[{"additional_stderr": case}])],
                        "gaps": [],
                    }
                    e.write_bundle(bundle, pathlib.Path(directory))
                    saved = (pathlib.Path(directory) / "evidence.json").read_text()
                    self.assertNotIn("PRIVATE_MARKER", saved)
                    self.assertIn("redacted", saved)

    def test_credentials_and_signed_queries_are_removed_from_nested_samples(self):
        raw = {
            "password": "private-one",
            "nested": [
                "https://tenant.antithesis.com/report/a?auth=private-two&other=private-three#private-four",
                'error: {"token":"private-five"}',
                "Authorization: Basic private-six",
                "Authorization: Bearer private-seven",
                "Authorization: token private-eight",
                "Cookie: session=private-nine",
            ],
            "api_key": "private-ten",
            "MOOG_REQUESTER_WALLET": "private-eleven",
        }
        output = json.dumps(e.clean(raw))
        self.assertNotIn("private-", output)
        self.assertIn("https://tenant.antithesis.com/report/a", output)

    def test_download_failure_does_not_expose_curl_arguments_or_stderr(self):
        with mock.patch.object(
            e.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 22, b"403\n", b"auth=private"),
        ):
            with self.assertRaisesRegex(e.EvidenceError, "HTTP 403") as error:
                e.Downloader().get("https://tenant.antithesis.com/a?auth=private")
        self.assertNotIn("private", str(error.exception))

    def test_github_token_is_file_scoped_and_not_forwarded_to_storage(self):
        headers = []

        def run(args, **kwargs):
            output = pathlib.Path(args[args.index("--output") + 1])
            output.write_bytes(b"{}")
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertNotIn("private-token", repr(args))
            if "--header" in args:
                path = pathlib.Path(args[args.index("--header") + 1][1:])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn("private-token", path.read_text())
                headers.append(path)
                return subprocess.CompletedProcess(
                    [], 0, b"302\nhttps://results.blob.core.windows.net/a?sig=private", b""
                )
            return subprocess.CompletedProcess([], 0, b"200\n", b"")

        with mock.patch.object(e.subprocess, "run", side_effect=run):
            self.assertEqual(
                e.Downloader().get(
                    "https://api.github.com/repos/org/repo/actions/runs/123/logs",
                    token="private-token",
                    github_redirect=True,
                ),
                b"{}",
            )
        self.assertEqual(len(headers), 1)
        self.assertFalse(headers[0].exists())

    def test_log_selection_keeps_errors_without_wallet_dumps_or_bearer_urls(self):
        logs = archive(
            {
                "job/log.txt": "\n".join(
                    [
                        "outcome: failure",
                        "##[error]Process completed with exit code 1.",
                        'Error: {"mnemonics":"private-one"}',
                        "Error: password=private-two",
                        "Error: https://tenant.antithesis.com/report/a?auth=private-three",
                    ]
                )
            }
        )
        result = e.github_logs(logs)
        saved = json.dumps(result)
        self.assertIn("outcome: failure", saved)
        self.assertIn("exit code 1", saved)
        self.assertNotIn("private-", saved)

    def test_oversized_archives_are_refused_without_reading_members(self):
        entry = zipfile.ZipInfo("log.txt")
        entry.file_size = e.MAX_TOTAL + 1
        with mock.patch.object(e.zipfile, "ZipFile") as z:
            z.return_value.__enter__.return_value.infolist.return_value = [entry]
            with self.assertRaises(e.EvidenceError):
                e.github_logs(b"ignored")
            z.return_value.__enter__.return_value.read.assert_not_called()


class FinalEvidence(unittest.TestCase):
    def test_fork_wrong_workflow_unfinished_and_wrong_attempt_are_rejected(self):
        invalid = [
            RUN | {"head_repository": None},
            RUN | {"head_repository": {"full_name": "fork/repo"}},
            RUN | {"path": ".github/workflows/other.yaml"},
            RUN | {"status": "in_progress"},
            RUN | {"head_branch": "untrusted"},
            RUN | {"run_attempt": 3},
        ]
        for run in invalid:
            with self.subTest(run=run), self.assertRaises(e.EvidenceError):
                e.verify_run(run, "org/repo", 2)

    def test_missing_old_report_artifact_still_collects_github_failure_without_node_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "run.json").write_text(json.dumps(RUN))
            (root / "logs.zip").write_bytes(
                archive({"job.txt": "##[error]Process completed with exit code 1."})
            )
            result = e.finalize(
                argparse.Namespace(
                    repository="org/repo",
                    github_run_json=root / "run.json",
                    github_run_id=None,
                    expected_attempt=2,
                    seed=None,
                    github_logs_zip=root / "logs.zip",
                )
            )
            self.assertEqual(result["github"]["conclusion"], "failure")
            self.assertEqual(result["test"]["outcome"], "unknown")
            self.assertEqual(result["properties"], [])
            self.assertEqual(result["github_logs"]["status"], "collected")

    def test_another_attempts_artifact_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "run.json").write_text(json.dumps(RUN))
            seed = {
                "schema_version": 1,
                **CONTEXT,
                "properties": [record()],
                "gaps": [],
                "report": {"status": "extracted_partial"},
            }
            seed["github"] = CONTEXT["github"] | {"run_attempt": 1}
            (root / "seed.json").write_text(json.dumps(seed))
            (root / "logs.zip").write_bytes(archive({"job.txt": "outcome: failure"}))
            result = e.finalize(
                argparse.Namespace(
                    repository="org/repo",
                    github_run_json=root / "run.json",
                    github_run_id=None,
                    expected_attempt=2,
                    seed=root / "seed.json",
                    github_logs_zip=root / "logs.zip",
                )
            )
            self.assertEqual(result["properties"], [])
            self.assertTrue(any("provenance" in gap for gap in result["gaps"]))

    def test_online_finalizer_selects_only_the_requested_attempt_artifact(self):
        seed = {
            "schema_version": 1,
            **CONTEXT,
            "properties": [],
            "gaps": [],
            "report": {"status": "extracted_partial"},
        }
        calls = []

        def get(url, **_kwargs):
            calls.append(url)
            if url.endswith("/attempts/2"):
                return json.dumps(RUN).encode()
            if "/artifacts?" in url:
                return json.dumps(
                    {
                        "artifacts": [
                            {
                                "id": 77,
                                "name": "antithesis-report-evidence-123-1",
                                "expired": False,
                            },
                            {
                                "id": 78,
                                "name": "antithesis-report-evidence-123-2",
                                "expired": False,
                            },
                        ]
                    }
                ).encode()
            if url.endswith("/artifacts/78/zip"):
                return archive({"evidence.json": json.dumps(seed), "summary.md": "ignored"})
            if url.endswith("/attempts/2/logs"):
                return archive({"job.txt": "outcome: failure"})
            self.fail("Unexpected GitHub endpoint")

        with mock.patch.object(e.Downloader, "get", side_effect=get):
            result = e.finalize(
                argparse.Namespace(
                    repository="org/repo",
                    github_run_json=None,
                    github_run_id="123",
                    expected_attempt=2,
                    seed=None,
                    github_logs_zip=None,
                )
            )
        self.assertEqual(result["report"]["status"], "extracted_partial")
        self.assertEqual(result["github_logs"]["status"], "collected")
        self.assertFalse(any("/artifacts/77/" in url for url in calls))


class LegacyMetadataFallback(unittest.TestCase):
    def finalize(self, logs, seed=None, filename="0_run-cardano-node-governance.txt"):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "run.json").write_text(json.dumps(RUN))
            (root / "logs.zip").write_bytes(archive({filename: logs}))
            if seed:
                (root / "seed.json").write_text(json.dumps(seed))
            return e.finalize(
                argparse.Namespace(
                    repository="org/repo",
                    github_run_json=root / "run.json",
                    github_run_id=None,
                    expected_attempt=2,
                    seed=root / "seed.json" if seed else None,
                    github_logs_zip=root / "logs.zip",
                )
            )

    def test_metadata_is_recovered_with_sources_but_properties_remain_unavailable(self):
        result = self.finalize(legacy_logs())
        for field, expected in {
            "mode": "leios",
            "tested_commit": "c" * 40,
            "outcome": "failure",
            "request_id": "b" * 64,
        }.items():
            self.assertEqual(result["test"][field], expected)
            source = result["test"]["field_sources"][field]
            self.assertEqual(source["source"], "github_logs")
            self.assertTrue(all(p["line"] > 0 for p in source["evidence"]))
        self.assertNotEqual(result["test"]["tested_commit"], result["github"]["head_sha"])
        self.assertEqual(result["properties"], [])
        self.assertEqual(result["report"]["status"], "unavailable")
        self.assertNotIn("PRIVATE_MARKER", json.dumps(result))

    def test_all_supported_modes_are_explicit_not_defaulted(self):
        for mode in ("conway", "leios", "leios-latest"):
            self.assertEqual(self.finalize(legacy_logs(mode=mode))["test"]["mode"], mode)
        self.assertEqual(self.finalize(legacy_logs(mode="invalid"))["test"]["mode"], "unknown")

    def test_quoted_commands_and_historical_json_cannot_supply_an_outcome(self):
        logs = shell_step(
            'echo "outcome: failure"\necho "outcome: $OUTCOME"',
            output='echo "outcome: failure"\n{"outcome":"failure"}',
            env="  MODE: leios",
        )
        result = self.finalize(logs)
        self.assertEqual(result["test"]["outcome"], "unknown")
        self.assertEqual(result["test"]["mode"], "unknown")

    def test_commit_without_created_request_is_not_claimed_as_tested(self):
        result = self.finalize(legacy_logs(submitted=False))
        self.assertEqual(result["test"]["outcome"], "failure")
        self.assertFalse(result["test"].get("tested_commit"))

    def test_short_commit_hash_cannot_fall_back_to_workflow_head(self):
        result = self.finalize(legacy_logs(commit="c" * 7))
        self.assertFalse(result["test"].get("tested_commit"))

    def test_historical_facts_before_current_try_are_ignored(self):
        old = json.dumps({"txHash": "e" * 64, "value": {"testRunId": "d" * 64}})
        logs = legacy_logs().replace("TRY=1 for", old + "\nTRY=1 for")
        result = self.finalize(logs)
        self.assertEqual(result["test"]["request_id"], "b" * 64)
        self.assertEqual(result["test"]["tested_commit"], "c" * 40)

    def test_different_created_and_queried_requests_leave_outcome_unknown(self):
        logs = legacy_logs().replace('--test-run-id "' + "b" * 64, '--test-run-id "' + "d" * 64)
        result = self.finalize(logs)
        self.assertEqual(result["test"]["outcome"], "unknown")
        self.assertFalse(result["test"].get("tested_commit"))
        self.assertTrue(any("Conflicting" in x for x in result["gaps"]))

    def test_conflicting_report_outcomes_are_not_chosen_by_order(self):
        result = self.finalize(legacy_logs() + "outcome: success\n")
        self.assertEqual(result["test"]["outcome"], "unknown")
        self.assertTrue(any("Conflicting" in x for x in result["gaps"]))

    def test_structured_artifact_values_take_precedence_with_conflict_noted(self):
        seed = {
            "schema_version": 1,
            **CONTEXT,
            "properties": [],
            "gaps": [],
            "report": {"status": "extracted_partial"},
        }
        seed["test"] = CONTEXT["test"] | {"mode": "conway", "outcome": "success"}
        result = self.finalize(legacy_logs(), seed=seed)
        self.assertEqual(result["test"]["mode"], "conway")
        self.assertEqual(result["test"]["outcome"], "success")
        self.assertTrue(any("disagree" in x for x in result["gaps"]))

    def test_unrelated_job_logs_cannot_fill_metadata(self):
        result = self.finalize(legacy_logs(), filename="1_other-job.txt")
        self.assertEqual(result["test"]["outcome"], "unknown")
        self.assertEqual(result["test"]["mode"], "unknown")

    def test_log_outcome_for_a_different_artifact_request_cannot_fill_unknown_outcome(self):
        seed = {
            "schema_version": 1,
            **CONTEXT,
            "properties": [],
            "gaps": [],
            "report": {"status": "extracted_partial"},
        }
        result = self.finalize(legacy_logs(request_id="d" * 64), seed=seed)
        self.assertEqual(result["test"]["request_id"], "b" * 64)
        self.assertNotIn("outcome", result["test"])
        self.assertTrue(any("disagree" in x for x in result["gaps"]))


if __name__ == "__main__":
    unittest.main()
