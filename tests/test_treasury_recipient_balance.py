"""Recipient preparation must progress while immutable blocks advance."""

from __future__ import annotations

import contextlib
import importlib
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
treasury = importlib.import_module("helper_treasury")
creator = importlib.import_module("parallel_driver_create_treasury_withdrawal")


def tip(slot: int, epoch: int = 3) -> dict:
    return {"hash": f"block-{slot}", "slot": slot, "epoch": epoch}


class RecipientBalance(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = mock.Mock()

    def test_each_query_may_advance_the_block_without_starving_a_free_claim(self) -> None:
        calls = 0

        def query(_cluster: object, args: list[str], *, immutable: bool = False) -> object:
            nonlocal calls
            calls += 1
            assert immutable
            if args == ["tip"]:
                return tip(100 + calls)
            if args == ["gov-state"]:
                return {"proposals": []}
            assert args == ["stake-address-info", "--address", "recipient"]
            return [{"rewardAccountBalance": 20}]

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "treasury_recv1_stake.addr").write_text("recipient")
            with (
                mock.patch.multiple(
                    creator.g,
                    TREASURY_RECV_DIR=root,
                    NUM_DREPS=1,
                    current_epoch=lambda _c: 3,
                    pending_recv_slots=lambda: [],
                    claim_recv_slot=mock.DEFAULT,
                ) as journal,
                mock.patch.object(treasury, "query", side_effect=query),
                mock.patch.object(
                    treasury, "receiver_lock", side_effect=lambda _i: contextlib.nullcontext(True)
                ),
                contextlib.ExitStack() as preparing,
            ):
                assert creator._claim_recv_slot(self.cluster, preparing) == (1, "recipient")
                journal["claim_recv_slot"].assert_called_once_with(
                    1, pre_balance=20, claimed_epoch=3
                )
        assert calls == 3

    def test_epoch_crossing_discards_the_old_balance_and_retries(self) -> None:
        replies = [
            tip(2999),
            [{"rewardAccountBalance": 20}],
            tip(3000, 4),
            tip(3001, 4),
            [{"rewardAccountBalance": 30}],
            tip(3002, 4),
        ]
        with mock.patch.object(treasury, "query", side_effect=replies) as query:
            assert treasury.immutable_receiver_balance(self.cluster, "recipient") == 30
        assert query.call_count == 6

    def test_repeated_epoch_crossings_remain_unknown(self) -> None:
        replies = [tip(2999), [{"rewardAccountBalance": 20}], tip(3000, 4)] * 3
        with (
            mock.patch.object(treasury, "query", side_effect=replies),
            self.assertRaisesRegex(ValueError, "epoch-consistent"),
        ):
            treasury.immutable_receiver_balance(self.cluster, "recipient")

    def test_missing_or_malformed_accounts_never_become_a_zero_baseline(self) -> None:
        for accounts in [
            [],
            {},
            [{}, {}],
            [{}],
            [{"rewardAccountBalance": "20"}],
            [{"rewardAccountBalance": -1}],
            [{"rewardAccountBalance": True}],
        ]:
            with (
                self.subTest(accounts=accounts),
                mock.patch.object(treasury, "query", side_effect=[tip(100), accounts, tip(101)]),
                self.assertRaises((ValueError, TypeError, KeyError)),
            ):
                treasury.immutable_receiver_balance(self.cluster, "recipient")

    def test_invalid_tip_cannot_authorize_a_baseline(self) -> None:
        for value in [
            None,
            {},
            {"epoch": True, "slot": 1, "hash": "x"},
            tip(-1),
            tip(100, -1),
            {**tip(100), "hash": True},
        ]:
            with (
                self.subTest(tip=value),
                mock.patch.object(treasury, "query", return_value=value),
                self.assertRaisesRegex(ValueError, "epoch-consistent"),
            ):
                treasury.immutable_receiver_balance(self.cluster, "recipient")

    def test_rollback_or_conflicting_point_is_unknown(self) -> None:
        for after in [
            tip(99),
            {**tip(100), "hash": "conflict"},
            {**tip(101), "epoch": True},
            {**tip(101), "hash": None},
        ]:
            with (
                self.subTest(after=after),
                mock.patch.object(
                    treasury,
                    "query",
                    side_effect=[tip(100), [{"rewardAccountBalance": 20}], after] * 3,
                ),
                self.assertRaisesRegex(ValueError, "epoch-consistent"),
            ):
                treasury.immutable_receiver_balance(self.cluster, "recipient")

    def test_query_failure_is_not_a_balance(self) -> None:
        with (
            mock.patch.object(treasury, "query", side_effect=OSError("node unavailable")),
            self.assertRaises(OSError),
        ):
            treasury.immutable_receiver_balance(self.cluster, "recipient")


if __name__ == "__main__":
    unittest.main()
