"""Regime analysis on the reconstructed day outcomes.

Tie resolution 'heur': the barrier nearer the tie-bar's open is assumed hit
first; bounded by pess/opt either way.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BRACKETS = {
    "ts_eval_15_10":    (15.0, 10.0),
    "ts_nuke_80_25":    (80.0, 25.0),
    "ts_renuke_105_25": (105.0, 25.0),
    "ts_flip_85_50":    (8.5, 50.0),
    "ax_eval_30_10":    (30.0, 10.0),
    "ax_nuke_325_25":   (32.5, 25.0),
    "ax_flip_1625_50":  (16.25, 50.0),
}

df = pd.read_csv("day_outcomes.csv", parse_dates=["date"]).set_index("date")
df = df[df["dir"] != 0]

# ties -> 0.5 win credit for aggregate tables (unbiased between the bounds)
def wr(col: pd.Series) -> tuple[float, int]:
    r = col[(col != "unresolved") & col.notna()]
    if len(r) == 0:
        return np.nan, 0
    w = (r == "win").sum() + 0.5 * (r == "tie").sum()
    return w / len(r), len(r)

df["month"] = df.index.to_period("M")

print("=== Monthly drive win rates (ties counted 0.5; n days in parens) ===")
hdr = f"{'month':8s}"
for k in BRACKETS:
    hdr += f" {k.replace('ts_','').replace('ax_','AX_')[:12]:>13s}"
print(hdr + f" {'range':>7s}")
for m, g in df.groupby("month"):
    line = f"{str(m):8s}"
    for k in BRACKETS:
        v, n = wr(g[k])
        line += f" {v:12.1%} " if not np.isnan(v) else f" {'--':>12s} "
    line += f" {g['day_range'].median():6.1f}"
    print(line + f"  ({len(g)}d)")

print("\nBaselines:", {k: f"{s/(t+s):.1%}" for k, (t, s) in BRACKETS.items()})

# win indicator (heur=0.5 for ties) for rolling analysis
for k in BRACKETS:
    r = df[k]
    df[k + "_w"] = np.where(r == "win", 1.0, np.where(r == "tie", 0.5, np.where(r == "loss", 0.0, np.nan)))

print("\n=== Worst / best 42-day (2-month) rolling stretches ===")
for k in ["ts_eval_15_10", "ts_nuke_80_25", "ts_flip_85_50", "ax_nuke_325_25", "ax_flip_1625_50"]:
    roll = df[k + "_w"].rolling(42, min_periods=42).mean()
    t, s = BRACKETS[k]
    base = s / (t + s)
    lo_i, hi_i = roll.idxmin(), roll.idxmax()
    print(f"{k:18s} base {base:5.1%} | worst {roll.min():5.1%} ending {lo_i.date()} | "
          f"best {roll.max():5.1%} ending {hi_i.date()}")

# quarter-ish regime split for the stress sim
df["regime"] = pd.cut(df.index,
                      bins=pd.to_datetime(["2025-06-01", "2025-09-01", "2025-11-01",
                                           "2026-03-01", "2026-05-07"]),
                      labels=["Jun-Aug25", "Sep-Oct25", "Nov25-Feb26", "Mar-May26"])
print("\n=== Regime blocks ===")
for reg, g in df.groupby("regime", observed=True):
    line = f"{reg:12s} ({len(g):3d}d)"
    for k in ["ts_eval_15_10", "ts_nuke_80_25", "ts_flip_85_50", "ax_eval_30_10", "ax_nuke_325_25", "ax_flip_1625_50"]:
        v, n = wr(g[k])
        line += f" {k.split('_')[1][:4]}:{v:6.1%}"
    print(line + f" rng:{g['day_range'].median():6.1f}")

# daily P&L series for the lifecycle sim: save win indicators
df.to_csv("day_outcomes_enriched.csv")
print("\nsaved day_outcomes_enriched.csv")
