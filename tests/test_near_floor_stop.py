"""Near-floor stop handling: $1,000 (10pt) eval stop + 'no manual stop when room <= stop'.

Topstep's Combine MLL breaches intraday on unrealized P&L, so within one stop of the
floor we place no protective stop and let Topstep auto-liquidate (docs/PROBABILITY.md §0).
"""

from tophat.broker.projectx.brackets import plan_to_order, pts_to_ticks
from tophat.engine import AccountConfig, AccountState, Phase, TradePlan, should_omit_stop
from tophat.store.config import TopHatSettings


def _eval_plan(manual_stop: bool = True) -> TradePlan:
    return TradePlan(direction=1, contracts=5, target_pts=15.5, stop_pts=10.0,
                     label="eval", manual_stop=manual_stop)


def test_eval_stop_is_full_dll():
    cfg = TopHatSettings().to_account_config()
    assert cfg.eval_stop_pts == 10.0
    # 10pt * 5 minis * $20 = $1,000 = the daily loss limit
    assert cfg.eval_stop_pts * cfg.eval_contracts * cfg.point_value == cfg.dll
    assert AccountConfig().eval_stop_pts == 10.0


def test_trade_plan_keeps_stop_by_default():
    assert _eval_plan().manual_stop is True


def test_should_omit_stop_only_within_one_stop_of_floor():
    cfg = AccountConfig()                       # eval stop = $1,000
    plan = _eval_plan()
    st = AccountState(phase=Phase.EVAL, base_balance=50_000,
                      peak_equity_eod=50_000, equity=50_000)   # floor = 48,000
    assert not should_omit_stop(cfg, st, plan, balance=50_000)  # room 2,000
    assert not should_omit_stop(cfg, st, plan, balance=49_500)  # room 1,500
    assert should_omit_stop(cfg, st, plan, balance=49_000)      # room 1,000 (== stop)
    assert should_omit_stop(cfg, st, plan, balance=48_900)      # room   900


def test_plan_to_order_keeps_stop_when_manual():
    o = plan_to_order(_eval_plan(manual_stop=True))
    assert o["stopLossBracket"] == {"ticks": pts_to_ticks(10.0), "type": 4}
    assert o["takeProfitBracket"]["ticks"] == pts_to_ticks(15.5)


def test_plan_to_order_drops_stop_when_near_floor():
    o = plan_to_order(_eval_plan(manual_stop=False))
    assert o["stopLossBracket"] is None              # no protective stop
    assert o["takeProfitBracket"] is not None        # take-profit still placed
    assert o["size"] == 5 and o["type"] == 2         # still a 5-mini market entry
