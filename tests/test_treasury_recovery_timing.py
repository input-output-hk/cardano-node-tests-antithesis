"""Keep the Leios epoch transition in reach without changing Conway epochs."""

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
import helper_treasury_recovery as r


class RecoveryTiming(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = pathlib.Path(directory)
        (self.root / "shelley").mkdir()
        self.enterContext(mock.patch.object(r.g, "GOV_STATE_DIR", self.root))
        self.cluster = mock.Mock(epoch_length=15000, slot_length=0.2)
        self.sdk = self.enterContext(mock.patch.object(r, "sdk"))
        self.sleep = self.enterContext(mock.patch.object(r.time, "sleep"))
        self.genesis(12)
        self.enterContext(
            mock.patch.object(r.treasury, "lock", side_effect=lambda: contextlib.nullcontext(True))
        )
        self.enterContext(
            mock.patch.object(
                r.treasury, "receiver_lock", side_effect=lambda _i: contextlib.nullcontext(True)
            )
        )
        self.enterContext(
            mock.patch.object(r.g, "pending_recv_slots", return_value=[(1, {"txid": "guided"})])
        )
        self.query = self.enterContext(mock.patch.object(r.treasury, "gov_state"))
        self.observe = self.enterContext(mock.patch.object(r.checker, "observe"))

    def genesis(self, major):
        (self.root / "shelley/genesis.json").write_text(
            json.dumps(
                {
                    "securityParam": 150,
                    "slotLength": 0.2,
                    "activeSlotsCoeff": 0.1,
                    "protocolParams": {"protocolVersion": {"major": major}},
                }
            )
        )

    def observation(self, slot, ratified=1):
        def observe(*args, **kwargs):
            kwargs["observation"].update(
                immutable_slot=slot,
                immutable_epoch=slot // 15000,
                ratified_epoch=ratified,
                waiting_reason="immutable_proposal_live",
            )
            return False

        return observe

    def test_conway_keeps_its_setup_timing_and_has_no_new_properties(self):
        self.genesis(10)
        self.assertIsNone(r.leios_window())
        self.assertTrue(r.prepare(self.cluster))
        self.query.assert_not_called()
        self.observe.assert_not_called()
        self.assertEqual(self.sdk.mock_calls, [])
        self.sleep.assert_not_called()

    def test_production_window_is_derived_from_actual_genesis(self):
        self.assertEqual(r.leios_window(), (3000, 1200))

    def test_early_ratification_cannot_release_faults_with_a_fifty_minute_wait(self):
        def observe(*args, **kw):
            slot = 15037 if self.observe.call_count == 1 else 27000
            return self.observation(slot)(*args, **kw)

        self.observe.side_effect = observe
        self.assertTrue(r.prepare(self.cluster))
        self.assertEqual(self.observe.call_count, 2)
        self.sleep.assert_called_once_with(300)
        ready = self.sdk.sometimes.call_args
        self.assertTrue(ready.args[0])
        self.assertEqual(ready.args[2]["immutable_slot"], 27000)

    def test_query_failure_cannot_reuse_previous_ready_snapshot(self):
        self.observe.side_effect = OSError("query unavailable")
        self.cluster.epoch_length = 10
        self.cluster.slot_length = 1
        self.assertFalse(r.prepare(self.cluster))
        self.sdk.unreachable.assert_called_once_with("leios_treasury_recovery_setup_failed")
        self.assertFalse(any(c.args[0] for c in self.sdk.sometimes.call_args_list))

    def test_unratified_action_near_boundary_is_not_ready(self):
        self.cluster.epoch_length = 10
        self.cluster.slot_length = 1
        self.observe.side_effect = self.observation(29999, None)
        self.assertFalse(r.prepare(self.cluster))
        self.sdk.unreachable.assert_called_once()

    def test_already_settled_optional_setup_still_requires_a_fresh_exact_read(self):
        def paid(*args, **kw):
            kw["observation"].update(immutable_slot=30010, immutable_epoch=2, ratified_epoch=1)
            return True

        self.observe.side_effect = paid
        self.assertTrue(r.prepare(self.cluster))
        self.observe.assert_called_once()
        self.sleep.assert_not_called()

    def test_stalled_epoch_is_bounded_without_changing_global_parameters(self):
        self.observe.side_effect = self.observation(15037)
        self.assertFalse(r.prepare(self.cluster))
        self.assertEqual(sum(c.args[0] for c in self.sleep.call_args_list), 6000)
        self.assertLessEqual(self.observe.call_count, 21)
        self.sdk.unreachable.assert_called_once()

    def test_failed_queries_use_short_retry_not_an_old_epoch_backoff(self):
        self.assertEqual(r.poll_seconds(self.cluster, {}), 5)
        self.assertEqual(r.poll_seconds(self.cluster, {"ratified_epoch": None}), 5)
        self.assertEqual(
            r.poll_seconds(
                self.cluster,
                {
                    "ratified_epoch": 1,
                    "waiting_reason": "immutable_proposal_live",
                    "immutable_slot": 29990,
                },
            ),
            5,
        )

    def test_failed_window_preparation_cannot_publish_the_setup_marker(self):
        setup = importlib.import_module("first_setup")
        marker = self.root / "setup_done"
        with (
            mock.patch.multiple(
                setup.g,
                SETUP_MARKER=marker,
                PAYMENT_POOL=self.root / "payments",
                NUM_DREPS=0,
                NUM_CC=0,
                NUM_PAYMENT_ADDRS=0,
                ensure_dirs=mock.Mock(),
                make_cluster=mock.Mock(),
                wait_for_node=mock.Mock(return_value=True),
                wait_for_epoch=mock.Mock(),
                build_sign_submit=mock.Mock(),
                current_epoch=mock.Mock(return_value=0),
            ),
            mock.patch.object(setup, "_leios_pool_keys", return_value=True),
            mock.patch.object(setup, "_setup_special_dreps", return_value=[]),
            mock.patch.object(setup, "_confirm_special_dreps"),
            mock.patch.object(setup, "_setup_treasury_recv_pool"),
            mock.patch.object(setup, "_prepare_treasury_payout", return_value=True),
            mock.patch.object(setup.bls, "prepare", return_value=True),
            mock.patch.object(setup.plutus, "prepare", return_value=True),
            mock.patch.object(setup.storage_setup, "prepare", return_value=True),
            mock.patch.object(r, "prepare", return_value=False),
            mock.patch.object(setup, "sdk") as sdk,
        ):
            self.assertEqual(setup.main(), 1)
        self.assertFalse(marker.exists())
        sdk.setup_complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
