"""Read-only wallet checks: secret handling and unknown balance responses."""

import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wallet_check", ROOT / "scripts/check-moog-wallet.py")
wallet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wallet)
ADDRESS = "addr_test1" + "q" * 50


class WalletCheck(unittest.TestCase):
    def test_only_valid_public_address_is_returned_and_wallet_file_is_deleted(self):
        secret = b'{"encryptedMnemonics":"private-test-marker"}'
        paths = []

        def run(args, **kwargs):
            path = pathlib.Path(args[-1])
            paths.append(path)
            self.assertEqual(path.read_bytes(), secret)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("MOOG_REQUESTER_WALLET", kwargs["env"])
            self.assertEqual(kwargs["env"]["MOOG_WALLET_PASSPHRASE"], "test-passphrase")
            self.assertEqual(args[:3], ["moog", "wallet", "info"])
            return subprocess.CompletedProcess(args, 0, json.dumps({"address": ADDRESS}), "")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "MOOG_REQUESTER_WALLET": base64.b64encode(secret).decode(),
                    "MOOG_WALLET_PASSPHRASE": "test-passphrase",
                },
            ),
            mock.patch.object(wallet.subprocess, "run", side_effect=run),
        ):
            self.assertEqual(wallet.public_address(), ADDRESS)
        self.assertFalse(paths[0].exists())

    def test_wallet_failure_cannot_expose_cli_output_or_secret(self):
        private = "private-seed-marker"
        captured = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, {"MOOG_REQUESTER_WALLET": base64.b64encode(private.encode()).decode()}
            ),
            mock.patch.object(
                wallet.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, private, private),
            ),
            mock.patch.object(wallet, "balance") as query,
            mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}),
            contextlib.redirect_stdout(captured),
            contextlib.redirect_stderr(captured),
        ):
            self.assertEqual(wallet.main(), 1)
            query.assert_not_called()
        self.assertNotIn(private, captured.getvalue())
        self.assertIn("Unable to read the wallet", captured.getvalue())

    def test_malformed_or_mainnet_wallet_output_is_not_published(self):
        for output in [
            "private-marker",
            '{"error":"private-marker"}',
            '{"address":"addr1mainnet"}',
        ]:
            with (
                mock.patch.dict(os.environ, {"MOOG_REQUESTER_WALLET": "e30="}),
                mock.patch.object(
                    wallet.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, output, ""),
                ),
            ):
                with self.assertRaises(wallet.CheckError) as error:
                    wallet.public_address()
                self.assertNotIn("private-marker", str(error.exception))

    def test_exact_balance_including_zero(self):
        for amount in ["0", "1234567", 1234567]:
            self.assertEqual(
                wallet.parse_balance([{"address": ADDRESS, "balance": amount}], ADDRESS),
                int(amount),
            )

    def test_unknown_or_mismatched_balance_cannot_become_zero(self):
        records = [[], {}, None, [{"balance": "0"}], [{"address": "foreign", "balance": "0"}]]
        records += [
            [{"address": ADDRESS, "balance": value}]
            for value in [None, True, -1, "-1", 1.5, "1.5", ""]
        ]
        for data in records:
            with self.assertRaises(wallet.CheckError):
                wallet.parse_balance(data, ADDRESS)

    def test_public_api_receives_only_address(self):
        stream = io.BytesIO(json.dumps([{"address": ADDRESS, "balance": "1234567"}]).encode())
        with mock.patch.object(wallet.urllib.request, "urlopen", return_value=stream) as query:
            self.assertEqual(wallet.balance(ADDRESS), 1234567)
        request = query.call_args.args[0]
        self.assertEqual(request.full_url, wallet.ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"_addresses": [ADDRESS]})

    def test_network_failure_reports_unknown_instead_of_zero(self):
        with (
            mock.patch.object(
                wallet.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
            ) as query,
            mock.patch.object(wallet.time, "sleep"),
        ):
            with self.assertRaisesRegex(wallet.CheckError, "No balance was inferred"):
                wallet.balance(ADDRESS)
            self.assertEqual(query.call_count, 2)

    def test_successful_summary_contains_only_public_information(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = pathlib.Path(directory) / "summary.md"
            with (
                mock.patch.object(wallet, "public_address", return_value=ADDRESS),
                mock.patch.object(wallet, "balance", return_value=1234567),
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(wallet.main(), 0)
            text = summary.read_text()
            self.assertIn(ADDRESS, text)
            self.assertIn("1.234567 tADA", text)

    def test_unavailable_query_keeps_public_address_without_printing_zero(self):
        output = io.StringIO()
        with (
            mock.patch.object(wallet, "public_address", return_value=ADDRESS),
            mock.patch.object(
                wallet, "balance", side_effect=wallet.CheckError("Balance unavailable")
            ),
            mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(wallet.main(), 1)
        self.assertIn(ADDRESS, output.getvalue())
        self.assertIn("Balance unavailable", output.getvalue())
        self.assertNotIn("0.000000", output.getvalue())


if __name__ == "__main__":
    unittest.main()
