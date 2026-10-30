"""Regressions for certified-EB attribution and fault-safe observation."""

from __future__ import annotations

import contextlib
import hashlib
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
import helper_leios_transactions as t

observer = importlib.import_module("anytime_leios_transactions")
submitter = importlib.import_module("parallel_driver_leios_transactions")


class CertifiedPayments(unittest.TestCase):
    def setUp(self):
        self.payment = {
            "txid": "transaction",
            "reference": "1" * 64,
            "size": 250,
            "recipient": "recipient",
            "amount": 2_000_000,
            "expiry": 200,
            "ebs": {"eb": 250},
        }
        self.state = {
            "payments": [self.payment],
            "headers": {
                "tip": {"previous": "rb", "slot": 120},
                "rb": {"previous": "parent", "slot": 100},
            },
            "announcements": {"parent": {"eb_hash": "eb", "eb_slot": 90}},
            "certificates": {"rb": 100},
        }
        self.tip = {"hash": "tip", "slot": 120}
        self.outputs = {
            "transaction#0": {
                "address": "recipient",
                "value": {"lovelace": 2_000_000},
            }
        }

    def proof(self):
        return t.certified_payment(
            self.state, self.payment, t.immutable_ancestors(self.state, self.tip["hash"])
        )

    def test_linked_immutable_certificate_proves_payment(self):
        self.assertEqual(self.proof()["certifying_rb"], "rb")
        with mock.patch.object(observer, "sdk") as sdk:
            observer.observe(self.state, self.tip, self.outputs)
        self.assertTrue(self.state["complete"])
        self.assertTrue(all(c.args[0] for c in sdk.always.call_args_list))

    def test_ordinary_block_payment_is_not_eb_coverage(self):
        self.state["certificates"] = {}
        with mock.patch.object(observer, "sdk") as sdk:
            observer.observe(self.state, self.tip, self.outputs)
        sdk.always.assert_not_called()
        sdk.sometimes.assert_not_called()

    def test_orphaned_certificate_is_not_evidence(self):
        self.state["certificates"] = {"fork": 100}
        self.assertIsNone(self.proof())

    def test_missing_header_does_not_assume_ancestry(self):
        self.state["headers"] = {}
        self.assertIsNone(self.proof())

    def test_same_slot_aborted_certificate_cannot_bind_another_block(self):
        self.state["certificates"] = {"aborted_header_hash": 100}
        self.assertIsNone(self.proof())

    def test_announcement_on_another_parent_cannot_bind_payment(self):
        self.state["announcements"] = {"other_parent": {"eb_hash": "eb", "eb_slot": 90}}
        self.assertIsNone(self.proof())

    def test_pre_forge_certificate_event_is_not_a_header_certificate(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "p1").mkdir()
            event = {
                "host": "p1",
                "data": {
                    "kind": "LeiosBlockCertified",
                    "atSlot": 100,
                    "ebSlot": 90,
                    "ebHash": "eb",
                },
            }
            (root / "p1/node-test.json").write_text(json.dumps(event) + "\n")
            with (
                mock.patch.object(t, "LOGS", root),
                mock.patch.object(t, "HUMAN_LOGS", root / "absent"),
            ):
                state = {"tracking_started": True}
                t.ingest(state)
            path = root / "p1/node-test.json"
            self.assertEqual(state["cursors"][str(path)][1], path.stat().st_size)
            self.assertEqual(state["certificates"], {})

    def test_missing_output_fails_only_with_immutable_proof(self):
        with mock.patch.object(observer, "sdk") as sdk:
            observer.observe(self.state, self.tip, {})
        sdk.always.assert_any_call(False, "leios_certified_tx_payment_matches", mock.ANY)
        self.assertNotIn("complete", self.state)

    def test_changed_output_amount_is_a_failure(self):
        self.outputs["transaction#0"]["value"]["lovelace"] += 1
        with mock.patch.object(observer, "sdk") as sdk:
            observer.observe(self.state, self.tip, self.outputs)
        sdk.always.assert_any_call(False, "leios_certified_tx_payment_matches", mock.ANY)

    def test_unproven_records_expire_only_at_immutable_bound(self):
        self.state["certificates"] = {}
        with mock.patch.object(observer, "sdk"):
            observer.observe(self.state, self.tip, {})
            self.assertEqual(len(self.state["payments"]), 1)
            observer.observe(self.state, {"hash": "tip", "slot": 200}, {})
        self.assertEqual(self.state["payments"], [])

    def test_retained_proof_detects_later_missing_output(self):
        with mock.patch.object(observer, "sdk") as sdk:
            observer.observe(self.state, self.tip, self.outputs)
            self.state["headers"] = {}
            self.state["certificates"] = {}
            observer.observe(self.state, self.tip, {})
        sdk.always.assert_any_call(False, "leios_certified_tx_payment_matches", mock.ANY)

    def test_unknown_query_does_not_run_payment_assertions(self):
        with (
            mock.patch.object(t, "enabled", return_value=True),
            mock.patch.object(observer.g.SETUP_MARKER.__class__, "exists", return_value=True),
            mock.patch.object(t, "locked") as locked,
            mock.patch.object(t, "ingest"),
            mock.patch.object(t, "immutable_payments", side_effect=ValueError("unavailable")),
            mock.patch.object(observer.g, "make_cluster"),
            mock.patch.object(observer, "sdk") as sdk,
        ):
            locked.return_value.__enter__.return_value = self.state
            self.assertEqual(observer.main(), 0)
        sdk.always.assert_not_called()
        self.assertEqual(len(self.state["payments"]), 1)

    def test_conway_registers_no_properties(self):
        for driver in (observer, submitter):
            with (
                mock.patch.object(t, "enabled", return_value=False),
                mock.patch.object(driver, "sdk") as sdk,
            ):
                self.assertEqual(driver.main(), 0)
            self.assertEqual(sdk.mock_calls, [])

    def test_advancing_immutable_point_is_unknown(self):
        cluster = mock.Mock()
        cluster.g_query.query_cli.side_effect = [
            json.dumps({"hash": "a"}),
            "{}",
            json.dumps({"hash": "b"}),
        ] * 3
        with self.assertRaises(ValueError):
            t.immutable_payments(cluster, [self.payment])

    def test_partial_human_trace_cannot_prove_membership(self):
        text = "EB forged at slot SlotNo 1: MkLeiosEb {leiosEbTxs = [(" + "1" * 64 + ",250)]}"
        self.assertIsNotNone(t.eb_references(text))
        self.assertIsNone(t.eb_references(text[:-1]))
        self.assertIsNone(t.eb_references(text.replace(",250)", ",250),garbage")))

    def test_eb_hash_uses_complete_ordered_cbor_body(self):
        text = "EB forged at slot SlotNo 1: MkLeiosEb {leiosEbTxs = [(" + "1" * 64 + ",250)]}"
        raw = bytes.fromhex("a15820" + "1" * 64 + "18fa")
        self.assertEqual(t.eb_references(text)[0], hashlib.blake2b(raw, digest_size=32).hexdigest())

    def test_partial_log_record_is_retried_on_append(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "trace"
            path.write_text('first\n{"partial":')
            cursors = {}
            self.assertEqual(t._lines(path, cursors), ["first"])
            with path.open("a") as f:
                f.write("true}\n")
            self.assertEqual(t._lines(path, cursors), ['{"partial":true}'])

    def test_setup_backlog_cannot_hide_a_fresh_certified_payment(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            machine, human = root / "machine/p1", root / "human/p1"
            machine.mkdir(parents=True)
            human.mkdir(parents=True)
            machine_file, human_file = machine / "node-test.json", human / "node-test.log"
            machine_file.write_text("{}\n" * 4096)
            human_file.write_text("setup history\n" * 4096)
            state = {}
            with (
                mock.patch.object(t, "LOGS", root / "machine"),
                mock.patch.object(t, "HUMAN_LOGS", root / "human"),
                mock.patch.object(t, "MAX_READ", 1024),
            ):
                # First observer runs before this workload has broadcast.
                t.ingest(state)
                for path in (machine_file, human_file):
                    self.assertEqual(state["cursors"][str(path)][1], path.stat().st_size)
                state["payments"] = [self.payment]
                body = (
                    "EB forged at slot SlotNo 90: MkLeiosEb {leiosEbTxs = [(" + "1" * 64 + ",250)]}"
                )
                eb_hash = t.eb_references(body)[0]
                block, parent = "3" * 64, "4" * 64
                events = [
                    {"kind": "TraceForgedBlock", "block": block, "blockPrev": parent, "slot": 100},
                    {
                        "kind": "LeiosBlockAnnounced",
                        "rbHash": parent,
                        "ebHash": eb_hash,
                        "ebSlot": 90,
                    },
                    {"kind": "LeiosCertifiedAndAnnounced", "rbHash": block, "slotNo": 100},
                ]
                with machine_file.open("a") as stream:
                    for event in events:
                        stream.write(json.dumps({"host": "p1", "data": event}) + "\n")
                with human_file.open("a") as stream:
                    stream.write(body + "\n")
                t.ingest(state)
                proof = t.certified_payment(
                    state, self.payment, t.immutable_ancestors(state, block)
                )
                self.assertEqual(proof["eb_hash"], eb_hash)

    def test_existing_journal_keeps_unread_evidence_after_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "p1").mkdir()
            path = root / "p1/node-test.log"
            body = "EB forged at slot SlotNo 90: MkLeiosEb {leiosEbTxs = [(" + "1" * 64 + ",250)]}"
            path.write_text(body + "\n")
            state = {"payments": [self.payment]}
            with (
                mock.patch.object(t, "LOGS", root / "absent"),
                mock.patch.object(t, "HUMAN_LOGS", root),
            ):
                t.ingest(state)
            self.assertIn(t.eb_references(body)[0], self.payment["ebs"])

    def test_submitter_checkpoints_before_first_broadcast(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "p1").mkdir()
            path = root / "p1/node-test.json"
            path.write_text("setup\n" * 4096)
            state = {}
            checkpoints = []

            def broadcast(_cluster, journal):
                checkpoints.append(
                    (journal.get("tracking_started"), journal.get("cursors", {}).get(str(path)))
                )

            with (
                mock.patch.object(t, "enabled", return_value=True),
                mock.patch.object(t, "LOGS", root),
                mock.patch.object(t, "HUMAN_LOGS", root / "absent"),
                mock.patch.object(t, "locked", return_value=contextlib.nullcontext(state)),
                mock.patch.object(submitter.g.SETUP_MARKER.__class__, "exists", return_value=True),
                mock.patch.object(submitter.g, "make_cluster"),
                mock.patch.object(submitter, "submit_payments", side_effect=broadcast) as send,
                mock.patch.object(submitter, "sdk"),
            ):
                self.assertEqual(submitter.main(), 0)
                send.assert_called_once()
            self.assertEqual(checkpoints, [(True, [path.stat().st_ino, path.stat().st_size])])


if __name__ == "__main__":
    unittest.main()
