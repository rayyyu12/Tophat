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
