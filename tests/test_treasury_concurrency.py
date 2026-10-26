"""Exercise real process locks while a treasury submit is deliberately blocked."""

from __future__ import annotations

import importlib
import multiprocessing
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

DRIVERS = (
    pathlib.Path(__file__).resolve().parents[1] / "components/gov-cli/composer/governance-python"
)
sys.path.insert(0, str(DRIVERS))
g = importlib.import_module("helper_gov")
treasury = importlib.import_module("helper_treasury")
checker = importlib.import_module("anytime_treasury_withdrawal_enactment")
creator = importlib.import_module("parallel_driver_create_treasury_withdrawal")
voter = importlib.import_module("parallel_driver_vote_treasury_withdrawal")


class TreasuryConcurrency(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        self.pending = self.root / "pending"
        self.pending.mkdir()
        marker = self.root / "setup"
        marker.touch()
        (self.root / "treasury_recv1_stake.addr").write_text("receiver")
        self.cluster = mock.Mock(epoch_length=100)
        self.cluster.g_query.get_slot_no.return_value = 100
        self.action = {
            "actionId": {"txId": "action", "govActionIx": 0},
            "proposalProcedure": {"govAction": {"tag": "TreasuryWithdrawals"}},
        }
        self.state = {"proposals": [], "nextRatifyState": {"enactedGovActions": [self.action]}}
        self.tip = {"epoch": 2, "slot": 250, "hash": "immutable"}
        self.ready = multiprocessing.Event()
        self.release = multiprocessing.Event()
        patches = [
            mock.patch.multiple(
                g,
                STATE_DIR=self.root,
                PENDING_TREASURY_WITHDRAWALS_DIR=self.pending,
                SETUP_MARKER=marker,
                TREASURY_RECV_DIR=self.root,
                ensure_dirs=lambda: None,
                make_cluster=lambda: self.cluster,
                current_epoch=lambda _c: 1,
                rng_mod=lambda _n: 0,
                unique_token=lambda: "test",
                wait_for_node=lambda _c, **_k: True,
                try_acquire_payment_addr=lambda _i: (mock.Mock(), mock.Mock()),
                release_payment_addr=mock.Mock(),
                recent_stall=lambda _c: False,
            ),
            mock.patch.object(treasury, "gov_state", return_value=self.state),
            mock.patch.object(
                treasury, "immutable_snapshot", return_value=(self.tip, self.state, 0)
            ),
            mock.patch.object(checker, "sdk"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.sdk = checker.sdk

    def block_submit(self, _cluster: object, _name: str, **kwargs: object) -> str:
        callback = kwargs.get("before_submit")
        if callback:
            callback("action")
        self.ready.set()
        assert self.release.wait(5), "test did not release blocked submission"
        return "action"

    def exercise_blocked_driver(self, main: object) -> None:
        process = multiprocessing.get_context("fork").Process(target=main)
        process.start()
        try:
            assert self.ready.wait(3), "driver did not reach submission"
            for _ in range(3):
                assert checker.main() == 0
            self.sdk.sometimes.assert_any_call(True, "treasury_withdrawal_ratified", mock.ANY)
            assert dict(g.pending_recv_slots())[1]["ratified_epoch"] == 2
        finally:
            self.release.set()
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join()
        assert process.exitcode == 0

    def test_blocked_creator_does_not_hide_its_journalled_proposal(self) -> None:
        with mock.patch.object(g, "build_sign_submit", side_effect=self.block_submit):
            self.exercise_blocked_driver(creator.main)

    def test_blocked_voter_does_not_hide_ratification(self) -> None:
        assert g.claim_recv_slot(1, pre_balance=0, claimed_epoch=1)
        g.complete_recv_slot(
            1, txid="action", ix=0, recv_addr="receiver", transfer_amt=10, valid_until=200
        )

        def cast(cluster: object, _addr: object) -> int:
            self.block_submit(cluster, "vote")
            return 0

        with mock.patch.object(voter, "cast", side_effect=cast):
            self.exercise_blocked_driver(voter.main)

    def test_preparing_claim_is_protected_but_other_receivers_progress(self) -> None:
        assert g.claim_recv_slot(3, pre_balance=0, claimed_epoch=1)
        g.complete_recv_slot(
            3, txid="action", ix=0, recv_addr="other", transfer_amt=10, valid_until=200
        )

        def before_journal(*_args: object, **_kwargs: object) -> str:
            self.ready.set()
            assert self.release.wait(10)
            return "never-submitted"

        with (
            mock.patch.object(g, "build_sign_submit", side_effect=before_journal),
            mock.patch.object(treasury, "query", return_value={"epoch": 5}),
        ):
            process = multiprocessing.get_context("fork").Process(target=creator.main)
            process.start()
            try:
                assert self.ready.wait(3)
                assert checker.main() == 0
                records = dict(g.pending_recv_slots())
                assert "txid" not in records[1]
                assert records[3]["ratified_epoch"] == 2
            finally:
                # A kill before journalling must release the kernel lock,
                # leaving an abandoned claim that a later observer reclaims.
                process.terminate()
                process.join(3)
            assert not process.is_alive()
            assert checker.main() == 0
            assert 1 not in dict(g.pending_recv_slots())
            self.sdk.sometimes.assert_any_call(
                True, "treasury_withdrawal_slot_reclaimed_after_abandonment"
            )


if __name__ == "__main__":
    unittest.main()
