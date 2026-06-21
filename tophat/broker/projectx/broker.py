"""Live TopstepX broker: wraps ProjectXClient behind the shared Broker protocol."""

from __future__ import annotations

import time
import uuid

from tophat.broker.base import BrokerAccount
from tophat.broker.projectx.brackets import plan_to_order
from tophat.broker.projectx.client import ProjectXClient
from tophat.engine import TradePlan


class ProjectXBroker:
    def __init__(self, client: ProjectXClient | None = None) -> None:
        self._client = client or ProjectXClient()
        self._nq_contract_id: str | None = None

    @property
    def client(self) -> ProjectXClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    def login(self) -> None:
        self._client.login()

    def list_accounts(self, *, only_active: bool = True) -> list[BrokerAccount]:
        return [
            BrokerAccount(
                account_id=int(a["id"]),
                name=str(a["name"]),
                balance=float(a.get("balance", 0)),
                can_trade=bool(a.get("canTrade", False)),
                simulated=bool(a.get("simulated", True)),
            )
            for a in self._client.search_accounts(only_active=only_active)
        ]

    def resolve_nq_contract(self) -> str:
        if not self._nq_contract_id:
            self._nq_contract_id = self._client.active_nq_contract()["id"]
        return self._nq_contract_id

    def drive_direction(self, contract_id: str, *, live: bool = False) -> int:
        return self._client.drive_direction_from_bars(contract_id, live=live)

    def place_bracket(self, account_id: int, contract_id: str,
                      plan: TradePlan, *, tag: str | None = None) -> int:
        body = plan_to_order(plan)
        body["accountId"] = account_id
        body["contractId"] = contract_id
        body["customTag"] = tag or f"tophat-{plan.label}-{uuid.uuid4().hex[:8]}"
        return self._client.place_order(body)

    def close_contract(self, account_id: int, contract_id: str) -> None:
        self._client.close_contract(account_id, contract_id)

    def search_open_positions(self, account_id: int) -> list[dict]:
        return self._client.search_open_positions(account_id)

    def wait_flat(self, account_id: int, contract_id: str,
                  *, timeout_sec: float = 300, poll_sec: float = 2.0) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            pos = [
                p for p in self.search_open_positions(account_id)
                if p.get("contractId") == contract_id and int(p.get("size", 0)) != 0
            ]
            if not pos:
                return True
            time.sleep(poll_sec)
        return False
