"""Mirror accounts: store CRUD, per-firm outcome propagation, hazards, API."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tophat.server import service
from tophat.services import mirror_sync
from tophat.store import mirrors as MS
from tophat.store import registry as R
from tophat.store.config import load_settings, save_settings

ET = ZoneInfo("America/New_York")
DAY0 = datetime(2026, 6, 22, 9, 46, tzinfo=ET)


def mk(firm="lucid-50k", **kw):
    return MS.create_mirror(firm, **kw)


# ---------------------------------------------------------------- store CRUD
def test_create_defaults_and_auto_ids():
    a = mk()
    b = mk()
    assert (a.mirror_id, b.mirror_id) == ("lucid-01", "lucid-02")
    assert a.phase == "eval" and a.multiplier == 1.0 and a.enabled

    t = mk("tradeify-50k")
    assert t.mirror_id == "tradeify-01"
    assert t.multiplier == 0.8          # firm's eval copier scale pre-filled


def test_create_rejects_leader_firm_and_junk():
    with pytest.raises(ValueError):
        mk("topstep-50k")
    with pytest.raises(ValueError):
        mk("ftmo-100k")
    with pytest.raises(ValueError):
        mk("lucid-50k", phase="galaxy")


def test_patch_and_sync_balance():
    m = mk(alias="lucid one")
    out = MS.patch_mirror(m.mirror_id, {"alias": "L1", "leader_id": 42,
                                        "sync_balance": 1_234.5}, today="2026-07-02")
    assert out.alias == "L1" and out.leader_id == 42
    assert out.equity == 1_234.5 and out.peak == 1_234.5
    assert out.last_verified == "2026-07-02"


def test_delete_and_merge_save():
    a, b = mk(), mk()
    ms = MS.load_mirrors()
    ms[a.mirror_id].equity = 500.0
    ms[b.mirror_id].equity = 700.0
    MS.merge_save_mirrors(ms, [a.mirror_id])          # persist only a
    disk = MS.load_mirrors()
    assert disk[a.mirror_id].equity == 500.0
    assert disk[b.mirror_id].equity == 0.0
    assert MS.delete_mirror(a.mirror_id)
    assert not MS.delete_mirror(a.mirror_id)
    assert a.mirror_id not in MS.load_mirrors()


# ------------------------------------------------------- propagation: clones
def _advance(m, pnl, date="2026-07-02"):
    return mirror_sync.advance_mirror(m, pnl, date)


def test_lucid_eval_passes_in_lockstep():
    m = mk()
    _advance(m, 1_500.0, "2026-07-01")
    assert m.phase == "eval" and m.equity == 1_500.0
    events = _advance(m, 1_500.0, "2026-07-02")
    assert m.phase == "passed"
    assert any("EVAL PASSED" in e for e in events)


def test_two_full_stops_blow_a_fresh_mirror():
    m = mk()
    _advance(m, -1_000.0, "2026-07-01")
    assert m.phase == "eval"                     # one life left
    events = _advance(m, -1_000.0, "2026-07-02")
    assert m.phase == "blown"
    assert any("BLOWN" in e for e in events)


def test_floor_locks_at_breakeven_after_gains():
    m = mk()
    _advance(m, 1_500.0, "2026-07-01")
    _advance(m, 1_500.0, "2026-07-02")           # passed; keep going hypothetically
    m.phase = "funded"                            # simulate post-activation lifecycle
    m.equity = m.peak = 2_500.0
    assert m.floor() == 0.0                       # locked at breakeven (peak > trailing)
    _advance(m, -2_500.0, "2026-07-03")
    assert m.phase == "blown"                     # dropped exactly to the locked floor


def test_tradeify_1to1_needs_3750_but_0p8_needs_3000():
    # 1:1 — three $1,500 wins = 4,500 total, best 1500 <= 40%? 1500 <= 1800 yes;
    # but after TWO wins (3,000) best 1500 > 0.4*3000=1200 -> not passed yet.
    m = MS.create_mirror("tradeify-50k", multiplier=1.0)
    _advance(m, 1_500.0, "d1")
    _advance(m, 1_500.0, "d2")
    assert m.phase == "eval"                      # 3,000 but consistency fails
    _advance(m, 1_500.0, "d3")
    assert m.phase == "passed"                    # 4,500 >= 3,750 equivalent

    # 0.8x — leader's 1,500 arrives as 1,200; three wins = 3,600, best 1,200 <= 1,440
    t = MS.create_mirror("tradeify-50k")          # default 0.8
    for d in ("d1", "d2"):
        _advance(t, 1_500.0 * t.multiplier, d)
    assert t.phase == "eval"                      # 2,400 < 3,000 target
    _advance(t, 1_500.0 * t.multiplier, "d3")
    assert t.phase == "passed"


def test_win_day_qualification_is_net_based():
    m = mk(phase="funded")
    _advance(m, 150.0, "d1")                      # clone flip nets exactly the bar
    assert m.win_days == 1
    events = _advance(m, 120.0, "d2")             # under the bar -> no day + warning
    assert m.win_days == 1
    assert any("did NOT qualify" in e for e in events)


def test_clone_payout_cycle_and_retirement():
    m = mk(phase="funded")
    _advance(m, 3_150.0, "d1")                    # nuke win
    for i in range(4):
        _advance(m, 150.0, f"d{i+2}")             # four qualifying flips
    assert m.payout_ready
    assert mirror_sync.preview_payout(m) == 1_875.0   # min(0.5*3750, 2000)
    paid = mirror_sync.mark_mirror_payout(m)
    assert paid == 1_875.0 and m.payouts_taken == 1 and not m.payout_ready
    assert m.win_days == 0 and m.window_profit == 0.0
    m.payouts_taken = 3
    m.win_days = 4
    m.equity = 3_000.0
    _advance(m, 150.0, "d9")
    mirror_sync.mark_mirror_payout(m)
    assert m.phase == "retired"                   # 4th payout retires a clone


# ------------------------------------------------------- propagation: apex
def test_apex_gate_2470_is_not_eligible():
    """The commissions gap: gross 1300 + 4x325 'looks like' 2,600 but nets 2,470."""
    m = MS.create_mirror("apex-50k", phase="funded")
    _advance(m, 1_250.0, "d1")                    # nuke net
    for i in range(4):
        _advance(m, 305.0, f"d{i+2}")             # four $325-gross flips net 305
    assert m.equity == pytest.approx(2_470.0)
    assert m.win_days == 5                        # all qualify (>= 250 net)
    assert not m.payout_ready                     # balance gate: 2,470 < 2,600
    _advance(m, 305.0, "d6")                      # fifth flip
    assert m.payout_ready
    assert mirror_sync.preview_payout(m) == 1_500.0


def test_apex_consistency_fails_by_15_in_cycle_two():
    m = MS.create_mirror("apex-50k", phase="funded")
    for pnl, d in [(1_250.0, "d1")] + [(305.0, f"d{i}") for i in range(2, 7)]:
        _advance(m, pnl, d)
    mirror_sync.mark_mirror_payout(m)             # cycle 1 done, balance 1,275
    assert m.equity == pytest.approx(1_275.0)
    _advance(m, 1_250.0, "e1")                    # cycle-2 nuke
    for i in range(4):
        _advance(m, 305.0, f"e{i+2}")
    # window = 2,470; best 1,250 > 0.5*2,470 = 1,235 -> NOT eligible despite balance
    assert m.equity > 2_600.0 and m.win_days >= 5
    assert not m.payout_ready
    _advance(m, 305.0, "e6")                      # window 2,775 -> 1,250 <= 1,387.5
    assert m.payout_ready


def test_apex_retires_at_six_and_channel_resets_to_nuke():
    # Operator decision 2026-07-06: Apex harvests 6 payouts (was: never retired).
    m = MS.create_mirror("apex-50k", phase="funded")
    m.channel = "flip"
    m.payouts_taken = 4
    m.equity = 5_000.0
    m.peak = 5_000.0
    m.win_days = 5
    m.window_profit = 3_000.0
    m.best_day = 1_250.0
    mirror_sync.mark_mirror_payout(m)             # payout #5: still trading
    assert m.phase == "funded" and m.payouts_taken == 5
    assert m.channel == "nuke"                    # new cycle opens on the nuke channel
    m.win_days = 5
    m.window_profit = 3_000.0
    m.best_day = 1_250.0
    m.payout_ready = True
    mirror_sync.mark_mirror_payout(m)             # payout #6 retires the account
    assert m.phase == "retired" and m.payouts_taken == 6


def test_apex_eval_one_day_pass_no_consistency():
    m = MS.create_mirror("apex-50k")
    events = _advance(m, 3_000.0, "d1")
    assert m.phase == "passed" and any("EVAL PASSED" in e for e in events)


# ------------------------------------------------- activation / pairing flow
def test_activate_funded_resets_and_waits_or_pairs():
    t = MS.create_mirror("tradeify-50k")
    _advance(t, 1_200.0, "d1")
    _advance(t, 1_200.0, "d2")
    _advance(t, 1_200.0, "d3")
    assert t.phase == "passed"
    mirror_sync.activate_funded(t)                # no leader -> waiting
    assert t.phase == "waiting" and t.leader_id is None
    assert t.multiplier == 1.0                    # back to full size for funded
    assert t.equity == 0.0 and t.win_days == 0 and t.days_traded == 0
    mirror_sync.pair_waiting(t, 777)
    assert t.phase == "funded" and t.leader_id == 777

    lu = mk()
    _advance(lu, 1_500.0, "d1")
    _advance(lu, 1_500.0, "d2")
    mirror_sync.activate_funded(lu, leader_id=888)   # lucid twin pairs immediately
    assert lu.phase == "funded" and lu.leader_id == 888

    with pytest.raises(ValueError):
        mirror_sync.activate_funded(lu)           # only valid from passed


def test_apex_activation_opens_on_nuke_channel():
    m = MS.create_mirror("apex-50k")
    _advance(m, 3_000.0, "d1")
    mirror_sync.activate_funded(m, leader_id=99)
    assert m.channel == "nuke" and m.phase == "funded"


# -------------------------------------------------------- leader propagation
def test_apply_leader_outcome_scales_skips_and_is_idempotent():
    a = mk(leader_id=1)                                        # 1.0x
    b = MS.create_mirror("tradeify-50k", leader_id=1)          # 0.8x
    c = mk(leader_id=2)                                        # different leader
    d = mk(leader_id=1)
    d.enabled = False
    ms = {m.mirror_id: m for m in (a, b, c, d)}

    touched = mirror_sync.apply_leader_outcome(ms, 1, 1_500.0, "2026-07-02")
    assert set(touched) == {a.mirror_id, b.mirror_id}
    assert a.equity == 1_500.0 and b.equity == pytest.approx(1_200.0)
    assert c.equity == 0.0 and d.equity == 0.0

    again = mirror_sync.apply_leader_outcome(ms, 1, 1_500.0, "2026-07-02")
    assert again == [] and a.equity == 1_500.0    # same date never double-books


def test_run_session_reconcile_propagates_to_mirrors(ctl):
    s = load_settings(); s.auto_execute = True; save_settings(s)
    reg = R.load_registry(); reg.entry(ctl.aid).enabled = True; R.save_registry(reg)
    m = mk(leader_id=ctl.aid, phase="funded")

    service.run_session(ctl, execute=True, now_et=DAY0)        # places the nuke
    ctl.set_balance(ctl.balance + 3_200)                        # nuke wins
    ctl.flat = True
    service.run_session(ctl, execute=True, now_et=DAY0 + timedelta(days=1))

    disk = MS.load_mirrors()[m.mirror_id]
    assert disk.equity == pytest.approx(3_200.0)               # leader's actual delta
    assert disk.win_days == 1


# ----------------------------------------------------------------- hazards
def test_hazards_cover_the_operator_playbook():
    near = mk(leader_id=1, phase="funded", alias="danger-close")
    near.equity = 400.0
    near.peak = 2_400.0                            # floor locked at 0 -> room 400
    passed = MS.create_mirror("apex-50k")
    passed.phase = "passed"
    waiting = MS.create_mirror("tradeify-50k")
    waiting.phase = "waiting"
    unmapped = mk(phase="funded")                  # no leader
    stale = mk(leader_id=3, phase="funded")
    stale.last_verified = "2026-06-20"
    ms = {m.mirror_id: m for m in (near, passed, waiting, unmapped, stale)}

    hz = mirror_sync.hazards(ms, "2026-07-02")
    texts = {h["mirror_id"]: [x["text"] for x in hz if x["mirror_id"] == h["mirror_id"]]
             for h in hz}
    assert any("blows this account" in t for t in texts[near.mirror_id])
    assert any("activate funded" in t for t in texts[passed.mirror_id])
    assert any("fresh funded leader" in t for t in texts[waiting.mirror_id])
    assert any("no leader mapped" in t for t in texts[unmapped.mirror_id])
    assert any("last verified" in t for t in texts[stale.mirror_id])
    assert hz[0]["severity"] == "danger"           # sorted most-severe first


# ----------------------------------------------------------------- API layer
def test_mirror_api_crud_and_actions(client):
    r = client.post("/api/mirrors", json={"firm": "lucid-50k", "alias": "L1",
                                          "account_number": "LU-9001"})
    assert r.status_code == 200
    mid = r.json()["mirror_id"]

    r = client.post("/api/mirrors", json={"firm": "topstep-50k"})
    assert r.status_code == 400

    # never-verified eval with no leader -> hazards fire before any sync
    body = client.get("/api/mirrors").json()
    assert len(body["mirrors"]) == 1
    assert any(h["mirror_id"] == mid and "verified" in h["text"] for h in body["hazards"])
    assert any(h["mirror_id"] == mid and "no leader" in h["text"] for h in body["hazards"])

    r = client.post(f"/api/mirrors/{mid}/update",
                    json={"leader_id": 5, "sync_balance": 3_000.0})
    assert r.json()["equity"] == 3_000.0 and r.json()["leader_id"] == 5

    # eval -> passed -> funded -> payout via the API
    client.post(f"/api/mirrors/{mid}/update", json={"phase": "passed"})
    r = client.post(f"/api/mirrors/{mid}/activate-funded", json={})
    assert r.json()["phase"] == "waiting"
    r = client.post(f"/api/mirrors/{mid}/pair", json={"leader_id": 6})
    assert r.json()["phase"] == "funded" and r.json()["leader_id"] == 6

    client.post(f"/api/mirrors/{mid}/update",
                json={"win_days": 5, "equity": 3_000.0, "peak": 3_000.0,
                      "payout_ready": True})
    r = client.post(f"/api/mirrors/{mid}/payout-taken")
    assert r.json()["paid"] == 1_500.0 and r.json()["payouts_taken"] == 1

    r = client.delete(f"/api/mirrors/{mid}")
    assert r.json()["ok"]
    assert client.get("/api/mirrors").json()["mirrors"] == []


def test_firms_api(client):
    r = client.get("/api/firms")
    assert set(r.json()["follower_firms"]) == {"lucid-50k", "tradeify-50k", "apex-50k"}
    assert r.json()["firms"]["apex-50k"]["win_day_min"] == 250.0
