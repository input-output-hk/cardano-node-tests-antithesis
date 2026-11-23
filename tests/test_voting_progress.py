"""Leios submission stalls, independent recovery and Conway gating."""

from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

DRIVERS = (
    pathlib.Path(__file__).resolve().parents[1] / "components/gov-cli/composer/governance-python"
)
sys.path.insert(0, str(DRIVERS))
p = importlib.import_module("helper_voting_progress")


class BoundedVoting(unittest.TestCase):
    def setUp(self):
        self.cluster = mock.Mock(
            command_era="dijkstra", magic_args=["--testnet-magic", "42"], socket_args=[]
        )
        self.original_cli = self.cluster.cli
        self.original_submit = self.cluster.g_transaction.submit_tx
        self.cluster.g_transaction.get_txid.return_value = "signed-id"
        for patch in (
            mock.patch.object(p.g, "make_cluster", return_value=self.cluster),
            mock.patch.object(p.g, "GOV_COMMAND_ERA", "dijkstra"),
            mock.patch.object(p.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_stalled_chain_yields_without_waiting_for_blocks_or_losing_source_lock(self):
        c = p.make_cluster()
        c.g_query.get_utxo.return_value = ["still-unspent"]
        with self.assertRaises(TimeoutError):
            c.g_transaction.submit_tx(tx_file="same.signed", txins=["input"])
        self.assertEqual(c.g_query.get_utxo.call_count, p.SUBMISSION_CHECKS)
        self.original_submit.assert_not_called()
        c.wait_for_new_block.assert_not_called()
        # One exact submission, rather than clusterlib's twenty block waits.
        self.original_cli.assert_called_once()

    def test_inclusion_does_not_require_two_more_blocks(self):
        c = p.make_cluster()
        c.g_query.get_utxo.side_effect = [["input"], []]
        self.assertEqual(c.g_transaction.submit_tx("same.signed", ["input"]), "signed-id")
        self.assertEqual(c.g_query.get_utxo.call_count, 2)
        c.wait_for_new_block.assert_not_called()

    def test_each_cli_call_has_a_process_group_deadline(self):
        c = p.make_cluster()
        c.cli(["query", "gov-state"], timeout=600)
        self.original_cli.assert_called_with(
            ["timeout", "--kill-after=2s", "20s", "cardano-cli", "dijkstra", "query", "gov-state"],
            add_default_args=False,
        )
        c.cli(["cardano-cli", "version"], timeout=2, add_default_args=False)
        self.original_cli.assert_called_with(
            ["timeout", "--kill-after=2s", "2s", "cardano-cli", "version"], add_default_args=False
        )

    def test_conway_cluster_and_submission_are_unchanged(self):
        with mock.patch.object(p.g, "GOV_COMMAND_ERA", "conway"):
            c = p.make_cluster()
        self.assertIs(c.cli, self.original_cli)
        self.assertIs(c.g_transaction.submit_tx, self.original_submit)

    def test_deadline_releases_locks_held_by_a_stuck_cli_and_its_child(self):
        import fcntl

        def original_cli(args, **_kwargs):
            # Match clusterlib's Popen context: a Python communicate timeout
            # alone would block in __exit__. The external deadline must kill
            # the whole process group and make this return.
            with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                child.communicate(timeout=6)

        self.cluster.cli = original_cli
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(p, "CLI_SECONDS", 0.2):
            path = pathlib.Path(directory) / "held.lock"
            program = (
                "import fcntl,os,signal,sys,time;"
                "f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "os.fork();time.sleep(60)"
            )
            c = p.make_cluster()
            c.cli([sys.executable, "-c", program, str(path)], add_default_args=False)
            self.assertTrue(path.exists())
            with path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


class Recovery(unittest.TestCase):
    def setUp(self):
        self.voter = mock.Mock()
        self.cluster = mock.Mock()
        self.cluster.g_query.get_epoch.return_value = 1
        self.cluster.g_query.get_slot_no.return_value = 1
        self.sdk = mock.Mock()
        for patch in (
            mock.patch.object(p.g, "GOV_COMMAND_ERA", "dijkstra"),
            mock.patch.object(p.g, "SETUP_MARKER", mock.Mock(exists=lambda: True)),
            mock.patch.object(p, "make_cluster", return_value=self.cluster),
            mock.patch.object(p, "recovery_limits", return_value=(2, 100)),
            mock.patch.object(p.importlib, "import_module", return_value=self.voter),
            mock.patch.object(p, "sdk", self.sdk),
            mock.patch.object(p.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.prop = {
            "expiresAfter": 2,
            "proposalProcedure": {"govAction": {"tag": "ParameterChange"}},
            "dRepVotes": {str(i): "VoteYes" for i in range(5)},
            "committeeVotes": {"cc": "VoteNo"},
            "stakePoolVotes": {},
        }

    def test_recovery_retries_unavailable_then_empty_queries_before_real_votes(self):
        with mock.patch.object(
            p.g,
            "live_info_actions",
            side_effect=[
                OSError("fault"),
                [],
                [{**self.prop, "stakePoolVotes": {"spo": "VoteAbstain"}}],
            ],
        ):
            self.assertEqual(p.recover("info"), 0)
        self.assertEqual(self.voter.main.call_count, 3)
        self.sdk.sometimes.assert_any_call(False, "actions_live", {"live": 0})
        self.sdk.sometimes.assert_any_call(True, "actions_live", {"live": 1})

    def test_submission_success_without_recorded_votes_cannot_finish_recovery(self):
        empty = {**self.prop, "dRepVotes": {}, "committeeVotes": {}}
        self.cluster.g_query.get_gov_state.return_value = {"proposals": [empty]}
        self.assertEqual(p.recover("pparam"), 0)
        self.assertEqual(self.voter.main.call_count, 3)

    def test_expired_majority_does_not_finish_recovery(self):
        self.cluster.g_query.get_gov_state.side_effect = [
            {"proposals": [{**self.prop, "expiresAfter": 0}]},
            {"proposals": [self.prop]},
        ]
        self.assertEqual(p.recover("pparam"), 0)
        self.assertEqual(self.voter.main.call_count, 2)

    def test_conway_does_not_run_new_recovery_or_query_anything(self):
        with mock.patch.object(p.g, "GOV_COMMAND_ERA", "conway"):
            self.assertEqual(p.recover("info"), 0)
            self.assertEqual(p.recover("pparam"), 0)
        self.voter.main.assert_not_called()
        self.cluster.g_query.get_gov_state.assert_not_called()

    def test_submission_waits_are_included_in_the_slot_budget(self):
        self.cluster.g_query.get_slot_no.side_effect = [100, 200]
        self.cluster.g_query.get_gov_state.return_value = {"proposals": []}
        self.assertEqual(p.recover("pparam"), 0)
        self.voter.main.assert_called_once()

    def test_a_fully_stalled_chain_has_a_small_step_limit(self):
        self.cluster.g_query.get_gov_state.return_value = {"proposals": []}
        with mock.patch.object(p, "recovery_limits", return_value=(240, 6000)):
            self.assertEqual(p.recover("pparam"), 0)
        self.assertEqual(self.voter.main.call_count, p.MAX_STALLED_STEPS)

    def test_unavailable_tip_does_not_wait_through_hundreds_of_voter_invocations(self):
        self.cluster.g_query.get_slot_no.side_effect = OSError("socket unavailable")
        with mock.patch.object(p, "recovery_limits", return_value=(240, 6000)):
            self.assertEqual(p.recover("info"), 0)
        self.voter.main.assert_not_called()
        self.assertEqual(self.cluster.g_query.get_slot_no.call_count, p.MAX_STALLED_STEPS)
        self.sdk.sometimes.assert_any_call(False, "action_majority_reached")


if __name__ == "__main__":
    unittest.main()
