"""Regressions for concentrated InfoAction participation under interrupted submits."""

from __future__ import annotations

import copy
import importlib
import pathlib
import sys
import types
import unittest
from unittest import mock

DRIVERS = (
    pathlib.Path(__file__).resolve().parents[1] / "components/gov-cli/composer/governance-python"
)
sys.path.insert(0, str(DRIVERS))
voter = importlib.import_module("parallel_driver_vote")
g = voter.g


class InfoVoting(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = mock.Mock()
        self.prop = {
            "actionId": {"txId": "first", "govActionIx": 0},
            "expiresAfter": 10,
            "dRepVotes": {},
            "stakePoolVotes": {},
            "committeeVotes": {},
        }
        self.voters = [
            (
                kind,
                mock.Mock(return_value=types.SimpleNamespace(vote_file=f"{kind}.vote")),
                "vkey_file",
                pathlib.Path(f"{kind}.vkey"),
                f"{kind}.skey",
            )
            for kind in ("drep", "spo", "cc")
        ]
        self.submit = mock.Mock()
        self.sdk = mock.Mock()
        for patch in (
            mock.patch.object(voter, "sdk", self.sdk),
            mock.patch.object(voter, "build_voters", return_value=self.voters),
            mock.patch.object(voter, "voter_hash", side_effect=lambda _c, v: v[0]),
            mock.patch.multiple(
                g,
                rng_mod=lambda _n: 0,
                unique_token=lambda: "batch",
                build_sign_submit=self.submit,
                recent_stall=lambda _c: False,
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.cluster.g_query.get_gov_state.return_value = {"proposals": [self.prop]}

    def cast(self, props: list[dict]) -> None:
        with mock.patch.object(g, "live_info_actions", return_value=props):
            assert voter.cast(self.cluster, mock.Mock()) == 0

    def test_batch_concentrates_all_roles_on_one_action(self) -> None:
        later = copy.deepcopy(self.prop)
        later["actionId"]["txId"] = "second"
        self.cast([later, self.prop])
        for _kind, create, *_rest in self.voters:
            assert create.call_args.kwargs["action_txid"] == "first"
        assert len(self.submit.call_args.kwargs["vote_files"]) == 3

    def test_existing_credentials_are_not_overwritten_while_votes_are_missing(self) -> None:
        self.prop["dRepVotes"] = {"keyHash-drep": "VoteNo"}
        self.prop["stakePoolVotes"] = {"spo": "VoteAbstain"}
        self.cast([self.prop])
        self.voters[0][1].assert_not_called()
        self.voters[1][1].assert_not_called()
        self.voters[2][1].assert_called_once()

    def test_interrupted_batch_stays_eligible_on_next_tick(self) -> None:
        self.submit.side_effect = [OSError("fault"), "txid"]
        self.cast([self.prop])
        self.cast([self.prop])
        assert self.submit.call_count == 2
        assert self.voters[0][1].call_count == 2
        self.sdk.sometimes.assert_any_call(True, "vote_transient_failure", mock.ANY)

    def test_fully_participated_action_does_not_starve_next_action(self) -> None:
        self.prop.update(
            dRepVotes={"drep": "VoteYes"},
            stakePoolVotes={"spo": "VoteNo"},
            committeeVotes={"keyHash-cc": "VoteAbstain"},
        )
        next_prop = copy.deepcopy(self.prop)
        next_prop["actionId"]["txId"] = "next"
        next_prop["committeeVotes"] = {}
        self.cast([next_prop, self.prop])
        assert self.voters[2][1].call_args.kwargs["action_txid"] == "next"
        assert len(self.submit.call_args.kwargs["vote_files"]) == 1

    def test_observed_majority_is_recorded_even_if_new_submission_fails(self) -> None:
        self.prop["dRepVotes"] = {f"keyHash-drep{n}": "VoteYes" for n in range(7)}
        self.submit.side_effect = OSError("fault")
        self.cast([self.prop])
        self.sdk.sometimes.assert_any_call(True, "action_majority_reached", mock.ANY)

    def test_another_voter_does_not_count_as_submitted_credential(self) -> None:
        self.prop["dRepVotes"] = {"keyHash-other": "VoteYes"}
        self.cast([self.prop])
        self.sdk.sometimes.assert_any_call(False, "vote_recorded_drep")

    def test_post_submit_query_failure_does_not_invent_recorded_votes(self) -> None:
        self.cluster.g_query.get_gov_state.side_effect = OSError("query fault")
        self.cast([self.prop])
        self.sdk.reachable.assert_called_once_with("vote_submitted")
        assert not any(
            call.args[1].startswith("vote_recorded_") for call in self.sdk.sometimes.call_args_list
        )

    def test_voter_can_create_and_vote_without_the_creator_command(self) -> None:
        with (
            mock.patch.object(g, "live_info_actions", side_effect=[[], [self.prop]]),
            mock.patch.object(voter.creator, "create", return_value="first") as create,
        ):
            address = mock.Mock()
            self.assertEqual(voter.cast(self.cluster, address), 0)
        create.assert_called_once_with(self.cluster, address)
        self.submit.assert_called_once()
        self.sdk.sometimes.assert_any_call(True, "actions_live", {"live": 1})

    def test_unknown_query_cannot_trigger_a_new_proposal_or_invent_coverage(self) -> None:
        with (
            mock.patch.object(g, "live_info_actions", side_effect=OSError("query fault")),
            mock.patch.object(voter.creator, "create") as create,
        ):
            self.assertEqual(voter.cast(self.cluster, mock.Mock()), 0)
        create.assert_not_called()
        self.submit.assert_not_called()
        self.sdk.sometimes.assert_any_call(
            True, "info_action_vote_unavailable", mock.ANY, must_hit=False
        )
        self.assertFalse(
            any(c.args[:2] == (True, "actions_live") for c in self.sdk.sometimes.call_args_list)
        )


class StrictInfoQuery(unittest.TestCase):
    def test_only_votable_info_actions_are_returned(self):
        cluster = mock.Mock()
        cluster.g_query.get_tip.return_value = {"epoch": 2}
        live = {"expiresAfter": 2, "proposalProcedure": {"govAction": {"tag": "InfoAction"}}}
        expired = copy.deepcopy(live)
        expired["expiresAfter"] = 1
        cluster.g_query.get_gov_state.return_value = {"proposals": [expired, live]}
        self.assertEqual(g.live_info_actions(cluster, strict=True), [live])

    def test_missing_or_unavailable_query_is_not_a_known_empty_queue(self):
        cluster = mock.Mock()
        for value in [{}, {"proposals": None}, []]:
            cluster.g_query.get_gov_state.return_value = value
            with self.assertRaises(TypeError):
                g.live_info_actions(cluster, strict=True)
        cluster.g_query.get_gov_state.side_effect = OSError("fault")
        with self.assertRaises(OSError):
            g.live_info_actions(cluster, strict=True)


if __name__ == "__main__":
    unittest.main()
