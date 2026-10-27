"""Exercise terminal recovery with no anytime observer scheduled."""

from __future__ import annotations

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
recovery = importlib.import_module("eventually_treasury_withdrawal")


class TreasuryRecovery(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.marker = self.root / "setup_done"
        self.marker.touch()
        self.claim = {
            "txid": "guided",
            "ix": 0,
            "recv_addr": "isolated-recipient",
            "pre_balance": 20,
            "transfer_amt": 10,
            "valid_until": 100,
        }
        (self.root / "recv_1.json").write_text(json.dumps(self.claim))
        self.prop = {
            "actionId": {"txId": "guided", "govActionIx": 0},
            "proposalProcedure": {"govAction": {"tag": "TreasuryWithdrawals"}},
            "expiresAfter": 10,
        }
        self.empty = {"proposals": [], "nextRatifyState": {"enactedGovActions": []}}
        self.ratified = {
            "proposals": [self.prop],
            "nextRatifyState": {"enactedGovActions": [self.prop]},
        }
        self.tip = {"epoch": 1, "slot": 150, "hash": "epoch1"}
        self.cluster = mock.Mock(epoch_length=10, slot_length=1)
        self.sdk = mock.Mock()
        patches = [
            mock.patch.multiple(
                recovery.g,
                SETUP_MARKER=self.marker,
                PENDING_TREASURY_WITHDRAWALS_DIR=self.root,
                make_cluster=mock.Mock(return_value=self.cluster),
                build_sign_submit=mock.DEFAULT,
            ),
            mock.patch.object(recovery, "sdk", self.sdk),
            mock.patch.object(recovery.checker, "sdk", self.sdk),
            mock.patch.object(recovery.time, "sleep"),
            mock.patch.object(
                recovery.treasury, "lock", side_effect=lambda: contextlib.nullcontext(True)
            ),
            mock.patch.object(
                recovery.treasury,
                "receiver_lock",
                side_effect=lambda _i: contextlib.nullcontext(True),
            ),
            mock.patch.object(recovery.treasury, "gov_state", return_value=self.ratified),
            mock.patch.object(
                recovery.treasury, "immutable_snapshot", return_value=(self.tip, self.ratified, 20)
            ),
            mock.patch.object(
                recovery.checker, "main", side_effect=AssertionError("anytime commands are stopped")
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_recovers_through_query_failure_ratification_and_next_epoch_payout(self) -> None:
        recovery.treasury.gov_state.side_effect = [self.empty, self.ratified, self.empty]
        recovery.treasury.immutable_snapshot.side_effect = [
            OSError("node recovering"),
            (self.tip, self.ratified, 20),
            ({"epoch": 2, "slot": 200, "hash": "epoch2"}, self.empty, 30),
        ]
        assert recovery.main() == 0
        self.sdk.sometimes.assert_any_call(True, "treasury_withdrawal_ratified", mock.ANY)
        self.sdk.sometimes.assert_any_call(True, "treasury_withdrawal_enacted_correctly", mock.ANY)
        self.sdk.always.assert_any_call(
            True, "treasury_withdrawal_payout_matches_ratification", mock.ANY
        )
        self.sdk.always.assert_any_call(True, "treasury_withdrawal_recovered")
        assert recovery.time.sleep.call_count == 2
        recovery.g.build_sign_submit.assert_not_called()
        self.cluster.g_transaction.assert_not_called()
        recovery.checker.main.assert_not_called()

    def test_stalled_epoch_is_bounded_and_fails_recovery_without_reducing_coverage(self) -> None:
        assert recovery.main() == 0
        self.sdk.always.assert_any_call(False, "treasury_withdrawal_recovered", mock.ANY)
        assert recovery.time.sleep.call_count == 6
        assert not any(
            c.args[:2] == (True, "treasury_withdrawal_enacted_correctly")
            for c in self.sdk.sometimes.call_args_list
        )
        self.sdk.sometimes.assert_any_call(False, "treasury_withdrawal_enacted_correctly")

    def test_missing_claim_is_not_success(self) -> None:
        (self.root / "recv_1.json").unlink()
        assert recovery.main() == 0
        self.sdk.always.assert_called_once_with(
            False,
            "treasury_withdrawal_recovered",
            {"checks": 7, "last_error": "guided recipient claim is missing"},
        )

    def test_production_genesis_bounds_retries_without_advancing_slots(self) -> None:
        self.cluster.epoch_length = 15_000
        self.cluster.slot_length = 0.2
        (self.root / "recv_1.json").unlink()
        assert recovery.main() == 0
        assert recovery.time.sleep.call_count == 1800
        self.sdk.always.assert_called_once_with(
            False,
            "treasury_withdrawal_recovered",
            {"checks": 1801, "last_error": "guided recipient claim is missing"},
        )

    def test_cold_start_does_not_query_or_register_payout_coverage(self) -> None:
        self.marker.unlink()
        assert recovery.main() == 0
        recovery.g.make_cluster.assert_not_called()
        self.sdk.sometimes.assert_not_called()
        self.sdk.always.assert_not_called()

    def test_unpaid_ratified_action_still_fails_exact_accounting(self) -> None:
        self.claim["ratified_epoch"] = 1
        (self.root / "recv_1.json").write_text(json.dumps(self.claim))
        recovery.treasury.gov_state.return_value = self.empty
        recovery.treasury.immutable_snapshot.return_value = (
            {"epoch": 2, "slot": 200, "hash": "epoch2"},
            self.empty,
            20,
        )
        assert recovery.main() == 0
        self.sdk.always.assert_any_call(
            False, "treasury_withdrawal_payout_matches_ratification", mock.ANY
        )
        self.sdk.always.assert_any_call(False, "treasury_withdrawal_recovered", mock.ANY)


if __name__ == "__main__":
    unittest.main()
