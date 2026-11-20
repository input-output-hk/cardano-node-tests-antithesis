"""Asynchronous callers must retain bytes before broadcast; default waits stay intact."""

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
import helper_gov as g


class JournalledSubmission(unittest.TestCase):
    def fixture(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = pathlib.Path(directory)
        self.enterContext(mock.patch.object(g, "WORK", root))
        signed = root / "original.signed"
        signed.write_bytes(b"exact signed transaction")
        cluster = mock.Mock()
        cluster.g_transaction.build_tx.return_value = types.SimpleNamespace(
            out_file=root / "body", txins=["input"]
        )
        cluster.g_transaction.sign_tx.return_value = signed
        cluster.g_transaction.get_txid.return_value = "txid"
        address = types.SimpleNamespace(address="own-source", skey_file=root / "own.skey")
        return root, signed, cluster, address

    def test_asynchronous_submission_journals_exact_bytes_before_node_response(self):
        root, signed, cluster, address = self.fixture()
        retained = root / "retained.signed"
        remember = mock.Mock()

        def broadcast(**kwargs):
            remember.assert_called_once_with("txid")
            self.assertEqual(retained.read_bytes(), signed.read_bytes())
            self.assertEqual(kwargs["tx_file"], signed)

        cluster.g_transaction.submit_tx_bare.side_effect = broadcast
        self.assertEqual(
            g.build_sign_submit(
                cluster,
                "bootstrap",
                src_addr=address,
                submission_file=retained,
                before_submit=remember,
                wait_for_confirmation=False,
            ),
            "txid",
        )
        cluster.g_transaction.submit_tx.assert_not_called()

    def test_existing_callers_keep_the_confirmation_wait(self):
        _, signed, cluster, address = self.fixture()
        g.build_sign_submit(cluster, "unchanged", src_addr=address)
        cluster.g_transaction.submit_tx.assert_called_once_with(tx_file=signed, txins=["input"])
        cluster.g_transaction.submit_tx_bare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
