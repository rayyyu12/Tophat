"""Unit tests for the decision engine."""

from tophat.engine import (
    AccountConfig, AccountState, Action, Phase, decide, eod_floor, is_dead,
    is_nuke_cycle,
)

CFG = AccountConfig()


def funded(**kw):
    d = dict(phase=Phase.FUNDED, equity=0.0, peak_equity_eod=0.0, base_balance=0.0)
    d.update(kw)
    return AccountState(**d)


def test_nuke_bracket_base_and_recovery():
    assert CFG.nuke_bracket_pts(0) == (80.0, 25.0)           # $3,200 / $1,000
    assert CFG.nuke_bracket_pts(1) == (105.0, 25.0)          # $4,200 recovery
    assert CFG.nuke_recovery_target_dollars == 4_200.0


def test_flip_bracket_one_mini():
    tgt, stp = CFG.flip_bracket_pts()
    assert tgt == 8.5 and stp == 50.0
    assert tgt * CFG.flip_contracts * CFG.point_value == 170.0
    assert stp * CFG.flip_contracts * CFG.point_value == 1_000.0  # full DLL


def test_eval_decides_eval_plan():
    st = AccountState(phase=Phase.EVAL, base_balance=50_000, equity=50_000, peak_equity_eod=50_000)
    d = decide(CFG, st, 1)
    assert d.action == Action.TRADE and d.plan.label == "eval"
    assert d.plan.contracts == 5 and d.plan.direction == 1


def test_funded_fresh_is_nuke_then_flip_after_hit():
    st = funded()
    assert decide(CFG, st, 1).plan.label == "nuke"
    st.nuke_hit_this_cycle = True
    assert decide(CFG, st, 1).plan.label == "flip"


def test_renuke_on_third_cycle():
    st = funded(payouts_taken=2)
    assert decide(CFG, st, 1).plan.label == "renuke"


def test_retire_after_target_payouts():
    st = funded(payouts_taken=4)
    assert decide(CFG, st, 1).action == Action.RETIRE


def test_no_trade_on_flat_drive():
    assert decide(CFG, funded(), 0).action == Action.HOLD


def test_eval_vs_funded_floor():
    ev = AccountState(phase=Phase.EVAL, base_balance=50_000, equity=50_000, peak_equity_eod=50_000)
    assert eod_floor(CFG, ev) == 48_000.0
    fn = funded()
    assert eod_floor(CFG, fn) == -2_000.0                    # $0 start, $2k floor


def test_funded_floor_locks_at_breakeven():
    st = funded(peak_equity_eod=3_200)                       # banked the nuke
    assert eod_floor(CFG, st) == 0.0                          # can't go below breakeven


def test_eval_floor_locks_at_start():
    # The MLL only trails for the first $2,000 of profit, then locks at the
    # STARTING balance: an eval at 52,900 has a 50,000 floor, not 50,900.
    ev = AccountState(phase=Phase.EVAL, base_balance=50_000,
                      equity=52_900, peak_equity_eod=52_900)
    assert eod_floor(CFG, ev) == 50_000.0
    ev.peak_equity_eod = 51_000                              # still inside the trail
    assert eod_floor(CFG, ev) == 49_000.0


def test_is_dead_at_floor():
    assert is_dead(CFG, funded(equity=-2_000, peak_equity_eod=0))
    assert not is_dead(CFG, funded(equity=-1_000, peak_equity_eod=0))


def test_terminal_phases_do_not_trade():
    for ph, act in [(Phase.PASSED, Action.HOLD), (Phase.BLOWN, Action.MANUAL),
                    (Phase.RETIRED, Action.RETIRE)]:
        assert decide(CFG, funded(phase=ph), 1).action == act


def test_nuke_cycle_parity():
    assert is_nuke_cycle(0) and is_nuke_cycle(2)
    assert not is_nuke_cycle(1) and not is_nuke_cycle(3)
