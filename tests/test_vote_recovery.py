"""Private recovery supply, immutable votes, and interrupted-submission regressions."""

import contextlib
import copy
import importlib
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(
    0,
    str(
        pathlib.Path(__file__).resolve().parents[1]
        / "components/gov-cli/composer/governance-python"
    ),
)
import helper_vote_recovery as r

recovery = importlib.import_module("eventually_votes_recorded")


class PrivateVotes(unittest.TestCase):
    def setUp(self):
        self.cluster = mock.Mock()
        self.cluster.epoch_length = 800
        self.cluster.g_query.get_slot_no.return_value = 100
        self.cluster.g_query.get_gov_action_deposit.return_value = 1_000_000
        self.tip = {"slot": 100, "epoch": 1, "hash": "immutable"}
        self.prop = {
            "actionId": {"txId": "target", "govActionIx": 0},
            "expiresAfter": 10,
            "dRepVotes": {},
            "stakePoolVotes": {},
        }
        self.gov = {"proposals": [self.prop]}
        self.cluster.g_query.get_gov_state.return_value = self.gov
        self.state = {
            "funded": True,
            "action": {"txid": "target", "expiry": 150, "signed": "action.signed"},
        }
        self.accounts = dict(
            address="private-source",
            source_vkey="source.vkey",
            source_skey="source.skey",
            stake_vkey="return.vkey",
            stake_skey="return.skey",
            stake_cert="return.cert",
        )
        self.voters = [
            (
                kind,
                mock.Mock(return_value=types.SimpleNamespace(vote_file=kind + ".vote")),
                "vkey_file",
                pathlib.Path(kind + ".vkey"),
                pathlib.Path(kind + ".skey"),
            )
            for kind in ["drep", "spo"]
        ]
        self.query = self.enterContext(
            mock.patch.object(r, "immutable_query", side_effect=lambda *_: (self.tip, self.gov))
        )
        self.enterContext(mock.patch.object(r, "fixtures", return_value=self.accounts))
        self.enterContext(mock.patch.object(r.voting, "build_voters", return_value=self.voters))
        self.enterContext(
            mock.patch.object(r.voting, "voter_hash", side_effect=lambda _c, v: v[0] + "-key")
        )
        self.enterContext(mock.patch.object(r.g, "unique_token", return_value="unique"))
        self.save = self.enterContext(mock.patch.object(r, "save"))
        self.enterContext(mock.patch.object(r, "sdk"))
        self.submit = self.enterContext(mock.patch.object(r.g, "build_sign_submit"))

    def test_only_own_immutable_proposal_with_selected_role_votes_succeeds(self):
        self.prop["dRepVotes"] = {"keyHash-drep-key": "VoteNo"}
        self.prop["stakePoolVotes"] = {"spo-key": "VoteAbstain"}
        self.assertTrue(r.step(self.cluster, self.state))
        self.submit.assert_not_called()
        self.assertEqual(self.state["last"]["recorded"], {"drep": "VoteNo", "spo": "VoteAbstain"})
        self.query.assert_called_once_with(self.cluster, ["gov-state"])

    def test_foreign_voted_proposal_cannot_satisfy_recovery(self):
        foreign = copy.deepcopy(self.prop)
        foreign["actionId"]["txId"] = "foreign"
        foreign.update(dRepVotes={"drep-key": "VoteYes"}, stakePoolVotes={"spo-key": "VoteYes"})
        self.gov["proposals"] = [foreign]
        self.assertFalse(r.step(self.cluster, self.state))
        self.cluster.g_transaction.submit_tx_bare.assert_called_once_with(tx_file="action.signed")
        self.submit.assert_not_called()

    def test_immutable_expiry_releases_absent_proposal_for_fresh_supply(self):
        self.gov["proposals"] = []
        self.state["votes"] = {"signed": "votes.signed", "expiry": 120}
        self.tip["slot"] = 150
        self.assertFalse(r.step(self.cluster, self.state))
        self.assertNotIn("action", self.state)
        self.assertNotIn("votes", self.state)
        self.assertFalse(r.step(self.cluster, self.state))
        self.submit.assert_called_once()
        args = self.cluster.g_governance.action.create_info.call_args.kwargs
        self.assertEqual(args["deposit_return_stake_vkey_file"], r.ROOT / "keys/return.vkey")

    def test_expired_listed_action_with_missing_votes_cannot_block_recovery(self):
        self.prop["expiresAfter"] = 0
        self.state["votes"] = {"signed": "old-votes.signed", "expiry": 9999}
        self.assertFalse(r.step(self.cluster, self.state))
        self.assertNotIn("action", self.state)
        self.assertNotIn("votes", self.state)
        self.submit.assert_not_called()
        self.cluster.g_transaction.submit_tx_bare.assert_not_called()
        self.assertFalse(r.step(self.cluster, self.state))
        self.submit.assert_called_once()

    def test_last_votable_epoch_keeps_the_same_private_action(self):
        self.prop["expiresAfter"] = self.tip["epoch"]
        self.assertFalse(r.step(self.cluster, self.state))
        self.assertEqual(self.state["action"]["txid"], "target")
        self.submit.assert_called_once()
        self.assertEqual(self.voters[1][1].call_args.kwargs["action_txid"], "target")

    def test_recorded_immutable_votes_are_observed_before_expiry_replacement(self):
        self.prop["expiresAfter"] = 0
        self.prop["dRepVotes"] = {"drep-key": "VoteYes"}
        self.prop["stakePoolVotes"] = {"spo-key": "VoteNo"}
        self.assertTrue(r.step(self.cluster, self.state))
        self.assertIn("action", self.state)
        self.submit.assert_not_called()

    def test_missing_proposals_is_unknown_not_an_empty_live_set(self):
        self.gov.clear()
        before = copy.deepcopy(self.state)
        with self.assertRaises(TypeError):
            r.step(self.cluster, self.state)
        self.assertEqual(self.state, before)
        self.submit.assert_not_called()

    def test_unknown_query_cannot_retire_action(self):
        self.query.side_effect = OSError("query fault")
        self.tip["slot"] = 9999
        with self.assertRaises(OSError):
            r.step(self.cluster, self.state)
        self.assertIn("action", self.state)
        self.submit.assert_not_called()

    def test_interrupted_create_is_journalled_then_exactly_rebroadcast(self):
        self.state.pop("action")

        def interrupt(*args, **kw):
            kw["before_submit"]("target")
            self.assertEqual(self.state["action"]["signed"], str(r.ROOT / "action.signed"))
            self.save.assert_called()
            raise OSError("interrupted")

        self.submit.side_effect = interrupt
        with self.assertRaises(OSError):
            r.step(self.cluster, self.state)
        self.gov["proposals"] = []
        self.assertFalse(r.step(self.cluster, self.state))
        self.assertEqual(self.submit.call_count, 1)
        self.cluster.g_transaction.submit_tx_bare.assert_called_once_with(
            tx_file=str(r.ROOT / "action.signed")
        )

    def test_other_credentials_votes_do_not_count_as_selected_voters(self):
        self.prop.update(dRepVotes={"unrelated": "VoteYes"}, stakePoolVotes={"other": "VoteYes"})
        self.assertFalse(r.step(self.cluster, self.state))
        for voter in self.voters:
            self.assertEqual(voter[1].call_args.kwargs["action_txid"], "target")
        self.assertEqual(len(self.submit.call_args.kwargs["vote_files"]), 2)
        self.assertEqual(self.submit.call_args.kwargs["src_addr"].address, "private-source")

    def test_pending_votes_use_immutable_expiry_not_volatile_head(self):
        self.state["votes"] = {"signed": "retained.signed", "expiry": 200}
        self.cluster.g_query.get_slot_no.return_value = 9999
        self.assertFalse(r.step(self.cluster, self.state))
        self.submit.assert_not_called()
        self.cluster.g_transaction.submit_tx_bare.assert_called_once_with(tx_file="retained.signed")

    def test_only_missing_role_is_supplied_after_vote_ttl_expiry(self):
        self.prop["dRepVotes"] = {"drep-key": "VoteYes"}
        self.state["votes"] = {"signed": "expired.signed", "expiry": 100}
        self.assertFalse(r.step(self.cluster, self.state))
        self.voters[0][1].assert_not_called()
        self.voters[1][1].assert_called_once()
        self.assertEqual(
            self.submit.call_args.kwargs["signing_key_files"], [pathlib.Path("spo.skey")]
        )

    def test_funding_and_private_stake_registration_are_journalled_together(self):
        self.query.side_effect = None
        self.query.return_value = (self.tip, {})
        self.state = {}

        def submit(*args, **kw):
            self.assertEqual(kw["certificate_files"], [r.ROOT / "keys/return.cert"])
            self.assertEqual(kw["signing_key_files"], [r.ROOT / "keys/return.skey"])
            self.assertEqual(kw["txouts"][0].address, "private-source")
            # The pinned CLI requires registration on-chain before it can
            # build a proposal returning its deposit to this credential.
            self.assertNotIn("proposal_files", kw)
            self.assertFalse(kw["wait_for_confirmation"])
            kw["before_submit"]("funding-tx")
            self.assertEqual(self.state["funding"]["txid"], "funding-tx")
            self.assertNotIn("action", self.state)
            self.save.assert_called()

        self.submit.side_effect = submit
        self.assertFalse(r.fund(self.cluster, self.state, self.accounts))
        self.assertNotIn("funded", self.state)
        self.query.return_value = (self.tip, {"funding-tx#0": {}})
        self.assertTrue(r.fund(self.cluster, self.state, self.accounts))
        self.assertTrue(self.state["funded"])

    def test_volatile_proposal_is_enough_to_submit_votes_but_not_to_pass(self):
        self.state.pop("funded")
        self.gov["proposals"] = []
        self.cluster.g_query.get_gov_state.return_value = {"proposals": [self.prop]}
        with mock.patch.object(r, "fund") as fund:
            self.assertFalse(r.step(self.cluster, self.state))
            fund.assert_not_called()
        self.assertIn("vote_files", self.submit.call_args.kwargs)
        self.assertFalse(self.submit.call_args.kwargs["wait_for_confirmation"])
        self.assertNotIn("funded", self.state)

    def test_volatile_votes_are_not_an_immutable_recovery_witness(self):
        self.gov["proposals"] = []
        voted = copy.deepcopy(self.prop)
        voted.update(dRepVotes={"drep-key": "VoteYes"}, stakePoolVotes={"spo-key": "VoteYes"})
        self.cluster.g_query.get_gov_state.return_value = {"proposals": [voted]}
        self.assertFalse(r.step(self.cluster, self.state))
        self.submit.assert_not_called()
        self.gov["proposals"] = [voted]
        self.assertTrue(r.step(self.cluster, self.state))

    def test_own_volatile_funding_can_prepare_a_proposal_without_claiming_immutability(self):
        self.query.side_effect = None
        self.query.return_value = (self.tip, {})
        self.state = {"funding": {"txid": "funding", "expiry": 200, "signed": "funding.signed"}}
        self.cluster.g_query.get_utxo.return_value = [types.SimpleNamespace(utxo_hash="funding")]
        self.assertTrue(r.fund(self.cluster, self.state, self.accounts))
        self.assertNotIn("funded", self.state)
        self.submit.assert_not_called()

    def test_failed_volatile_query_cannot_retire_an_absent_immutable_action(self):
        self.gov["proposals"] = []
        self.tip["slot"] = 9999
        self.cluster.g_query.get_gov_state.side_effect = OSError("fault")
        with self.assertRaises(OSError):
            r.step(self.cluster, self.state)
        self.assertIn("action", self.state)
        self.submit.assert_not_called()

    def test_rolled_back_volatile_funding_is_replayed_before_the_dependent_proposal(self):
        self.state.pop("funded")
        self.state["funding"] = {"txid": "fund", "expiry": 200, "signed": "fund.signed"}
        self.gov["proposals"] = []
        self.cluster.g_query.get_utxo.return_value = []
        self.assertFalse(r.step(self.cluster, self.state))
        self.assertEqual(
            self.cluster.g_transaction.submit_tx_bare.call_args_list,
            [mock.call(tx_file="fund.signed"), mock.call(tx_file="action.signed")],
        )

    def test_duplicate_funding_rejection_cannot_block_replay_of_its_proposal(self):
        self.state.pop("funded")
        self.state["funding"] = {"txid": "fund", "expiry": 200, "signed": "fund.signed"}
        self.gov["proposals"] = []
        self.cluster.g_query.get_utxo.return_value = []
        self.cluster.g_transaction.submit_tx_bare.side_effect = [OSError("already included"), None]
        self.assertFalse(r.step(self.cluster, self.state))
        self.cluster.g_transaction.submit_tx_bare.assert_called_with(tx_file="action.signed")

    def test_expired_funding_is_not_rebroadcast_with_a_still_pending_proposal(self):
        self.state.pop("funded")
        self.state["funding"] = {"txid": "fund", "expiry": 100, "signed": "fund.signed"}
        self.gov["proposals"] = []
        self.assertFalse(r.step(self.cluster, self.state))
        self.cluster.g_transaction.submit_tx_bare.assert_called_once_with(tx_file="action.signed")


