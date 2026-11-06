"""SQLite/WAL and recovery evidence regressions, without a paid Antithesis run."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import importlib
import pathlib
import runpy
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

DRIVERS = (
    pathlib.Path(__file__).resolve().parents[1] / "components/gov-cli/composer/governance-python"
)
sys.path.insert(0, str(DRIVERS))
import helper_leios_storage as storage
import helper_leios_transactions as traces
import helper_leios_storage_traffic as traffic

observer = importlib.import_module("anytime_leios_storage")
submitter = importlib.import_module("parallel_driver_leios_storage")

SCHEMA = """
CREATE TABLE ebs(ebSlot INTEGER, ebHashBytes BLOB, missingTxCount INTEGER);
CREATE TABLE ebTxs(ebHashBytes BLOB,txOffset INTEGER,txHashBytes BLOB,txBytesSize INTEGER);
CREATE TABLE txs(txHashBytes BLOB PRIMARY KEY,txBytes BLOB,txBytesSize INTEGER);
"""


class SQLiteEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = pathlib.Path(self.temporary.name) / "leios.db"
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.executescript(SCHEMA)
        self.raw = b"serialized transaction"
        self.tx_hash = hashlib.blake2b(self.raw, digest_size=32).digest()
        eb_hash = traces.eb_body_hash([(self.tx_hash.hex(), len(self.raw))])
        self.proof = {"eb_hash": eb_hash, "eb_slot": 80, "cert_slot": 100, "certifying_rb": "rb"}
        self.writer.execute("INSERT INTO ebs VALUES(?,?,0)", (80, bytes.fromhex(eb_hash)))
        self.writer.execute(
            "INSERT INTO ebTxs VALUES(?,0,?,?)",
            (bytes.fromhex(eb_hash), self.tx_hash, len(self.raw)),
        )
        self.writer.execute(
            "INSERT INTO txs VALUES(?,?,?)", (self.tx_hash, self.raw, len(self.raw))
        )
        self.writer.commit()

    def snapshot(self):
        with mock.patch.object(storage, "DB", self.path):
            values, integrity = storage.read_snapshot([self.proof], True)
        self.assertEqual(integrity, ["ok"])
        return values[self.proof["eb_hash"]]

    def test_live_wal_is_visible_without_checkpoint(self):
        self.assertTrue(pathlib.Path(str(self.path) + "-wal").exists())
        self.assertEqual(self.snapshot()["tx_count"], 1)

    def test_changed_closure_cannot_pass_under_original_hash(self):
        self.writer.execute("UPDATE txs SET txBytes=?", (b"x" * len(self.raw),))
        self.writer.commit()
        self.assertIsNone(self.snapshot())

    def test_missing_closure_is_not_evidence(self):
        self.writer.execute("DELETE FROM txs")
        self.writer.commit()
        self.assertIsNone(self.snapshot())

    def test_wrong_body_order_or_size_is_not_evidence(self):
        self.writer.execute("UPDATE ebTxs SET txOffset=2")
        self.writer.commit()
        self.assertIsNone(self.snapshot())

    def test_malformed_sqlite_value_is_failed_evidence_not_query_unknown(self):
        self.writer.execute("UPDATE txs SET txBytes=123")
        self.writer.commit()
        self.assertIsNone(self.snapshot())

    def test_wrong_announcement_hash_cannot_match_rows(self):
        self.proof["eb_hash"] = "0" * 64
        self.assertIsNone(self.snapshot())

    def test_incomplete_body_is_not_baseline(self):
        self.writer.execute("UPDATE ebs SET missingTxCount=1")
        self.writer.commit()
        self.assertIsNone(self.snapshot())

    def test_missing_database_is_never_created(self):
        missing = self.path.with_name("absent.db")
        with mock.patch.object(storage, "DB", missing), self.assertRaises(sqlite3.OperationalError):
            storage.read_snapshot([], True)
        self.assertFalse(missing.exists())

    def test_read_only_connection_cannot_mutate(self):
        with (
            mock.patch.object(storage, "DB", self.path),
            mock.patch.object(storage, "fingerprint") as fingerprint,
        ):
            fingerprint.side_effect = lambda db, _: db.execute("DELETE FROM txs")
            with self.assertRaises(sqlite3.OperationalError):
                storage.read_snapshot([self.proof], False)
        self.assertEqual(self.writer.execute("SELECT COUNT(*) FROM txs").fetchone()[0], 1)


class RecoveryEvidence(unittest.TestCase):
    def setUp(self):
        self.proof = {"eb_hash": "eb", "cert_slot": 100, "certifying_rb": "rb"}
        self.value = {"body": "eb", "closure": "closure", "tx_count": 2}
        self.state = {}
        self.sdk = mock.patch.object(storage, "sdk").start()
        self.addCleanup(mock.patch.stopall)
        self.assess([1, 2, 3])
        self.baseline = copy.deepcopy(self.state["baseline"])

    def assess(self, socket, *, head=130, tip=120, values=None, proofs=None, integrity=None):
        storage.assess(
            self.state,
            {"hash": "tip", "slot": tip},
            {"slot": head},
            socket,
            proofs if proofs is not None else [self.proof],
            values if values is not None else {"eb": self.value},
            integrity if integrity is not None else ["ok"],
        )

    def test_initial_baseline_is_not_restart_recovery(self):
        self.assertNotIn("restart_slot", self.state)
        self.assertNotIn("recovered", self.state)

    def test_old_certificate_does_not_prove_post_restart_progress(self):
        self.assess([1, 2, 4])
        self.assertEqual(self.state["restart_slot"], 130)
        self.assertFalse(self.state["recovered"])

    def test_restart_behind_baseline_cannot_replay_old_certificate_as_recovery(self):
        self.assess([1, 2, 4], head=60, tip=50)
        self.assess([1, 2, 4], head=130)
        self.assertFalse(self.state["recovered"])

    def test_later_immutable_certificate_completes_recovery(self):
        self.assess([1, 2, 4])
        new = {**self.proof, "eb_hash": "new", "cert_slot": 140}
        self.assess(
            [1, 2, 4], tip=150, head=170, proofs=[new], values={"eb": self.value, "new": self.value}
        )
        self.assertTrue(self.state["recovered"])
        self.assertEqual(self.state["baseline"], self.baseline)

    def test_lost_baseline_is_failure_and_is_never_replaced(self):
        self.assess([1, 2, 4], values={"eb": None})
        self.sdk.always.assert_any_call(
            False, "leios_storage_certified_eb_preserved", self.baseline
        )
        self.assertEqual(self.state["baseline"], self.baseline)
        self.assertFalse(self.state["recovered"])

    def test_catching_up_before_baseline_is_not_loss(self):
        self.sdk.always.reset_mock()
        self.assess([1, 2, 4], tip=90, values={"eb": None}, integrity=[])
        self.sdk.always.assert_not_called()

    def test_failed_integrity_cannot_complete_or_reset_baseline(self):
        self.assess([1, 2, 4], integrity=["corrupt page"])
        self.assertEqual(self.state["baseline"], self.baseline)
        self.sdk.always.assert_any_call(
            False, "leios_storage_integrity", {"result": ["corrupt page"]}
        )

    def test_only_certificates_on_actual_immutable_ancestry_count(self):
        trace = {
            "headers": {
                "tip": {"previous": "rb", "slot": 120},
                "rb": {"previous": "parent", "slot": 100},
            },
            "certificates": {"rb": 100, "orphan": 110},
            "announcements": {"parent": {"eb_hash": "eb", "eb_slot": 80}},
        }
        proofs = storage.certified_ebs(trace, {"hash": "tip"})
        self.assertEqual([p["certifying_rb"] for p in proofs], ["rb"])
        trace["headers"]["rb"]["slot"] = 99
        self.assertEqual(storage.certified_ebs(trace, {"hash": "tip"}), [])

    def test_socket_change_mid_snapshot_does_not_publish_evidence(self):
        cluster = mock.Mock()
        cluster.g_query.query_cli.return_value = '{"hash":"tip", "slot":120}'
        cluster.g_query.get_tip.return_value = {"slot": 130}
        state = {"trace": {"tracking_started": True}}
        with (
            mock.patch.object(storage.traces, "ingest"),
            mock.patch.object(storage.clusterlib, "ClusterLib", return_value=cluster),
            mock.patch.object(storage, "read_snapshot", return_value=({}, ["ok"])),
            mock.patch.object(storage, "identity", side_effect=[[1, 2, 3], [1, 4, 5], [1, 2, 6]]),
            mock.patch.object(storage, "assess") as assess,
        ):
            storage.observe(state)
        assess.assert_not_called()


class Gating(unittest.TestCase):
    def test_faulted_read_keeps_required_coverage_pending(self):
        with (
            mock.patch.object(storage, "enabled", return_value=True),
            mock.patch.object(observer.g.SETUP_MARKER.__class__, "exists", return_value=True),
            mock.patch.object(storage, "locked", return_value=contextlib.nullcontext({})),
            mock.patch.object(storage, "observe", side_effect=OSError("fault")),
            mock.patch.object(observer, "sdk") as sdk,
        ):
            self.assertEqual(observer.main(), 0)
        for property_id in storage.COVERAGE:
            sdk.sometimes.assert_any_call(False, property_id, {"reason": "awaiting_evidence"})
        sdk.sometimes.assert_any_call(
            True, "leios_storage_query_unavailable", {"error": "fault"}, must_hit=False
        )
        sdk.always.assert_not_called()

    def test_lock_contention_does_not_create_a_second_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with (
                mock.patch.object(storage, "ROOT", root),
                mock.patch.object(storage, "STATE", root / "tracking.json"),
            ):
                with storage.locked() as first:
                    first["baseline"] = "retained"
                    with storage.locked() as second:
                        self.assertIsNone(second)
                with storage.locked() as saved:
                    self.assertEqual(saved["baseline"], "retained")

    def test_disabled_or_conway_wrappers_emit_nothing_and_do_not_query(self):
        for flag, leios in [("false", True), ("true", False)]:
            with (
                self.subTest(flag=flag, leios=leios),
                mock.patch.dict("os.environ", LEIOS_STORAGE_CHECKS=flag),
                mock.patch.object(traces, "enabled", return_value=leios),
                mock.patch.object(storage, "locked") as locked,
                mock.patch.object(storage, "observe") as observe,
                mock.patch.object(observer.sdk, "_emit") as emit,
            ):
                for name in [
                    "anytime_leios_storage",
                    "eventually_leios_storage",
                    "parallel_driver_leios_storage",
                ]:
                    with self.assertRaises(SystemExit) as exited:
                        runpy.run_path(str(DRIVERS / f"{name}.py"), run_name="__main__")
                    self.assertEqual(exited.exception.code, 0)
                locked.assert_not_called()
                observe.assert_not_called()
                emit.assert_not_called()


class IsolatedTraffic(unittest.TestCase):
    def test_full_pending_journal_cannot_strand_funds_in_unused_fanouts(self):
        parent = {"traffic": {"payments": [{"expiry": 200}] * traffic.Traffic.MAX_PAYMENTS}}
        cluster = mock.Mock()
        cluster.g_query.query_cli.return_value = '{"slot":100}'
        with (
            mock.patch.object(storage, "enabled", return_value=True),
            mock.patch.object(submitter.g.SETUP_MARKER.__class__, "exists", return_value=True),
            mock.patch.object(storage, "locked", return_value=contextlib.nullcontext(parent)),
            mock.patch.object(submitter.g, "make_cluster", return_value=cluster),
            mock.patch.object(submitter, "submit_payments") as send,
            mock.patch.object(submitter, "sdk"),
        ):
            self.assertEqual(submitter.main(), 0)
        send.assert_not_called()
        self.assertEqual(len(parent["traffic"]["payments"]), traffic.Traffic.MAX_PAYMENTS)

    def test_funding_is_journaled_only_in_storage_namespace_before_submission(self):
        import json

        cluster = mock.Mock()
        cluster.epoch_length = 800
        cluster.g_query.get_tip.return_value = {"slot": 100}
        cluster.g_query.get_utxo.return_value = []
        cluster.g_address.gen_payment_addr_and_keys.return_value = mock.Mock(address="owned")
        parent = {"baseline": "unchanged", "traffic": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with (
                mock.patch.object(storage, "ROOT", root),
                mock.patch.object(storage, "STATE", root / "tracking.json"),
                mock.patch.object(traces, "save") as other_save,
            ):
                adapter = traffic.Traffic(parent)

                def broadcast(*args, **kwargs):
                    kwargs["before_submit"]("funding-tx")
                    persisted = json.loads(storage.STATE.read_text())
                    self.assertEqual(persisted["traffic"]["funding"]["txid"], "funding-tx")
                    self.assertEqual(persisted["baseline"], "unchanged")

                with mock.patch.object(submitter.g, "build_sign_submit", side_effect=broadcast):
                    submitter.submit_payments(cluster, parent["traffic"], workload=adapter)
                other_save.assert_not_called()
                self.assertEqual(adapter.ROOT, root / "payments")

    def test_old_completion_cannot_end_recovery_when_latest_probe_failed(self):
        terminal = importlib.import_module("eventually_leios_storage")
        parent = {"recovered": True, "last_probe_ok": False}
        with (
            mock.patch.object(storage, "enabled", return_value=True),
            mock.patch.object(terminal.g.SETUP_MARKER.__class__, "exists", return_value=True),
            mock.patch.object(terminal.timing, "recovery_checks", return_value=0),
            mock.patch.object(terminal.observer, "main"),
            mock.patch.object(storage, "locked", return_value=contextlib.nullcontext(parent)),
            mock.patch.object(terminal.submitter, "main") as send,
            mock.patch.object(terminal, "sdk"),
        ):
            self.assertEqual(terminal.main(), 0)
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
