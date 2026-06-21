"""Unit tests for the live outcome -> state reconcile loop."""

from tophat.engine import AccountConfig, AccountState, Phase, decide, start_new_day
from tophat.services.lifecycle import (
    mark_payout_taken, reconcile, record_pending,
)

CFG = AccountConfig()


def funded(**kw):
    d = dict(phase=Phase.FUNDED, equity=0.0, peak_equity_eod=0.0, base_balance=0.0)
    d.update(kw)
    return AccountState(**d)


def place(cfg, st, drive=1):
    d = decide(cfg, st, drive)
    record_pending(st, d.plan, st.equity, "2026-06-22", cfg.point_value)
    return d.plan


def test_record_pending_fields():
    st = funded()
    place(CFG, st)
    assert st.pending_label == "nuke"
    assert st.pending_target_dollars == 3_200.0
    assert st.pending_stop_dollars == 1_000.0
    assert st.last_fire_date == "2026-06-22"


def test_reconcile_requires_flat():
    st = funded()
    place(CFG, st)
    assert reconcile(CFG, st, 3_200, position_flat=False) is None  # still in trade
    assert st.pending_label == "nuke"


def test_reconcile_nuke_win():
    st = funded()
    place(CFG, st)
    assert reconcile(CFG, st, 3_200, True) == "win"
    assert st.nuke_hit_this_cycle and st.winning_days_this_cycle == 1
    assert st.equity == 3_200 and st.pending_label == ""


def test_reconcile_nuke_loss_then_recovery_bracket():
    st = funded()
    place(CFG, st)                       # day1 nuke
    start_new_day(st)
    assert reconcile(CFG, st, -1_000, True) == "loss"
    assert st.nuke_tries_this_cycle == 1 and not st.nuke_hit_this_cycle
    plan = decide(CFG, st, 1).plan       # day2 must be the wider recovery
    assert (plan.target_pts, plan.stop_pts) == (105.0, 25.0)


def test_reconcile_flat_counts_no_win():
    st = funded(nuke_hit_this_cycle=True)
    d = decide(CFG, st, 1)               # a flip
    record_pending(st, d.plan, st.equity, "d", CFG.point_value)
    assert reconcile(CFG, st, st.equity, True) == "flat"   # no balance change
    assert st.winning_days_this_cycle == 0


def test_full_payout_cycle_sets_ready():
    st = funded()
    bal = 0.0
    place(CFG, st); bal = 3_200                     # nuke
    for _ in range(5):
        start_new_day(st)
        reconcile(CFG, st, bal, True)
        if st.payout_ready:
            break
        d = place(CFG, st)
        bal += d.target_pts * d.contracts * CFG.point_value  # flip win
    assert st.winning_days_this_cycle == 5 and st.payout_ready


def test_mark_payout_advances_cycle():
    st = funded(winning_days_this_cycle=5, payout_ready=True, nuke_hit_this_cycle=True)
    mark_payout_taken(CFG, st)
    assert st.payouts_taken == 1 and not st.payout_ready
    assert st.winning_days_this_cycle == 0 and not st.nuke_hit_this_cycle


def test_mark_payout_retires_at_target():
    st = funded(payouts_taken=3, winning_days_this_cycle=5, payout_ready=True)
    mark_payout_taken(CFG, st)
    assert st.payouts_taken == 4 and st.phase == Phase.RETIRED


def test_funded_blown_after_two_nuke_losses():
    st = funded()
    place(CFG, st); start_new_day(st)
    reconcile(CFG, st, -1_000, True)                # loss 1
    place(CFG, st); start_new_day(st)
    reconcile(CFG, st, -2_000, True)                # loss 2 -> floor
    assert st.phase == Phase.BLOWN


def test_eval_pass_sets_passed():
    st = AccountState(phase=Phase.EVAL, base_balance=50_000, equity=50_000, peak_equity_eod=50_000)
    d = decide(CFG, st, 1)
    record_pending(st, d.plan, 50_000, "d1", CFG.point_value)
    start_new_day(st); reconcile(CFG, st, 51_550, True)        # day1 win
    d = decide(CFG, st, 1)
    record_pending(st, d.plan, 51_550, "d2", CFG.point_value)
    start_new_day(st); reconcile(CFG, st, 53_100, True)        # day2 -> >= +3,000
    assert st.phase == Phase.PASSED
