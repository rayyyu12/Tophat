"""
Real-time SignalR streaming (WebSocket push — no REST polling).

- Market hub: NQ quotes -> DriveTracker + last price
- User hub: account balance / order / position / trade updates

Docs: https://gateway.docs.projectx.com/docs/realtime/
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from signalrcore.hub_connection_builder import HubConnectionBuilder

from tophat.streaming.drive import DriveTracker

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class AccountLive:
    account_id: int
    name: str = ""
    balance: float = 0.0
    can_trade: bool = False
    simulated: bool = True
    open_position: int = 0
    last_order_status: str = ""
    last_trade_pnl: float | None = None
    updated_at: str = ""


@dataclass
class LiveSnapshot:
    connected: bool = False
    market_connected: bool = False
    user_connected: bool = False
    nq_contract: str = ""
    last_price: float | None = None
    drive: DriveTracker = field(default_factory=DriveTracker)
    accounts: dict[int, AccountLive] = field(default_factory=dict)
    last_error: str = ""
    quote_updates: int = 0
    account_updates: int = 0


class StreamManager:
    """Runs ProjectX SignalR hubs in a background thread."""

    def __init__(self, token: str, rtc_base: str, nq_contract: str) -> None:
        self._token = token
        self._rtc = rtc_base.rstrip("/")
        self._nq = nq_contract
        self._lock = threading.Lock()
        self.snapshot = LiveSnapshot(nq_contract=nq_contract)
        self._market_hub = None
        self._user_hub = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._account_ids: set[int] = set()
        self._on_change: Callable[[], None] | None = None

    def set_account_ids(self, ids: set[int]) -> None:
        self._account_ids = set(ids)
        if self._user_hub and self.snapshot.user_connected:
            self._subscribe_user_accounts()

    def on_change(self, cb: Callable[[], None]) -> None:
        self._on_change = cb

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tophat-stream")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for hub in (self._market_hub, self._user_hub):
            if hub:
                try:
                    hub.stop()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self.snapshot.connected = False
            self.snapshot.market_connected = False
            self.snapshot.user_connected = False

    def _notify(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                log.exception("on_change callback failed")

    def _run(self) -> None:
        try:
            self._start_market()
            self._start_user()
            with self._lock:
                self.snapshot.connected = (
                    self.snapshot.market_connected or self.snapshot.user_connected)
            self._notify()
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except Exception as exc:
            with self._lock:
                self.snapshot.last_error = str(exc)
            log.exception("stream manager failed")

    def _start_market(self) -> None:
        url = f"{self._rtc}/hubs/market?access_token={self._token}"
        hub = HubConnectionBuilder().with_url(
            url, options={"skip_negotiation": True, "timeout": 15000},
        ).build()

        def on_quote(msg):
            try:
                if isinstance(msg, list) and len(msg) >= 2:
                    quote = msg[1] if isinstance(msg[1], dict) else msg[0]
                elif isinstance(msg, dict):
                    quote = msg
                else:
                    return
                with self._lock:
                    self.snapshot.drive.on_quote(quote)
                    if self.snapshot.drive.last_price is not None:
                        self.snapshot.last_price = self.snapshot.drive.last_price
                    self.snapshot.quote_updates += 1
                self._notify()
            except Exception:
                log.exception("quote handler")

        hub.on("GatewayQuote", on_quote)
        hub.on_open(lambda: self._on_market_open(hub))
        self._market_hub = hub
        hub.start()

    def _on_market_open(self, hub) -> None:
        hub.send("SubscribeContractQuotes", [self._nq])
        with self._lock:
            self.snapshot.market_connected = True

    def _start_user(self) -> None:
        url = f"{self._rtc}/hubs/user?access_token={self._token}"
        hub = HubConnectionBuilder().with_url(
            url, options={"skip_negotiation": True, "timeout": 15000},
        ).build()

        hub.on("GatewayUserAccount", self._on_account)
        hub.on("GatewayUserPosition", self._on_position)
        hub.on("GatewayUserOrder", self._on_order)
        hub.on("GatewayUserTrade", self._on_trade)
        hub.on_open(lambda: self._on_user_open(hub))
        self._user_hub = hub
        hub.start()

    def _on_user_open(self, hub) -> None:
        with self._lock:
            self.snapshot.user_connected = True
        hub.send("SubscribeAccounts", [])
        self._subscribe_user_accounts(hub)

    def _subscribe_user_accounts(self, hub=None) -> None:
        hub = hub or self._user_hub
        if not hub:
            return
        for aid in self._account_ids:
            hub.send("SubscribeOrders", [aid])
            hub.send("SubscribePositions", [aid])
            hub.send("SubscribeTrades", [aid])

    def _on_account(self, data) -> None:
        try:
            aid = int(data["id"])
            with self._lock:
                live = self.snapshot.accounts.setdefault(aid, AccountLive(account_id=aid))
                live.name = str(data.get("name", live.name))
                live.balance = float(data.get("balance", live.balance))
                live.can_trade = bool(data.get("canTrade", live.can_trade))
                live.simulated = bool(data.get("simulated", live.simulated))
                live.updated_at = datetime.now(ET).strftime("%H:%M:%S")
                self.snapshot.account_updates += 1
            self._notify()
        except Exception:
            log.exception("account update")

    def _on_position(self, data) -> None:
        try:
            aid = int(data["accountId"])
            with self._lock:
                live = self.snapshot.accounts.setdefault(aid, AccountLive(account_id=aid))
                live.open_position = int(data.get("size", 0))
                live.updated_at = datetime.now(ET).strftime("%H:%M:%S")
            self._notify()
        except Exception:
            log.exception("position update")

    def _on_order(self, data) -> None:
        try:
            aid = int(data["accountId"])
            status_map = {0: "none", 1: "open", 2: "filled", 3: "cancelled",
                          4: "expired", 5: "rejected", 6: "pending"}
            with self._lock:
                live = self.snapshot.accounts.setdefault(aid, AccountLive(account_id=aid))
                live.last_order_status = status_map.get(int(data.get("status", 0)), "?")
                live.updated_at = datetime.now(ET).strftime("%H:%M:%S")
            self._notify()
        except Exception:
            log.exception("order update")

    def _on_trade(self, data) -> None:
        try:
            aid = int(data["accountId"])
            with self._lock:
                live = self.snapshot.accounts.setdefault(aid, AccountLive(account_id=aid))
                pnl = data.get("profitAndLoss")
                if pnl is not None:
                    live.last_trade_pnl = float(pnl)
                live.updated_at = datetime.now(ET).strftime("%H:%M:%S")
            self._notify()
        except Exception:
            log.exception("trade update")
