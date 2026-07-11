"""Trade log store + Analytics aggregation + recording hooks."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tophat.engine import AccountState, Phase
from tophat.server import analytics, service
from tophat.store import registry as R
from tophat.store import states as ST
from tophat.store import trade_log
from tophat.store.config import load_settings, save_settings

ET = ZoneInfo("America/New_York")
DAY0 = datetime(2026, 6, 22, 9, 46, tzinfo=ET)


def test_trade_log_roundtrip_and_torn_line():
    trade_log.log_event("trade", account_id=1, label="eval", outcome="win", pnl=1500.0)
    trade_log.log_event("payout", account_id=1, amount=2000.0, source="leader",
                        estimated=True)
    # a crash mid-append leaves a torn final line - reads must survive it
    from tophat.store import tenant
    with open(tenant.resolve(trade_log.TRADE_LOG_FILE), "a", encoding="utf-8") as f:
        f.write('{"type":"trade","account')
    evts = trade_log.read_events()
    assert [e["type"] for e in evts] == ["trade", "payout"]
    assert evts[0]["label"] == "eval" and evts[0]["pnl"] == 1500.0
    assert evts[1]["amount"] == 2000.0


def test_build_analytics_aggregates_legs_curve_and_totals():
    trade_log.log_event("trade", account_id=7, label="eval", outcome="win",
                        pnl=1500.0, trade_date="2026-06-22", balance=51_500.0)
    trade_log.log_event("trade", account_id=7, label="eval", outcome="loss",
                        pnl=-1000.0, trade_date="2026-06-23", balance=50_500.0)
    trade_log.log_event("trade", account_id=8, label="flip", outcome="win",
                        pnl=150.0, trade_date="2026-06-23", balance=150.0)
    trade_log.log_event("trade", account_id=8, label="flip", outcome="flat",
                        pnl=10.0, trade_date="2026-06-24", balance=160.0)
    trade_log.log_event("payout", account_id=8, amount=1500.0, source="leader",
                        estimated=True)

    a = analytics.build_analytics([])
    assert a["has_data"] and a["trades_recorded"] == 4
    legs = {l["label"]: l for l in a["legs"]}
    assert legs["eval"]["n"] == 2 and legs["eval"]["live_wr"] == 0.5
    assert legs["flip"]["wins"] == 1 and legs["flip"]["flats"] == 1
    assert legs["flip"]["live_wr"] == 1.0        # flats excluded from the rate
    assert legs["nuke"]["n"] == 0 and legs["nuke"]["live_wr"] is None
    assert a["totals"]["payouts_banked"] == 1500.0
    assert a["totals"]["realized_pnl"] == 660.0
    c = a["curves"]
    assert c["dates"] == sorted(c["dates"])
    # zero baseline the day before the first chartable day, so even a single
    # recorded day draws a line
    assert c["dates"][0] == "2026-06-22"         # first flip lands 06-23
    s = {x["key"]: x for x in c["series"]}
    assert list(s) == ["topstep", "apex", "lucid", "tradeify", "total", "banked"]
    assert all(x["cum"][0] == 0.0 for x in c["series"])
    assert s["topstep"]["cum"][-1] == 160.0      # flips only - evals never chart
    assert s["total"]["cum"][-1] == 160.0
    assert s["banked"]["cum"][-1] == 1500.0
    assert s["topstep"]["has_data"] and not s["apex"]["has_data"]
    assert len(a["recent"]) == 4


def test_recent_trades_resolve_names_for_deleted_accounts():
    """Rows must show broker names, not '#25157729', even after Topstep prunes
    the account: event-stamped name first, then the account_names.json cache."""
    import json
    from tophat.store import tenant
    from tophat.store.paths import ACCOUNT_NAMES_FILE
    trade_log.log_event("trade", account_id=99, label="flip", outcome="win",
                        pnl=100.0, trade_date="2026-07-09", balance=100.0,
                        account_name="EXPRESS-V2-9790-30778194")
    trade_log.log_event("trade", account_id=98, label="nuke", outcome="loss",
                        pnl=-1000.0, trade_date="2026-07-09", balance=-1000.0)
    trade_log.log_event("trade", account_id=97, label="flip", outcome="win",
                        pnl=50.0, trade_date="2026-07-09", balance=50.0)
    p = tenant.resolve(ACCOUNT_NAMES_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"98": "50KTC-V2-260527-11111111"}), encoding="utf-8")

    accounts = {r["account"] for r in analytics.build_analytics([])["recent"]}
    assert "EXPRESS-V2-9790-30778194" in accounts   # stamped on the event
    assert "50KTC-V2-260527-11111111" in accounts   # write-through cache
    assert "#97" in accounts                        # nothing anywhere: honest #id


def test_single_day_series_still_charts():
    """One chartable day must render a line, not a blank panel: the curves
    carry a $0 baseline dated the day before the first event."""
    trade_log.log_event("trade", account_id=8, label="flip", outcome="win",
                        pnl=150.0, trade_date="2026-07-09", balance=150.0)
    c = analytics.build_analytics([])["curves"]
    assert c["dates"][:2] == ["2026-07-08", "2026-07-09"]
    s = {x["key"]: x for x in c["series"]}
    assert s["topstep"]["cum"][:2] == [0.0, 150.0]


def test_mirror_bookings_feed_firm_curves_only():
    from tophat.services import mirror_sync
    from tophat.store import mirrors as MS
    a = MS.create_mirror("lucid-50k", leader_id=1, phase="funded")
    b = MS.create_mirror("apex-50k", leader_id=1)            # eval phase
    ms = {a.mirror_id: a, b.mirror_id: b}
    mirror_sync.apply_leader_outcome(ms, 1, 300.0, "2026-06-22")
    mirror_sync.apply_leader_outcome(ms, 1, -100.0, "2026-06-23")

    a = analytics.build_analytics([])
    s = {x["key"]: x for x in a["curves"]["series"]}
    # leading 0 = the baseline point prepended for day-one charting
    assert s["lucid"]["cum"] == [0.0, 300.0, 200.0]   # funded mirror charts
    assert not s["apex"]["has_data"]             # eval-phase P&L never charts
    assert s["total"]["cum"] == [0.0, 300.0, 200.0]
    # leader-side aggregates ignore mirror rows entirely
    assert a["trades_recorded"] == 0
    assert a["totals"]["realized_pnl"] == 0.0
    assert a["recent"] == []


def test_reconcile_writes_trade_events(ctl):
    s = load_settings(); s.auto_execute = True; save_settings(s)
    reg = R.load_registry(); reg.entry(ctl.aid).enabled = True; R.save_registry(reg)
    service.run_session(ctl, execute=True, now_et=DAY0, owner="tester")   # fires the nuke
    ctl.set_balance(3_150.0); ctl.flat = True
    service.run_session(ctl, execute=True, now_et=DAY0 + timedelta(days=1))  # reconciles it
    trades = [e for e in trade_log.read_events() if e["type"] == "trade"]
    assert len(trades) == 1
    t = trades[0]
    assert t["account_id"] == ctl.aid and t["label"] == "nuke"
    assert t["outcome"] == "win" and t["pnl"] == 3_150.0
    assert t["trade_date"] == "2026-06-22"


def test_analytics_counts_only_tracked_accounts():
    # States on disk that no live credential reports and that never traded
    # (mock leftovers, removed API keys) must not count toward fleet or spend.
    ST.save_all({
        101: AccountState(phase=Phase.EVAL, base_balance=50_000.0,
                          equity=49_000.0, peak_equity_eod=50_000.0),
        102: AccountState(phase=Phase.FUNDED, base_balance=0.0,
                          equity=3_200.0, peak_equity_eod=3_200.0),
    })
    a = analytics.build_analytics([])
    assert a["funnel"]["started"] == 0
    assert a["funnel"]["funded_active"] == 0
    assert a["spend_breakdown"] == []
    assert a["totals"]["spend_est"] == 0.0

    # a recorded trade keeps an account tracked even after the broker delists it
    trade_log.log_event("trade", account_id=102, label="nuke", outcome="win",
                        pnl=3_200.0, trade_date="2026-06-22", balance=3_200.0)
    a = analytics.build_analytics([])
    assert a["funnel"]["funded_active"] == 1
    assert a["fleet"]["funded_equity"] == 3_200.0
    # Topstep is $85 all-in - no activation-fee spend line, ever
    assert all("activation" not in r["what"].lower() for r in a["spend_breakdown"])


def test_exclude_analytics_flag_removes_account_everywhere():
    ST.save_all({
        7: AccountState(phase=Phase.EVAL, base_balance=50_000.0, equity=51_000.0,
                        peak_equity_eod=51_000.0),
    })
    trade_log.log_event("trade", account_id=7, label="eval", outcome="win",
                        pnl=1_000.0, trade_date="2026-06-22", balance=51_000.0)
    reg = R.load_registry()
    reg.entry(7).exclude_analytics = True
    R.save_registry(reg)
    a = analytics.build_analytics([])
    assert a["trades_recorded"] == 0
    assert a["totals"]["realized_pnl"] == 0.0
    assert a["funnel"]["started"] == 0
    assert a["spend_breakdown"] == []


def test_mark_payout_writes_estimated_amount(ctl):
    st = AccountState(phase=Phase.FUNDED, equity=4_000.0, peak_equity_eod=4_000.0,
                      base_balance=0.0, payout_ready=True,
                      winning_days_this_cycle=5, nuke_hit_this_cycle=True)
    ST.save_all({ctl.aid: st})
    service.mark_payout(ctl.aid)
    pays = [e for e in trade_log.read_events() if e["type"] == "payout"]
    assert len(pays) == 1
    assert pays[0]["amount"] == 2_000.0          # min(cap, half of $4,000)
    assert pays[0]["estimated"] is True and pays[0]["source"] == "leader"
