"""Simulation engine: day resolution, lifecycle fidelity, determinism,
template store, and the /api/sim endpoints."""

from __future__ import annotations

import json

import numpy as np
import pytest

from tophat.services import simulator
from tophat.store import simstore
from tophat.store.simdata import DayView


def view(date="2026-01-05", direction=1, entry=100.0, hi=None, lo=None,
         eod=None) -> DayView:
    hi = np.array(hi if hi is not None else [entry + 1] * 5, dtype=float)
    lo = np.array(lo if lo is not None else [entry - 1] * 5, dtype=float)
    return DayView(date=date, direction=direction, entry=entry,
                   hi=hi, lo=lo, eod_close=eod if eod is not None else entry)


# Big always-win days: every bracket's target hits, no stop ever threatened.
def win_day(i: int) -> DayView:
    return view(date=f"2026-01-{i % 28 + 1:02d}", entry=100.0,
                hi=[500.0] * 60, lo=[99.0] * 60, eod=400.0)


def loss_day(i: int) -> DayView:
    return view(date=f"2026-02-{i % 28 + 1:02d}", entry=100.0,
                hi=[101.0] * 60, lo=[-500.0] * 60, eod=50.0)


# ------------------------------------------------------------ resolve_day
def test_resolve_first_touch_win_and_loss():
    v = view(hi=[105, 111, 111], lo=[98, 98, 98])
    assert simulator.resolve_day(v, 1, 10.0, 10.0) == 10.0       # target 110 hit
    v = view(hi=[105, 111, 111], lo=[89, 98, 98])
    assert simulator.resolve_day(v, 1, 10.0, 10.0) == -10.0      # stop bar first


def test_resolve_tie_pessimistic_vs_optimistic():
    v = view(hi=[111], lo=[89])                                  # both in one bar
    assert simulator.resolve_day(v, 1, 10.0, 10.0) == -10.0
    assert simulator.resolve_day(v, 1, 10.0, 10.0,
                                 tie_pessimistic=False) == 10.0


def test_resolve_unresolved_exits_at_close():
    v = view(hi=[104] * 5, lo=[97] * 5, eod=102.5)
    assert simulator.resolve_day(v, 1, 10.0, 10.0) == 2.5
    v = view(direction=-1, hi=[104] * 5, lo=[97] * 5, eod=102.5)
    assert simulator.resolve_day(v, -1, 10.0, 10.0) == -2.5


def test_resolve_short_direction():
    v = view(direction=-1, hi=[101] * 5, lo=[85] * 5)
    assert simulator.resolve_day(v, -1, 10.0, 10.0) == 10.0      # short target 90


# ------------------------------------------------------------ run_sim
def test_run_sim_needs_enough_days():
    p = simulator.params_from_dict({"lifecycle": "eval"})
    with pytest.raises(ValueError, match="usable days"):
        simulator.run_sim(p, [win_day(i) for i in range(5)])


def test_eval_always_win_passes_at_min_days():
    days = [win_day(i) for i in range(25)]
    p = simulator.params_from_dict(
        {"lifecycle": "eval", "n_paths": 50, "seed": 3})
    r = simulator.run_sim(p, days)
    assert r["probs"]["eval_passed"] == 1.0
    assert r["probs"]["blown"] == 0.0
    assert r["days_to_outcome"]["median"] == 2       # eval_min_days
    legs = {l["label"]: l for l in r["legs"]}
    assert legs["eval"]["wr"] == 1.0


def test_eval_always_lose_blows_in_two_days():
    # -$1,000/day from $50,000 hits the $48,000 trailing floor on day 2
    days = [loss_day(i) for i in range(25)]
    p = simulator.params_from_dict(
        {"lifecycle": "eval", "n_paths": 50, "seed": 3})
    r = simulator.run_sim(p, days)
    assert r["probs"]["blown"] == 1.0
    assert r["days_to_outcome"]["median"] == 2
    assert r["net"]["median"] == -85.0               # lost ticket, nothing banked


def test_full_lifecycle_always_win_matches_engine_arithmetic():
    """End-to-end fidelity: pass at day 2, then nuke+flips through 4 payouts.

    Cycle math (engine + lifecycle reuse): payout 1 = min(2000, 3880/2) = 1940;
    payout 2 flips-only = min(2000, 2790/2) = 1395; payout 3 renuke =
    min(2000, 5275/2) = 2000; payout 4 = min(2000, 4125/2) = 2000. Banked 7335.
    """
    days = [win_day(i) for i in range(25)]
    p = simulator.params_from_dict(
        {"lifecycle": "full", "n_paths": 20, "seed": 1, "ticket_cost": 85.0,
         "activation_cost": 0.0})
    r = simulator.run_sim(p, days)
    assert r["probs"]["eval_passed"] == 1.0
    assert r["probs"]["retired"] == 1.0
    assert r["net"]["median"] == 7335.0 - 85.0
    assert r["net"]["p5"] == r["net"]["p95"] == 7250.0   # fully deterministic
    assert r["days_to_outcome"]["median"] == 22          # 2 eval + 4 cycles x 5