class RecoveryScheduling(unittest.TestCase):
    def setUp(self):
        self.sdk = self.enterContext(mock.patch.object(recovery, "sdk"))
        self.marker = self.enterContext(mock.patch.object(recovery.g, "SETUP_MARKER"))
        self.marker.exists.return_value = True
        self.enterContext(mock.patch.object(recovery.g, "ensure_dirs"))
        self.enterContext(mock.patch.object(recovery.g, "make_cluster"))
        self.enterContext(mock.patch.object(r, "recovery_checks", return_value=2))
        self.sleep = self.enterContext(mock.patch.object(recovery.time, "sleep"))
        self.state = {}

        @contextlib.contextmanager
        def locked():
            yield self.state

        self.enterContext(mock.patch.object(r, "locked", locked))

    def test_terminal_supplies_until_fresh_immutable_evidence(self):
        def step(*_):
            self.state["last"] = {"immutable_slot": 100}
            return step_mock.call_count == 3

        with mock.patch.object(r, "step", side_effect=step) as step_mock:
            self.assertEqual(recovery.main(), 0)
            self.assertEqual(step_mock.call_count, 3)
        self.sdk.sometimes.assert_any_call(True, r.COVERAGE, {"immutable_slot": 100})
        self.sdk.unreachable.assert_not_called()

    def test_missing_setup_registers_mandatory_coverage_without_side_effects(self):
        self.marker.exists.return_value = False
        with mock.patch.object(r, "step") as step:
            self.assertEqual(recovery.main(), 0)
            step.assert_not_called()
        self.sdk.sometimes.assert_called_once_with(
            False, r.COVERAGE, {"reason": "awaiting_private_immutable_votes"}
        )

    def test_old_snapshot_cannot_hide_unavailable_current_queries(self):
        self.state["last"] = {"immutable_slot": 99}
        with mock.patch.object(r, "step", side_effect=OSError("fault")):
            self.assertEqual(recovery.main(), 0)
        self.sdk.unreachable.assert_called_once_with("eventually_cold_start")
        self.assertFalse(
            any(c.args[0] for c in self.sdk.sometimes.call_args_list if c.args[1] == r.COVERAGE)
        )

    def test_submit_fault_after_readable_query_is_not_a_cold_start_failure(self):
        def step(*_):
            self.state["last"] = {"immutable_slot": 100}
            raise OSError("submit fault")

        with mock.patch.object(r, "step", side_effect=step):
            self.assertEqual(recovery.main(), 0)
        self.sdk.unreachable.assert_not_called()
        self.assertFalse(
            any(c.args[0] for c in self.sdk.sometimes.call_args_list if c.args[1] == r.COVERAGE)
        )


class ImmutableReads(unittest.TestCase):
    def test_moving_or_unknown_read_point_cannot_count(self):
        cluster = mock.Mock()
        for value in ["{}", "[]"]:
            cluster.g_query.query_cli.side_effect = [
                json.dumps({"hash": "a"}),
                value,
                json.dumps({"hash": "b"}),
            ] * 3
            with self.assertRaises(ValueError):
                r.immutable_query(cluster, ["gov-state"])

    def test_production_budget_retains_retry_headroom_for_pipelined_confirmation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(r.g, "GOV_STATE_DIR", pathlib.Path(directory)),
        ):
            p = pathlib.Path(directory) / "shelley"
            p.mkdir()
            (p / "genesis.json").write_text(
                json.dumps({"securityParam": 150, "slotLength": 0.2, "activeSlotsCoeff": 0.1})
            )
            self.assertEqual(r.recovery_checks(), 240)


if __name__ == "__main__":
    unittest.main()
