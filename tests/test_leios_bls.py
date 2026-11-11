"""Registration witnesses and scheduling regressions, without paid runs."""

from __future__ import annotations

import contextlib
import importlib
import json
import pathlib
import runpy
import sys
import tempfile
import unittest
from unittest import mock

DRIVERS = (
    pathlib.Path(__file__).resolve().parents[1] / "components/gov-cli/composer/governance-python"
)
sys.path.insert(0, str(DRIVERS))
import helper_leios_bls as bls


class BlsRegistration(unittest.TestCase):
    def setUp(self):
        self.cluster = mock.Mock()
        self.state = {
            "registration": {
                "pool_id": "private_pool",
                "txid": "registration_tx",
                "public_key": "ab" * 96,
                "signed": "retained.signed",
            }
        }
        self.sdk = mock.patch.object(bls, "sdk").start()
        self.addCleanup(mock.patch.stopall)

    def pool(self, key):
        self.cluster.g_query.query_cli.return_value = json.dumps(
            {"pool": {"poolParams": {"spsLeiosKey": {"leiosPubKey": key}}}}
        )

    def test_only_matching_immutable_key_is_a_witness(self):
        self.pool("ab" * 96)
        self.assertTrue(bls.observe(self.cluster, self.state))
        self.assertEqual(
            self.cluster.g_query.query_cli.call_args.kwargs, {"cli_sub_args": ["--immutable-tip"]}
        )
        self.assertTrue(self.sdk.sometimes.call_args.args[0])

    def test_wrong_registered_key_is_a_safety_failure(self):
        self.pool("cd" * 96)
        self.assertFalse(bls.observe(self.cluster, self.state))
        self.assertEqual(
            self.sdk.always.call_args.args[:2], (False, "leios_bls_registered_key_matches")
        )

    def test_absence_or_unknown_schema_cannot_publish_success(self):
        for response in ["{}", "null", "[]", '{"pool":{"poolParams":null}}']:
            self.cluster.g_query.query_cli.return_value = response
            try:
                self.assertFalse(bls.observe(self.cluster, self.state))
            except (ValueError, TypeError):
                pass
        self.sdk.sometimes.assert_not_called()
        self.sdk.always.assert_not_called()

    def tick(self, observe):
        with (
            mock.patch.object(bls.g.SETUP_MARKER.__class__, "exists", return_value=True),
            mock.patch.object(bls, "locked", return_value=contextlib.nullcontext(self.state)),
            mock.patch.object(bls.g, "make_cluster", return_value=self.cluster),
            mock.patch.object(bls, "observe", side_effect=observe),
        ):
            return bls.tick(submit=True)

    def test_observed_registration_stops_rebroadcast(self):
        self.assertTrue(self.tick(lambda *_: True))
        self.cluster.g_transaction.submit_tx_bare.assert_not_called()

    def test_submission_unknown_retries_exact_signed_bytes(self):
        self.cluster.g_transaction.submit_tx_bare.side_effect = OSError("interrupted")
        self.assertFalse(self.tick(lambda *_: False))
        self.assertFalse(self.tick(lambda *_: False))
        self.assertEqual(
            self.cluster.g_transaction.submit_tx_bare.call_args_list,
            [mock.call(tx_file="retained.signed")] * 2,
        )
        self.assertEqual(self.state["registration"]["public_key"], "ab" * 96)

    def test_query_unknown_does_not_claim_success_or_submit(self):
        def unknown(*_):
            raise OSError("fault")

        self.assertFalse(self.tick(unknown))
        self.cluster.g_transaction.submit_tx_bare.assert_not_called()
        self.sdk.sometimes.assert_any_call(
            False, bls.COVERAGE, {"reason": "awaiting_immutable_registration"}
        )

    def test_terminal_driver_supplies_even_without_parallel_driver(self):
        terminal = importlib.import_module("eventually_leios_bls")
        with (
            mock.patch.object(bls, "enabled", return_value=True),
            mock.patch.object(terminal.timing, "recovery_checks", return_value=2),
            mock.patch.object(terminal.time, "sleep"),
            mock.patch.object(bls, "tick", side_effect=[False, False, True]) as tick,
        ):
            self.assertEqual(terminal.main(), 0)
        self.assertEqual(tick.call_args_list, [mock.call(submit=True)] * 3)

    def test_terminal_rechecks_instead_of_trusting_old_success(self):
        self.state["complete"] = True
        self.assertFalse(self.tick(lambda *_: False))
        self.cluster.g_transaction.submit_tx_bare.assert_called_once()

    def test_disabled_and_conway_emit_nothing_or_touch_state(self):
        for flag, leios in [("false", True), ("true", False)]:
            with (
                mock.patch.dict("os.environ", LEIOS_BLS_CHECKS=flag),
                mock.patch.object(bls.leios, "enabled", return_value=leios),
                mock.patch.object(bls, "locked") as locked,
                mock.patch.object(bls.g, "make_cluster") as cluster,
                mock.patch.object(importlib.import_module("helper_sdk"), "_emit") as emit,
            ):
                for prefix in ["parallel_driver", "anytime", "eventually"]:
                    with self.assertRaises(SystemExit) as exited:
                        runpy.run_path(str(DRIVERS / f"{prefix}_leios_bls.py"), run_name="__main__")
                    self.assertEqual(exited.exception.code, 0)
                self.assertTrue(bls.prepare())
                locked.assert_not_called()
                cluster.assert_not_called()
                emit.assert_not_called()

    def test_lock_contention_keeps_one_journal_writer(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(bls, "ROOT", pathlib.Path(directory)),
        ):
            with bls.locked() as first:
                first["funding"] = "retained"
                with bls.locked() as second:
                    self.assertIsNone(second)
            with bls.locked() as reloaded:
                self.assertEqual(reloaded["funding"], "retained")

    def test_pending_funding_is_rebroadcast_without_spending_faucet_again(self):
        self.cluster.g_query.query_cli.side_effect = ['{"slot":10}', "{}"]
        self.cluster.g_query.get_utxo.return_value = []
        with (
            mock.patch.object(bls, "fixtures", return_value={"address": "private"}),
            mock.patch.object(bls.g, "build_sign_submit") as fund,
        ):
            self.assertFalse(bls.prepare_step(self.cluster, {"funding": {"expiry": 20}}))
        fund.assert_not_called()
        self.cluster.g_transaction.submit_tx_bare.assert_called_once_with(
            tx_file=bls.ROOT / "funding.signed"
        )

    def test_funding_identity_is_saved_before_interrupted_broadcast(self):
        self.cluster.epoch_length = 800
        self.cluster.g_query.query_cli.side_effect = ['{"slot":10}', "{}"]
        self.cluster.g_query.get_utxo.return_value = []
        self.cluster.g_query.get_slot_no.return_value = 20
        state = {}
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def broadcast(*_args, **kwargs):
                kwargs["before_submit"]("retained-txid")
                saved = json.loads((root / "tracking.json").read_text())
                self.assertEqual(saved["funding"], {"txid": "retained-txid", "expiry": 820})
                self.assertEqual(kwargs["submission_file"], root / "funding.signed")
                raise OSError("interrupted submission")

            with (
                mock.patch.object(bls, "ROOT", root),
                mock.patch.object(
                    bls, "fixtures", return_value={"address": "private", "deposit": 502_000_000}
                ),
                mock.patch.object(bls.g, "build_sign_submit", side_effect=broadcast),
                self.assertRaises(OSError),
            ):
                bls.prepare_step(self.cluster, state)
            self.assertEqual(state["funding"]["txid"], "retained-txid")

    def test_setup_cannot_release_faults_without_a_prepared_transaction(self):
        with (
            mock.patch.object(bls, "enabled", return_value=True),
            mock.patch.object(bls.timing, "recovery_checks", return_value=1),
            mock.patch.object(bls.time, "sleep"),
            mock.patch.object(bls, "locked", side_effect=lambda: contextlib.nullcontext({})),
            mock.patch.object(bls.g, "make_cluster", return_value=self.cluster),
            mock.patch.object(bls, "prepare_step", return_value=False),
        ):
            self.assertFalse(bls.prepare())
        self.sdk.unreachable.assert_called_once_with("leios_bls_setup_failed")

    def test_registration_does_not_expire_before_terminal_submission(self):
        funds = mock.Mock(utxo_hash="fund", utxo_ix=0, coin="lovelace")
        self.cluster.g_query.query_cli.side_effect = ['{"slot":10}', '{"fund#0":{}}']
        self.cluster.g_query.get_utxo.return_value = [funds]
        self.cluster.g_transaction.sign_tx.return_value = pathlib.Path("retained.signed")
        self.cluster.g_transaction.get_txid.return_value = "registration_tx"
        manifest = {
            "address": "private",
            "deposit": 502_000_000,
            "stake_cert": "owner.cert",
            "source_skey": "source.skey",
            "owner_skey": "owner.skey",
            "cold_skey": "cold.skey",
        }
        state = {}
        with (
            mock.patch.object(bls, "fixtures", return_value=manifest),
            mock.patch.object(bls, "save") as save,
        ):
            self.assertTrue(bls.prepare_step(self.cluster, state))
        for builder in [
            self.cluster.g_transaction.calculate_tx_fee,
            self.cluster.g_transaction.build_raw_tx,
        ]:
            self.assertNotIn("invalid_hereafter", builder.call_args.kwargs)
            self.assertNotIn("ttl", builder.call_args.kwargs)
        self.cluster.g_transaction.submit_tx_bare.assert_not_called()
        save.assert_called_once_with(state)

    def test_bls_preparation_cannot_add_a_whole_delegation_epoch(self):
        setup = importlib.import_module("first_setup")
        epoch = mock.Mock(return_value=4)
        wait = mock.Mock()

        def prepare():
            epoch.assert_called_once()
            epoch.return_value = 5  # Immutable funding crossed the boundary.
            return True

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with (
                mock.patch.multiple(
                    setup.g,
                    SETUP_MARKER=root / "setup_done",
                    PAYMENT_POOL=root / "payments",
                    NUM_DREPS=0,
                    NUM_CC=0,
                    NUM_PAYMENT_ADDRS=0,
                    ensure_dirs=mock.Mock(),
                    make_cluster=mock.Mock(),
                    wait_for_node=mock.Mock(return_value=True),
                    wait_for_epoch=wait,
                    build_sign_submit=mock.Mock(),
                    current_epoch=epoch,
                ),
                mock.patch.object(setup, "_leios_pool_keys", return_value=True),
                mock.patch.object(setup, "_setup_special_dreps", return_value=[]),
                mock.patch.object(setup, "_confirm_special_dreps"),
                mock.patch.object(setup, "_setup_treasury_recv_pool"),
                mock.patch.object(setup, "_prepare_treasury_payout", return_value=True),
                mock.patch.object(bls, "prepare", side_effect=prepare),
                mock.patch.object(setup.storage_setup, "prepare", return_value=True),
                mock.patch.object(setup, "sdk"),
            ):
                self.assertEqual(setup.main(), 0)
        self.assertEqual(wait.call_args.args[1:], (5, 5400))
        epoch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
