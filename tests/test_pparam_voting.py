"""Adversarial vote selection and interrupted-submit coverage for ParameterChange."""

from __future__ import annotations

import copy
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
voter = importlib.import_module("parallel_driver_vote_pparam_update")
g = voter.g
clusterlib = voter.clusterlib


class ParameterVoting(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        marker = root / "setup"
        marker.touch()
        self.prop = {
            "actionId": {"txId": "target", "govActionIx": 0},
            "expiresAfter": 10,
            "dRepVotes": {},
            "committeeVotes": {},
            "stakePoolVotes": {},
            "proposalProcedure": {"govAction": {"tag": "ParameterChange"}},
        }
        self.cluster = mock.Mock()
        self.cluster.g_query.get_epoch.return_value = 1
        self.cluster.g_query.get_gov_state.return_value = {"proposals": [self.prop]}
        self.cluster.g_governance.drep.get_id.side_effect = lambda **kw: kw["drep_vkey_file"].stem
        self.cluster.g_governance.committee.get_key_hash.side_effect = lambda **kw: (
            kw["vkey_file"].stem
        )
        self.files = {}
        self.roster = []
        for kind in ("drep", "cc"):
            for i in range(5):
                key = f"{kind}{i}"

                def create(
                    *, _kind: str = kind, _key: str = key, **kw: object
                ) -> types.SimpleNamespace:
                    decision = {
                        clusterlib.Votes.YES: "VoteYes",
                        clusterlib.Votes.NO: "VoteNo",
                        clusterlib.Votes.ABSTAIN: "VoteAbstain",
                    }[kw["vote"]]
                    self.files[kw["vote_name"]] = (_kind, _key, decision)
                    return types.SimpleNamespace(vote_file=kw["vote_name"])

                arg = "drep_vkey_file" if kind == "drep" else "cc_hot_vkey_file"
                self.roster.append(
                    (
                        kind,
                        mock.Mock(side_effect=create),
                        arg,
                        pathlib.Path(f"{key}.vkey"),
                        f"{key}.skey",
                    )
                )
        self.submit = mock.Mock(side_effect=self.apply_votes)
        for patch in (
            mock.patch.multiple(
                g,
                SETUP_MARKER=marker,
                STATE_DIR=root,
                ensure_dirs=lambda: None,
                make_cluster=lambda: self.cluster,
                wait_for_node=lambda *_a, **_k: True,
                rng_mod=lambda _n: 0,
                unique_token=lambda: "tick",
                try_acquire_payment_addr=lambda _i: (mock.Mock(), mock.Mock()),
                release_payment_addr=mock.Mock(),
                build_sign_submit=self.submit,
                recent_stall=lambda _c: False,
                live_pparam_update_actions=lambda _c: [self.prop],
            ),
            mock.patch.object(voter, "build_voters", return_value=self.roster),
            mock.patch.object(voter, "sdk"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.sdk = voter.sdk

    def apply_votes(self, _cluster: object, _name: str, **kwargs: object) -> str:
        for filename in kwargs["vote_files"]:
            kind, key, decision = self.files[filename]
            field = "dRepVotes" if kind == "drep" else "committeeVotes"
            self.prop[field][f"keyHash-{key}"] = decision
        return "vote-tx"

    def assert_majority(self) -> None:
        assert any(
            call.args[:2] == (True, "pparam_update_majority_reached")
            for call in self.sdk.sometimes.call_args_list
        )

    def test_repeated_rng_choice_cannot_starve_participation(self) -> None:
        for _ in range(8):
            assert voter.main() == 0
        self.assert_majority()
        assert len(self.prop["dRepVotes"]) == 5
        assert len(self.prop["committeeVotes"]) == 5

    def test_missing_credentials_are_filled_before_replacements(self) -> None:
        self.prop["dRepVotes"]["keyHash-drep0"] = "VoteNo"
        assert voter.main() == 0
        assert self.prop["dRepVotes"]["keyHash-drep0"] == "VoteNo"
        self.assert_majority()

    def test_interrupted_submit_is_retried_from_chain_state(self) -> None:
        attempts = 0

        def interrupted(*args: object, **kwargs: object) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                message = "connection lost"
                raise OSError(message)
            return self.apply_votes(*args, **kwargs)

        self.submit.side_effect = interrupted
        assert voter.main() == 0
        assert not self.prop["dRepVotes"]
        assert not self.prop["committeeVotes"]
        assert voter.main() == 0
        self.assert_majority()

    def test_post_submit_query_failure_is_not_fabricated_vote_evidence(self) -> None:
        self.cluster.g_query.get_gov_state.side_effect = [
            {"proposals": [self.prop]},
            OSError("query unavailable"),
        ]
        assert voter.main() == 0
        assert not any(
            call.args[0] and call.args[1].startswith("pparam_update_vote_recorded_")
            for call in self.sdk.sometimes.call_args_list
        )

    def test_expired_proposal_cannot_block_a_votable_proposal(self) -> None:
        expired = copy.deepcopy(self.prop)
        expired["actionId"]["txId"] = "expired"
        expired["expiresAfter"] = 0
        self.cluster.g_query.get_gov_state.return_value = {"proposals": [expired, self.prop]}
        assert voter.main() == 0
        self.assert_majority()
        assert all(
            create.call_args.kwargs["action_txid"] == "target"
            for _kind, create, *_rest in self.roster
        )

    def test_existing_majority_is_observed_before_interrupted_submission(self) -> None:
        self.prop["dRepVotes"] = {f"keyHash-drep{i}": "VoteNo" for i in range(5)}
        self.submit.side_effect = OSError("fault")
        assert voter.main() == 0
        self.assert_majority()

    def test_missing_proposal_query_is_unknown_not_an_empty_live_set(self) -> None:
        self.cluster.g_query.get_gov_state.side_effect = OSError("fault")
        assert voter.main() == 0
        self.submit.assert_not_called()
        self.sdk.sometimes.assert_any_call(True, "pparam_update_vote_query_unavailable")
        assert not any(
            call.args[1] == "pparam_update_actions_live"
            for call in self.sdk.sometimes.call_args_list
        )

    def test_another_credentials_vote_is_not_submission_evidence(self) -> None:
        foreign = copy.deepcopy(self.prop)
        foreign["dRepVotes"]["keyHash-unrelated"] = "VoteYes"
        self.cluster.g_query.get_gov_state.side_effect = [
            {"proposals": [self.prop]},
            {"proposals": [foreign]},
        ]
        assert voter.main() == 0
        self.sdk.sometimes.assert_any_call(False, "pparam_update_vote_recorded_drep")


if __name__ == "__main__":
    unittest.main()
