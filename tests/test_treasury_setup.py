"""Keep mandatory treasury settlement reachable before fault exploration."""

from __future__ import annotations

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
setup = importlib.import_module("first_setup")
build_sign_submit = setup.g.build_sign_submit


class TreasurySetup(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        (self.root / "guided_payment.addr").write_text("dedicated-payment\n")
        self.cluster = mock.Mock()
        self.cluster.g_query.get_utxo.return_value = [object()]
        self.action_id = None
        self.prop = {
            "actionId": {"txId": "guided", "govActionIx": 0},
            "proposalProcedure": {"govAction": {"tag": "TreasuryWithdrawals"}},
            "dRepVotes": {},
            "committeeVotes": {},
        }
        voters = [(kind, None, None, None, None) for kind in ["drep", "cc"]]
        patches = [
            mock.patch.multiple(
                setup.g,
                TREASURY_RECV_DIR=self.root,
                NUM_DREPS=1,
                NUM_CC=1,
                current_epoch=mock.Mock(return_value=0),
                build_sign_submit=mock.DEFAULT,
            ),
            mock.patch.object(setup, "sdk"),
            mock.patch.object(setup.time, "sleep"),
            mock.patch.object(setup.treasury, "guided_id", side_effect=lambda: self.action_id),
            mock.patch.object(setup.treasury, "gov_state", return_value={"proposals": [self.prop]}),
            mock.patch.object(setup.treasury_creator, "create", side_effect=self.create),
            mock.patch.object(setup.treasury_voter, "cast", side_effect=self.vote),
            mock.patch.object(setup.treasury_voter, "build_voters", return_value=voters),
            mock.patch.object(
                setup.treasury_voter, "voter_hash", side_effect=lambda _c, voter: voter[0]
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def create(self, _cluster: object, _source: object, **_kwargs: object) -> int:
        self.action_id = ("guided", 0)
        return 0

    def vote(self, _cluster: object, _source: object) -> int:
        assert self.action_id is not None, "votes must target the journalled guided proposal"
        self.prop["dRepVotes"] = {"keyHash-drep": "VoteYes"}
        self.prop["committeeVotes"] = {"keyHash-cc": "VoteYes"}
        return 0

    def test_funds_and_prepares_a_dedicated_lane(self) -> None:
        self.cluster.g_query.get_utxo.return_value = []
        assert setup._prepare_treasury_payout(self.cluster)
        funding = setup.g.build_sign_submit.call_args
        assert funding.kwargs["txouts"][0].address == "dedicated-payment"
        assert funding.kwargs["treasury_donation"] == setup.treasury_creator.MAX_TRANSFER
        source = setup.treasury_creator.create.call_args.args[1]
        assert source.address == "dedicated-payment"
        assert source.skey_file == self.root / "guided_payment.skey"
        setup.treasury_voter.cast.assert_called_once_with(self.cluster, source)
        setup.sdk.sometimes.assert_any_call(
            True, "treasury_withdrawal_setup_ready", {"txid": "guided", "epoch": 0}
        )
        setup.time.sleep.assert_not_called()

    def test_snapshot_failure_is_retried_without_losing_the_lane(self) -> None:
        attempts = iter([False, True])

        def retry(cluster: object, source: object, **kwargs: object) -> int:
            if not next(attempts):
                raise OSError("moving tip")
            return self.create(cluster, source, **kwargs)

        setup.treasury_creator.create.side_effect = retry
        assert setup._prepare_treasury_payout(self.cluster)
        assert setup.treasury_creator.create.call_count == 2
        setup.time.sleep.assert_called_once_with(5)
        setup.sdk.unreachable.assert_not_called()

    def test_existing_funded_proposal_is_not_recreated(self) -> None:
        self.action_id = ("guided", 0)
        assert setup._prepare_treasury_payout(self.cluster)
        setup.treasury_creator.create.assert_not_called()
        setup.g.build_sign_submit.assert_not_called()

    def test_donation_is_forwarded_to_transaction_balancing(self) -> None:
        source = setup.clusterlib.AddressRecord(
            address="dedicated-payment",
            vkey_file=self.root / "guided_payment.vkey",
            skey_file=self.root / "guided_payment.skey",
        )
        build_sign_submit(self.cluster, "donate", src_addr=source, treasury_donation=25_000_000)
        assert (
            self.cluster.g_transaction.build_tx.call_args.kwargs["treasury_donation"] == 25_000_000
        )
        self.cluster.g_transaction.build_tx.reset_mock()
        build_sign_submit(self.cluster, "ordinary", src_addr=source)
        assert "treasury_donation" not in self.cluster.g_transaction.build_tx.call_args.kwargs

    def test_journalled_absent_proposal_rebroadcasts_the_same_signed_transaction(self) -> None:
        self.action_id = ("guided", 0)
        submission_file = self.root / "guided_proposal.signed"
        submission_file.write_text("signed proposal")
        self.cluster.g_transaction.get_txid.return_value = "guided"
        setup.treasury.gov_state.side_effect = [
            {"proposals": []},
            {"proposals": [self.prop]},
            {"proposals": [self.prop]},
        ]
        assert setup._prepare_treasury_payout(self.cluster)
        setup.treasury_creator.create.assert_not_called()
        self.cluster.g_transaction.submit_tx_bare.assert_called_once_with(tx_file=submission_file)
        setup.time.sleep.assert_called_once_with(5)

    def test_saved_transaction_must_match_the_journalled_action(self) -> None:
        self.action_id = ("guided", 0)
        (self.root / "guided_proposal.signed").write_text("wrong proposal")
        self.cluster.g_transaction.get_txid.return_value = "other"
        setup.treasury.gov_state.return_value = {"proposals": []}
        assert not setup._prepare_treasury_payout(self.cluster)
        self.cluster.g_transaction.submit_tx_bare.assert_not_called()
        setup.sdk.unreachable.assert_called_once_with("treasury_withdrawal_setup_failed")

    def test_exact_signed_transaction_is_saved_before_submission(self) -> None:
        signed = self.root / "built.signed"
        signed.write_text("exact signed transaction")
        self.cluster.g_transaction.sign_tx.return_value = signed
        saved = self.root / "guided_proposal.signed"
        source = setup.clusterlib.AddressRecord(
            address="dedicated-payment", vkey_file=self.root / "vkey", skey_file=self.root / "skey"
        )

        def journal(_txid: str) -> None:
            assert saved.read_bytes() == signed.read_bytes()
            self.cluster.g_transaction.submit_tx.assert_not_called()

        build_sign_submit(
            self.cluster, "proposal", src_addr=source, submission_file=saved, before_submit=journal
        )
        self.cluster.g_transaction.submit_tx.assert_called_once()

    def test_foreign_yes_votes_cannot_pass_preparation(self) -> None:
        self.action_id = ("guided", 0)
        self.prop["dRepVotes"] = {"keyHash-unrelated": "VoteYes"}
        self.prop["committeeVotes"] = {"keyHash-cc": "VoteYes"}
        setup.treasury_voter.cast.side_effect = None
        assert not setup._prepare_treasury_payout(self.cluster)
        setup.sdk.unreachable.assert_called_once_with("treasury_withdrawal_setup_failed")
        assert not any(call.args[0] for call in setup.sdk.sometimes.call_args_list)

    def test_failed_preparation_cannot_complete_setup(self) -> None:
        marker = self.root / "setup_done"
        with (
            mock.patch.multiple(
                setup.g,
                SETUP_MARKER=marker,
                NUM_DREPS=0,
                NUM_CC=0,
                ensure_dirs=mock.Mock(),
                make_cluster=mock.Mock(return_value=self.cluster),
                wait_for_node=mock.Mock(return_value=True),
                wait_for_epoch=mock.DEFAULT,
            ) as helpers,
            mock.patch.object(setup, "_leios_pool_keys", return_value=True, create=True),
            mock.patch.object(setup, "_setup_special_dreps"),
            mock.patch.object(setup, "_setup_treasury_recv_pool"),
            mock.patch.object(setup, "_prepare_treasury_payout", return_value=False),
        ):
            assert setup.main() == 1
        assert not marker.exists()
        helpers["wait_for_epoch"].assert_not_called()
        setup.sdk.setup_complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
