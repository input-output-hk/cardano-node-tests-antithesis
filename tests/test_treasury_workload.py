"""Fault-path regressions for treasury accounting, run with the gov-cli image."""

from __future__ import annotations

import importlib
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

DRIVERS = (
    pathlib.Path(__file__).resolve().parents[1] / "components/gov-cli/composer/governance-python"
)
sys.path.insert(0, str(DRIVERS))

import helper_gov as g
import helper_treasury as treasury

checker = importlib.import_module("anytime_treasury_withdrawal_enactment")
voter = importlib.import_module("parallel_driver_vote_treasury_withdrawal")
creator = importlib.import_module("parallel_driver_create_treasury_withdrawal")


def proposal() -> dict:
    return {
        "actionId": {"txId": "action", "govActionIx": 0},
        "proposalProcedure": {"govAction": {"tag": "TreasuryWithdrawals"}},
        "dRepVotes": {},
        "committeeVotes": {},
        "expiresAfter": 10,
    }


def state(props: list | None = None, ratified: list | None = None) -> dict:
    return {
        "proposals": props or [],
        "nextRatifyState": {"enactedGovActions": ratified or []},
    }


class TreasuryFaultPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = mock.Mock()
        self.record = {
            "txid": "action",
            "ix": 0,
            "recv_addr": "recipient",
            "pre_balance": 20,
            "transfer_amt": 10,
            "valid_until": 100,
        }
        self.tip = {"hash": "immutable", "slot": 150, "epoch": 3}
        self.patches = mock.patch.multiple(
            g,
            complete_recv_slot=mock.DEFAULT,
            release_recv_slot=mock.DEFAULT,
        )
        self.journal = self.patches.start()
        self.addCleanup(self.patches.stop)
        self.sdk = mock.patch.object(checker, "sdk").start()
        self.addCleanup(mock.patch.stopall)

    def observe(
        self, *, balance: int = 20, immutable: dict | None = None, live: dict | None = None
    ) -> None:
        with mock.patch.object(
            treasury, "immutable_snapshot", return_value=(self.tip, immutable or state(), balance)
        ):
            checker.observe(self.cluster, 1, self.record, live or state())

    def test_live_immutable_action_survives_volatile_disappearance(self) -> None:
        self.observe(immutable=state([proposal()]))
        self.journal["release_recv_slot"].assert_not_called()
        self.sdk.sometimes.assert_not_called()
        details = self.sdk.always.call_args.args[2]
        self.assertEqual(details["waiting_reason"], "immutable_proposal_live")
        self.assertEqual(details["immutable_epoch"], 3)
        self.assertIsNone(details["ratified_epoch"])

    def test_uncertain_submission_waits_until_validity_is_immutable(self) -> None:
        self.tip["slot"] = 99
        self.observe()
        self.journal["release_recv_slot"].assert_not_called()
        self.assertEqual(
            self.sdk.always.call_args.args[2]["waiting_reason"],
            "submission_validity_not_immutable",
        )

    def test_late_volatile_inclusion_preserves_claim(self) -> None:
        self.observe(live=state([proposal()]))
        self.journal["release_recv_slot"].assert_not_called()
        self.assertEqual(
            self.sdk.always.call_args.args[2]["waiting_reason"], "volatile_proposal_live"
        )

    def test_ratification_is_ledger_evidence_and_waits_for_next_epoch(self) -> None:
        self.observe(immutable=state(ratified=[proposal()]))
        self.journal["complete_recv_slot"].assert_called_once_with(1, ratified_epoch=3)
        self.sdk.sometimes.assert_called_once_with(True, "treasury_withdrawal_ratified", mock.ANY)
        details = self.sdk.sometimes.call_args.args[2]
        self.assertEqual(details["txid"], "action")
        self.assertEqual(details["immutable_slot"], 150)
        self.assertEqual(details["ratified_epoch"], 3)
        self.assertEqual(details["waiting_reason"], "next_immutable_epoch")
        self.journal["release_recv_slot"].assert_not_called()

    def test_ratified_action_without_payout_fails_exact_accounting(self) -> None:
        self.record["ratified_epoch"] = 2
        self.observe()
        self.assertTrue(
            any(
                c.args[:2] == (False, "treasury_withdrawal_payout_matches_ratification")
                for c in self.sdk.always.call_args_list
            )
        )
        self.assertIsNone(self.sdk.always.call_args.args[2]["waiting_reason"])

    def test_successful_guided_payout_keeps_recipient_reserved(self) -> None:
        self.record["ratified_epoch"] = 2
        self.observe(balance=30)
        self.journal["release_recv_slot"].assert_not_called()
        self.assertTrue(
            any(
                c.args[:2] == (True, "treasury_withdrawal_enacted_correctly")
                for c in self.sdk.sometimes.call_args_list
            )
        )

    def test_delayed_duplicate_payout_fails_even_after_success(self) -> None:
        self.record["ratified_epoch"] = 2
        self.observe(balance=30)
        self.sdk.reset_mock()
        self.observe(balance=40)
        self.assertEqual(
            self.sdk.always.call_args_list[0].args[:2], (False, "treasury_withdrawal_no_overpay")
        )

    def test_failed_submission_is_not_expiry_coverage(self) -> None:
        self.observe()
        self.assertFalse(
            any(
                c.args[1] == "treasury_withdrawal_resolved_without_payout"
                for c in self.sdk.sometimes.call_args_list
            )
        )

    def test_immutable_inclusion_records_expiry_for_later_ticks(self) -> None:
        self.observe(immutable=state([proposal()]))
        self.journal["complete_recv_slot"].assert_called_once_with(1, expires_after=10)
        self.journal["release_recv_slot"].assert_not_called()

    def test_volatile_only_inclusion_is_not_expiry_evidence(self) -> None:
        self.observe(live=state([proposal()]))
        self.journal["complete_recv_slot"].assert_not_called()

    def test_expiry_receiver_is_retained_and_late_payment_fails(self) -> None:
        self.record["expires_after"] = 1
        for balance in (20, 30):
            with mock.patch.object(
                treasury, "immutable_snapshot", return_value=(self.tip, state(), balance)
            ):
                checker.observe(self.cluster, treasury.EXPIRY_RECEIVER, self.record, state())
        self.sdk.sometimes.assert_any_call(
            True, "treasury_withdrawal_resolved_without_payout", mock.ANY, must_hit=False
        )
        self.sdk.always.assert_any_call(False, "treasury_withdrawal_expired_no_payout", mock.ANY)
        self.journal["release_recv_slot"].assert_not_called()

    def test_ratified_action_does_not_count_as_expiry(self) -> None:
        self.record.update(expires_after=1, ratified_epoch=2)
        self.observe(balance=30)
        self.assertFalse(
            any(
                c.args[1] == "treasury_withdrawal_resolved_without_payout"
                for c in self.sdk.sometimes.call_args_list
            )
        )

    def test_unknown_query_preserves_claim_and_registers_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            marker = root / "setup_done"
            marker.touch()
            with (
                mock.patch.multiple(
                    g,
                    SETUP_MARKER=marker,
                    STATE_DIR=root,
                    make_cluster=mock.Mock(return_value=self.cluster),
                ),
                mock.patch.object(treasury, "gov_state", side_effect=OSError("fault")),
            ):
                self.assertEqual(checker.main(), 0)
        self.journal["release_recv_slot"].assert_not_called()
        self.sdk.sometimes.assert_any_call(False, "treasury_withdrawal_ratified")
        self.sdk.sometimes.assert_any_call(False, "treasury_withdrawal_enacted_correctly")
        self.sdk.sometimes.assert_any_call(
            False, "treasury_withdrawal_resolved_without_payout", must_hit=False
        )

    def test_advancing_immutable_point_is_not_a_snapshot(self) -> None:
        def query(_cluster: object, args: list, **_kwargs: object) -> object:
            if args[0] == "tip":
                query.calls += 1
                return {"hash": str(query.calls), "slot": 150, "epoch": 3}
            return [{"rewardAccountBalance": 20}]

        query.calls = 0
        with (
            mock.patch.object(treasury, "query", side_effect=query),
            mock.patch.object(treasury, "gov_state", return_value=state()),
            self.assertRaisesRegex(ValueError, "consistent immutable"),
        ):
            treasury.immutable_snapshot(self.cluster, "recipient")

    def test_empty_account_result_is_unknown_not_zero(self) -> None:
        with (
            mock.patch.object(treasury, "query", side_effect=[self.tip, [], self.tip]),
            mock.patch.object(treasury, "gov_state", return_value=state()),
            self.assertRaisesRegex(ValueError, "not registered"),
        ):
            treasury.immutable_snapshot(self.cluster, "recipient")

    def test_recorded_vote_checks_exact_credential(self) -> None:
        prop = proposal()
        prop["dRepVotes"] = {"keyHash-other": "VoteYes"}
        self.assertIsNone(voter.recorded_vote(prop, "drep", "wanted"))

    def test_random_lane_cannot_replace_guided_yes_votes(self) -> None:
        guided = proposal()
        guided["dRepVotes"] = {"keyHash-drep": "VoteYes"}
        guided["committeeVotes"] = {"keyHash-cc": "VoteYes"}
        other = proposal()
        other["actionId"]["txId"] = "random"
        self.check_selection(guided, other, expected_action="random", expected_kind="drep")

    def test_guided_batch_retries_only_missing_yes_votes(self) -> None:
        guided = proposal()
        guided["dRepVotes"] = {"keyHash-drep": "VoteYes"}
        guided["committeeVotes"] = {"keyHash-cc": "VoteNo"}
        self.check_selection(guided, proposal(), expected_action="action", expected_kind="cc")

    def test_random_lane_never_votes_on_expiry_proposal(self) -> None:
        guided = proposal()
        guided["dRepVotes"] = {"keyHash-drep": "VoteYes"}
        guided["committeeVotes"] = {"keyHash-cc": "VoteYes"}
        expiry = proposal()
        expiry["actionId"]["txId"] = "random"
        with mock.patch.object(treasury, "receiver_id", return_value=("random", 0)):
            self.check_selection(guided, expiry, expected_action=None, expected_kind=None)

    def check_selection(
        self, guided: dict, other: dict, *, expected_action: str | None, expected_kind: str | None
    ) -> None:
        create_drep = mock.Mock(return_value=types.SimpleNamespace(vote_file="drep.vote"))
        create_cc = mock.Mock(return_value=types.SimpleNamespace(vote_file="cc.vote"))
        voters = [
            ("drep", create_drep, "drep_vkey_file", pathlib.Path("drep.vkey"), "drep.skey"),
            ("cc", create_cc, "cc_hot_vkey_file", pathlib.Path("cc.vkey"), "cc.skey"),
        ]
        with (
            mock.patch.object(voter, "build_voters", return_value=voters),
            mock.patch.object(voter, "voter_hash", side_effect=lambda _c, v: v[0]),
            mock.patch.object(treasury, "guided_id", return_value=("action", 0)),
            mock.patch.object(treasury, "gov_state", return_value=state([guided, other])),
            mock.patch.object(treasury, "participation"),
            mock.patch.object(voter, "sdk"),
            mock.patch.multiple(
                g,
                rng_mod=lambda _n: 0,
                unique_token=lambda: "test",
                build_sign_submit=mock.Mock(),
                recent_stall=lambda _c: False,
            ),
        ):
            self.assertEqual(voter.cast(self.cluster, mock.Mock()), 0)
        if expected_action is None:
            create_drep.assert_not_called()
            create_cc.assert_not_called()
            return
        chosen = create_drep if expected_kind == "drep" else create_cc
        rejected = create_cc if expected_kind == "drep" else create_drep
        rejected.assert_not_called()
        chosen.assert_called_once()
        self.assertEqual(chosen.call_args.kwargs["action_txid"], expected_action)

    def test_submission_failure_preserves_the_prebroadcast_claim(self) -> None:
        address = mock.Mock()
        self.cluster.epoch_length = 1000
        self.cluster.g_query.get_slot_no.return_value = 100
        self.cluster.g_transaction.get_txid.return_value = "known-before-broadcast"
        self.cluster.g_transaction.submit_tx.side_effect = OSError("connection lost")
        with (
            mock.patch.object(creator, "_claim_recv_slot", return_value=(1, "recipient")),
            mock.patch.object(creator, "sdk"),
            mock.patch.multiple(g, rng_mod=lambda _n: 0, unique_token=lambda: "test"),
        ):
            self.assertEqual(creator.create(self.cluster, address), 0)
        self.journal["complete_recv_slot"].assert_any_call(1, txid="known-before-broadcast", ix=0)
        self.journal["release_recv_slot"].assert_not_called()
        self.assertEqual(
            self.cluster.g_transaction.build_tx.call_args.kwargs["invalid_hereafter"], 1100
        )


if __name__ == "__main__":
    unittest.main()
