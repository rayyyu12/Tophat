"""Fleet simulation over REAL day sequences with full same-day correlation:
every account trading a bracket on day t gets the same outcome (they share the
09:45 drive trade). Ties resolve with one shared coin per bracket-day.
Unresolved days are scratch (0 P&L).

Topstep: 10-eval pipeline (2 slots/day, replenish weekly to 10), funded fleet
(1 nuke slot/day rotation, everyone else flips), corrected intraday MLL.
Apex native: cohort intake (2/day), 20-PA cap, 1 nuke channel/day rotation,
all flip-mode PAs fire together, $1,500 payout policy.

Scenarios: FULL (Jun25-May26), launch-at-month grid, BADYEAR (Mar-May26 tiled),
COIN (i.i.d. barrier baselines).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

BRACKS = ["ts_eval_15_10", "ts_nuke_80_25", "ts_renuke_105_25", "ts_flip_85_50",
          "ax_eval_30_10", "ax_eval2_40_10", "ax_nuke_325_25", "ax_flip_1625_50"]
BASE = {"ts_eval_15_10": 10 / 25, "ts_nuke_80_25": 25 / 105,
        "ts_renuke_105_25": 25 / 130, "ts_flip_85_50": 50 / 58.5,
        "ax_eval_30_10": 10 / 40, "ax_eval2_40_10": 10 / 50,
        "ax_nuke_325_25": 25 / 57.5, "ax_flip_1625_50": 50 / 66.25}


def load_days() -> pd.DataFrame:
    df = pd.read_csv("day_outcomes.csv", parse_dates=["date"]).set_index("date")
    return df[df["dir"] != 0]


def outcomes_matrix(df: pd.DataFrame, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Per bracket: array of 1 (win), 0 (loss), -1 (scratch) per day; shared coin for ties."""
    out = {}
    for b in BRACKS:
        r = df[b].to_numpy()
        coin = rng.random(len(r)) < 0.5
        out[b] = np.where(r == "win", 1, np.where(r == "loss", 0,
                          np.where(r == "tie", coin.astype(int), -1)))
    return out