def test_run_sim_deterministic_for_seed():
    days = [win_day(i) if i % 3 else loss_day(i) for i in range(30)]
    p = simulator.params_from_dict(
        {"lifecycle": "full", "n_paths": 200, "seed": 42})
    a = simulator.run_sim(p, days)
    b = simulator.run_sim(p, days)
    for k in ("net", "probs", "curve", "days_to_outcome"):
        assert json.dumps(a[k]) == json.dumps(b[k])


def test_single_mode_and_drive_rule_skips_flat_days():
    flat = [view(date=f"2026-03-{i+1:02d}", direction=0) for i in range(10)]
    days = [win_day(i) for i in range(20)] + flat
    p = simulator.params_from_dict(
        {"lifecycle": "single", "direction_rule": "drive", "n_paths": 30,
         "seed": 5, "single_target_pts": 10, "single_stop_pts": 10,
         "max_days": 40, "ticket_cost": 0.0})
    r = simulator.run_sim(p, days)
    assert r["probs"]["blown"] == 0.0
    assert r["success"]["label"] == "profitable"
    # flat-drive days trade nothing; win days always pay - never negative
    assert r["net"]["p5"] >= 0.0


def test_validation_rejects_junk():
    with pytest.raises(ValueError):
        simulator.params_from_dict({"lifecycle": "warp"})
    with pytest.raises(ValueError):
        simulator.params_from_dict({"entry_time": "99:99"})
    with pytest.raises(ValueError):
        simulator.params_from_dict({"tie_rule": "hopeful"})
    with pytest.raises(ValueError):
        simulator.params_from_dict({"n_paths": 20000, "max_days": 1000})


# ------------------------------------------------------------ template store
def test_template_save_overwrite_run_delete():
    p = simulator.params_from_dict({"lifecycle": "eval"})
    from dataclasses import asdict
    t = simstore.save_template("My Test", asdict(p))
    assert t["id"] == "my-test" and t["summary"] is None
    # same name updates in place, different name gets a new slug
    t2 = simstore.save_template("my test", {**asdict(p), "seed": 9})
    assert t2["id"] == t["id"] and t2["params"]["seed"] == 9
    assert len(simstore.list_templates()) == 1

    result = {"success": {"label": "eval pass rate", "prob": 0.4},
              "net": {"median": 120.0}, "probs": {"blown": 0.5},
              "params": {"n_paths": 100}}
    assert simstore.attach_run(t["id"], result) is not None
    stored = simstore.load_run(t["id"])
    assert stored["success"]["prob"] == 0.4
    listed = simstore.list_templates()[0]
    assert listed["summary"]["net_median"] == 120.0

    assert simstore.delete_template(t["id"]) is True
    assert simstore.load_run(t["id"]) is None
    assert simstore.list_templates() == []
    assert simstore.delete_template("nope") is False


# ------------------------------------------------------------ API
def _write_tiny_parquet():
    """25 identical up-days of RTH minute bars into the test bars path."""
    import pandas as pd
    from tophat.store import simdata
    from tophat.store.paths import SIM_BARS_FILE
    times = pd.date_range("2026-01-05 09:30", "2026-01-05 16:00", freq="1min")
    rows = []
    for d in range(25):
        date = f"2026-01-{d + 1:02d}"
        for i, ts in enumerate(times):
            base = 100.0 + i * 0.5          # steady climb: longs always win
            rows.append({"date": date, "time": ts.strftime("%H:%M"),
                         "open": base, "high": base + 1.0,
                         "low": base - 0.4, "close": base + 0.5})
    pd.DataFrame(rows).to_parquet(SIM_BARS_FILE, index=False)
    simdata.reset_cache()


def test_sim_api_meta_run_and_templates(client):
    meta = client.get("/api/sim/meta").json()
    assert meta["coverage"]["available"] is False    # no cache yet
    assert meta["defaults"]["lifecycle"] == "full"
    assert "topstep-50k" in meta["firms"]

    r = client.post("/api/sim/run", json={"params": {}})
    assert r.status_code == 400 and "tick data" in r.json()["error"]

    _write_tiny_parquet()
    assert client.get("/api/sim/meta").json()["coverage"]["days"] == 25

    t = client.post("/api/sim/templates", json={
        "name": "api test", "params": {"lifecycle": "single", "n_paths": 40,
                                       "direction_rule": "long",
                                       "single_target_pts": 5,
                                       "single_stop_pts": 5}}).json()
    assert t["id"] == "api-test"

    r = client.post("/api/sim/run", json={
        "params": t["params"], "template_id": t["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["days_used"] == 25
    assert body["probs"]["blown"] == 0.0             # steady climb, longs only

    stored = client.get(f"/api/sim/templates/{t['id']}/result")
    assert stored.status_code == 200
    assert stored.json()["days_used"] == 25
    listed = client.get("/api/sim/templates").json()["templates"]
    assert listed[0]["summary"]["n_paths"] == 40

    assert client.delete(f"/api/sim/templates/{t['id']}").status_code == 200
    assert client.get(f"/api/sim/templates/{t['id']}/result").status_code == 404

    r = client.post("/api/sim/run", json={"params": {"lifecycle": "bogus"}})
    assert r.status_code == 400
