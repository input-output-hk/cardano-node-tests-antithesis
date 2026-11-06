"""Own accounts and journal for SQLite recovery traffic; no payment-test state."""

from __future__ import annotations

import helper_leios_storage as storage
import helper_leios_transactions as payments
from cardano_clusterlib import clusterlib


class Traffic:
    """Payment builder interface, persisted under the caller-held storage lock."""

    BATCH_SIZE = payments.BATCH_SIZE
    INPUT_AMOUNT = payments.INPUT_AMOUNT
    PAYMENT_AMOUNT = payments.PAYMENT_AMOUNT
    FUND_AMOUNT = payments.FUND_AMOUNT
    MAX_PAYMENTS = payments.MAX_PAYMENTS
    tx_reference = staticmethod(payments.tx_reference)

    def __init__(self, parent: dict) -> None:
        self.parent = parent
        self.ROOT = storage.ROOT / "payments"
        self.ROOT.mkdir(parents=True, exist_ok=True)

    def save(self, traffic: dict) -> None:
        self.parent["traffic"] = traffic
        storage.save(self.parent)

    def account(self, cluster: clusterlib.ClusterLib, name: str) -> clusterlib.AddressRecord:
        path = self.ROOT / f"{name}.addr"
        if not path.exists():
            return cluster.g_address.gen_payment_addr_and_keys(name=name, destination_dir=self.ROOT)
        return clusterlib.AddressRecord(
            address=path.read_text().strip(),
            vkey_file=self.ROOT / f"{name}.vkey",
            skey_file=self.ROOT / f"{name}.skey",
        )
