"""In-memory mock broker implementing the Broker protocol.

Lets the dashboard and dry-runs work with no ProjectX credentials. Generates a
spread of accounts across lifecycle phases so the UI has something to show.
"""

from __future__ import annotations

import itertools
import random

from tophat.broker.base import BrokerAccount
from tophat.engine import TradePlan

_ids = itertools.count(7_000_001)


class MockBroker:
    def __init__(self, n_eval: int = 4, n_funded: int = 5, seed: int = 7) -> None:
        rng = random.Random(seed)
        self._nq = "CON.F.US.ENQ.MOCK"
        self._positions: dict[int, list[dict]] = {}
        self._order_seq = itertools.count(1)
        self._accounts: list[BrokerAccount] = []
        for i in range(n_eval):                       # combines start at $50k
            aid = next(_ids)
            self._accounts.append(BrokerAccount(
                aid, f"Combine-{i+1}", 50_000 + rng.choice([0, 750, 1500, -950]),
                can_trade=True, simulated=True))
        for i in range(n_funded):                     # Express funded start at $0
            aid = next(_ids)
            self._accounts.append(BrokerAccount(
                aid, f"XFA-{i+1}", rng.choice([0.0, 1200.0, 3200.0, 1940.0, -500.0, 3880.0]),
                can_trade=True, simulated=True))

    # --- Broker protocol ---
    def login(self) -> None:  # no-op
        return

    def list_accounts(self, *, only_active: bool = True) -> list[BrokerAccount]:
        return list(self._accounts)

    def resolve_nq_contract(self) -> str:
        return self._nq

    def drive_direction(self, contract_id: str, *, live: bool = False) -> int:
        return 1  # deterministic LONG for demo/dry-run

    def place_bracket(self, account_id: int, contract_id: str,
                      plan: TradePlan, *, tag: str | None = None) -> int:
        oid = next(self._order_seq)
        self._positions.setdefault(account_id, []).append(
            {"contractId": contract_id, "size": plan.contracts, "orderId": oid})
        return oid

    def close_contract(self, account_id: int, contract_id: str) -> None:
        self._positions[account_id] = []

    def search_open_positions(self, account_id: int) -> list[dict]:
        return self._positions.get(account_id, [])

    def wait_flat(self, account_id: int, contract_id: str, **_) -> bool:
        return True
