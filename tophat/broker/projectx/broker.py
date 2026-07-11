"""Live TopstepX broker: wraps ProjectXClient behind the shared Broker protocol."""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone

from tophat.broker.base import BrokerAccount
from tophat.broker.projectx.brackets import plan_to_order, probe_order
from tophat.broker.projectx.client import ProjectXClient
from tophat.engine import TradePlan

# The probe's marker text (see check_oco_bracket_support). Matching is on the
# broker's error string, same snippet the live fire path keys on.
_OCO_ERR_SNIPPET = "auto oco"


class ProjectXBroker:
    def __init__(self, client: ProjectXClient | None = None) -> None:
        self._client = client or ProjectXClient()
        self._nq_contract_id: str | None = None
        self._nq_resolved_on: str = ""

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
        # Re-resolve once per day: the server runs for months unattended and the
        # active NQ contract rolls quarterly — a stale cached id would place
        # orders on the expired contract.
        today = date.today().isoformat()
        if not self._nq_contract_id or self._nq_resolved_on != today:
            self._nq_contract_id = self._client.active_nq_contract()["id"]
            self._nq_resolved_on = today
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

    def last_price(self, contract_id: str) -> float | None:
        """Most recent 1-minute close, or None when the market is closed
        (no bar in the last ~20 minutes — weekend/holiday/maintenance)."""
        now = datetime.now(timezone.utc)
        bars = self._client.retrieve_bars(
            contract_id, start_time=now - timedelta(minutes=20), end_time=now,
            unit=2, unit_number=1, limit=25)
        if not bars:
            return None
        return float(max(bars, key=lambda b: b["t"])["c"])

    def check_oco_bracket_support(self, account_id: int,
                                  contract_id: str) -> tuple[str, str]:
        """Nightly Auto-OCO probe for one account -> (status, detail).

        status: "on"  — a bracketed limit order was accepted (and cancelled)
                "off" — rejected with the Auto OCO Brackets error
                "error" — anything else (market closed, other rejection, or a
                          probe order we could not cancel — detail says which).
        The order is a 1-lot limit ~100pts below market; on acceptance it is
        cancelled immediately (one retry). A stuck probe order is reported
        loudly in detail — it must be cancelled by hand."""
        try:
            px = self.last_price(contract_id)
        except Exception as exc:
            return "error", f"price read failed: {exc}"
        if px is None:
            return "error", "no recent bars - market closed?"
        body = probe_order(px - 100.0)
        body["accountId"] = account_id
        body["contractId"] = contract_id
        body["customTag"] = f"tophat-oco-probe-{uuid.uuid4().hex[:8]}"
        try:
            order_id = self._client.place_order(body)
        except Exception as exc:
            if _OCO_ERR_SNIPPET in str(exc).lower():
                return "off", str(exc)
            return "error", f"probe rejected for another reason: {exc}"
        for attempt in (1, 2):
            try:
                self._client.cancel_order(account_id, order_id)
                return "on", ""
            except Exception as exc:
                if attempt == 2:
                    return "error", (f"PROBE ORDER STUCK (id {order_id}) — cancel it "
                                     f"manually in the platform: {exc}")
                time.sleep(1.0)
        return "on", ""   # unreachable; keeps type-checkers happy

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
