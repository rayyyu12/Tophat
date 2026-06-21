"""Broker protocol shared by backtest, mock, and live implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tophat.engine import TradePlan


@dataclass(frozen=True)
class BrokerAccount:
    account_id: int
    name: str
    balance: float
    can_trade: bool
    simulated: bool


class Broker(Protocol):
    def login(self) -> None: ...
    def list_accounts(self, *, only_active: bool = True) -> list[BrokerAccount]: ...
    def resolve_nq_contract(self) -> str: ...
    def drive_direction(self, contract_id: str, *, live: bool = False) -> int: ...
    def place_bracket(self, account_id: int, contract_id: str,
                      plan: TradePlan, *, tag: str | None = None) -> int: ...
    def close_contract(self, account_id: int, contract_id: str) -> None: ...
    def search_open_positions(self, account_id: int) -> list[dict]: ...
