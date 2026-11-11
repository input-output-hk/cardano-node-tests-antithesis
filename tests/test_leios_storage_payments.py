"""Recovered-node ledger evidence, independent of the regular relay's ledger."""

from __future__ import annotations

import contextlib
import copy
import json
import pathlib
import sys
import unittest
from unittest import mock

DRIVERS = (
    pathlib.Path(__file__).resolve().parents[1] / "components/gov-cli/composer/governance-python"
)
sys.path.insert(0, str(DRIVERS))
import helper_leios_storage as storage


class StoragePaymentEvidence(unittest.TestCase):
    def setUp(self):
        self.cert = "a" * 64
        previous = "b" * 64
        self.proof = {"eb_hash": "eb", "eb_slot": 80, "certifying_rb": self.cert, "cert_slot": 100}
        self.payment = {
            "txid": "private",
            "reference": "serialized-ref",
            "size": 735,
            "recipient": "private-recipient",
            "amount": 2_000_000,
            "ebs": {"eb": 735},
        }
        self.tip = {"hash": self.cert, "slot": 100}
        self.state = {
            "trace": {
                "tracking_started": True,
                "headers": {
                    self.cert: {"previous": previous, "slot": 100},
                    previous: {"previous": "c" * 64, "slot": 90},
                },
                "certificates": {self.cert: 100},
                "announcements": {previous: {"eb_hash": "eb", "eb_slot": 80}},
            },
            "traffic": {"payments": [self.payment]},
        }
        self.utxos = {
            "private#0": {"address": "private-recipient", "value": {"lovelace": 2_000_000}}
        }
        self.sdk = mock.patch.object(storage, "sdk").start()
        self.addCleanup(mock.patch.stopall)

    def test_candidate_requires_a_certificate_on_this_nodes_immutable_chain(self):
        candidate = storage.payment_candidate(self.state, self.tip)
        self.assertEqual(candidate["proof"], self.proof)
        self.assertIsNone(storage.payment_candidate(self.state, {"hash": "orphan", "slot": 200}))
        self.state["trace"]["certificates"].clear()
        self.assertIsNone(storage.payment_candidate(self.state, self.tip))

    def test_other_workloads_eb_cannot_supply_our_payment_witness(self):
        self.payment["ebs"] = {"unrelated-eb": 735}
        self.assertIsNone(storage.payment_candidate(self.state, self.tip))

    def test_committed_payment_survives_traffic_retirement_but_waits_for_catchup(self):
        candidate = storage.payment_candidate(self.state, self.tip)
        storage.assess_payment(self.state, candidate, self.tip, self.utxos)
        self.state["traffic"]["payments"].clear()
        self.assertEqual(storage.payment_candidate(self.state, self.tip), candidate)
        self.assertIsNone(storage.payment_candidate(self.state, {"slot": 99}))
        self.sdk.sometimes.assert_not_called()  # No recovery has happened yet.

    def test_storage_progress_alone_is_not_complete_ledger_recovery(self):
        self.state.update(recovered=True, last_probe_ok=True, last_payment_ok=False)
        self.assertFalse(storage.recovery_complete(self.state))
        storage.assess_payment(
            self.state, storage.payment_candidate(self.state, self.tip), self.tip, self.utxos
        )
        self.assertTrue(storage.recovery_complete(self.state))
        self.sdk.sometimes.assert_called_once()
        self.assertEqual(
            self.sdk.sometimes.call_args.args[:2], (True, "leios_storage_recovered_payment")
        )

    def test_missing_or_incorrect_output_fails_the_safety_check(self):
        candidate = storage.payment_candidate(self.state, self.tip)
        wrong = [
            {},
            {"private#0": {"address": "foreign", "value": {"lovelace": 2_000_000}}},
            {"private#0": {"address": "private-recipient", "value": {"lovelace": 1}}},
        ]
        for utxos in wrong:
            self.state.update(recovered=True, last_probe_ok=True, last_payment_ok=True)
            storage.assess_payment(self.state, candidate, self.tip, utxos)
            self.assertFalse(storage.recovery_complete(self.state))
            self.assertEqual(
                self.sdk.always.call_args.args[:2], (False, "leios_storage_payment_matches")
            )

    def observe(self, *, after=None, socket_changed=False, snapshots=True, response=None):
        cluster = mock.Mock()
        cluster.g_query.get_tip.return_value = {"slot": 130}
        replies = [json.dumps(self.tip)]
        if snapshots:
            replies.append(json.dumps(self.utxos if response is None else response))
        replies.append(json.dumps(after or self.tip))
        cluster.g_query.query_cli.side_effect = replies
        identities = [[1, 2, 3], [1, 4, 5], [1, 2, 4] if socket_changed else [1, 2, 3], [1, 4, 5]]
        with (
            mock.patch.object(storage.traces, "ingest"),
            mock.patch.object(
                storage.clusterlib, "ClusterLib", return_value=cluster
            ) as constructor,
            mock.patch.object(storage, "identity", side_effect=identities),
            mock.patch.object(storage, "certified_ebs", return_value=[self.proof]),
            mock.patch.object(
                storage,
                "read_snapshot",
                return_value=({"eb": {"body": "eb"} if snapshots else None}, ["ok"]),
            ),
        ):
            storage.observe(self.state)
        self.assertEqual(constructor.call_args.kwargs["socket_path"], storage.SOCKET)
        return cluster

    def test_utxo_is_read_at_the_storage_nodes_immutable_point(self):
        cluster = self.observe()
        cluster.g_query.query_cli.assert_any_call(
            ["utxo", "--tx-in", "private#0", "--output-json"], cli_sub_args=["--immutable-tip"]
        )
        self.assertTrue(self.state["last_payment_ok"])
        self.assertNotIn("payments", self.state["trace"])

    def test_moving_point_or_restarted_socket_cannot_publish_a_witness(self):
        for kwargs in [{"after": {"hash": "moved", "slot": 101}}, {"socket_changed": True}]:
            self.sdk.reset_mock()
            self.observe(**kwargs)
            self.assertFalse(self.state["last_payment_ok"])
            self.sdk.always.assert_not_called()

    def test_absent_closure_is_unknown_not_a_ledger_safety_failure(self):
        cluster = self.observe(snapshots=False)
        self.assertFalse(self.state["last_payment_ok"])
        self.assertEqual(cluster.g_query.query_cli.call_count, 2)
        self.assertFalse(
            any(
                c.args[1] == "leios_storage_payment_matches" for c in self.sdk.always.call_args_list
            )
        )

    def test_unknown_query_schema_cannot_leave_cached_payment_success(self):
        self.state["last_payment_ok"] = True
        with self.assertRaises(TypeError):
            self.observe(response=[])
        self.assertFalse(self.state["last_payment_ok"])
        self.sdk.always.assert_not_called()

    def test_traffic_continues_after_sqlite_recovery_until_ledger_is_checked(self):
        import parallel_driver_leios_storage as submitter

        self.state.update(recovered=True, last_probe_ok=True, last_payment_ok=False)
        with (
            mock.patch.object(storage, "enabled", return_value=True),
            mock.patch.object(submitter.g.SETUP_MARKER.__class__, "exists", return_value=True),
            mock.patch.object(storage, "locked", return_value=contextlib.nullcontext(self.state)),
            mock.patch.object(submitter, "supply") as supply,
            mock.patch.object(submitter, "sdk"),
        ):
            self.assertEqual(submitter.main(), 0)
        supply.assert_called_once_with(self.state)

    def test_recovery_waits_for_a_fresh_ledger_probe(self):
        import eventually_leios_storage as terminal

        self.state.update(recovered=True, last_probe_ok=True, last_payment_ok=False)
        states = []

        def observe():
            states.append(copy.deepcopy(self.state))
            if len(states) == 3:
                self.state["last_payment_ok"] = True

        with (
            mock.patch.object(storage, "enabled", return_value=True),
            mock.patch.object(terminal.g.SETUP_MARKER.__class__, "exists", return_value=True),
            mock.patch.object(
                storage, "locked", side_effect=lambda: contextlib.nullcontext(self.state)
            ),
            mock.patch.object(terminal.timing, "recovery_checks", return_value=1),
            mock.patch.object(terminal.observer, "main", side_effect=observe),
            mock.patch.object(terminal.submitter, "main") as submit,
            mock.patch.object(terminal.time, "sleep"),
            mock.patch.object(terminal, "sdk"),
        ):
            self.assertEqual(terminal.main(), 0)
        submit.assert_called_once()
        self.assertEqual(len(states), 3)


if __name__ == "__main__":
    unittest.main()
