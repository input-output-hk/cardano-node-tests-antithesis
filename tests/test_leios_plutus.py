"""Harness regressions: certified script attribution and collateral isolation."""

import contextlib
import importlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0,
    str(
        pathlib.Path(__file__).resolve().parents[1]
        / "components/gov-cli/composer/governance-python"
    ),
)
import helper_leios_plutus as p


class ScriptObservation(unittest.TestCase):
    def setUp(self):
        self.payment = dict(
            txid="tx",
            size=250,
            recipient="recipient",
            amount=8_000_000,
            script_input="fund#0",
            collateral="fund#16",
            collateral_address="collateral",
            expiry=200,
            ebs={"eb": 250},
        )
        self.state = dict(
            payments=[self.payment],
            certificates={"rb": 100},
            headers={
                "tip": {"previous": "rb", "slot": 120},
                "rb": {"previous": "parent", "slot": 100},
            },
            announcements={"parent": {"eb_hash": "eb", "eb_slot": 90}},
        )
        self.tip = dict(hash="tip", slot=120)
        self.utxos = {
            "tx#0": dict(address="recipient", value={"lovelace": 8_000_000}),
            "fund#16": dict(address="collateral", value={"lovelace": p.COLLATERAL_AMOUNT}),
        }
        self.sdk = self.enterContext(mock.patch.object(p, "sdk"))
        self.query = self.enterContext(
            mock.patch.object(p, "immutable_utxos", side_effect=lambda *_: (self.tip, self.utxos))
        )

    def observe(self):
        return p.observe(mock.Mock(), self.state)

    def test_valid_certified_spend_checks_all_four_invariants(self):
        self.assertTrue(self.observe())
        self.assertEqual(len(self.sdk.always.call_args_list), 4)
        self.assertTrue(all(c.args[0] for c in self.sdk.always.call_args_list))
        self.assertEqual(self.payment["proof"]["cert_slot"], 100)
        queried = self.query.call_args.args[1]
        for ref in ["tx#0", "fund#0", "fund#16"]:
            self.assertIn(ref, queried)

    def test_ordinary_rb_output_is_not_script_eb_coverage(self):
        self.state["certificates"] = {}
        self.assertFalse(self.observe())
        self.sdk.always.assert_not_called()

    def test_orphan_certificate_does_not_qualify(self):
        self.tip["hash"] = "other-fork"
        self.assertFalse(self.observe())
        self.sdk.always.assert_not_called()

    def test_each_ledger_corruption_is_reported(self):
        cases = [
            ("leios_plutus_output_matches", lambda: self.utxos["tx#0"]["value"].update(lovelace=1)),
            ("leios_plutus_output_matches", lambda: self.utxos["tx#0"].update(address="foreign")),
            ("leios_plutus_input_consumed", lambda: self.utxos.update({"fund#0": {}})),
            ("leios_plutus_collateral_preserved", lambda: self.utxos.pop("fund#16")),
            ("leios_plutus_tx_size_matches", lambda: self.payment["ebs"].update(eb=249)),
        ]
        import copy

        original = copy.deepcopy((self.utxos, self.payment))
        for name, corrupt in cases:
            self.utxos, restored = copy.deepcopy(original)
            self.payment.clear()
            self.payment.update(restored)
            self.sdk.reset_mock()
            corrupt()
            self.assertFalse(self.observe(), name)
            self.assertIn((False, name), [c.args[:2] for c in self.sdk.always.call_args_list])

    def test_saved_proof_waits_for_relay_replay(self):
        self.assertTrue(self.observe())
        self.state["certificates"] = {}
        self.state["headers"] = {}
        self.tip["slot"] = 99
        self.utxos = {}
        self.sdk.reset_mock()
        self.assertFalse(self.observe())
        self.sdk.always.assert_not_called()
        self.assertIn(self.payment, self.state["payments"])

    def test_saved_proof_survives_trace_pruning(self):
        self.assertTrue(self.observe())
        self.state["certificates"] = {}
        self.state["headers"] = {}
        self.assertTrue(self.observe())

    def test_retirement_uses_immutable_expiry_only(self):
        self.state["certificates"] = {}
        self.assertFalse(self.observe())
        self.assertEqual(len(self.state["payments"]), 1)
        self.tip["slot"] = 200
        self.assertFalse(self.observe())
        self.assertEqual(self.state["payments"], [])


