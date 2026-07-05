"""Three-panel summary chart: EV vs edge, monthly regime WRs, fleet stress curves."""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sweep = json.load(open("mc_sweep.json"))
fleet = json.load(open("fleet_results.json"))

fig, axes = plt.subplots(1, 3, figsize=(19, 5.6))
fig.suptitle("TopHat reality check — break-even edge, regimes, and a stress year",
             fontsize=14, fontweight="bold")

# ---- panel 1: EV per ticket vs edge multiplier ----
ax = axes[0]
lams = [r["lam"] for r in sweep]
ts = [r["ts"]["ev"] for r in sweep]
axv = [r["ax"]["ev"] for r in sweep]
ax.axhline(0, color="k", lw=1)
ax.plot(lams, ts, "o-", color="#1f77b4", label="Topstep 50K ($85 ticket)")
ax.plot(lams, axv, "s-", color="#d62728", label="Apex native (~$104 ticket)")
ax.axvline(1.0, color="green", ls="--", lw=1)
ax.axvline(0.0, color="gray", ls="--", lw=1)
ax.axvline(-1.1, color="orange", ls="--", lw=1)
ax.text(1.0, ax.get_ylim()[1] * 0.02 + 1150, "full drive\nedge", ha="center", fontsize=8, color="green")
ax.text(0.0, 1150, "pure coin\nflip", ha="center", fontsize=8, color="gray")
ax.text(-1.1, 1150, "break-even\n(edge inverted)", ha="center", fontsize=8, color="darkorange")
ax.set_xlabel("edge multiplier λ  (1 = documented drive edge, 0 = coin, −1 = edge reversed)")
ax.set_ylabel("EV per eval ticket ($)")
ax.set_title("Where profitability actually dies")
ax.legend(loc="upper left", fontsize=9)
ax.grid(alpha=0.3)

# ---- panel 2: monthly drive WR vs baseline ----
ax = axes[1]
df = pd.read_csv("day_outcomes.csv", parse_dates=["date"]).set_index("date")
df = df[df["dir"] != 0]
df["month"] = df.index.to_period("M")


def wr(col):
    r = col[(col != "unresolved") & col.notna()]
    return ((r == "win").sum() + 0.5 * (r == "tie").sum()) / len(r) if len(r) else np.nan


months, ev_wr, nk_wr, axnk_wr = [], [], [], []
for m, g in df.groupby("month"):
    if len(g) < 10:
        continue
    months.append(str(m))
    ev_wr.append(wr(g["ts_eval_15_10"]))
    nk_wr.append(wr(g["ts_nuke_80_25"]))
    axnk_wr.append(wr(g["ax_nuke_325_25"]))
x = np.arange(len(months))
ax.plot(x, ev_wr, "o-", color="#1f77b4", label="TS eval 15/10 (coin = 40%)")
ax.axhline(0.40, color="#1f77b4", ls=":", lw=1)
ax.plot(x, nk_wr, "s-", color="#9467bd", label="TS nuke 80/25 (coin = 23.8%)")
ax.axhline(0.238, color="#9467bd", ls=":", lw=1)
ax.plot(x, axnk_wr, "^-", color="#d62728", label="Apex nuke 32.5/25 (coin = 43.5%)")
ax.axhline(0.435, color="#d62728", ls=":", lw=1)
ax.axvspan(2.5, 4.5, color="red", alpha=0.07)
ax.axvspan(8.5, 10.5, color="red", alpha=0.12)
ax.text(3.5, 0.93, "weak", ha="center", fontsize=8, color="darkred")
ax.text(9.5, 0.93, "weakest\n(most recent)", ha="center", fontsize=8, color="darkred")
ax.set_xticks(x)
ax.set_xticklabels(months, rotation=60, fontsize=7)
ax.set_ylim(0, 1.0)
ax.set_ylabel("drive win rate (month)")
ax.set_title("The edge is regime-dependent (dotted = no-edge coin)")
ax.legend(loc="upper left", fontsize=8)
ax.grid(alpha=0.3)

# ---- panel 3: fleet cumulative cash, three scenarios ----
ax = axes[2]
colors = {"FULL Jun25-May26": "#2ca02c", "COIN 252d": "#7f7f7f",
          "BADYEAR Mar-May26 tiled": "#d62728"}
labels = {"FULL Jun25-May26": "real year (Jun25–May26)",
          "COIN 252d": "edge fades to coin flip",
          "BADYEAR Mar-May26 tiled": "worst regime all year"}
for r in fleet:
    if r["name"] not in colors:
        continue
    c = colors[r["name"]]
    for k, ls in (("ts", "-"), ("ax", "--")):
        med = np.array(r[k]["med_curve"]) / 1000
        ax.plot(np.arange(len(med)), med, ls, color=c, lw=2,
                label=f"{labels[r['name']]} — {'Topstep' if k == 'ts' else 'Apex'}")
        p10 = np.array(r[k]["p10_curve"]) / 1000
        p90 = np.array(r[k]["p90_curve"]) / 1000
        ax.fill_between(np.arange(len(med)), p10, p90, color=c, alpha=0.08)
ax.axhline(0, color="k", lw=1)
ax.set_xlabel("trading day")
ax.set_ylabel("cumulative net cash ($k)")
ax.set_title("A year of the fleet: real vs coin vs worst regime")
ax.legend(fontsize=7.5, loc="upper left")
ax.grid(alpha=0.3)

plt.tight_layout()
plt.savefig("tophat_reality_check.png", dpi=130, bbox_inches="tight")
print("saved tophat_reality_check.png")
