"""Propagation joins must survive scheduling gaps, restarts and bounded reads."""

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
import helper_leios_endorser as e

anytime = importlib.import_module("anytime_leios_endorser")
terminal = importlib.import_module("eventually_leios_endorser")


def forged(host="p1", block="eb", slot=10, count=1):
    return {
        "host": host,
        "ns": "Consensus.LeiosKernel.BlockForged",
        "data": {"hash": block, "slot": slot, "numTxs": count},
    }


def acquired(host="p2", block="eb", slot=10):
    return {
        "host": host,
        "ns": "Consensus.LeiosKernel.BlockAcquired",
        "data": {"ebHash": block, "ebSlot": slot},
    }


class EndorserEvidence(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = pathlib.Path(temporary)
        self.enterContext(mock.patch.object(e, "TRACER_LOGS", self.root / "logs"))
        self.enterContext(mock.patch.object(e, "ROOT", self.root / "state"))
        self.enterContext(mock.patch.object(e.g, "NUM_POOLS", 3))
        self.enterContext(
            mock.patch.object(e.g, "make_cluster", return_value=mock.Mock())
        ).return_value.g_query.get_slot_no.return_value = 0
        self.log = self.root / "logs/p1/node-1.json"
        self.log.parent.mkdir(parents=True)
        self.state = {}

    def append(self, *events):
        with self.log.open("a") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def test_acquisition_between_invocations_still_joins_its_forge(self):
        self.append(forged())
        self.assertFalse(e.observe(self.state)[0])
        self.append(acquired())
        complete, evidence = e.observe(self.state)
        self.assertTrue(complete)
        self.assertEqual((evidence["forger"], evidence["acquirer"]), ("p1", "p2"))

    def test_acquisition_read_before_forge_is_not_lost(self):
        e.ingest(self.state, acquired())
        self.assertNotIn("witness", self.state)
        e.ingest(self.state, forged())
        self.assertEqual(self.state["witness"]["eb_hash"], "eb")

    def test_wrong_slot_same_producer_empty_block_or_foreign_hash_cannot_match(self):
        for forge, acquire in [
            (forged(), acquired(slot=11)),
            (forged(), acquired(host="p1")),
            (forged(count=0), acquired()),
            (forged(), acquired(block="other")),
        ]:
            state = {}
            e.ingest(state, forge)
            e.ingest(state, acquire)
            self.assertNotIn("witness", state)

    def test_only_configured_producers_and_both_hostname_forms_count(self):
        for host in ["relay1", "p4", "p1.example.example", None]:
            self.assertEqual(e.producer({"host": host}), "")
        e.ingest(self.state, forged(host="p1.example"))
        e.ingest(self.state, acquired(host="p2.example"))
        self.assertIn("witness", self.state)

    def test_rotation_and_replaced_inode_are_read_from_the_start(self):
        self.append(forged())
        e.observe(self.state)
        self.log.rename(self.log.with_name("node-0.json"))
        self.append(acquired())
        self.assertTrue(e.observe(self.state)[0])

    def test_partial_last_record_is_retried_after_append(self):
        self.append(forged())
        raw = json.dumps(acquired())
        with self.log.open("a") as stream:
            stream.write(raw[:40])
        self.assertFalse(e.observe(self.state)[0])
        with self.log.open("a") as stream:
            stream.write(raw[40:])
        self.assertTrue(e.observe(self.state)[0])

    def test_bounded_read_preserves_earlier_evidence_and_drains_backlog(self):
        self.append(forged(), *[{"padding": "x" * 100} for _ in range(8)], acquired())
        with mock.patch.object(e, "MAX_READ_BYTES", 250):
            for _ in range(20):
                complete, details = e.observe(self.state)
                if complete:
                    break
            self.assertTrue(complete)
            self.assertFalse(details["backlog"])

    def test_restart_reads_journal_and_does_not_replay_whole_logs(self):
        self.append(forged())
        with e.locked() as state:
            e.observe(state)
        self.append(acquired())
        with e.locked() as state:
            self.assertTrue(e.observe(state)[0])
        with e.locked() as state:
            complete, details = e.observe(state)
            self.assertTrue(complete)
            self.assertEqual(details["bytes_read"], 0)

    def test_oversized_unrelated_record_cannot_block_later_propagation(self):
        self.append(forged(), {"padding": "x" * 3000}, acquired())
        with mock.patch.object(e, "MAX_READ_BYTES", 250):
            for _ in range(30):
                complete, _ = e.observe(self.state)
                if complete:
                    break
            self.assertTrue(complete)

    def test_global_read_budget_does_not_starve_another_producer(self):
        self.append(forged(), *[{"padding": "x" * 100} for _ in range(10)])
        other = self.log.parent.parent / "p2/node-1.json"
        other.parent.mkdir()
        other.write_text(json.dumps(acquired()) + "\n")
        with (
            mock.patch.object(e, "MAX_READ_BYTES", 250),
            mock.patch.object(e, "MAX_TOTAL_BYTES", 250),
        ):
            self.assertFalse(e.observe(self.state)[0])
            self.assertTrue(e.observe(self.state)[0])

    def test_bounded_metadata_cannot_manufacture_a_match(self):
        with mock.patch.object(e, "MAX_BLOCKS", 2):
            for i in range(3):
                e.ingest(self.state, forged(block=str(i)))
            self.assertEqual(len(self.state["blocks"]), 2)
            e.ingest(self.state, acquired(block="0"))
            self.assertNotIn("witness", self.state)

    def test_missing_logs_are_pending_not_a_witness(self):
        self.assertFalse(e.observe(self.state)[0])

    def test_supply_uses_only_private_accounts_and_persists_its_own_journal(self):
        cluster = mock.Mock()
        cluster.g_query.query_cli.return_value = '{"slot": 100}'
        cluster.g_query.get_slot_no.return_value = 110
        with (
            mock.patch.object(e.g, "make_cluster", return_value=cluster),
            mock.patch.object(e, "submit_payments") as submit,
        ):
            e.supply(self.state)
            workload = submit.call_args.kwargs["workload"]
            self.assertEqual(workload.ROOT, e.ROOT / "payments")
            self.assertNotEqual(workload.ROOT, e.payments.ROOT)
            workload.save({"payments": [{"signed": "own.signed", "expiry": 200}]})
        saved = json.loads((e.ROOT / "tracking.json").read_text())
        self.assertEqual(saved["traffic"]["payments"][0]["signed"], "own.signed")

    def test_supply_retries_exact_bytes_and_only_immutable_expiry_retires_them(self):
        self.state["traffic"] = {"payments": [{"signed": "own.signed", "expiry": 200}]}
        cluster = mock.Mock()
        cluster.g_query.query_cli.return_value = '{"slot": 100}'
        cluster.g_query.get_slot_no.return_value = 150
        with (
            mock.patch.object(e.g, "make_cluster", return_value=cluster),
            mock.patch.object(e, "submit_payments"),
        ):
            e.supply(self.state)
            cluster.g_transaction.submit_tx_bare.assert_called_once_with(tx_file="own.signed")
            cluster.g_query.get_slot_no.return_value = 999
            e.supply(self.state)
            self.assertEqual(len(self.state["traffic"]["payments"]), 1)
            cluster.g_query.query_cli.return_value = '{"slot": 200}'
            e.supply(self.state)
            self.assertEqual(self.state["traffic"]["payments"], [])

    def test_full_pending_batch_cannot_create_an_unused_fanout(self):
        self.state["traffic"] = {
            "payments": [{"signed": "pending.signed", "expiry": 200}] * e.Traffic.MAX_PAYMENTS
        }
        cluster = mock.Mock()
        cluster.g_query.query_cli.return_value = '{"slot": 100}'
        cluster.g_query.get_slot_no.return_value = 150
        with (
            mock.patch.object(e.g, "make_cluster", return_value=cluster),
            mock.patch.object(e, "submit_payments") as submit,
        ):
            e.supply(self.state)
            submit.assert_not_called()

    def test_old_startup_pair_cannot_satisfy_exploration(self):
        e.g.make_cluster.return_value.g_query.get_slot_no.return_value = 20
        self.append(forged(slot=10), acquired(slot=10))
        self.assertFalse(e.observe(self.state)[0])
        self.append(forged(slot=21), acquired(slot=21))
        self.assertTrue(e.observe(self.state)[0])

    def test_unknown_starting_slot_cannot_advance_cursors_or_count_old_pairs(self):
        e.g.make_cluster.return_value.g_query.get_slot_no.side_effect = OSError("query fault")
        self.append(forged(), acquired())
        with self.assertRaises(OSError):
            e.observe(self.state)
        self.assertEqual(self.state, {})


class EndorserScheduling(unittest.TestCase):
    def test_conway_has_no_properties_queries_or_state(self):
        with (
            mock.patch.object(e, "enabled", return_value=False),
            mock.patch.object(e, "locked") as lock,
            mock.patch.object(anytime, "sdk") as sdk,
            mock.patch.object(terminal, "sdk") as terminal_sdk,
        ):
            self.assertEqual(anytime.main(), 0)
            self.assertEqual(terminal.main(), 0)
            lock.assert_not_called()
            self.assertEqual(sdk.mock_calls, [])
            self.assertEqual(terminal_sdk.mock_calls, [])

    def test_terminal_continues_without_any_anytime_invocation(self):
        with (
            mock.patch.object(e, "enabled", return_value=True),
            mock.patch.object(e.g, "SETUP_MARKER") as marker,
            mock.patch.object(e, "locked", side_effect=lambda: contextlib.nullcontext({})),
            mock.patch.object(e, "observe", side_effect=[(False, {}), (True, {"eb_hash": "real"})]),
            mock.patch.object(e, "supply") as supply,
            mock.patch.object(terminal.timing, "recovery_checks", return_value=2),
            mock.patch.object(terminal.time, "sleep") as sleep,
            mock.patch.object(terminal, "sdk") as sdk,
        ):
            marker.exists.return_value = True
            self.assertEqual(terminal.main(), 0)
            sleep.assert_called_once()
            supply.assert_called_once()
            sdk.sometimes.assert_any_call(True, e.PROPERTY, {"eb_hash": "real"})

    def test_missing_setup_still_registers_mandatory_leios_property(self):
        with (
            mock.patch.object(e, "enabled", return_value=True),
            mock.patch.object(e.g, "SETUP_MARKER") as marker,
            mock.patch.object(e, "locked") as lock,
            mock.patch.object(terminal, "sdk") as sdk,
        ):
            marker.exists.return_value = False
            terminal.main()
            lock.assert_not_called()
            sdk.sometimes.assert_called_once_with(False, e.PROPERTY, mock.ANY)


if __name__ == "__main__":
    unittest.main()
