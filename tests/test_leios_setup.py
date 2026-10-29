"""Regressions for Leios voting-key bootstrap prerequisites."""

from __future__ import annotations

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
setup = importlib.import_module("first_setup")


class LeiosBootstrap(unittest.TestCase):
    def test_newer_key_state_schema(self):
        self.assertEqual(
            setup._leios_public_key(
                {
                    "spsBlsKey": {"bksKey": {"blsPubKey": "01" * 96}, "bksRegEpoch": 1},
                }
            ),
            "01" * 96,
        )
        self.assertIsNone(setup._leios_public_key({"spsBlsKey": None}))

    def fixture(self, major=12):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        (root / "shelley").mkdir()
        (root / "shelley/genesis.json").write_text(
            json.dumps(
                {
                    "protocolParams": {"protocolVersion": {"major": major}},
                }
            )
        )
        pool = root / "pools/node-pool1"
        pool.mkdir(parents=True)
        (pool / "bls.vkey").write_text(json.dumps({"cborHex": "5860" + "01" * 96}))
        patches = mock.patch.multiple(setup.g, GOV=root, GOV_STATE_DIR=root, NUM_POOLS=1)
        patches.start()
        self.addCleanup(patches.stop)
        return mock.Mock()

    def test_conway_does_not_register_bls_properties(self):
        cluster = self.fixture(major=10)
        with mock.patch.object(setup, "sdk") as sdk:
            self.assertTrue(setup._leios_pool_keys(cluster))
        self.assertEqual(sdk.mock_calls, [])
        self.assertEqual(cluster.mock_calls, [])

    def test_genesis_key_is_verified_without_pool_update(self):
        cluster = self.fixture()
        cluster.g_query.get_pool_state.return_value = mock.Mock(
            pool_params={"spsLeiosKey": {"leiosPubKey": "01" * 96}},
            future_pool_params={},
        )
        with (
            mock.patch.object(setup, "sdk"),
            mock.patch.object(setup.g, "build_sign_submit") as submit,
        ):
            self.assertTrue(setup._leios_pool_keys(cluster))
        submit.assert_not_called()
        cluster.cli.assert_not_called()

    def test_missing_genesis_key_is_not_repaired_with_a_delayed_pool_update(self):
        cluster = self.fixture()
        cluster.g_query.get_pool_state.return_value = mock.Mock(
            pool_params={"spsLeiosKey": None},
            future_pool_params={},
        )
        with mock.patch.object(setup, "sdk") as sdk:
            self.assertFalse(setup._leios_pool_keys(cluster))
        sdk.unreachable.assert_called_once_with("leios_pool_keys_setup_failed")
        cluster.cli.assert_not_called()

    def test_pending_key_does_not_pass_active_confirmation(self):
        cluster = self.fixture()
        cluster.g_query.get_pool_state.return_value = mock.Mock(
            pool_params={"spsLeiosKey": None},
            future_pool_params={"spsLeiosKey": {"leiosPubKey": "01" * 96}},
        )
        with mock.patch.object(setup, "sdk") as sdk:
            self.assertFalse(setup._leios_pool_keys(cluster, confirm=True))
        sdk.unreachable.assert_called_once_with("leios_pool_keys_setup_failed")


if __name__ == "__main__":
    unittest.main()