def coin_matrix(n_days: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    return {b: (rng.random(n_days) < BASE[b]).astype(int) for b in BRACKS}


# ---------------------------------------------------------------- Topstep ----

class TsEval:
    __slots__ = ("bal", "floor", "days")
    def __init__(self):
        self.bal, self.floor, self.days = 50_000.0, 48_000.0, 0


class TsFunded:
    __slots__ = ("bal", "floor", "payouts", "withdrawn", "win_days", "nuke_hit", "tries", "last_nuke")
    def __init__(self):
        self.bal, self.floor = 0.0, -2_000.0
        self.payouts, self.withdrawn, self.win_days = 0, 0.0, 0
        self.nuke_hit, self.tries, self.last_nuke = False, 0, -999


def sim_topstep(om: dict[str, np.ndarray], n_days: int) -> np.ndarray:
    cash = np.zeros(n_days)
    c = 0.0
    evals: list[TsEval] = []
    funded: list[TsFunded] = []
    for t in range(n_days):
        # weekly replenish to 10 live evals
        if t % 5 == 0:
            need = 10 - len(evals)
            c -= 85.0 * need
        for _ in range(max(0, 10 - len(evals))) if t % 5 == 0 else []:
            evals.append(TsEval())
        # 2 eval slots, most progressed first
        evals.sort(key=lambda e: -e.bal)
        o = om["ts_eval_15_10"][t]
        for e in list(evals[:2]):
            e.days += 1
            if o == 1:
                e.bal += 1_500.0
            elif o == 0:
                stop = min(1_000.0, e.bal - e.floor)
                e.bal -= stop
                if e.bal <= e.floor + 1e-9:
                    evals.remove(e)
                    continue
            if e.bal >= 53_000.0 and e.days >= 2:
                evals.remove(e)
                funded.append(TsFunded())
                continue
            e.floor = min(50_000.0, max(e.floor, e.bal - 2_000.0))
        # funded: 1 nuke slot
        need_nuke = [f for f in funded if f.payouts in (0, 2) and not f.nuke_hit]
        nuker = min(need_nuke, key=lambda f: f.last_nuke) if need_nuke else None
        for f in list(funded):
            room = f.bal - f.floor
            stop = min(1_000.0, room)
            if f is nuker:
                f.last_nuke = t
                key = "ts_renuke_105_25" if f.tries >= 1 else "ts_nuke_80_25"
                of = om[key][t]
                f.tries += 1
                if of == 1:
                    f.bal += (4_200.0 if f.tries > 1 else 3_200.0) - 50.0
                    f.nuke_hit = True
                    f.win_days += 1
                elif of == 0:
                    f.bal -= stop
            elif f.payouts in (0, 2) and not f.nuke_hit:
                pass  # queued for a nuke slot: idle, costless
            else:
                of = om["ts_flip_85_50"][t]
                if of == 1:
                    f.bal += 150.0
                    f.win_days += 1
                elif of == 0:
                    f.bal -= stop
            if f.bal <= f.floor + 1e-9:
                funded.remove(f)
                continue
            f.floor = min(0.0, max(f.floor, f.bal - 2_000.0))
            if f.bal + f.withdrawn >= 4_000.0 and f.win_days >= 5 and f.bal > 0:
                w = min(2_000.0, f.bal / 2.0)
                f.withdrawn += w
                f.bal -= w
                c += w
                f.payouts += 1
                f.win_days = 0
                f.nuke_hit, f.tries = False, 0
                if f.payouts >= 4:
                    funded.remove(f)
        cash[t] = c
    return cash


# ------------------------------------------------------------------- Apex ----

class AxEval:
    __slots__ = ("attempt",)
    def __init__(self):
        self.attempt = 0


class AxPA:
    __slots__ = ("bal", "floor", "win_start", "qual", "best", "nuke_hit", "last_nuke", "payouts")
    def __init__(self):
        self.bal, self.floor = 0.0, -2_000.0
        self.win_start, self.qual, self.best = 0.0, 0, 0.0
        self.nuke_hit, self.last_nuke, self.payouts = False, -999, 0


def sim_apex(om: dict[str, np.ndarray], n_days: int) -> np.ndarray:
    cash = np.zeros(n_days)
    c = 0.0
    evals: list[AxEval] = []
    pas: list[AxPA] = []
    for t in range(n_days):
        # cohort intake: buy 10-eval cohort when PAs + 0.47*evals < 16, cap 20 PAs
        if len(pas) + 0.47 * len(evals) < 16 and len(pas) < 20 and len(evals) == 0:
            c -= 39.0 * 10
            evals = [AxEval() for _ in range(10)]
        # 2 intakes/day
        for e in list(evals[:2]):
            key = "ax_eval_30_10" if e.attempt == 0 else "ax_eval2_40_10"
            o = om[key][t]
            e.attempt += 1
            if o == 1:
                evals.remove(e)
                if len(pas) < 20:
                    c -= 139.0
                    pas.append(AxPA())
            elif o == 0 and e.attempt >= 2:
                evals.remove(e)
        # PAs: 1 nuke channel
        need_nuke = [p for p in pas if not p.nuke_hit]
        nuker = min(need_nuke, key=lambda p: p.last_nuke) if need_nuke else None
        for p in list(pas):
            room = p.bal - p.floor
            stop = min(1_000.0, room)
            pnl = 0.0
            if p is nuker:
                p.last_nuke = t
                o = om["ax_nuke_325_25"][t]
                if o == 1:
                    pnl = 1_250.0
                    p.nuke_hit = True
                elif o == 0:
                    pnl = -stop
            elif p.nuke_hit:
                o = om["ax_flip_1625_50"][t]
                pnl = 305.0 if o == 1 else (-stop if o == 0 else 0.0)
            # nuke-mode PAs not holding the slot idle (queue), pnl stays 0
            p.bal += pnl
            if p.bal <= p.floor + 1e-9:
                pas.remove(p)
                continue
            if pnl >= 250.0:
                p.qual += 1
                p.best = max(p.best, pnl)
            p.floor = min(0.0, max(p.floor, p.bal - 2_000.0))
            wp = p.bal - p.win_start
            if p.bal >= p.win_start + 2_600.0 and p.qual >= 5 and p.best <= 0.5 * wp:
                c += 1_500.0
                p.bal -= 1_500.0
                p.payouts += 1
                p.win_start = p.bal
                p.qual, p.best, p.nuke_hit = 0, 0.0, False
        cash[t] = c
    return cash


# -------------------------------------------------------------- scenarios ----

def run_scenario(df: pd.DataFrame | None, n_days: int, reps: int, seed: int,
                 kind: str = "real") -> dict[str, np.ndarray]:
    ts_c = np.empty((reps, n_days))
    ax_c = np.empty((reps, n_days))
    for r in range(reps):
        rng = np.random.default_rng(seed + r)
        om = coin_matrix(n_days, rng) if kind == "coin" else outcomes_matrix(df, rng)
        ts_c[r] = sim_topstep(om, n_days)
        ax_c[r] = sim_apex(om, n_days)
    return {"ts": ts_c, "ax": ax_c}


def summarize(name: str, res: dict[str, np.ndarray]) -> dict:
    out = {"name": name}
    for k in ("ts", "ax"):
        c = res[k]
        end = c[:, -1]
        dd = (np.maximum.accumulate(c, axis=1) - c).max(axis=1)
        troughs = c.min(axis=1)
        out[k] = {
            "end_med": float(np.median(end)), "end_p10": float(np.percentile(end, 10)),
            "end_p90": float(np.percentile(end, 90)),
            "maxdd_med": float(np.median(dd)), "maxdd_p90": float(np.percentile(dd, 90)),
            "trough_med": float(np.median(troughs)), "trough_p10": float(np.percentile(troughs, 10)),
            "p_end_neg": float((end < 0).mean()),
            "med_curve": np.median(c, axis=0).tolist(),
            "p10_curve": np.percentile(c, 10, axis=0).tolist(),
            "p90_curve": np.percentile(c, 90, axis=0).tolist(),
        }
    return out


if __name__ == "__main__":
    df = load_days()
    reps = 400
    res_all = []

    full = run_scenario(df, len(df), reps, 11)
    res_all.append(summarize("FULL Jun25-May26", full))

    bad = df[df.index >= "2026-03-01"]
    bad_tiled = pd.concat([bad] * 6).iloc[:252]
    res_all.append(summarize("BADYEAR Mar-May26 tiled", run_scenario(bad_tiled, 252, reps, 23)))

    res_all.append(summarize("COIN 252d", run_scenario(None, 252, reps, 37, kind="coin")))

    # launch-at-month grid (real sequence from month start to data end)
    months = ["2025-07-01", "2025-09-01", "2025-11-01", "2026-01-01", "2026-03-01"]
    for m in months:
        sub = df[df.index >= m]
        r = run_scenario(sub, len(sub), 200, 51)
        res_all.append(summarize(f"LAUNCH {m[:7]} ({len(sub)}d)", r))

    print(f"{'scenario':28s} {'firm':4s} {'end med':>9s} {'end p10':>9s} {'end p90':>9s} "
          f"{'maxDD med':>10s} {'trough p10':>11s} {'P(end<0)':>9s}")
    for r in res_all:
        for k in ("ts", "ax"):
            s = r[k]
            print(f"{r['name']:28s} {k.upper():4s} {s['end_med']:9.0f} {s['end_p10']:9.0f} "
                  f"{s['end_p90']:9.0f} {s['maxdd_med']:10.0f} {s['trough_p10']:11.0f} "
                  f"{s['p_end_neg']:9.1%}")

    json.dump(res_all, open("fleet_results.json", "w"))
    print("\nsaved fleet_results.json")
