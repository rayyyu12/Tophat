"""End-to-end: drive the live runner day-by-day with a balance-controllable mock."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tophat.engine import Phase
from tophat.server import service
from tophat.store import registry as R
from tophat.store import states as ST
from tophat.store.config import load_settings, save_settings

ET = ZoneInfo("America/New_York")
DAY0 = datetime(2026, 6, 22, 9, 46, tzinfo=ET)


def _arm_and_enable(ctl):
    s = load_settings(); s.auto_execute = True; save_settings(s)
    reg = R.load_registry(); reg.entry(ctl.aid).enabled = True; R.save_registry(reg)


def _state(ctl):
    return ST.load_all()[ctl.aid]


def test_full_funded_lifecycle_to_payout(ctl):
    _arm_and_enable(ctl)
    actions = []
    for i in range(8):
        r = service.run_session(ctl, execute=True, now_et=DAY0 + timedelta(days=i))
        row = [x for x in r["results"] if x["account_id"] == ctl.aid][0]
        st = _state(ctl)
        actions.append(row["action"])
        if st.payout_ready:
            break
        # simulate this day's trade winning
        win = 3_200 if row["action"] in ("nuke", "renuke") else 170
        ctl.set_balance(ctl.balance + win); ctl.flat = True

    st = _state(ctl)
    assert "nuke" in actions                          # day 1 was the nuke
    assert st.nuke_hit_this_cycle
    assert st.winning_days_this_cycle == 5 and st.payout_ready

    out = service.mark_payout(ctl.aid)
    assert out["payouts_taken"] == 1
    assert R.load_registry().is_enabled(ctl.aid)       # re-enabled for next cycle


def test_one_fire_per_calendar_day(ctl):
    _arm_and_enable(ctl)
    r1 = service.run_session(ctl, execute=True, now_et=DAY0)
    assert r1["orders_placed"] == 1
    r2 = service.run_session(ctl, execute=True, now_et=DAY0)   # same day again
    row = [x for x in r2["results"] if x["account_id"] == ctl.aid][0]
    assert r2["orders_placed"] == 0 and row["action"] == "done"


def test_blown_account_drops_out(ctl):
    _arm_and_enable(ctl)
    # day1 nuke -> loss
    service.run_session(ctl, execute=True, now_et=DAY0)
    ctl.set_balance(-1_000); ctl.flat = True
    # day2 recovery nuke -> loss -> floor
    service.run_session(ctl, execute=True, now_et=DAY0 + timedelta(days=1))
    ctl.set_balance(-2_000); ctl.flat = True
    # day3 reconciles the blow-up
    service.run_session(ctl, execute=True, now_et=DAY0 + timedelta(days=2))
    assert _state(ctl).phase == Phase.BLOWN

    snap = service.build_snapshot(ctl, mode="mock")
    row = [a for a in snap["accounts"] if a["account_id"] == ctl.aid][0]
    assert row["phase"] == "blown" and row["plan_action"] == "blown"


def test_hedge_guard_skips_when_not_flat(ctl):
    _arm_and_enable(ctl)
    service.run_session(ctl, execute=True, now_et=DAY0)   # places nuke
    ctl.flat = False                                       # position still open
    r = service.run_session(ctl, execute=True, now_et=DAY0 + timedelta(days=1))
    row = [x for x in r["results"] if x["account_id"] == ctl.aid][0]
    # already fired? no — new day; but reconcile sees not-flat so nothing resolves,
    # and the still-open position trips the hedge guard on the new entry
    assert "skipped" in row or row["action"] in ("done", "idle")
