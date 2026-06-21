"""Opening-range drive tracker fed by streamed quotes (not REST polling)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


@dataclass
class DriveTracker:
    """Tracks 09:30-09:45 ET and locks drive direction at 09:45."""

    or_open: float | None = None
    or_close: float | None = None
    locked_direction: int | None = None
    last_price: float | None = None
    quote_count: int = 0

    def on_quote(self, quote: dict, ts: datetime | None = None) -> None:
        self.quote_count += 1
        price = quote.get("lastPrice")
        if price is None:
            bid, ask = quote.get("bestBid"), quote.get("bestAsk")
            if bid is not None and ask is not None:
                price = (bid + ask) / 2
            else:
                price = bid or ask
        if price is None:
            return
        self.last_price = float(price)

        now = (ts or datetime.now(ET))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ET)
        else:
            now = now.astimezone(ET)

        t0930 = now.replace(hour=9, minute=30, second=0, microsecond=0)
        t0945 = now.replace(hour=9, minute=45, second=0, microsecond=0)

        if self.locked_direction is not None:
            return

        if now >= t0945:
            self._lock()
            return

        if now >= t0930:
            if self.or_open is None:
                self.or_open = self.last_price
            self.or_close = self.last_price

    def _lock(self) -> None:
        if self.or_open is None or self.or_close is None:
            self.locked_direction = 0
            return
        if self.or_close > self.or_open:
            self.locked_direction = 1
        elif self.or_close < self.or_open:
            self.locked_direction = -1
        else:
            self.locked_direction = 0

    @property
    def direction(self) -> int:
        if self.locked_direction is not None:
            return self.locked_direction
        if self.or_open is not None and self.or_close is not None:
            if self.or_close > self.or_open:
                return 1
            if self.or_close < self.or_open:
                return -1
        return 0

    @property
    def status_label(self) -> str:
        d = self.direction
        if d == 1:
            return "LONG"
        if d == -1:
            return "SHORT"
        if self.or_open is None:
            return "waiting for 08:30 CT (09:30 ET)"
        if self.locked_direction is None:
            return "OR forming until 08:45 CT"
        return "FLAT"
