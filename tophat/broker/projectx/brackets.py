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
    return {
        "type": ORDER_MARKET,
        "side": side,
        "size": plan.contracts,
        "limitPrice": None,
        "stopPrice": None,
        "trailPrice": None,
        "isAutomated": True,
        "stopLossBracket": {"ticks": pts_to_ticks(plan.stop_pts), "type": ORDER_STOP},
        "takeProfitBracket": {"ticks": pts_to_ticks(plan.target_pts), "type": ORDER_LIMIT},
    }
