"""Leios query deadlines, conservative recovery accounting and Conway isolation."""

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
q = importlib.import_module("helper_treasury_queries")
treasury = importlib.import_module("helper_treasury")
recovery = importlib.import_module("eventually_treasury_withdrawal")


class QueryDeadlines(unittest.TestCase):
    def setUp(self):
        self.cluster = mock.Mock(command_era="dijkstra")
        self.original = self.cluster.cli

    def test_one_cli_attempt_reserves_its_full_possible_duration(self):
        budget = q.QueryBudget(1200)
        callback = mock.Mock()
        q.bounded(self.cluster, budget=budget, progress=callback)
        with mock.patch.object(
            q.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b'{"epoch":2}', b""),
        ) as run:
            result = self.cluster.cli(["query", "SUBCOMMAND", "tip", "--immutable-tip"])
        self.assertEqual(result.stdout, b'{"epoch":2}')
        self.assertEqual(budget.remaining, 1192)
        self.assertNotIn("SUBCOMMAND", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        self.original.assert_not_called()
        self.assertEqual(callback.call_args.args[0]["phase"], "finished")
        self.assertEqual(callback.call_args.args[0]["query"], "tip")

    def test_exhausted_budget_does_not_issue_another_query(self):
        budget = q.QueryBudget(1200)
        budget.remaining = 7
        q.bounded(self.cluster, budget=budget)
        with mock.patch.object(q.subprocess, "run") as run:
            with self.assertRaisesRegex(TimeoutError, "budget exhausted"):
                self.cluster.cli(["query", "tip"])
        run.assert_not_called()

    def test_timeout_is_charged_even_without_a_result(self):
        budget = q.QueryBudget(1200)
        q.bounded(self.cluster, budget=budget)
        with mock.patch.object(
            q.subprocess, "run", side_effect=subprocess.TimeoutExpired("query", 8)
        ):
            with self.assertRaises(TimeoutError):
                self.cluster.cli(["query", "gov-state"])
        self.assertEqual(budget.remaining, 1192)

    def test_small_genesis_deadline_still_leaves_room_for_multiple_snapshots(self):
        budget = q.QueryBudget(120)
        self.assertAlmostEqual(budget.query_seconds, 1.2)
        for _ in range(15):
            budget.reserve(budget.query_seconds + 3)
        budget.reserve(32)
        self.assertGreater(budget.remaining, 0)

    def test_conway_returns_original_cluster_without_changing_its_cli(self):
        with (
            mock.patch.object(treasury.g, "make_cluster", return_value=self.cluster),
            mock.patch.object(treasury.g, "GOV_COMMAND_ERA", "conway"),
        ):
            self.assertIs(treasury.observer_cluster(), self.cluster)
        self.assertIs(self.cluster.cli, self.original)

    def test_leios_uses_local_adapter_and_optional_progress_only(self):
        with (
            mock.patch.object(treasury.g, "make_cluster", return_value=self.cluster),
            mock.patch.object(treasury.g, "GOV_COMMAND_ERA", "dijkstra"),
            mock.patch.object(treasury, "sdk") as sdk,
            mock.patch.object(
                q.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"{}", b"")
            ),
        ):
            cluster = treasury.observer_cluster()
            cluster.cli(["query", "gov-state", "--immutable-tip"])
        sdk.sometimes.assert_any_call(
            True, "treasury_withdrawal_query_progress", mock.ANY, must_hit=False
        )
        self.assertIsNot(cluster.cli, self.original)

    def test_stuck_query_and_descendant_release_their_lock_at_the_deadline(self):
        import fcntl

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(q, "QUERY_SECONDS", 0.2):
            path = pathlib.Path(directory) / "held.lock"
            program = (
                "import fcntl,os,signal,sys,time;"
                "f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "os.fork();time.sleep(60)"
            )
            q.bounded(self.cluster)
            with self.assertRaises(q.exceptions.CLIError):
                self.cluster.cli([sys.executable, "-c", program, str(path)], add_default_args=False)
            self.assertTrue(path.exists())
            with path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


class RecoveryAccounting(unittest.TestCase):
    def test_failed_query_time_cannot_be_multiplied_into_hours_of_retry_sleeps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / "setup_done"
            marker.touch()
            cluster = mock.Mock(epoch_length=15000, slot_length=0.2)
            query = mock.Mock(side_effect=subprocess.TimeoutExpired("query", 8))
            budget = q.QueryBudget(1200)
            q.bounded(cluster, budget=budget)

            def unavailable(*_args):
                cluster.cli(["query", "gov-state"])

            import contextlib

            with (
                mock.patch.object(recovery.g, "SETUP_MARKER", marker),
                mock.patch.object(
                    recovery.g, "pending_recv_slots", return_value=[(1, {"txid": "guided"})]
                ),
                mock.patch.object(recovery.recovery, "leios_window", return_value=(3000, 1200)),
                mock.patch.object(recovery.queries, "QueryBudget", return_value=budget),
                mock.patch.object(recovery.treasury, "observer_cluster", return_value=cluster),
                mock.patch.object(
                    recovery.treasury, "lock", side_effect=lambda: contextlib.nullcontext(True)
                ),
                mock.patch.object(
                    recovery.treasury,
                    "receiver_lock",
                    side_effect=lambda _i: contextlib.nullcontext(True),
                ),
                mock.patch.object(recovery.treasury, "gov_state", side_effect=unavailable),
                mock.patch.object(q.subprocess, "run", query),
                mock.patch.object(recovery, "sdk") as sdk,
                mock.patch.object(recovery.time, "sleep") as sleep,
            ):
                self.assertEqual(recovery.main(), 0)
            slept = sum(call.args[0] for call in sleep.call_args_list)
            self.assertLessEqual(slept, 1200)
            self.assertLessEqual(query.call_count * 8, 1200)
            self.assertLessEqual(query.call_count * 8 + slept, 2400)
            self.assertLess(query.call_count, 100)
            sdk.always.assert_any_call(False, "treasury_withdrawal_recovered", mock.ANY)
            self.assertFalse(
                any(
                    c.args[:2] == (True, "treasury_withdrawal_enacted_correctly")
                    for c in sdk.sometimes.call_args_list
                )
            )


if __name__ == "__main__":
    unittest.main()
