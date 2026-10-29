"""Recovery must drive fresh payments, without leaking Leios checks into Conway."""

from __future__ import annotations

import contextlib
import importlib
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(
    0,
    str(
        pathlib.Path(__file__).resolve().parents[1]
        / "components/gov-cli/composer/governance-python"
    ),
)
recovery = importlib.import_module("eventually_leios_transactions")


class LeiosRecovery(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(contextlib.ExitStack())
        self.state = {"payments": [{"expiry": 1}]}

        def patch(target, **kwargs):
            return stack.enter_context(mock.patch(target, **kwargs))

        self.enabled = patch("eventually_leios_transactions.t.enabled", return_value=True)
        self.marker = stack.enter_context(
            mock.patch.object(recovery.g.SETUP_MARKER.__class__, "exists", return_value=True)
        )
        self.make_cluster = patch("eventually_leios_transactions.g.make_cluster")
        patch("eventually_leios_transactions.RECOVERY_CHECKS", new=2)
        self.observe = patch("eventually_leios_transactions.observer.main")
        self.submit = patch("eventually_leios_transactions.submitter.main")
        patch(
            "eventually_leios_transactions.t.locked",
            return_value=contextlib.nullcontext(self.state),
        )
        self.sleep = patch("eventually_leios_transactions.time.sleep")
        self.sdk = patch("eventually_leios_transactions.sdk")

    def test_conway_has_no_sdk_or_cluster_calls(self):
        self.enabled.return_value = False
        self.assertEqual(recovery.main(), 0)
        self.assertEqual(self.sdk.mock_calls, [])
        recovery.g.make_cluster.assert_not_called()
        self.observe.assert_not_called()
        self.submit.assert_not_called()

    def test_missing_setup_still_registers_mandatory_coverage(self):
        self.marker.return_value = False
        self.assertEqual(recovery.main(), 0)
        self.sdk.sometimes.assert_called_once_with(
            False, "leios_tx_certified_and_immutable", mock.ANY
        )
        self.observe.assert_not_called()
        self.submit.assert_not_called()

    def test_expired_pre_warmup_payments_are_replaced_before_success(self):
        def observe():
            if self.observe.call_count == 1:
                self.state["payments"] = []
            else:
                self.assertEqual(self.state["payments"], ["fresh"])
                self.state["complete"] = True

        def submit():
            self.assertEqual(self.state["payments"], [])
            self.state["payments"] = ["fresh"]

        self.observe.side_effect, self.submit.side_effect = observe, submit
        self.assertEqual(recovery.main(), 0)
        self.assertEqual(self.observe.call_count, 2)
        self.submit.assert_called_once()
        self.sleep.assert_called_once_with(recovery.POLL_SECONDS)

    def test_completed_payment_is_rechecked_without_new_submission(self):
        self.state["complete"] = True
        self.assertEqual(recovery.main(), 0)
        self.observe.assert_called_once()
        self.submit.assert_not_called()
        self.sleep.assert_not_called()

    def test_unavailable_evidence_does_not_satisfy_coverage_or_loop_forever(self):
        self.assertEqual(recovery.main(), 0)
        self.assertEqual(self.observe.call_count, 3)
        self.assertEqual(self.submit.call_count, 2)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertFalse(self.state.get("complete", False))
        self.sdk.sometimes.assert_called_once_with(
            False, "leios_tx_certified_and_immutable", mock.ANY
        )
        self.sdk.always.assert_not_called()
        self.make_cluster.assert_not_called()


if __name__ == "__main__":
    unittest.main()
