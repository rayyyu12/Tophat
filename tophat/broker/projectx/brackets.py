"""Convert engine TradePlan point brackets to ProjectX tick brackets (NQ tick = 0.25)."""

from __future__ import annotations

from tophat.engine import TradePlan

NQ_TICK = 0.25
ORDER_MARKET = 2
ORDER_LIMIT = 1
ORDER_STOP = 4
SIDE_BID = 0
SIDE_ASK = 1


def pts_to_ticks(points: float) -> int:
    return max(1, round(points / NQ_TICK))


def probe_order(limit_price: float) -> dict:
    """A far-out-of-the-money 1-lot LIMIT BUY carrying both bracket legs.

    Used by the nightly Auto-OCO probe: with the account on "Position Brackets"
    the place call rejects with error 2 BEFORE any order rests (that's the
    detection); with "Auto OCO Brackets" it's accepted and the caller cancels
    it immediately. The limit sits ~100pts below market so it cannot fill in
    the second it exists."""
    px = round(limit_price / NQ_TICK) * NQ_TICK   # exchange requires tick-aligned prices
    return {
        "type": ORDER_LIMIT,
        "side": SIDE_BID,
        "size": 1,
        "limitPrice": px,
        "stopPrice": None,
        "trailPrice": None,
        "isAutomated": True,
        "stopLossBracket": {"ticks": -pts_to_ticks(10), "type": ORDER_STOP},
        "takeProfitBracket": {"ticks": pts_to_ticks(10), "type": ORDER_LIMIT},
    }


def plan_to_order(plan: TradePlan) -> dict:
    side = SIDE_BID if plan.direction == 1 else SIDE_ASK
    # ProjectX bracket `ticks` is a SIGNED offset from entry (above entry = +, below = -):
    #   LONG  -> stop below entry (negative), target above entry (positive)
    #   SHORT -> stop above entry (positive), target below entry (negative)
    # An unsigned magnitude is rejected pre-fill: "Invalid stop loss ticks (N).
    # Ticks should be less than zero when longing." (verified live on a practice
    # account 2026-07-08, both directions — this was blocking every order).
    sign = 1 if plan.direction == 1 else -1
    # manual_stop=False (within one stop of the trailing floor): place NO protective
    # stop and let Topstep auto-liquidate at the MLL. A manual stop there would sit
    # at/below the floor and fill messily (docs/PROBABILITY.md §0, STRATEGY §6.2).
    stop_bracket = ({"ticks": -sign * pts_to_ticks(plan.stop_pts), "type": ORDER_STOP}
                    if plan.manual_stop else None)
    return {
        "type": ORDER_MARKET,
        "side": side,
        "size": plan.contracts,
        "limitPrice": None,
        "stopPrice": None,
        "trailPrice": None,
        "isAutomated": True,
        "stopLossBracket": stop_bracket,
        "takeProfitBracket": {"ticks": sign * pts_to_ticks(plan.target_pts), "type": ORDER_LIMIT},
    }
