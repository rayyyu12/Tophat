"""Trading-day clock.

The CME session (and the prop-firm trading day) does NOT roll at midnight — it
turns over in the late afternoon Central: the session that settles ~4:00 PM CT
is that day's session, and everything after counts as the NEXT trading day. The
rest of the app keys "today" off the ET calendar date, which rolls at midnight
ET, so between the afternoon rollover and midnight the dashboard keeps showing
the just-finished day's state (a consumed nuke slot, a locked drive). Keying the
slot rotation and the drive off `trading_day` instead makes them reset when the
session actually turns over.

Anchored in America/Chicago because that's the exchange's clock. The rollover is
safely inside the dead zone between the morning entry window (ends 11:30 ET =
10:30 CT) and the next morning's window (09:45 ET = 08:45 CT), so moving the
boundary here never splits a fire window — the morning that matters always sees
`trading_day == ET calendar date`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

# Session rollover, America/Chicago HH:MM. 16:00 = 4:00 PM CT (the CME equity
# settlement / Topstep trading-day reset the operator flagged). Times at/after
# this roll to the next trading day.
TRADING_RESET_CT = "16:00"


def trading_day(now: datetime, reset_ct: str = TRADING_RESET_CT) -> str:
    """The trading date `now` belongs to, as 'YYYY-MM-DD'.

    `now` must be timezone-aware. At/after `reset_ct` (CT), the date rolls to the
    next calendar day; before it, it's the current CT calendar date.
    """
    ct = now.astimezone(CT)
    try:
        h, m = (int(x) for x in str(reset_ct).split(":"))
    except (ValueError, TypeError):
        h, m = 16, 0
    if (ct.hour, ct.minute) >= (h, m):
        ct = ct + timedelta(days=1)
    return ct.strftime("%Y-%m-%d")