class PrivateWorkload(unittest.TestCase):
    def test_disabled_prepare_has_no_side_effects(self):
        with (
            mock.patch.object(p, "enabled", return_value=False),
            mock.patch.object(p, "locked") as lock,
            mock.patch.object(p, "sdk") as sdk,
        ):
            self.assertTrue(p.prepare())
            lock.assert_not_called()
            sdk.assert_not_called()

    def test_disabled_scheduled_drivers_emit_nothing(self):
        for name in [
            "anytime_leios_plutus",
            "parallel_driver_leios_plutus",
            "eventually_leios_plutus",
        ]:
            driver = importlib.import_module(name)
            with (
                mock.patch.object(p, "enabled", return_value=False),
                mock.patch.object(driver, "sdk") as sdk,
                mock.patch.object(p, "tick") as tick,
            ):
                self.assertEqual(driver.main(), 0)
                sdk.reachable.assert_not_called()
                tick.assert_not_called()

    def test_explicit_flag_still_requires_leios_genesis(self):
        with (
            mock.patch.dict(p.os.environ, {"LEIOS_PLUTUS_CHECKS": "true"}),
            mock.patch.object(p.traces, "enabled", return_value=False),
        ):
            self.assertFalse(p.enabled())

    def test_terminal_supplies_work_until_fresh_completion(self):
        driver = importlib.import_module("eventually_leios_plutus")
        with (
            mock.patch.object(p, "enabled", return_value=True),
            mock.patch.object(p, "tick", side_effect=[False, False, True]) as tick,
            mock.patch.object(driver.timing, "recovery_checks", return_value=4),
            mock.patch.object(driver.time, "sleep"),
            mock.patch.object(driver, "sdk"),
        ):
            self.assertEqual(driver.main(), 0)
            self.assertEqual(tick.call_args_list, [mock.call(submit=True)] * 3)

    def test_failed_current_query_cannot_reuse_cached_completion(self):
        state = {"complete": True}

        @contextlib.contextmanager
        def locked():
            yield state

        with (
            mock.patch.object(p.g, "SETUP_MARKER") as marker,
            mock.patch.object(p, "locked", locked),
            mock.patch.object(p.traces, "start_tracking"),
            mock.patch.object(p.traces, "ingest"),
            mock.patch.object(p, "observe", side_effect=OSError("fault")),
            mock.patch.object(p, "sdk"),
        ):
            marker.exists.return_value = True
            self.assertFalse(p.tick(submit=False))

    def test_batch_is_journalled_before_broadcast_and_collateral_is_private(self):
        cluster = mock.Mock()
        cluster.epoch_length = 15000
        cluster.g_query.get_slot_no.return_value = 100
        cluster.g_query.get_protocol_params.return_value = {"costModels": {"PlutusV3": [1]}}
        state = {}
        accounts = dict(source="source", script="script", collateral="private-collateral")

        def broadcast(**kw):
            self.assertIn("batch", state)
            save.assert_called_once_with(state)

        cluster.g_transaction.submit_tx_bare.side_effect = broadcast
        with (
            mock.patch.object(p, "fixtures", return_value=accounts),
            mock.patch.object(p, "source_ready", return_value=True),
            mock.patch.object(p, "save") as save,
        ):
            self.assertFalse(p.batch_ready(cluster, state))
        kwargs = cluster.g_transaction.build_tx.call_args.kwargs
        self.assertEqual(kwargs["src_address"], "source")
        self.assertFalse(kwargs["join_txouts"])
        self.assertEqual(
            [o.address for o in kwargs["txouts"]],
            ["script"] * p.BATCH_SIZE + ["private-collateral"] * p.BATCH_SIZE,
        )

    def test_all_spends_are_journalled_before_any_broadcast(self):
        from types import SimpleNamespace

        cluster = mock.Mock()
        cluster.epoch_length = 15000
        cluster.g_query.get_slot_no.return_value = 500
        cluster.g_query.get_protocol_params.return_value = dict(
            txFeePerByte=44, maxTxSize=16384, txFeeFixed=155381, collateralPercentage=150
        )
        cluster.g_query.get_utxo.return_value = [
            SimpleNamespace(
                utxo_ix=i,
                coin="lovelace",
                address="script" if i < p.BATCH_SIZE else "collateral",
                amount=p.SCRIPT_AMOUNT,
                inline_datum=42,
            )
            for i in range(2 * p.BATCH_SIZE)
        ]
        state = {"batch": {"txid": "fund"}}
        with (
            mock.patch.object(
                p,
                "fixtures",
                return_value=dict(
                    source="source", collateral="collateral", script="script", recipient="recipient"
                ),
            ),
            mock.patch.object(p.traces, "tx_reference", return_value=("reference", 14000)),
            mock.patch.object(p, "save") as save,
        ):
            p.build_spends(cluster, state)
            save.assert_called_once_with(state)
        cluster.g_transaction.submit_tx_bare.assert_not_called()
        self.assertNotIn("batch", state)
        self.assertEqual(len(state["payments"]), p.BATCH_SIZE)
        self.assertEqual(len({x["script_input"] for x in state["payments"]}), p.BATCH_SIZE)
        self.assertEqual(len({x["collateral"] for x in state["payments"]}), p.BATCH_SIZE)
        self.assertTrue(all(x["expiry"] == 2000 for x in state["payments"]))
        for call in cluster.g_transaction.sign_tx.call_args_list:
            self.assertEqual(
                [x.name for x in call.kwargs["signing_key_files"]], ["collateral.skey"]
            )

    def test_interrupted_batch_build_cannot_broadcast_partial_spends(self):
        from types import SimpleNamespace

        cluster = mock.Mock()
        cluster.epoch_length = 15000
        cluster.g_query.get_slot_no.return_value = 500
        cluster.g_query.get_protocol_params.return_value = dict(
            txFeePerByte=44, maxTxSize=16384, txFeeFixed=155381, collateralPercentage=150
        )
        cluster.g_query.get_utxo.return_value = [
            SimpleNamespace(
                utxo_ix=i,
                coin="lovelace",
                address="script" if i < p.BATCH_SIZE else "collateral",
                amount=p.SCRIPT_AMOUNT,
                inline_datum=42,
            )
            for i in range(2 * p.BATCH_SIZE)
        ]
        cluster.g_transaction.sign_tx.side_effect = [
            pathlib.Path("/tmp/signed"),
            OSError("interrupted"),
        ]
        state = {"batch": {"txid": "fund"}}
        with (
            mock.patch.object(
                p,
                "fixtures",
                return_value=dict(script="script", collateral="collateral", recipient="recipient"),
            ),
            mock.patch.object(p.traces, "tx_reference", return_value=("reference", 14000)),
            mock.patch.object(p, "save") as save,
        ):
            with self.assertRaises(OSError):
                p.build_spends(cluster, state)
            save.assert_not_called()
        cluster.g_transaction.submit_tx_bare.assert_not_called()
        self.assertEqual(state, {"batch": {"txid": "fund"}})

    def test_pending_funding_reuses_signed_bytes_until_immutable_expiry(self):
        cluster = mock.Mock()
        cluster.g_query.get_slot_no.return_value = 9999
        state = {"funding": {"txid": "fund", "expiry": 200}}
        with (
            mock.patch.object(p, "immutable_utxos", return_value=({"slot": 199}, {})),
            mock.patch.object(p.g, "build_sign_submit") as fund,
        ):
            self.assertFalse(p.source_ready(cluster, state, {"source": "private"}))
            fund.assert_not_called()
        cluster.g_transaction.submit_tx_bare.assert_called_once_with(
            tx_file=p.ROOT / "funding.signed"
        )

    def test_setup_guard_registers_coverage_without_submitting(self):
        with (
            mock.patch.object(p.g, "SETUP_MARKER") as marker,
            mock.patch.object(p, "locked") as locked,
            mock.patch.object(p, "sdk") as sdk,
        ):
            marker.exists.return_value = False
            self.assertFalse(p.tick(submit=True))
            locked.assert_not_called()
            sdk.sometimes.assert_called_once_with(
                False, p.COVERAGE, {"reason": "awaiting_immutable_script_certificate"}
            )

    def test_reader_rejects_moving_tip_and_unknown_utxo_schema(self):
        cluster = mock.Mock()
        for outputs in ["{}", "[]"]:
            cluster.g_query.query_cli.side_effect = [
                json.dumps({"hash": "a"}),
                outputs,
                json.dumps({"hash": "b"}),
            ] * 3
            with self.assertRaises(ValueError):
                p.immutable_utxos(cluster, [])

    def test_atomic_journal_roundtrip_under_own_lock(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(p, "ROOT", pathlib.Path(directory)),
            mock.patch.object(p, "STATE", pathlib.Path(directory) / "tracking.json"),
        ):
            with p.locked() as state:
                state["private"] = True
            with p.locked() as state:
                self.assertEqual(state, {"private": True})


if __name__ == "__main__":
    unittest.main()
