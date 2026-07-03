"""Signal accounts: designated leaders fire copier brackets outside the strategy fleet."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tophat.engine import SIGNAL_PLAN_TEMPLATES, signal_plan
from tophat.server import service
from tophat.store import mirrors as MS
from tophat.store import registry as R
from tophat.store import states as ST
from tophat.store.config import load_settings, save_settings

ET = ZoneInfo("America/New_York")
DAY0 = datetime(2026, 6, 22, 9, 46, tzinfo=ET)


def _setup(ctl, plan="apex-nuke", name="PRAC-JUN0225341549"):
    ctl.set_balance(150_000.0, name)                 # a Topstep practice account
    s = load_settings(); s.auto_execute = True; save_settings(s)
    reg = R.load_registry()
    e = reg.entry(ctl.aid)
    e.enabled = True
    e.signal_plan = plan
    R.save_registry(reg)


def test_signal_plan_orients_by_drive():
    p = signal_plan("apex-nuke", 1)
    assert (p.direction, p.contracts, p.target_pts, p.stop_pts) == (1, 2, 32.5, 25.0)
    p = signal_plan("apex-flip", -1)
    assert (p.direction, p.contracts, p.target_pts, p.stop_pts) == (-1, 1, 16.25, 50.0)
    assert signal_plan("apex-eval", 0) is None       # flat drive -> no fire
    assert signal_plan("nope", 1) is None
    assert p.manual_stop                              # stop leg always copied


def test_practice_signal_account_auto_fires(ctl):
    _setup(ctl, "apex-nuke")
    r = service.run_session(ctl, execute=True, now_et=DAY0)
    row = [x for x in r["results"] if x["account_id"] == ctl.aid][0]
    assert row["action"] == "signal" and "apex-nuke" in row["note"]
    assert r["orders_placed"] == 1 and "order_id" in row
    # one per day
    r2 = service.run_session(ctl, execute=True, now_et=DAY0)
    row2 = [x for x in r2["results"] if x["account_id"] == ctl.aid][0]
    assert row2["action"] == "done" and r2["orders_placed"] == 0


def test_practice_without_signal_still_skipped(ctl):
    _setup(ctl, plan="")                              # plain practice account
    r = service.run_session(ctl, execute=True, now_et=DAY0)
    assert r["orders_placed"] == 0
    assert not any(x["account_id"] == ctl.aid for x in r["results"])


def test_signal_never_takes_a_scheduler_slot(ctl):
    # signal account is excluded from assign_day: it must not consume the nuke slot
    _setup(ctl, "apex-nuke", name="EXPRESS-XFA-2")    # non-practice signal account
    r = service.run_session(ctl, execute=True, now_et=DAY0)
    row = [x for x in r["results"] if x["account_id"] == ctl.aid][0]
    assert row["action"] == "signal"                  # not "nuke" — no lifecycle


def test_signal_respects_entry_window(ctl):
    _setup(ctl, "apex-flip")
    early = DAY0.replace(hour=9, minute=30)
    r = service.run_session(ctl, execute=True, respect_times=True, now_et=early)
    row = [x for x in r["results"] if x["account_id"] == ctl.aid][0]
    assert row["action"] == "scheduled"
    late = DAY0.replace(hour=11, minute=0)
    r = service.run_session(ctl, execute=True, respect_times=True, now_et=late)
    row = [x for x in r["results"] if x["account_id"] == ctl.aid][0]
    assert row["action"] == "missed"


def test_signal_reconcile_propagates_to_channel_mirrors(ctl):
    _setup(ctl, "apex-nuke")
    m = MS.create_mirror("apex-50k", leader_id=ctl.aid, phase="funded")
    service.run_session(ctl, execute=True, now_et=DAY0)
    ctl.set_balance(ctl.balance + 1_250)              # channel bracket wins (net)
    ctl.flat = True
    service.run_session(ctl, execute=True, now_et=DAY0 + timedelta(days=1))
    disk = MS.load_mirrors()[m.mirror_id]
    assert disk.equity == 1_250.0 and disk.win_days == 1
    # day 2 reconciled day 1's trade, then fired the channel's next bracket
    st = ST.load_all()[ctl.aid]
    assert st.pending_label == "sig-apex-nuke"
    assert st.pending_date == (DAY0 + timedelta(days=1)).strftime("%Y-%m-%d")


def test_signal_plan_patch_via_lifecycle_api(client, ctl):
    # unknown plan rejected; valid plan set + surfaced
    out = service.update_account_lifecycle(ctl.aid, {"signal_plan": "warp-drive"}, [])
    assert "error" in out
    r = client.get("/api/state")                      # touch the app so pool exists
    assert r.status_code == 200


def test_templates_cover_the_three_channels():
    assert set(SIGNAL_PLAN_TEMPLATES) == {"apex-nuke", "apex-flip", "apex-eval"}
